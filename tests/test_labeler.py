import asyncio
import json
import re
from datetime import UTC, datetime, timedelta

import pytest
from app.db import Database, Message
from app.labeler import Labeler, same_place, snippet_title
from app.llm import Completion, LLMChain, LLMError
from app.taxonomy import DEFAULT_TAXONOMY

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


class ScriptedModel:
    """Отвечает по словарю «фрагмент текста → разметка»; запоминает, что ему прислали."""

    name = "fake"

    def __init__(self, rules, fail=False):
        self.rules = rules
        self.fail = fail
        self.prompts = []

    async def complete(self, system, user, max_tokens):
        self.prompts.append(user)
        if self.fail:
            raise LLMError("payment", "no money")
        items = []
        for line in user.splitlines()[1:]:
            n = int(re.match(r"\[(\d+)\]", line).group(1))
            label = next((v for k, v in self.rules.items() if k in line), {"category": "flood"})
            items.append({"n": n, **label})
        return Completion(json.dumps({"items": items}, ensure_ascii=False), 100, 50)

    async def close(self):
        pass


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    d.seed_taxonomy(DEFAULT_TAXONOMY)
    yield d
    d.close()


def add(db, sid, msg_id, text, minutes_ago, reply_to=None, author="a1"):
    db.upsert_message(Message(sid, msg_id, NOW - timedelta(minutes=minutes_ago), text, reply_to=reply_to,
                              author_hash=author))


def label(db, sid, msg_id):
    return db.conn.execute(
        """SELECT c.slug, l.method, l.event_id, l.place FROM labels l JOIN categories c ON c.id = l.category_id
           WHERE l.source_id = ? AND l.msg_id = ?""",
        (sid, msg_id),
    ).fetchone()


WATER = {"category": "water", "place": "ул. Ленина", "confidence": 0.9, "title": "Нет воды на ул. Ленина"}


def test_rules_first_only_candidates_go_to_llm(db):
    sid = db.ensure_source("telegram", "c")
    add(db, sid, 1, "ок", 50)
    add(db, sid, 2, "Всем доброго утра, соседи!", 49)
    add(db, sid, 3, "Продам велосипед недорого", 48)
    add(db, sid, 4, "На Ленина нет воды с утра", 47)
    model = ScriptedModel({"нет воды": WATER})
    lab = Labeler(db, LLMChain([model]))
    res = asyncio.run(lab.run_once(now=NOW))

    assert (res.total, res.by_rules, res.by_llm) == (4, 3, 1)
    assert len(model.prompts) == 1 and "велосипед" not in model.prompts[0]
    assert label(db, sid, 1)["slug"] == "flood"
    assert label(db, sid, 2)["slug"] == "other"
    assert label(db, sid, 3)["slug"] == "ads"
    assert label(db, sid, 4)["slug"] == "water" and label(db, sid, 4)["event_id"]


def test_messages_grouped_into_one_event_by_place_and_reply(db):
    sid = db.ensure_source("telegram", "c")
    add(db, sid, 1, "На Ленина нет воды с утра", 60, author="a1")
    add(db, sid, 2, "Подтверждаю, у нас тоже нет воды на Ленина", 40, author="a2")
    add(db, sid, 3, "дали!", 10, reply_to=1, author="a3")
    add(db, sid, 4, "На Гагарина тоже нет воды", 5, author="a4")
    model = ScriptedModel({
        "дали": {"category": "water", "place": None, "confidence": 0.8, "title": "Воду дали"},
        "Гагарина": {"category": "water", "place": "ул. Гагарина", "confidence": 0.9, "title": "Нет воды на Гагарина"},
        "нет воды": WATER,
    })
    lab = Labeler(db, LLMChain([model]))
    asyncio.run(lab.run_once(now=NOW))  # 1, 2, 4 — кандидаты; 3 — ответ, но родитель ещё не размечен
    asyncio.run(lab.run_once(now=NOW))

    e1 = label(db, sid, 1)["event_id"]
    assert label(db, sid, 2)["event_id"] == e1
    assert label(db, sid, 4)["event_id"] != e1
    ev = {r["id"]: r for r in db.events_overview(NOW - timedelta(days=1))}
    assert ev[e1]["messages"] >= 2 and ev[e1]["authors"] >= 2


