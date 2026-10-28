"""Разметка сохранённых сообщений и группировка в события (FR-K).

Цикл раз в пару минут берёт неразмеченные сообщения, отсеивает шум правилами,
кандидатов пакетами отправляет в LLM и привязывает размеченное к событиям.
Если ни одна модель не доступна, кандидаты размечаются только правилами.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta

from .classifier import (
    CLASSIFIER_VERSION,
    Category,
    LlmItem,
    LlmLabel,
    build_prompt,
    normalize,
    parse_response,
    prefilter,
    rule_category,
)
from .db import Database
from .llm import LLMChain
from .taxonomy import ADS, FLOOD, OTHER

log = logging.getLogger(__name__)

BATCH_SIZE = 50
CYCLE_LIMIT = 500
EVENT_IDLE_HOURS = 6


@dataclass
class CycleResult:
    total: int = 0
    by_rules: int = 0
    by_llm: int = 0
    rule_fallback: int = 0


def same_place(a: str | None, b: str | None) -> bool:
    if not a or not b:
        return False
    a, b = normalize(a).strip(" .,"), normalize(b).strip(" .,")
    return a == b or a in b or b in a


class Labeler:
    def __init__(
        self,
        db: Database,
        chain: LLMChain | None,
        *,
        mode: str = "candidates",
        event_window_hours: int = 3,
        interval_s: int = 120,
    ) -> None:
        self.db = db
        self.chain = chain
        self.mode = mode
        self.window = timedelta(hours=event_window_hours)
        self.interval_s = interval_s
        self.last_run_at: datetime | None = None
        self.last_result: CycleResult | None = None
        self._categories: list[Category] | None = None

    # --- классификатор -------------------------------------------------

    def categories(self) -> list[Category]:
        if self._categories is None:
            self._categories = [
                Category(
                    id=r["id"],
                    slug=r["slug"],
                    name=r["name"],
                    group_slug=r["group_slug"],
                    group_name=r["group_name"],
                    description=r["description"],
                    keywords=json.loads(r["keywords"] or "[]"),
                    in_digest=bool(r["in_digest"]),
                    active=bool(r["active"]),
                )
                for r in self.db.categories()
            ]
        return self._categories

    def reload_categories(self) -> None:
        self._categories = None

    def _cat(self, slug: str) -> Category:
        by_slug = {c.slug: c for c in self.categories()}
        return by_slug.get(slug) or by_slug[OTHER]

    # --- цикл ----------------------------------------------------------

    async def run_forever(self) -> None:
        while True:
            try:
                res = await self.run_once()
                if res.total:
                    log.info(
                        "Размечено %d: правилами %d, LLM %d, без LLM (запасной режим) %d",
                        res.total, res.by_rules, res.by_llm, res.rule_fallback,
                    )
            except Exception:
                log.exception("Сбой цикла разметки")
            await asyncio.sleep(self.interval_s)

    async def run_once(self, now: datetime | None = None) -> CycleResult:
        res = CycleResult()
        rows = self.db.unlabeled_messages(CYCLE_LIMIT)
        cats = self.categories()
        candidates = []
        for row in rows:
            reply_in_event = self._reply_event(row) is not None
            pf = prefilter(row["text"], cats, reply_in_event=reply_in_event)
            if pf.is_candidate or (self.mode == "all" and pf.kind == "no_keywords"):
                candidates.append((row, pf))
                continue
            slug = {"flood": FLOOD, "ads": ADS}.get(pf.kind, OTHER)
            self.db.save_label(row["source_id"], row["msg_id"], self._cat(slug).id, "rule", CLASSIFIER_VERSION,
                               confidence=1.0 if slug != OTHER else 0.5, now=now)
            res.by_rules += 1

        for i in range(0, len(candidates), BATCH_SIZE):
            batch = candidates[i : i + BATCH_SIZE]
            labels = await self._llm_batch(batch)
            for n, (row, pf) in enumerate(batch, start=1):
                if labels is not None and n in labels:
                    self._apply(row, labels[n], "llm", now)
                    res.by_llm += 1
                else:
                    # Модель недоступна или пропустила сообщение — размечаем правилами,
                    # чтобы не отправлять его в LLM повторно в каждом цикле.
                    self._apply_rules(row, pf.matched, now)
                    res.rule_fallback += 1

        self.db.close_stale_events(EVENT_IDLE_HOURS, now=now)
        res.total = res.by_rules + res.by_llm + res.rule_fallback
        self.last_run_at = now or datetime.now().astimezone()
        self.last_result = res
        return res

    async def _llm_batch(self, batch) -> dict[int, LlmLabel] | None:
        if not self.chain or not self.chain.providers:
            return None
        cats = self.categories()
        index = {(row["source_id"], row["msg_id"]): n for n, (row, _) in enumerate(batch, start=1)}
        items = [
            LlmItem(n=n, text=row["text"], reply_n=index.get((row["source_id"], row["reply_to"])))
            for n, (row, _) in enumerate(batch, start=1)
        ]
        system, user = build_prompt(cats, items)
        known = {c.slug for c in cats if c.active}
        numbers = list(range(1, len(batch) + 1))
        result, _provider = await self.chain.run(
            system, user, lambda content: parse_response(content, numbers, known), max_tokens=120 * len(batch) + 200
        )
        return result

    # --- метки и события ------------------------------------------------

    def _reply_event(self, row) -> int | None:
        if not row["reply_to"]:
            return None
        parent = self.db.get_label(row["source_id"], row["reply_to"])
        return parent["event_id"] if parent and parent["event_id"] else None

    def _apply(self, row, lab: LlmLabel, method: str, now: datetime | None) -> None:
        cat = self._cat(lab.category)
        event_id = None
        if cat.slug not in (FLOOD, ADS, OTHER):
            event_id = self._assign_event(row, cat, lab.place, lab.title)
        self.db.save_label(
            row["source_id"], row["msg_id"], cat.id, method, CLASSIFIER_VERSION,
            extra=lab.extra, place=lab.place, official=lab.official,
            confidence=lab.confidence, event_id=event_id, now=now,
        )

    def _apply_rules(self, row, matched: list[str], now: datetime | None) -> None:
        """Запасной режим без LLM: категория по ключевым словам, без места."""
        cat = rule_category(matched, self.categories()) or self._cat(OTHER)
        lab = LlmLabel(category=cat.slug, extra=[], place=None, official=False, confidence=0.3, title=None)
        self._apply(row, lab, "rule_fallback", now)

    def _assign_event(self, row, cat: Category, place: str | None, title: str | None) -> int:
        at = datetime.fromisoformat(row["date"])
        # 1) Ответ на сообщение, уже входящее в событие той же категории.
        parent_event = self._reply_event(row)
        if parent_event:
            ev = self.db.get_event(parent_event)
            if ev and ev["category_id"] == cat.id:
                self.db.touch_event(parent_event, at, place)
                return parent_event
        # 2) Активное событие той же категории в окне: то же место или оба без места.
        for ev in self.db.active_events(cat.id, at - self.window):
            if same_place(ev["place"], place) or (not ev["place"] and not place):
                self.db.touch_event(ev["id"], at, place)
                return ev["id"]
        # 3) Новое событие.
        title = title or (f"{cat.name}: {place}" if place else cat.name)
        return self.db.create_event(cat.id, title, place, at)
