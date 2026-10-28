"""Правила отсева, очистка персональных данных, промпт и разбор ответа LLM.

Модуль не ходит в сеть и не зависит от БД, поэтому легко тестируется.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .taxonomy import ADS, FLOOD, OTHER

CLASSIFIER_VERSION = "1"
MIN_TEXT_LEN = 15
MAX_TEXT_FOR_LLM = 600

# Порядок важности групп: при разметке без LLM берётся первая совпавшая категория.
GROUP_PRIORITY = ["safety", "utilities", "roads", "city"]


@dataclass
class Category:
    id: int
    slug: str
    name: str
    group_slug: str
    group_name: str
    description: str
    keywords: list[str]
    in_digest: bool = True
    active: bool = True
    _pattern: re.Pattern | None = field(default=None, repr=False)

    @property
    def pattern(self) -> re.Pattern | None:
        if self._pattern is None and self.keywords:
            alts = "|".join(re.escape(normalize(k)) for k in self.keywords)
            self._pattern = re.compile(rf"(?<!\w)(?:{alts})")
        return self._pattern


def normalize(text: str) -> str:
    return (text or "").lower().replace("ё", "е")


# --- персональные данные ------------------------------------------------

_PHONE = re.compile(r"(?<!\d)(?:\+7|8|7)?[\s\-(]*\d{3}[\s\-)]*\d{3}[\s\-]*\d{2}[\s\-]*\d{2}(?!\d)")
_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_USERNAME = re.compile(r"(?<![\w@])@\w{3,}")
_APARTMENT = re.compile(r"(?i)\b(кв\.?|квартир[аеуы])\s*№?\s*\d+")
_LINK = re.compile(r"https?://\S+")


def scrub_pii(text: str) -> str:
    """Убирает телефоны, почту, ники, номера квартир и ссылки перед отправкой в LLM (NFR-6)."""
    text = _LINK.sub("[ссылка]", text)
    text = _EMAIL.sub("[почта]", text)
    text = _PHONE.sub("[телефон]", text)
    text = _USERNAME.sub("[ник]", text)
    text = _APARTMENT.sub("[квартира]", text)
    return text


# --- правила отсева ------------------------------------------------------


@dataclass
class Prefilter:
    """Итог отсева: kind — candidate (в LLM), flood, ads или no_keywords."""

    kind: str
    matched: list[str] = field(default_factory=list)  # slug совпавших категорий

    @property
    def is_candidate(self) -> bool:
        return self.kind == "candidate"


def prefilter(text: str, categories: list[Category], reply_in_event: bool = False) -> Prefilter:
    """Бесплатный отсев правилами (FR-K5).

    Ответ на сообщение, уже привязанное к событию, всегда идёт в LLM:
    так «свет дали» попадает в своё событие, даже без ключевых слов.
    """
    norm = normalize(text)
    matched = [
        c.slug
        for c in categories
        if c.active and c.slug not in (FLOOD, ADS, OTHER) and c.pattern and c.pattern.search(norm)
    ]
    if reply_in_event:
        return Prefilter("candidate", matched)
    if len(norm.strip()) < MIN_TEXT_LEN and not matched:
        # Короткое без ключевых слов — флуд; а «Пожар на рынке» короткое, но важное.
        return Prefilter("flood")
    ads = next((c for c in categories if c.slug == ADS), None)
    important = {c.slug for c in categories if c.group_slug in ("safety", "utilities")}
    if ads and ads.pattern and ads.pattern.search(norm) and not important.intersection(matched):
        return Prefilter("ads", matched)
    if not matched:
        return Prefilter("no_keywords")
    return Prefilter("candidate", matched)


def rule_category(matched: list[str], categories: list[Category]) -> Category | None:
    """Категория для разметки без LLM: первая совпавшая по важности группы."""
    by_slug = {c.slug: c for c in categories}
    for group in GROUP_PRIORITY:
        for slug in matched:
            c = by_slug.get(slug)
            if c and c.group_slug == group:
                return c
    return by_slug.get(matched[0]) if matched else None


# --- промпт и ответ ------------------------------------------------------


@dataclass
class LlmItem:
    """Сообщение для разметки: n — локальный номер в пакете, reply_n — на какое из пакета это ответ."""

    n: int
    text: str
    reply_n: int | None = None


@dataclass
class LlmLabel:
    category: str
    extra: list[str]
    place: str | None
    official: bool
    confidence: float
    title: str | None


SYSTEM_PROMPT = """Ты размечаешь сообщения из городского чата. Для каждого сообщения выбери одну категорию из списка.

