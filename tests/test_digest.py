from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from app.digest import (
    DigestEvent,
    build_category_answer,
    build_group_answer,
    build_main_digest,
    build_night_digest,
    event_phrase,
    join_limited,
    night_bounds,
    plural,
)

MSK = ZoneInfo("Europe/Moscow")
NOW = datetime(2026, 10, 28, 11, 20, tzinfo=UTC)  # 14:20 по Москве


def ev(title, cat="water", group="utilities", minutes_ago=60, active=True, authors=2, official=False):
    t = NOW - timedelta(minutes=minutes_ago)
    return DigestEvent(title=title, place=None, first_at=t, last_at=t, active=active, category_slug=cat,
                       category=cat, group_slug=group, group_name=group, authors=authors, official=official)


@pytest.mark.parametrize("n,word", [(1, "событие"), (2, "события"), (5, "событий"), (11, "событий"),
                                    (21, "событие"), (24, "события"), (112, "событий")])
def test_plural(n, word):
    assert plural(n, "событие", "события", "событий") == word


def test_event_phrase_confirmed_and_not():
    assert event_phrase(ev("Нет воды на ул. Ленина", minutes_ago=40), MSK) == "Нет воды на ул. Ленина, с 13:40"
    assert event_phrase(ev("Нет воды на ул. Ленина.", authors=1), MSK).startswith("пишут, что нет воды")
    assert event_phrase(ev("Сообщение МЧС", authors=1, official=True), MSK).startswith("Сообщение МЧС")
    assert event_phrase(ev("Пожар", active=False, minutes_ago=200), MSK).endswith("было в 11:00")


def test_main_digest_empty_and_order():
    assert build_main_digest([], NOW, MSK) == (
        "По данным на 14:20. За последние 6 часов в городском чате ничего важного."
    )
    text = build_main_digest(
        [ev("Концерт в парке", "events", "city", 10), ev("Нет воды на Ленина", minutes_ago=30),
         ev("Пожар на рынке", "fire", "safety", 50), ev("ДТП на объездной", "accident", "roads", 5, active=False)],
        NOW, MSK,
    )
    assert text.startswith("По данным на 14:20. За последние 6 часов: Пожар на рынке")
    assert text.index("Нет воды") < text.index("Концерт") < text.index("ДТП")
    assert text.endswith(".")


def test_main_digest_respects_length_limit():
    events = [ev(f"Событие номер {i} с довольно длинным заголовком про улицу", minutes_ago=i) for i in range(40)]
    text = build_main_digest(events, NOW, MSK)
    assert len(text) <= 600
    assert "и ещё" in text and text.endswith((" событий.", " события.", " событие."))


def test_join_limited_single_item_never_dropped():
    assert join_limited("X: ", ["a" * 700], limit=600).startswith("X: aaa")


def test_night_bounds():
    s, e = night_bounds(datetime(2026, 10, 28, 6, 0, tzinfo=MSK), MSK)  # ночь ещё идёт
    assert (s.hour, s.day, e.hour) == (22, 27, 6)
    s, e = night_bounds(datetime(2026, 10, 28, 14, 0, tzinfo=MSK), MSK)
    assert (s.day, s.hour, e.day, e.hour) == (27, 22, 28, 7)


def test_night_digest():
    assert build_night_digest([], MSK) == "За ночь в городском чате ничего важного."
    assert build_night_digest([ev("Штормовое предупреждение", "weather", "safety")], MSK).startswith("За ночь: Шторм")


def test_category_answer():
    assert build_category_answer("Электричество", [], 24, MSK) == (
        "Электричество: за последние 24 часа сообщений нет."
    )
    t = build_category_answer("Вода", [ev("Нет воды на Ленина"), ev("Нет воды на Гагарина", authors=1)], 24, MSK)
    assert t.startswith("Вода, за последние 24 часа 2 события: Нет воды на Ленина")
    assert "пишут, что нет воды на Гагарина" in t


def test_group_answer_skips_empty_categories():
    cats = [("power", "Электричество"), ("water", "Вода"), ("gas", "Газ")]
    t = build_group_answer("ЖКХ", cats, [ev("Нет воды на Ленина")], 24, MSK)
    assert t == "ЖКХ, за последние 24 часа: вода: Нет воды на Ленина, с 13:20; по остальным темам сообщений нет."
    assert build_group_answer("ЖКХ", cats, [], 24, MSK) == "ЖКХ: за последние 24 часа сообщений нет."
