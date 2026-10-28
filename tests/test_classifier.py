import json

import pytest
from app.classifier import (
    Category,
    LlmItem,
    build_prompt,
    parse_response,
    prefilter,
    rule_category,
    scrub_pii,
)
from app.taxonomy import DEFAULT_TAXONOMY


@pytest.fixture
def cats():
    out, i = [], 0
    for g in DEFAULT_TAXONOMY:
        for c in g.categories:
            i += 1
            out.append(Category(i, c.slug, c.name, g.slug, g.name, c.description, c.keywords, c.in_digest))
    return out


@pytest.mark.parametrize(
    "text,kind,expect",
    [
        ("ок", "flood", None),
        ("Пожар на рынке", "candidate", "fire"),
        ("Всем привет, как дела у всех?", "no_keywords", None),
        ("На Ленина с утра нет воды, кто знает когда дадут?", "candidate", "water"),
        ("Опять отключили свет на Гагарина", "candidate", "power"),
        ("Продам велосипед недорого, писать в личку", "ads", None),
        ("Продаю генератор, света опять нет", "candidate", "power"),
        ("ДТП на объездной, стоим в пробке", "candidate", "accident"),
        ("Штормовое предупреждение, ветер до 25 м/с", "candidate", "weather"),
        ("Водитель маршрутки опять опоздал", "candidate", "transport"),
    ],
)
def test_prefilter(cats, text, kind, expect):
    pf = prefilter(text, cats)
    assert pf.kind == kind
    if expect:
        assert expect in pf.matched


def test_prefilter_reply_to_event_always_candidate(cats):
    assert prefilter("дали!", cats, reply_in_event=True).is_candidate


def test_rule_category_prefers_safety(cats):
    assert rule_category(["improvement", "fire"], cats).slug == "fire"
    assert rule_category([], cats) is None


@pytest.mark.parametrize(
    "text,hidden,kept",
    [
        ("Звоните +7 (978) 123-45-67", "123-45-67", "[телефон]"),
        ("пишите 89781234567", "89781234567", "[телефон]"),
        ("почта ivan.petrov@mail.ru", "ivan.petrov", "[почта]"),
        ("спросите у @vasya_sudak", "vasya_sudak", "[ник]"),
        ("затопило кв. 45 на Ленина", "45", "[квартира]"),
        ("подробнее https://t.me/x/123", "t.me", "[ссылка]"),
    ],
)
def test_scrub_pii(text, hidden, kept):
    out = scrub_pii(text)
    assert hidden not in out
    assert kept in out


def test_scrub_keeps_times_and_dates():
    assert scrub_pii("свет дадут в 18:00 28.10") == "свет дадут в 18:00 28.10"


def test_build_prompt_contains_categories_and_scrubbed_text(cats):
    system, user = build_prompt(cats, [LlmItem(1, "нет воды, звоните 89781234567"), LlmItem(2, "дали", reply_n=1)])
    assert "water — ЖКХ / Вода" in system
    assert "89781234567" not in user
    assert "[2] (ответ на [1]) дали" in user


def test_parse_response_valid(cats):
    known = {c.slug for c in cats}
    content = "Вот ответ:\n" + json.dumps(
        {
            "items": [
                {"n": 1, "category": "water", "extra": ["power", "nonsense"], "place": "ул. Ленина",
                 "official": False, "confidence": 0.93, "title": "Нет воды на ул. Ленина"},
                {"n": 2, "category": "unknown_cat", "place": "null", "confidence": "high"},
                {"n": 99, "category": "fire"},
            ]
        },
        ensure_ascii=False,
    )
    r = parse_response(content, [1, 2], known)
    assert r[1].category == "water" and r[1].extra == ["power"] and r[1].place == "ул. Ленина"
    assert r[1].confidence == pytest.approx(0.93)
    assert r[2].category == "other" and r[2].place is None and r[2].confidence == 0.5
    assert 99 not in r


@pytest.mark.parametrize("content", ["", "не знаю", '{"foo": 1}', '{"items": [{"n": 5}]}'])
def test_parse_response_invalid(cats, content):
    with pytest.raises(ValueError):
        parse_response(content, [1, 2], {c.slug for c in cats})
