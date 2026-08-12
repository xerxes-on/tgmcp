"""Local SQLite mirror for cross-chat full-text search.

Messages are pulled on demand via the ``xerxes-tg sync`` CLI command and
stored at ``~/.local/state/xerxes-tg/mirror.db`` with an FTS5 index over
message text. The MCP tool ``SearchLocal`` queries this index.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from typing import Any, Iterable

from xdg_base_dirs import xdg_state_home  # type: ignore[import-error]

logger = logging.getLogger(__name__)

DB_PATH = xdg_state_home() / "xerxes-tg" / "mirror.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    dialog_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    ts INTEGER NOT NULL,
    sender_id INTEGER,
    sender_name TEXT,
    text TEXT,
    reply_to INTEGER,
    PRIMARY KEY (dialog_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_messages_dialog_ts ON messages(dialog_id, ts DESC);
CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
    text, content='messages', content_rowid='rowid'
);
CREATE TABLE IF NOT EXISTS sync_state (
    dialog_id INTEGER PRIMARY KEY,
    last_synced_msg_id INTEGER NOT NULL,
    last_synced_ts INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS messages_ai AFTER INSERT ON messages BEGIN
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_au AFTER UPDATE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
    INSERT INTO messages_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS messages_ad AFTER DELETE ON messages BEGIN
    INSERT INTO messages_fts(messages_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
"""


def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(_SCHEMA)
    return conn


def last_synced(dialog_id: int) -> int:
    with _connect() as conn:
        row = conn.execute(
            "SELECT last_synced_msg_id FROM sync_state WHERE dialog_id = ?",
            (dialog_id,),
        ).fetchone()
        return int(row["last_synced_msg_id"]) if row else 0


def upsert_messages(dialog_id: int, rows: Iterable[dict[str, Any]]) -> int:
    """Insert or update a batch of messages. Returns the count written."""
    count = 0
    max_id = 0
    max_ts = 0
    with _connect() as conn:
        for r in rows:
            conn.execute(
                """
                INSERT INTO messages (dialog_id, message_id, ts, sender_id, sender_name, text, reply_to)
                VALUES (:dialog_id, :message_id, :ts, :sender_id, :sender_name, :text, :reply_to)
                ON CONFLICT(dialog_id, message_id) DO UPDATE SET
                    ts = excluded.ts,
                    sender_id = excluded.sender_id,
                    sender_name = excluded.sender_name,
                    text = excluded.text,
                    reply_to = excluded.reply_to
                """,
                r,
            )
            count += 1
            if r["message_id"] > max_id:
                max_id = r["message_id"]
            if r["ts"] > max_ts:
                max_ts = r["ts"]
        if count:
            conn.execute(
                """
                INSERT INTO sync_state (dialog_id, last_synced_msg_id, last_synced_ts)
                VALUES (?, ?, ?)
                ON CONFLICT(dialog_id) DO UPDATE SET
                    last_synced_msg_id = MAX(sync_state.last_synced_msg_id, excluded.last_synced_msg_id),
                    last_synced_ts = excluded.last_synced_ts
                """,
                (dialog_id, max_id, int(time.time())),
            )
    return count


def search(
    *,
    query: str,
    dialog_id: int | None = None,
    limit: int = 50,
    since_hours: int | None = None,
) -> list[dict[str, Any]]:
    """FTS5 search over mirrored messages, newest first."""
    sql = [
        "SELECT m.dialog_id, m.message_id, m.ts, m.sender_id, m.sender_name, m.text, m.reply_to",
        "FROM messages m JOIN messages_fts f ON m.rowid = f.rowid",
        "WHERE messages_fts MATCH ?",
    ]
    params: list[Any] = [query]
    if dialog_id is not None:
        sql.append("AND m.dialog_id = ?")
        params.append(dialog_id)
    if since_hours is not None:
        cutoff = int(time.time() - since_hours * 3600)
        sql.append("AND m.ts >= ?")
        params.append(cutoff)
    sql.append("ORDER BY m.ts DESC LIMIT ?")
    params.append(limit)

    with _connect() as conn:
        rows = conn.execute(" ".join(sql), params).fetchall()
        return [dict(r) for r in rows]


def stats() -> dict[str, int]:
    with _connect() as conn:
        total = conn.execute("SELECT COUNT(*) as n FROM messages").fetchone()["n"]
        dialogs = conn.execute("SELECT COUNT(*) as n FROM sync_state").fetchone()["n"]
        return {"messages": int(total), "dialogs": int(dialogs)}


def purge() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()


async def sync_dialog(
    client,  # noqa: ANN001 — telethon TelegramClient, avoid import cycle
    dialog_id: int,
    *,
    since_hours: int | None = None,
    max_messages: int = 5000,
) -> int:
    """Pull messages newer than ``last_synced`` (or ``since_hours`` ago) into the mirror.

    Returns the number of messages written.
    """
    min_id = last_synced(dialog_id)
    from datetime import datetime, timedelta, timezone

    cutoff_dt: datetime | None = None
    if since_hours is not None:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(hours=since_hours)

    batch: list[dict[str, Any]] = []
    async for msg in client.iter_messages(entity=dialog_id, min_id=min_id, limit=max_messages):
        if cutoff_dt and msg.date < cutoff_dt:
            break
        text = msg.text or ""
        if msg.media and not text:
            text = f"[media: {type(msg.media).__name__}]"
        if not text:
            continue
        sender_name = ""
        sender_id = None
        if msg.sender:
            sender_id = getattr(msg.sender, "id", None)
            if hasattr(msg.sender, "first_name"):
                sender_name = msg.sender.first_name or ""
            elif hasattr(msg.sender, "title"):
                sender_name = msg.sender.title or ""
        reply_to = msg.reply_to.reply_to_msg_id if msg.reply_to else None
        batch.append(
            {
                "dialog_id": dialog_id,
                "message_id": msg.id,
                "ts": int(msg.date.timestamp()),
                "sender_id": sender_id,
                "sender_name": sender_name,
                "text": text,
                "reply_to": reply_to,
            }
        )

    if not batch:
        return 0
    return upsert_messages(dialog_id, batch)


__all__ = [
    "DB_PATH",
    "last_synced",
    "upsert_messages",
    "search",
    "stats",
    "purge",
    "sync_dialog",
]
