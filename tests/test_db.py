from datetime import UTC, datetime, timedelta

import pytest
from app.db import Database, Message

NOW = datetime(2026, 10, 28, 12, 0, tzinfo=UTC)


@pytest.fixture
def db(tmp_path):
    d = Database(tmp_path / "t.db")
    yield d
    d.close()


def msg(source_id, msg_id, hours_ago=0, text="текст", **kw):
    return Message(source_id=source_id, msg_id=msg_id, date=NOW - timedelta(hours=hours_ago), text=text, **kw)


def test_ensure_source_is_idempotent_and_updates_title(db):
    a = db.ensure_source("telegram", "chatsudaka")
    b = db.ensure_source("telegram", "chatsudaka", title="Чат Судака", peer_id=-100123)
    assert a == b
    s = db.stats(now=NOW)[0]
    assert s.title == "Чат Судака"


def test_upsert_has_no_duplicates_and_edit_updates_text(db):
    sid = db.ensure_source("telegram", "c")
    assert db.upsert_message(msg(sid, 1, text="нет воды")) is True
    assert db.upsert_message(msg(sid, 1, text="нет воды")) is False
    assert db.upsert_message(msg(sid, 1, text="воду дали", edited_at=NOW)) is False
    row = db.conn.execute("SELECT text, edited_at FROM messages").fetchall()
    assert len(row) == 1
    assert row[0]["text"] == "воду дали"
    assert row[0]["edited_at"] is not None


def test_same_msg_id_in_different_sources_kept_apart(db):
    a = db.ensure_source("telegram", "a")
    b = db.ensure_source("max", "b")
    db.upsert_message(msg(a, 5))
    db.upsert_message(msg(b, 5))
    assert db.conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 2


def test_last_msg_id(db):
    sid = db.ensure_source("telegram", "c")
    assert db.last_msg_id(sid) is None
    for i in (3, 10, 7):
        db.upsert_message(msg(sid, i))
    assert db.last_msg_id(sid) == 10


def test_cleanup_removes_only_old(db):
    sid = db.ensure_source("telegram", "c")
    db.upsert_message(msg(sid, 1, hours_ago=24 * 31))
    db.upsert_message(msg(sid, 2, hours_ago=24 * 29))
    assert db.cleanup(30, now=NOW) == 1
    assert db.last_msg_id(sid) == 2


def test_stats_counts_last_24h(db):
    sid = db.ensure_source("telegram", "c")
    db.upsert_message(msg(sid, 1, hours_ago=30))
    db.upsert_message(msg(sid, 2, hours_ago=2))
    db.upsert_message(msg(sid, 3, hours_ago=1))
    s = db.stats(now=NOW)[0]
    assert (s.total, s.last_24h) == (3, 2)
    assert s.last_message_at.startswith("2026-10-28T11:00")


def test_naive_datetime_treated_as_utc(db):
    sid = db.ensure_source("telegram", "c")
    db.upsert_message(Message(source_id=sid, msg_id=1, date=datetime(2026, 10, 28, 10, 0), text="x"))
    assert db.conn.execute("SELECT date FROM messages").fetchone()[0] == "2026-10-28T10:00:00+00:00"


def test_meta(db):
    assert db.get_meta("k") is None
    db.set_meta("k", "1")
    db.set_meta("k", "2")
    assert db.get_meta("k") == "2"
