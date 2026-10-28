"""Файл голосовых фраз для интеграции Yandex.Station Intents (FR-Q3).

Фразы берутся из классификатора, ответ — шаблон, который читает текст из
сенсора HA. Шаблоны обязательны: готовый текст Яндекс ограничивает 100
символами, а шаблон зачитывается целиком (проверено на прототипе).
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

INTENTS_FILE = "info_digest_intents.yaml"
MAX_INTENTS = 180
MAX_EXTRA = 3
_PHRASE_OK = re.compile(r"[А-Яа-яЁё0-9 ]+")

MAIN_SENSOR = "sensor.city_digest_6h"
NIGHT_SENSOR = "sensor.city_digest_night"
CLASSES_SENSOR = "sensor.city_digest_classes"
HEALTH_SENSOR = "binary_sensor.city_digest_healthy"


@dataclass
class Intent:
    phrase: str
    extra: list[str] = field(default_factory=list)
    say_template: str = ""


def clean_phrase(p: str) -> str | None:
    p = " ".join((p or "").split())
    return p if p and _PHRASE_OK.fullmatch(p) else None


def say_template(entity: str, attribute: str) -> str:
    """Ответ из атрибута сенсора; если сбор не работает — честно об этом (FR-V3)."""
    return (
        f"{{% set t = state_attr('{entity}', '{attribute}') %}}"
        f"{{% if t and is_state('{HEALTH_SENSOR}', 'on') %}}{{{{ t }}}}"
        f"{{% elif t %}}{{{{ t }}}} Сбор новостей сейчас не работает, данные могут быть устаревшими."
        f"{{% else %}}Не могу получить новости из городского чата.{{% endif %}}"
    )


def build_intents(groups: list[dict], categories: list[dict]) -> list[Intent]:
    """groups и categories: dict с ключами slug, phrases (list[str]), active/in_digest."""
    intents = [
        Intent("Что нового в городе", ["Что происходит в городе", "Какие новости в городе"],
               say_template(MAIN_SENSOR, "text")),
        Intent("Что было за ночь", ["Что случилось ночью", "Новости за ночь"], say_template(NIGHT_SENSOR, "text")),
    ]
    items = [g for g in groups if g.get("in_digest", True)] + [
        c for c in categories if c.get("in_digest", True) and c.get("active", True)
    ]
    for it in items:
        phrases = [p for p in (clean_phrase(x) for x in it.get("phrases") or []) if p]
        bad = [x for x in it.get("phrases") or [] if not clean_phrase(x)]
        if bad:
            log.warning("Фразы %s для «%s» пропущены: можно только кириллицу, цифры и пробелы", bad, it["slug"])
        if phrases:
            intents.append(Intent(phrases[0], phrases[1 : 1 + MAX_EXTRA], say_template(CLASSES_SENSOR, it["slug"])))

    # Одна фраза — один сценарий в «Доме с Алисой»: повторы убираем.
    seen: set[str] = set()
    result = []
    for intent in intents:
        key = intent.phrase.lower()
        if key in seen:
            log.warning("Фраза «%s» повторяется — оставлена первая", intent.phrase)
            continue
        seen.add(key)
        intent.extra = [e for e in intent.extra if e.lower() not in seen]
        seen.update(e.lower() for e in intent.extra)
        result.append(intent)
    if len(result) > MAX_INTENTS:
        log.warning("Фраз больше %d — лишние не созданы, чтобы не превысить лимит сценариев Яндекса", MAX_INTENTS)
        result = result[:MAX_INTENTS]
    return result


def render_yaml(intents: list[Intent]) -> str:
    """YAML для `yandex_station_intents: !include info_digest_intents.yaml`.

    Строки записываются в JSON-кавычках: это корректный YAML и не требует PyYAML.
    """
    q = lambda s: json.dumps(s, ensure_ascii=False)
    lines = [
        "# Создано дополнением Info Digest автоматически — не редактируйте вручную.",
        "# Фразы меняются в классификаторе дополнения.",
        "intents:",
    ]
    for it in intents:
        lines.append(f"  {q(it.phrase)}:")
        if it.extra:
            lines.append("    extra_phrases:")
            lines.extend(f"      - {q(e)}" for e in it.extra)
        lines.append(f"    say_phrase: {q(it.say_template)}")
    return "\n".join(lines) + "\n"
