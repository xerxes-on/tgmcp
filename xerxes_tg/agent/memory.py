from __future__ import annotations

import json
import time
from typing import Any

import aiosqlite

from .config import AGENT_DB

_SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    chat_id     INTEGER PRIMARY KEY,
    summary     TEXT    NOT NULL DEFAULT '',
    updated_at  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS messages (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    tg_msg_id    INTEGER NOT NULL,
    sender_id    INTEGER,
    sender_name  TEXT,
    text         TEXT,
    ts           INTEGER NOT NULL,
    is_outgoing  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_messages_chat_ts ON messages(chat_id, ts DESC);

CREATE TABLE IF NOT EXISTS pending_approvals (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id      INTEGER NOT NULL,
    draft_text   TEXT    NOT NULL,
    context_json TEXT    NOT NULL,
    created_at   INTEGER NOT NULL,
    ttl_at       INTEGER NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_approvals_status ON pending_approvals(status, created_at);

CREATE TABLE IF NOT EXISTS processed_ids (
    chat_id    INTEGER NOT NULL,
    tg_msg_id  INTEGER NOT NULL,
    PRIMARY KEY (chat_id, tg_msg_id)
);
"""


async def init() -> aiosqlite.Connection:
    AGENT_DB.parent.mkdir(parents=True, exist_ok=True)
    db = await aiosqlite.connect(AGENT_DB)
    await db.execute("PRAGMA journal_mode=WAL")
    await db.executescript(_SCHEMA)
    await db.commit()
    return db


async def is_processed(db: aiosqlite.Connection, chat_id: int, tg_msg_id: int) -> bool:
    async with db.execute(
        "SELECT 1 FROM processed_ids WHERE chat_id=? AND tg_msg_id=?", (chat_id, tg_msg_id)
    ) as cur:
        return await cur.fetchone() is not None


async def mark_processed(db: aiosqlite.Connection, chat_id: int, tg_msg_id: int) -> None:
    await db.execute(
        "INSERT OR IGNORE INTO processed_ids(chat_id, tg_msg_id) VALUES(?,?)",
        (chat_id, tg_msg_id),
    )
    await db.commit()


async def save_message(
    db: aiosqlite.Connection,
    *,
    chat_id: int,
    tg_msg_id: int,
    sender_id: int | None,
    sender_name: str | None,
    text: str | None,
    ts: int,
    is_outgoing: bool = False,
) -> None:
    await db.execute(
        """INSERT OR IGNORE INTO messages(chat_id, tg_msg_id, sender_id, sender_name, text, ts, is_outgoing)
           VALUES(?,?,?,?,?,?,?)""",
        (chat_id, tg_msg_id, sender_id, sender_name, text or "", ts, int(is_outgoing)),
    )
    await db.commit()


async def get_history(
    db: aiosqlite.Connection, chat_id: int, limit: int = 50
) -> list[dict[str, Any]]:
    async with db.execute(
        """SELECT sender_name, text, ts, is_outgoing
           FROM messages WHERE chat_id=?
           ORDER BY ts DESC LIMIT ?""",
        (chat_id, limit),
    ) as cur:
        rows = await cur.fetchall()
    return [
        {"sender": r[0], "text": r[1], "ts": r[2], "outgoing": bool(r[3])}
        for r in reversed(rows)
    ]


async def get_summary(db: aiosqlite.Connection, chat_id: int) -> str:
    async with db.execute(
        "SELECT summary FROM conversations WHERE chat_id=?", (chat_id,)
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else ""


async def save_summary(db: aiosqlite.Connection, chat_id: int, summary: str) -> None:
    await db.execute(
        """INSERT INTO conversations(chat_id, summary, updated_at) VALUES(?,?,?)
           ON CONFLICT(chat_id) DO UPDATE SET summary=excluded.summary, updated_at=excluded.updated_at""",
        (chat_id, summary, int(time.time())),
    )
    await db.commit()


async def queue_approval(
    db: aiosqlite.Connection,
    *,
    chat_id: int,
    draft_text: str,
    context: dict[str, Any],
    ttl_minutes: int,
) -> int:
    now = int(time.time())
    cursor = await db.execute(
        """INSERT INTO pending_approvals(chat_id, draft_text, context_json, created_at, ttl_at)
           VALUES(?,?,?,?,?)""",
        (chat_id, draft_text, json.dumps(context, ensure_ascii=False), now, now + ttl_minutes * 60),
    )
    await db.commit()
    return cursor.lastrowid  # type: ignore[return-value]


async def get_pending_approval(db: aiosqlite.Connection) -> dict[str, Any] | None:
    now = int(time.time())
    async with db.execute(
        """SELECT id, chat_id, draft_text, context_json, created_at
           FROM pending_approvals
           WHERE status='pending' AND ttl_at > ?
           ORDER BY created_at ASC LIMIT 1""",
        (now,),
    ) as cur:
        row = await cur.fetchone()
    if row is None:
        return None
    return {
        "id": row[0],
        "chat_id": row[1],
        "draft_text": row[2],
        "context": json.loads(row[3]),
        "created_at": row[4],
    }


async def update_approval_status(
    db: aiosqlite.Connection, approval_id: int, status: str
) -> None:
    await db.execute(
        "UPDATE pending_approvals SET status=? WHERE id=?", (status, approval_id)
    )
    await db.commit()


async def expire_old_approvals(db: aiosqlite.Connection) -> None:
    now = int(time.time())
    await db.execute(
        "UPDATE pending_approvals SET status='expired' WHERE status='pending' AND ttl_at <= ?",
        (now,),
    )
    await db.commit()


async def pending_count(db: aiosqlite.Connection) -> int:
    now = int(time.time())
    async with db.execute(
        "SELECT COUNT(*) FROM pending_approvals WHERE status='pending' AND ttl_at > ?", (now,)
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0


async def messages_today(db: aiosqlite.Connection) -> int:
    midnight = int(time.time()) - (int(time.time()) % 86400)
    async with db.execute(
        "SELECT COUNT(*) FROM messages WHERE ts >= ?", (midnight,)
    ) as cur:
        row = await cur.fetchone()
    return row[0] if row else 0