Категории (slug — группа / название: что относится):
{categories}

Правила:
- Если сообщение не сообщает о событии в городе (общение, споры, приветствия, вопросы без новой информации), ставь "{flood}". Продажа, покупка, аренда, услуги, реклама — "{ads}". Если не подходит ничего — "{other}".
- extra — до двух дополнительных категорий, если сообщение относится и к ним; иначе пустой список.
- place — улица, район, микрорайон или объект (например «ул. Ленина», «набережная», «район рынка»). Без номеров квартир. Если места нет — null.
- official — true, если это объявление или пересылка от официального источника (администрация, МЧС, энергетики, водоканал), иначе false.
- confidence — уверенность в категории от 0 до 1.
- title — короткий нейтральный заголовок события до 60 символов, без имён, например «Нет воды на ул. Ленина». Для {flood}, {ads} и {other} — null.

Ответь только JSON без пояснений, ровно в таком виде:
{{"items": [{{"n": 1, "category": "slug", "extra": [], "place": null, "official": false, "confidence": 0.9, "title": "..."}}]}}"""


def build_prompt(categories: list[Category], items: list[LlmItem]) -> tuple[str, str]:
    cats = "\n".join(
        f"- {c.slug} — {c.group_name} / {c.name}: {c.description}"
        for c in categories
        if c.active
    )
    system = SYSTEM_PROMPT.format(categories=cats, flood=FLOOD, ads=ADS, other=OTHER)
    lines = []
    for it in items:
        text = scrub_pii(it.text).replace("\n", " ").strip()[:MAX_TEXT_FOR_LLM]
        prefix = f"[{it.n}]" + (f" (ответ на [{it.reply_n}])" if it.reply_n else "")
        lines.append(f"{prefix} {text}")
    user = "Сообщения:\n" + "\n".join(lines)
    return system, user


_JSON_OBJECT = re.compile(r"\{.*\}", re.DOTALL)


def parse_response(content: str, numbers: list[int], known_slugs: set[str]) -> dict[int, LlmLabel]:
    """Разбирает ответ модели. Бросает ValueError, если это не JSON нужного вида.

    Неизвестная категория заменяется на «Прочее», пропущенные сообщения
    просто отсутствуют в результате — их разметит следующий проход.
    """
    m = _JSON_OBJECT.search(content or "")
    if not m:
        raise ValueError("в ответе модели нет JSON")
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError as e:
        raise ValueError(f"ответ модели — не JSON: {e}") from e
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ValueError("в ответе модели нет списка items")  # noqa: TRY004 — для цепочки это «плохой ответ»

    wanted = set(numbers)
    result: dict[int, LlmLabel] = {}
    for it in items:
        if not isinstance(it, dict):
            continue
        try:
            n = int(it.get("n"))
        except (TypeError, ValueError):
            continue
        if n not in wanted:
            continue
        cat = str(it.get("category") or OTHER).strip()
        if cat not in known_slugs:
            cat = OTHER
        extra = [str(x) for x in (it.get("extra") or []) if str(x) in known_slugs and str(x) != cat][:2]
        place = it.get("place")
        place = str(place).strip()[:80] if place not in (None, "", "null") else None
        try:
            conf = float(it.get("confidence", 0.5))
        except (TypeError, ValueError):
            conf = 0.5
        title = it.get("title")
        title = str(title).strip()[:80] if title not in (None, "", "null") else None
        result[n] = LlmLabel(
            category=cat,
            extra=extra,
            place=place,
            official=bool(it.get("official", False)),
            confidence=min(max(conf, 0.0), 1.0),
            title=title,
        )
    if numbers and not result:
        raise ValueError("модель не разметила ни одного сообщения из пакета")
    return result
