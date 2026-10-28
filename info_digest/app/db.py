"""Хранилище сообщений на SQLite.

Объём небольшой (тысячи сообщений в сутки), поэтому используется синхронный
sqlite3 в режиме WAL: отдельные операции занимают миллисекунды.
"""

from __future__ import annotations

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
            "SELECT 1 FROM messages WHERE source_id = ? AND msg_id = ?", (m.source_id, m.msg_id)
        ).fetchone()
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

    def cleanup(self, retention_days: int, now: datetime | None = None) -> int:
        """Удаляет сообщения старше срока хранения; возвращает число удалённых."""
        cutoff = iso((now or utcnow()) - timedelta(days=retention_days))
        cur = self.conn.execute("DELETE FROM messages WHERE date < ?", (cutoff,))
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
