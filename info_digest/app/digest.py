"""Тексты сводок и ответов Алисы по событиям (FR-S, FR-Q).

Тексты собираются шаблоном из заголовков событий, без вызова модели, поэтому
обновляются за секунды и ничего не стоят. Модуль без ввода-вывода — его
удобно тестировать.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, time, timedelta, tzinfo

from .taxonomy import GROUP_ORDER

MAX_LEN = 600


@dataclass
class DigestEvent:
    title: str
    place: str | None
    first_at: datetime
    last_at: datetime
    active: bool
    category_slug: str
    category: str
    group_slug: str
    group_name: str
    authors: int
    official: bool

    @classmethod
    def from_row(cls, r) -> DigestEvent:
        return cls(
            title=r["title"],
            place=r["place"],
            first_at=datetime.fromisoformat(r["first_at"]),
            last_at=datetime.fromisoformat(r["last_at"]),
            active=r["status"] == "active",
            category_slug=r["category_slug"],
            category=r["category"],
            group_slug=r["group_slug"],
            group_name=r["group_name"],
            authors=int(r["authors"] or 0),
            official=bool(r["official"]),
        )

    @property
    def confirmed(self) -> bool:
        """Подтверждено: официальный источник или не меньше двух независимых авторов (FR-S6)."""
        return self.official or self.authors >= 2


def hhmm(dt: datetime, tz: tzinfo) -> str:
    local = dt.astimezone(tz)
    return f"{local.hour}:{local.minute:02d}"


def plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def hours_text(h: int) -> str:
    return f"{h} {plural(h, 'час', 'часа', 'часов')}"


def _lower_first(s: str) -> str:
    return s[:1].lower() + s[1:] if s else s


def _upper_first(s: str) -> str:
    return s[:1].upper() + s[1:] if s else s


def event_phrase(e: DigestEvent, tz: tzinfo) -> str:
    """«Нет воды на ул. Ленина, с 9:40» или «пишут, что нет воды…», если не подтверждено."""
    text = e.title.strip().rstrip(".")
    if not e.confirmed:
        text = "пишут, что " + _lower_first(text)
    when = f"с {hhmm(e.first_at, tz)}" if e.active else f"было в {hhmm(e.last_at, tz)}"
    return f"{text}, {when}"


def sort_events(events: list[DigestEvent]) -> list[DigestEvent]:
    def key(e: DigestEvent):
        g = GROUP_ORDER.index(e.group_slug) if e.group_slug in GROUP_ORDER else len(GROUP_ORDER)
        return (not e.active, g, -e.last_at.timestamp())

    return sorted(events, key=key)


def join_limited(head: str, items: list[str], limit: int = MAX_LEN) -> str:
    """Склеивает пункты через «;», пока помещается в лимит; остаток — «и ещё N событий»."""
    out = head
    for i, item in enumerate(items):
        sep = "" if i == 0 else "; "
        rest = len(items) - i - 1
        tail = f"; и ещё {rest} {plural(rest, 'событие', 'события', 'событий')}." if rest else "."
        if len(out) + len(sep) + len(item) + len(tail) > limit and i > 0:
            left = len(items) - i
            return out + f"; и ещё {left} {plural(left, 'событие', 'события', 'событий')}."
        out += sep + item
    return out + "."


def build_main_digest(events: list[DigestEvent], now: datetime, tz: tzinfo, hours: int = 6) -> str:
    head = f"По данным на {hhmm(now, tz)}. "
    if not events:
        return head + f"За последние {hours_text(hours)} в городском чате ничего важного."
    items = [event_phrase(e, tz) for e in sort_events(events)]
    items[0] = _upper_first(items[0])
    return join_limited(head + f"За последние {hours_text(hours)}: ", items)


def night_bounds(now: datetime, tz: tzinfo) -> tuple[datetime, datetime]:
    """Ночь — с 22:00 до 7:00 по местному времени; до 7 утра — текущая ночь до сейчас."""
    local = now.astimezone(tz)
    today = local.date()
    if local.time() < time(7, 0):
        start = datetime.combine(today - timedelta(days=1), time(22, 0), tz)
        return start, now
    start = datetime.combine(today - timedelta(days=1), time(22, 0), tz)
    end = datetime.combine(today, time(7, 0), tz)
    return start, end


def build_night_digest(events: list[DigestEvent], tz: tzinfo) -> str:
    if not events:
        return "За ночь в городском чате ничего важного."
    items = [event_phrase(e, tz) for e in sort_events(events)]
    return join_limited("За ночь: ", items)


def build_category_answer(name: str, events: list[DigestEvent], hours: int, tz: tzinfo) -> str:
    if not events:
        return f"{name}: за последние {hours_text(hours)} сообщений нет."
    items = [event_phrase(e, tz) for e in sort_events(events)]
    n = len(events)
    head = f"{name}, за последние {hours_text(hours)} {n} {plural(n, 'событие', 'события', 'событий')}: "
    return join_limited(head, items)


def build_group_answer(
    name: str, categories: list[tuple[str, str]], events: list[DigestEvent], hours: int, tz: tzinfo
) -> str:
    """categories — (slug, название) в порядке классификатора. Пустые категории пропускаются (FR-Q8)."""
    parts, empty = [], []
    for slug, cat_name in categories:
        evs = [e for e in events if e.category_slug == slug]
        if not evs:
            empty.append(cat_name)
            continue
        phrases = ", ".join(event_phrase(e, tz) for e in sort_events(evs)[:3])
        parts.append(f"{_lower_first(cat_name)}: {phrases}")
    if not parts:
        return f"{name}: за последние {hours_text(hours)} сообщений нет."
    text = f"{name}, за последние {hours_text(hours)}: " + "; ".join(parts)
    if empty:
        text += "; по остальным темам сообщений нет"
    text += "."
    if len(text) > MAX_LEN:
        text = text[: MAX_LEN - 1].rsplit(";", 1)[0] + "."
    return text
