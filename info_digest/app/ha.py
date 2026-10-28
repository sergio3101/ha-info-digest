"""Связь с Home Assistant: сенсоры сводок, сенсор здоровья и файл голосовых фраз (FR-H, FR-Q).

Дополнение обращается к HA через прокси Supervisor (http://supervisor/core/api)
с токеном SUPERVISOR_TOKEN — для этого в config.yaml включено homeassistant_api.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo

import aiohttp

from .db import Database
from .digest import (
    DigestEvent,
    build_category_answer,
    build_group_answer,
    build_main_digest,
    build_night_digest,
    night_bounds,
)
from .intents import (
    CLASSES_SENSOR,
    HEALTH_SENSOR,
    INTENTS_FILE,
    MAIN_SENSOR,
    NIGHT_SENSOR,
    build_intents,
    render_yaml,
)
from .taxonomy import DEFAULT_WINDOW_HOURS, GROUP_WINDOW_HOURS

log = logging.getLogger(__name__)

SUPERVISOR_URL = "http://supervisor/core/api"
HA_CONFIG_DIR = Path("/homeassistant")
REPUBLISH_S = 10 * 60
CHECK_S = 30
MAIN_HOURS = 6
QUIET_SILENCE_MIN = 60


class HAClient:
    def __init__(self, base_url: str = SUPERVISOR_URL, token: str | None = None) -> None:
        self.base_url = base_url
        self.token = token or os.environ.get("SUPERVISOR_TOKEN")
        self._session: aiohttp.ClientSession | None = None

    @property
    def available(self) -> bool:
        return bool(self.token)

    async def _request(self, method: str, path: str, payload=None):
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"Authorization": f"Bearer {self.token}"},
            )
        async with self._session.request(method, f"{self.base_url}{path}", json=payload) as r:
            if r.status >= 400:
                raise RuntimeError(f"HA {method} {path}: HTTP {r.status} {(await r.text())[:200]}")
            return await r.json(content_type=None)

    async def set_state(self, entity_id: str, state: str, attributes: dict) -> None:
        await self._request("POST", f"/states/{entity_id}", {"state": state, "attributes": attributes})

    async def call_service(self, domain: str, service: str, data: dict | None = None) -> None:
        await self._request("POST", f"/services/{domain}/{service}", data or {})

    async def time_zone(self) -> str | None:
        cfg = await self._request("GET", "/config")
        return cfg.get("time_zone")

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()


def local_tz(name: str | None) -> tzinfo:
    for candidate in (name, os.environ.get("TZ"), "Europe/Moscow"):
        if candidate:
            try:
                return ZoneInfo(candidate)
            except Exception:  # noqa: BLE001
                log.warning("Неизвестный часовой пояс %r", candidate)
    return UTC


def build_states(db: Database, now: datetime, tz: tzinfo, healthy: bool, health_attrs: dict) -> dict:
    """Все сенсоры, которые публикует дополнение: entity_id → (state, attributes). Без ввода-вывода."""
    updated = now.isoformat(timespec="seconds")

    def events(since: datetime, until: datetime | None = None) -> list[DigestEvent]:
        return [DigestEvent.from_row(r) for r in db.events_for_digest(since, until or now)]

    def brief(evs: list[DigestEvent]) -> list[dict]:
        return [
            {"title": e.title, "category": e.category, "place": e.place, "active": e.active,
             "confirmed": e.confirmed, "authors": e.authors}
            for e in evs[:20]
        ]

    main_events = events(now - timedelta(hours=MAIN_HOURS))
    main_text = build_main_digest(main_events, now, tz, MAIN_HOURS)
    n_start, n_end = night_bounds(now, tz)
    night_events = events(n_start, n_end)
    night_text = build_night_digest(night_events, tz)

    # Ответы по группам и категориям (FR-Q5): ключ атрибута — slug.
    classes: dict[str, str] = {}
    cats = [c for c in db.categories() if c["in_digest"] and c["active"]]
    for g in db.groups():
        if not g["in_digest"]:
            continue
        hours = GROUP_WINDOW_HOURS.get(g["slug"], DEFAULT_WINDOW_HOURS)
        evs = events(now - timedelta(hours=hours))
        g_cats = [c for c in cats if c["group_slug"] == g["slug"]]
        g_events = [e for e in evs if e.group_slug == g["slug"]]
        classes[g["slug"]] = build_group_answer(
            g["name"], [(c["slug"], c["name"]) for c in g_cats], g_events, hours, tz
        )
        for c in g_cats:
            classes[c["slug"]] = build_category_answer(
                c["name"], [e for e in g_events if e.category_slug == c["slug"]], hours, tz
            )

    base = {"device_class": "timestamp", "updated_at": updated}
    return {
        MAIN_SENSOR: (updated, {**base, "friendly_name": "Сводка города за 6 часов", "icon": "mdi:newspaper",
                                "text": main_text, "window_hours": MAIN_HOURS, "events": brief(main_events)}),
        NIGHT_SENSOR: (updated, {**base, "friendly_name": "Сводка города за ночь", "icon": "mdi:weather-night",
                                 "text": night_text, "events": brief(night_events)}),
        CLASSES_SENSOR: (updated, {**base, "friendly_name": "Сводки города по темам", "icon": "mdi:tag-multiple",
                                   **classes}),
        HEALTH_SENSOR: ("on" if healthy else "off", {"friendly_name": "Сбор городских новостей",
                                                     "device_class": "connectivity", **health_attrs}),
    }


class Publisher:
    def __init__(self, db: Database, client: HAClient, collector, config_dir: Path = HA_CONFIG_DIR) -> None:
        self.db = db
        self.client = client
        self.collector = collector
        self.config_dir = config_dir
        self.tz: tzinfo = local_tz(None)
        self._dirty = asyncio.Event()
        self.last_publish_at: datetime | None = None
        self.last_error: str | None = None
        self.intents_status: str = "ещё не создавались"

    def mark_dirty(self) -> None:
        self._dirty.set()

    def health(self, now: datetime) -> tuple[bool, dict]:
        """Сбор жив, если Telegram подключён и чат не молчит дольше часа днём (FR-H4)."""
        state = getattr(self.collector, "state", "unknown")
        last = self.db.last_message_at()
        attrs = {"telegram": state, "last_message_at": last}
        if state != "running":
            return False, attrs
        local_hour = now.astimezone(self.tz).hour
        if last and 7 <= local_hour < 23:
            silent = now - datetime.fromisoformat(last)
            attrs["silent_minutes"] = int(silent.total_seconds() // 60)
            if silent > timedelta(minutes=QUIET_SILENCE_MIN):
                return False, attrs
        return True, attrs

    async def publish(self, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        healthy, attrs = self.health(now)
        for entity_id, (state, attributes) in build_states(self.db, now, self.tz, healthy, attrs).items():
            await self.client.set_state(entity_id, state, attributes)
        self.last_publish_at = now
        self.last_error = None

    def write_intents(self) -> bool:
        """Перезаписывает файл фраз, если он изменился. Возвращает True, если изменился."""
        groups = [{"slug": g["slug"], "phrases": json.loads(g["phrases"] or "[]"), "in_digest": bool(g["in_digest"])}
                  for g in self.db.groups()]
        cats = [{"slug": c["slug"], "phrases": json.loads(c["phrases"] or "[]"), "in_digest": bool(c["in_digest"]),
                 "active": bool(c["active"])} for c in self.db.categories()]
        intents = build_intents(groups, cats)
        content = render_yaml(intents)
        path = self.config_dir / INTENTS_FILE
        if path.exists() and path.read_text(encoding="utf-8") == content:
            self.intents_status = f"{len(intents)} фраз, без изменений"
            return False
        path.write_text(content, encoding="utf-8")
        self.intents_status = f"{len(intents)} фраз записано"
        log.info("Файл голосовых фраз обновлён: %d фраз", len(intents))
        return True

    def intents_included(self) -> bool:
        cfg = self.config_dir / "configuration.yaml"
        return cfg.exists() and INTENTS_FILE in cfg.read_text(encoding="utf-8")

    async def sync_intents(self) -> None:
        if not self.config_dir.exists():
            self.intents_status = "нет доступа к папке конфигурации HA"
            return
        changed = self.write_intents()
        if not self.intents_included():
            self.intents_status += "; не подключён в configuration.yaml"
            log.warning(
                "Добавьте в configuration.yaml строку: yandex_station_intents: !include %s", INTENTS_FILE
            )
            return
        if changed:
            try:
                await self.client.call_service("yandex_station_intents", "reload")
                self.intents_status += ", интеграция перезагружена"
            except Exception as e:  # noqa: BLE001
                self.intents_status += f", перезагрузить не удалось: {e}"
                log.warning("Не удалось перезагрузить Yandex.Station Intents: %s", e)

    async def run_forever(self) -> None:
        if not self.client.available:
            log.warning("Нет токена Supervisor — сенсоры в HA публиковаться не будут")
            return
        try:
            self.tz = local_tz(await self.client.time_zone())
        except Exception as e:  # noqa: BLE001
            log.warning("Не удалось узнать часовой пояс HA: %s", e)
        try:
            await self.sync_intents()
        except Exception:
            log.exception("Сбой при создании голосовых фраз")
        last = 0.0
        loop = asyncio.get_running_loop()
        while True:
            try:
                await asyncio.wait_for(self._dirty.wait(), timeout=CHECK_S)
            except TimeoutError:
                pass
            if self._dirty.is_set() or loop.time() - last >= REPUBLISH_S:
                self._dirty.clear()
                try:
                    await self.publish()
                    last = loop.time()
                except Exception as e:  # noqa: BLE001
                    self.last_error = str(e)
                    log.warning("Не удалось опубликовать сенсоры в HA: %s", e)