def test_reply_to_event_message_joins_it(db):
    sid = db.ensure_source("telegram", "c")
    add(db, sid, 1, "На Ленина нет воды с утра", 60)
    model = ScriptedModel({"нет воды": WATER, "дали": {"category": "water", "place": None, "title": "Воду дали"}})
    lab = Labeler(db, LLMChain([model]))
    asyncio.run(lab.run_once(now=NOW))
    add(db, sid, 2, "дали!", 5, reply_to=1, author="a2")
    asyncio.run(lab.run_once(now=NOW))
    assert label(db, sid, 2)["event_id"] == label(db, sid, 1)["event_id"]


def test_rule_fallback_when_model_down(db):
    sid = db.ensure_source("telegram", "c")
    add(db, sid, 1, "Пожар на рынке, много дыма", 10)
    lab = Labeler(db, LLMChain([ScriptedModel({}, fail=True)]))
    res = asyncio.run(lab.run_once(now=NOW))
    assert res.rule_fallback == 1
    row = label(db, sid, 1)
    assert row["slug"] == "fire" and row["method"] == "rule_fallback" and row["event_id"]


def test_no_model_configured_uses_rules(db):
    sid = db.ensure_source("telegram", "c")
    add(db, sid, 1, "Опять отключили свет на Гагарина", 10)
    res = asyncio.run(Labeler(db, LLMChain([])).run_once(now=NOW))
    assert res.rule_fallback == 1 and label(db, sid, 1)["slug"] == "power"


def test_edited_message_is_relabeled(db):
    sid = db.ensure_source("telegram", "c")
    add(db, sid, 1, "Всем доброго утра, соседи!", 10)
    lab = Labeler(db, LLMChain([]))
    asyncio.run(lab.run_once(now=NOW))
    assert label(db, sid, 1)["slug"] == "other"
    add(db, sid, 1, "Всем доброго утра! Кстати, на Ленина пожар", 10)
    asyncio.run(lab.run_once(now=NOW))
    assert label(db, sid, 1)["slug"] == "fire"


def test_stale_events_closed(db):
    sid = db.ensure_source("telegram", "c")
    add(db, sid, 1, "Пожар на рынке", 60 * 8)
    lab = Labeler(db, LLMChain([]))
    asyncio.run(lab.run_once(now=NOW))
    ev = db.events_overview(NOW - timedelta(days=1))[0]
    assert ev["status"] == "completed"


def test_cleanup_cascades_labels(db):
    sid = db.ensure_source("telegram", "c")
    add(db, sid, 1, "Пожар на рынке", 60 * 24 * 40)
    asyncio.run(Labeler(db, LLMChain([])).run_once(now=NOW))
    db.cleanup(30, now=NOW)
    assert db.conn.execute("SELECT COUNT(*) FROM labels").fetchone()[0] == 0


@pytest.mark.parametrize(
    "text,title",
    [("Пожар на рынке, много дыма", "Пожар на рынке, много дыма"),
     ("Нет света! Звоните 89781234567 в РЭС", "Нет света"),
     ("Опять " + "очень " * 20 + "долго нет воды", "Опять" + " очень" * 10),
     ("", None)],
)
def test_snippet_title(text, title):
    assert snippet_title(text) == title


@pytest.mark.parametrize(
    "a,b,same",
    [("ул. Ленина", "Ленина", True), ("ул. Ленина", "ул. Гагарина", False), (None, "x", False),
     ("Набережная", "набережная.", True)],
)
def test_same_place(a, b, same):
    assert same_place(a, b) is same
