"""Хранилище сообщений на SQLite.

Объём небольшой (тысячи сообщений в сутки), поэтому используется синхронный
sqlite3 в режиме WAL: отдельные операции занимают миллисекунды.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS sources (
    id          INTEGER PRIMARY KEY,
    kind        TEXT    NOT NULL,
    ref         TEXT    NOT NULL,
    title       TEXT,
    peer_id     INTEGER,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL,
    UNIQUE (kind, ref)
);

CREATE TABLE IF NOT EXISTS messages (
    source_id   INTEGER NOT NULL REFERENCES sources(id),
    msg_id      INTEGER NOT NULL,
    date        TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    has_media   INTEGER NOT NULL DEFAULT 0,
    reply_to    INTEGER,
    author_hash TEXT,
    is_forward  INTEGER NOT NULL DEFAULT 0,
    edited_at   TEXT,
    saved_at    TEXT    NOT NULL,
    PRIMARY KEY (source_id, msg_id)
);

CREATE INDEX IF NOT EXISTS idx_messages_date ON messages(date);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS groups (
    id        INTEGER PRIMARY KEY,
    slug      TEXT    NOT NULL UNIQUE,
    name      TEXT    NOT NULL,
    in_digest INTEGER NOT NULL DEFAULT 1,
    sort      INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS categories (
    id          INTEGER PRIMARY KEY,
    slug        TEXT    NOT NULL UNIQUE,
    group_id    INTEGER NOT NULL REFERENCES groups(id),
    name        TEXT    NOT NULL,
    description TEXT    NOT NULL DEFAULT '',
    keywords    TEXT    NOT NULL DEFAULT '[]',
    in_digest   INTEGER NOT NULL DEFAULT 1,
    active      INTEGER NOT NULL DEFAULT 1,
    sort        INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS events (
    id          INTEGER PRIMARY KEY,
    category_id INTEGER NOT NULL REFERENCES categories(id),
    title       TEXT    NOT NULL,
    place       TEXT,
    first_at    TEXT    NOT NULL,
    last_at     TEXT    NOT NULL,
    status      TEXT    NOT NULL DEFAULT 'active'
);

CREATE INDEX IF NOT EXISTS idx_events_last ON events(last_at);

CREATE TABLE IF NOT EXISTS labels (
    source_id          INTEGER NOT NULL,
    msg_id             INTEGER NOT NULL,
    category_id        INTEGER REFERENCES categories(id),
    extra              TEXT    NOT NULL DEFAULT '[]',
    place              TEXT,
    is_official        INTEGER NOT NULL DEFAULT 0,
    confidence         REAL    NOT NULL DEFAULT 0,
    method             TEXT    NOT NULL,
    classifier_version TEXT    NOT NULL,
    event_id           INTEGER REFERENCES events(id) ON DELETE SET NULL,
    labeled_at         TEXT    NOT NULL,
    PRIMARY KEY (source_id, msg_id),
    FOREIGN KEY (source_id, msg_id) REFERENCES messages(source_id, msg_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_labels_event ON labels(event_id);
"""


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    """Время в UTC в формате ISO 8601 с секундами — так строки сравнимы между собой."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC).isoformat(timespec="seconds")


@dataclass
class Message:
    source_id: int
    msg_id: int
    date: datetime
    text: str
    has_media: bool = False
    reply_to: int | None = None
    author_hash: str | None = None
    is_forward: bool = False
    edited_at: datetime | None = None


@dataclass
class SourceStats:
    id: int
    kind: str
    ref: str
    title: str | None
    enabled: bool
    total: int
    last_24h: int
    last_message_at: str | None


class Database:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self.conn = sqlite3.connect(self.path, isolation_level=None, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # --- источники -----------------------------------------------------

    def ensure_source(self, kind: str, ref: str, title: str | None = None, peer_id: int | None = None) -> int:
        """Создаёт источник или обновляет его название; возвращает id."""
        self.conn.execute(
            """
            INSERT INTO sources (kind, ref, title, peer_id, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (kind, ref) DO UPDATE SET
                title   = COALESCE(excluded.title, sources.title),
                peer_id = COALESCE(excluded.peer_id, sources.peer_id)
            """,
            (kind, ref, title, peer_id, iso(utcnow())),
        )
        row = self.conn.execute("SELECT id FROM sources WHERE kind = ? AND ref = ?", (kind, ref)).fetchone()
        return int(row["id"])

    # --- сообщения -----------------------------------------------------

    def upsert_message(self, m: Message) -> bool:
        """Сохраняет сообщение. Повтор того же сообщения не создаёт дубля,
        а правка обновляет текст. Возвращает True, если запись новая."""
        exists = self.conn.execute(
            "SELECT text FROM messages WHERE source_id = ? AND msg_id = ?", (m.source_id, m.msg_id)
        ).fetchone()
        if exists is not None and exists["text"] != m.text:
            # Текст изменился — разметка устарела, сообщение будет размечено заново.
            self.conn.execute("DELETE FROM labels WHERE source_id = ? AND msg_id = ?", (m.source_id, m.msg_id))
        self.conn.execute(
            """
            INSERT INTO messages (source_id, msg_id, date, text, has_media, reply_to,
                                  author_hash, is_forward, edited_at, saved_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source_id, msg_id) DO UPDATE SET
                text      = excluded.text,
                has_media = excluded.has_media,
                edited_at = COALESCE(excluded.edited_at, messages.edited_at)
            """,
            (
                m.source_id,
                m.msg_id,
                iso(m.date),
                m.text,
                int(m.has_media),
                m.reply_to,
                m.author_hash,
                int(m.is_forward),
                iso(m.edited_at) if m.edited_at else None,
                iso(utcnow()),
            ),
        )
        return exists is None

    def last_msg_id(self, source_id: int) -> int | None:
        row = self.conn.execute("SELECT MAX(msg_id) AS m FROM messages WHERE source_id = ?", (source_id,)).fetchone()
        return row["m"]

    def cleanup(self, retention_days: int, now: datetime | None = None, event_retention_days: int = 90) -> int:
        """Удаляет сообщения старше срока хранения (вместе с их метками) и старые
        события; возвращает число удалённых сообщений."""
        now = now or utcnow()
        cutoff = iso(now - timedelta(days=retention_days))
        cur = self.conn.execute("DELETE FROM messages WHERE date < ?", (cutoff,))
        self.conn.execute(
            "DELETE FROM events WHERE last_at < ? AND id NOT IN (SELECT event_id FROM labels WHERE event_id IS NOT NULL)",
            (iso(now - timedelta(days=event_retention_days)),),
        )
        return cur.rowcount

    def stats(self, now: datetime | None = None) -> list[SourceStats]:
        since = iso((now or utcnow()) - timedelta(hours=24))
        rows = self.conn.execute(
            """
            SELECT s.id, s.kind, s.ref, s.title, s.enabled,
                   COUNT(m.msg_id)                                   AS total,
                   COALESCE(SUM(CASE WHEN m.date >= ? THEN 1 END), 0) AS last_24h,
                   MAX(m.date)                                       AS last_message_at
            FROM sources s
            LEFT JOIN messages m ON m.source_id = s.id
            GROUP BY s.id
            ORDER BY s.id
            """,
            (since,),
        ).fetchall()
        return [
            SourceStats(
                id=r["id"],
                kind=r["kind"],
                ref=r["ref"],
                title=r["title"],
                enabled=bool(r["enabled"]),
                total=r["total"],
                last_24h=r["last_24h"],
                last_message_at=r["last_message_at"],
            )
            for r in rows
        ]

    # --- служебные значения -------------------------------------------

    def get_meta(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) ON CONFLICT (key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def add_meta_int(self, key: str, delta: int) -> int:
        current = int(self.get_meta(key) or 0) + delta
        self.set_meta(key, str(current))
        return current

    # --- классификатор -------------------------------------------------

    def seed_taxonomy(self, groups) -> bool:
        """Заполняет классификатор стартовым набором, если он пуст. Возвращает True, если заполнил."""
        if self.conn.execute("SELECT COUNT(*) FROM categories").fetchone()[0]:
            return False
        for gi, g in enumerate(groups):
            cur = self.conn.execute(
                "INSERT INTO groups (slug, name, in_digest, sort) VALUES (?, ?, ?, ?)",
                (g.slug, g.name, int(g.in_digest), gi),
            )
            for ci, c in enumerate(g.categories):
                self.conn.execute(
                    """INSERT INTO categories (slug, group_id, name, description, keywords, in_digest, sort)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (c.slug, cur.lastrowid, c.name, c.description, json.dumps(c.keywords, ensure_ascii=False),
                     int(c.in_digest and g.in_digest), ci),
                )
        return True

    def categories(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT c.id, c.slug, c.name, c.description, c.keywords, c.in_digest, c.active,
                   g.slug AS group_slug, g.name AS group_name
            FROM categories c JOIN groups g ON g.id = c.group_id
            ORDER BY g.sort, c.sort
            """
        ).fetchall()

    # --- разметка ------------------------------------------------------

    def unlabeled_messages(self, limit: int = 500) -> list[sqlite3.Row]:
        return self.conn.execute(
            """
            SELECT m.source_id, m.msg_id, m.date, m.text, m.reply_to, m.author_hash, m.is_forward
            FROM messages m
            LEFT JOIN labels l ON l.source_id = m.source_id AND l.msg_id = m.msg_id
            WHERE l.msg_id IS NULL
            ORDER BY m.date, m.msg_id
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

    def get_label(self, source_id: int, msg_id: int) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM labels WHERE source_id = ? AND msg_id = ?", (source_id, msg_id)
        ).fetchone()

    def save_label(
        self,
        source_id: int,
        msg_id: int,
        category_id: int | None,
        method: str,
        version: str,
        *,
        extra: list[str] | None = None,
        place: str | None = None,
        official: bool = False,
        confidence: float = 0.0,
        event_id: int | None = None,
        now: datetime | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO labels (source_id, msg_id, category_id, extra, place, is_official, confidence,
                                method, classifier_version, event_id, labeled_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (source_id, msg_id) DO UPDATE SET
                category_id = excluded.category_id, extra = excluded.extra, place = excluded.place,
                is_official = excluded.is_official, confidence = excluded.confidence, method = excluded.method,
                classifier_version = excluded.classifier_version, event_id = excluded.event_id,
                labeled_at = excluded.labeled_at
            """,
            (source_id, msg_id, category_id, json.dumps(extra or [], ensure_ascii=False), place, int(official),
             confidence, method, version, event_id, iso(now or utcnow())),
        )

    def label_stats(self, now: datetime | None = None, hours: int = 24) -> dict:
        """Счётчики разметки за последние часы: по категориям и по способу (правила/LLM)."""
        since = iso((now or utcnow()) - timedelta(hours=hours))
        by_cat = self.conn.execute(
            """
            SELECT g.name AS group_name, c.name AS category, c.in_digest, COUNT(*) AS n
            FROM labels l JOIN messages m ON m.source_id = l.source_id AND m.msg_id = l.msg_id
            JOIN categories c ON c.id = l.category_id JOIN groups g ON g.id = c.group_id
            WHERE m.date >= ?
            GROUP BY c.id ORDER BY g.sort, c.sort
            """,
            (since,),
        ).fetchall()
        by_method = dict(
            self.conn.execute(
                """
                SELECT l.method, COUNT(*) FROM labels l
                JOIN messages m ON m.source_id = l.source_id AND m.msg_id = l.msg_id
                WHERE m.date >= ? GROUP BY l.method
                """,
                (since,),
            ).fetchall()
        )
        pending = self.conn.execute(
            """SELECT COUNT(*) FROM messages m LEFT JOIN labels l
               ON l.source_id = m.source_id AND l.msg_id = m.msg_id WHERE l.msg_id IS NULL"""
        ).fetchone()[0]
        return {"by_category": [dict(r) for r in by_cat], "by_method": by_method, "pending": pending}

    # --- события -------------------------------------------------------

    def active_events(self, category_id: int, since: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            """SELECT * FROM events WHERE category_id = ? AND status = 'active' AND last_at >= ?
               ORDER BY last_at DESC""",
            (category_id, iso(since)),
        ).fetchall()

    def get_event(self, event_id: int) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()

    def create_event(self, category_id: int, title: str, place: str | None, at: datetime) -> int:
        cur = self.conn.execute(
            "INSERT INTO events (category_id, title, place, first_at, last_at) VALUES (?, ?, ?, ?, ?)",
            (category_id, title, place, iso(at), iso(at)),
        )
        return int(cur.lastrowid)

    def touch_event(self, event_id: int, at: datetime, place: str | None = None) -> None:
        self.conn.execute(
            """UPDATE events SET last_at = MAX(last_at, ?), place = COALESCE(place, ?), status = 'active'
               WHERE id = ?""",
            (iso(at), place, event_id),
        )

    def close_stale_events(self, idle_hours: int, now: datetime | None = None) -> int:
        cutoff = iso((now or utcnow()) - timedelta(hours=idle_hours))
        cur = self.conn.execute("UPDATE events SET status = 'completed' WHERE status = 'active' AND last_at < ?",
                                (cutoff,))
        return cur.rowcount

    def events_overview(self, since: datetime, limit: int = 50) -> list[sqlite3.Row]:
        """События, обновлявшиеся с момента since, с числом сообщений и независимых авторов."""
        return self.conn.execute(
            """
            SELECT e.id, e.title, e.place, e.first_at, e.last_at, e.status,
                   c.slug AS category_slug, c.name AS category, g.name AS group_name,
                   COUNT(l.msg_id) AS messages,
                   COUNT(DISTINCT m.author_hash) AS authors,
                   MAX(l.is_official) AS official
            FROM events e
            JOIN categories c ON c.id = e.category_id JOIN groups g ON g.id = c.group_id
            LEFT JOIN labels l ON l.event_id = e.id
            LEFT JOIN messages m ON m.source_id = l.source_id AND m.msg_id = l.msg_id
            WHERE e.last_at >= ?
            GROUP BY e.id
            ORDER BY e.status = 'active' DESC, e.last_at DESC
            LIMIT ?
            """,
            (iso(since), limit),
        ).fetchall()
