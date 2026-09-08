"""Durable temporary Telegram watches and webhook outbox state."""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import aiosqlite
from xdg_base_dirs import xdg_state_home  # type: ignore[import-error]

WATCH_DB = xdg_state_home() / "xerxes-tg" / "watches.db"

WatchMode = Literal["once", "conversation"]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS watches (
    watch_id              TEXT PRIMARY KEY,
    dialog_id             INTEGER NOT NULL,
    after_message_id      INTEGER,
    reply_to_message_id   INTEGER,
    sender_id             INTEGER,
    mode                  TEXT NOT NULL,
    idle_timeout_seconds  INTEGER NOT NULL,
    max_duration_seconds  INTEGER NOT NULL,
    created_at            INTEGER NOT NULL,
    last_activity_at      INTEGER NOT NULL,
    expires_at            INTEGER NOT NULL,
    hard_expires_at       INTEGER NOT NULL,
    codex_thread_id       TEXT,
    status                TEXT NOT NULL DEFAULT 'active',
    stop_reason           TEXT,
    stopped_at            INTEGER
);
CREATE INDEX IF NOT EXISTS idx_watches_active_dialog
    ON watches(status, dialog_id, expires_at);

CREATE TABLE IF NOT EXISTS watch_events (
    sequence          INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id          TEXT NOT NULL UNIQUE,
    watch_id          TEXT NOT NULL,
    dialog_id         INTEGER NOT NULL,
    message_id        INTEGER NOT NULL,
    reply_to_message_id INTEGER,
    sender_id         INTEGER,
    sender_name       TEXT,
    text              TEXT NOT NULL,
    received_at       INTEGER NOT NULL,
    payload_json      TEXT NOT NULL,
    delivery_status   TEXT NOT NULL DEFAULT 'pending',
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at   INTEGER NOT NULL,
    delivered_at      INTEGER,
    last_error        TEXT,
    FOREIGN KEY (watch_id) REFERENCES watches(watch_id),
    UNIQUE(watch_id, dialog_id, message_id)
);
CREATE INDEX IF NOT EXISTS idx_watch_events_delivery
    ON watch_events(delivery_status, next_attempt_at, sequence);
CREATE INDEX IF NOT EXISTS idx_watch_events_watch
    ON watch_events(watch_id, sequence);

CREATE TABLE IF NOT EXISTS codex_deliveries (
    sequence          INTEGER PRIMARY KEY,
    thread_id         TEXT NOT NULL,
    delivery_status   TEXT NOT NULL DEFAULT 'pending',
    delivery_attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at   INTEGER NOT NULL,
    delivered_at      INTEGER,
    delivery_method   TEXT,
    last_error        TEXT,
    FOREIGN KEY (sequence) REFERENCES watch_events(sequence) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_codex_deliveries_due
    ON codex_deliveries(delivery_status, next_attempt_at, sequence);
"""


def _iso(ts: int | None) -> str | None:
    if ts is None:
        return None
    return datetime.fromtimestamp(ts, UTC).isoformat().replace("+00:00", "Z")


async def _connect(db_path: Path = WATCH_DB) -> aiosqlite.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(db_path.parent, 0o700)
    db = await aiosqlite.connect(db_path)
    os.chmod(db_path, 0o600)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA foreign_keys=ON")
    await db.executescript(_SCHEMA)
    async with db.execute("PRAGMA table_info(watches)") as cursor:
        watch_columns = {row[1] for row in await cursor.fetchall()}
    if "codex_thread_id" not in watch_columns:
        await db.execute("ALTER TABLE watches ADD COLUMN codex_thread_id TEXT")
    await db.commit()
    for suffix in ("-wal", "-shm"):
        sidecar = Path(f"{db_path}{suffix}")
        if sidecar.exists():
            os.chmod(sidecar, 0o600)
    return db


async def init(db_path: Path = WATCH_DB) -> None:
    db = await _connect(db_path)
    await db.close()


def _watch_dict(row: aiosqlite.Row) -> dict[str, Any]:
    return {
        "watch_id": row["watch_id"],
        "dialog_id": row["dialog_id"],
        "after_message_id": row["after_message_id"],
        "reply_to_message_id": row["reply_to_message_id"],
        "sender_id": row["sender_id"],
        "mode": row["mode"],
        "idle_timeout_seconds": row["idle_timeout_seconds"],
        "max_duration_seconds": row["max_duration_seconds"],
        "codex_thread_id": row["codex_thread_id"],
        "status": row["status"],
        "stop_reason": row["stop_reason"],
        "created_at": _iso(row["created_at"]),
        "last_activity_at": _iso(row["last_activity_at"]),
        "expires_at": _iso(row["expires_at"]),
        "hard_expires_at": _iso(row["hard_expires_at"]),
        "stopped_at": _iso(row["stopped_at"]),
    }


async def expire_stale(*, now: int | None = None, db_path: Path = WATCH_DB) -> int:
    current = now if now is not None else int(time.time())
    db = await _connect(db_path)
    try:
        cursor = await db.execute(
            """UPDATE watches
               SET status='expired',
                   stop_reason=CASE
                       WHEN hard_expires_at <= expires_at THEN 'max_duration'
                       ELSE 'idle_timeout'
                   END,
                   stopped_at=?
               WHERE status='active' AND (expires_at <= ? OR hard_expires_at <= ?)""",
            (current, current, current),
        )
        await db.commit()
        return cursor.rowcount
    finally:
        await db.close()


async def start_watch(
    *,
    dialog_id: int,
    after_message_id: int | None = None,
    reply_to_message_id: int | None = None,
    sender_id: int | None = None,
    codex_thread_id: str | None = None,
    mode: WatchMode = "conversation",
    idle_timeout_seconds: int = 3600,
    max_duration_seconds: int = 86400,
    now: int | None = None,
    db_path: Path = WATCH_DB,
) -> dict[str, Any]:
    if mode not in ("once", "conversation"):
        raise ValueError("mode must be 'once' or 'conversation'")
    if idle_timeout_seconds < 30:
        raise ValueError("idle_timeout_seconds must be at least 30")
    if max_duration_seconds < idle_timeout_seconds:
        raise ValueError("max_duration_seconds must be greater than or equal to idle_timeout_seconds")
    if codex_thread_id is not None:
        codex_thread_id = codex_thread_id.strip()
        if not codex_thread_id:
            raise ValueError("codex_thread_id cannot be empty")

    current = now if now is not None else int(time.time())
    hard_expires_at = current + max_duration_seconds
    expires_at = min(current + idle_timeout_seconds, hard_expires_at)
    watch_id = str(uuid.uuid4())
    db = await _connect(db_path)
    try:
        await db.execute(
            """INSERT INTO watches(
                   watch_id, dialog_id, after_message_id, reply_to_message_id,
                   sender_id, mode, idle_timeout_seconds, max_duration_seconds,
                   created_at, last_activity_at, expires_at, hard_expires_at,
                   codex_thread_id
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                watch_id,
                dialog_id,
                after_message_id,
                reply_to_message_id,
                sender_id,
                mode,
                idle_timeout_seconds,
                max_duration_seconds,
                current,
                current,
                expires_at,
                hard_expires_at,
                codex_thread_id,
            ),
        )
        await db.commit()
        async with db.execute("SELECT * FROM watches WHERE watch_id=?", (watch_id,)) as cursor:
            row = await cursor.fetchone()
        if row is None:  # pragma: no cover - defensive SQLite guard
            raise RuntimeError("watch was not persisted")
        return _watch_dict(row)
    finally:
        await db.close()


async def stop_watch(
    watch_id: str,
    *,
    reason: str = "stopped",
    now: int | None = None,
    db_path: Path = WATCH_DB,
) -> dict[str, Any] | None:
    current = now if now is not None else int(time.time())
    db = await _connect(db_path)
    try:
        await db.execute(
            """UPDATE watches
               SET status='stopped', stop_reason=?, stopped_at=?
               WHERE watch_id=? AND status='active'""",
            (reason, current, watch_id),
        )
        await db.commit()
        async with db.execute("SELECT * FROM watches WHERE watch_id=?", (watch_id,)) as cursor:
            row = await cursor.fetchone()
        return _watch_dict(row) if row is not None else None
    finally:
        await db.close()


async def list_watches(
    *,
    include_inactive: bool = False,
    now: int | None = None,
    db_path: Path = WATCH_DB,
) -> list[dict[str, Any]]:
    await expire_stale(now=now, db_path=db_path)
    db = await _connect(db_path)
    try:
        where = "" if include_inactive else "WHERE status='active'"
        async with db.execute(
            f"SELECT * FROM watches {where} ORDER BY created_at DESC"
        ) as cursor:
            rows = await cursor.fetchall()
        return [_watch_dict(row) for row in rows]
    finally:
        await db.close()


async def record_incoming(
    *,
    dialog_id: int,
    message_id: int,
    reply_to_message_id: int | None,
    sender_id: int | None,
    sender_name: str,
    text: str,
    received_at: int | None = None,
    sent_at: int | None = None,
    db_path: Path = WATCH_DB,
) -> list[dict[str, Any]]:
    """Match an incoming Telegram message and atomically enqueue watch events."""
    current = received_at if received_at is not None else int(time.time())
    db = await _connect(db_path)
    events: list[dict[str, Any]] = []
    try:
        await db.execute("BEGIN IMMEDIATE")
        await db.execute(
            """UPDATE watches
               SET status='expired',
                   stop_reason=CASE
                       WHEN hard_expires_at <= expires_at THEN 'max_duration'
                       ELSE 'idle_timeout'
                   END,
                   stopped_at=?
               WHERE status='active' AND (expires_at <= ? OR hard_expires_at <= ?)""",
            (current, current, current),
        )
        async with db.execute(
            """SELECT * FROM watches
               WHERE status='active'
                 AND dialog_id=?
                 AND expires_at > ?
                 AND hard_expires_at > ?
                 AND created_at <= ?
                 AND (after_message_id IS NULL OR ? > after_message_id)
                 AND (reply_to_message_id IS NULL OR reply_to_message_id=?)
                 AND (sender_id IS NULL OR sender_id=?)
               ORDER BY created_at""",
            (
                dialog_id,
                current,
                current,
                sent_at if sent_at is not None else current,
                message_id,
                reply_to_message_id,
                sender_id,
            ),
        ) as cursor:
            watches = await cursor.fetchall()

        for watch in watches:
            event_id = str(uuid.uuid4())
            payload: dict[str, Any] = {
                "event": "telegram.message.received",
                "event_id": event_id,
                "watch_id": watch["watch_id"],
                "watch_mode": watch["mode"],
                "chat_id": dialog_id,
                "message_id": message_id,
                "reply_to_message_id": reply_to_message_id,
                "sender": {"id": sender_id, "name": sender_name},
                "text": text,
                "received_at": _iso(current),
            }
            cursor = await db.execute(
                """INSERT OR IGNORE INTO watch_events(
                       event_id, watch_id, dialog_id, message_id,
                       reply_to_message_id, sender_id, sender_name, text,
                       received_at, payload_json, next_attempt_at
                   ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    event_id,
                    watch["watch_id"],
                    dialog_id,
                    message_id,
                    reply_to_message_id,
                    sender_id,
                    sender_name,
                    text,
                    current,
                    json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                    current,
                ),
            )
            if cursor.rowcount == 0:
                continue

            sequence = cursor.lastrowid
            payload["sequence"] = sequence
            await db.execute(
                "UPDATE watch_events SET payload_json=? WHERE sequence=?",
                (json.dumps(payload, ensure_ascii=False, separators=(",", ":")), sequence),
            )
            if watch["codex_thread_id"]:
                await db.execute(
                    """INSERT INTO codex_deliveries(
                           sequence, thread_id, next_attempt_at
                       ) VALUES(?,?,?)""",
                    (sequence, watch["codex_thread_id"], current),
                )
            events.append(payload)

            if watch["mode"] == "once":
                await db.execute(
                    """UPDATE watches
                       SET status='completed', stop_reason='first_response',
                           last_activity_at=?, stopped_at=?
                       WHERE watch_id=?""",
                    (current, current, watch["watch_id"]),
                )
            else:
                renewed = min(current + watch["idle_timeout_seconds"], watch["hard_expires_at"])
                await db.execute(
                    """UPDATE watches
                       SET last_activity_at=?, expires_at=?
                       WHERE watch_id=? AND status='active'""",
                    (current, renewed, watch["watch_id"]),
                )

        await db.commit()
        return events
    except Exception:
        await db.rollback()
        raise
    finally:
        await db.close()


async def get_events(
    watch_id: str,
    *,
    after_sequence: int = 0,
    limit: int = 100,
    db_path: Path = WATCH_DB,
) -> list[dict[str, Any]]:
    db = await _connect(db_path)
    try:
        async with db.execute(
            """SELECT e.sequence, e.payload_json, e.delivery_status,
                      e.delivery_attempts, e.delivered_at, e.last_error,
                      c.thread_id AS codex_thread_id,
                      c.delivery_status AS codex_delivery_status,
                      c.delivery_attempts AS codex_delivery_attempts,
                      c.delivered_at AS codex_delivered_at,
                      c.delivery_method AS codex_delivery_method,
                      c.last_error AS codex_last_error
               FROM watch_events e
               LEFT JOIN codex_deliveries c ON c.sequence=e.sequence
               WHERE e.watch_id=? AND e.sequence>?
               ORDER BY e.sequence ASC LIMIT ?""",
            (watch_id, after_sequence, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        result = []
        for row in rows:
            payload = json.loads(row["payload_json"])
            payload["delivery"] = {
                "status": row["delivery_status"],
                "attempts": row["delivery_attempts"],
                "delivered_at": _iso(row["delivered_at"]),
                "last_error": row["last_error"],
            }
            if row["codex_thread_id"]:
                payload["codex_delivery"] = {
                    "thread_id": row["codex_thread_id"],
                    "status": row["codex_delivery_status"],
                    "attempts": row["codex_delivery_attempts"],
                    "delivered_at": _iso(row["codex_delivered_at"]),
                    "method": row["codex_delivery_method"],
                    "last_error": row["codex_last_error"],
                }
            result.append(payload)
        return result
    finally:
        await db.close()


async def due_deliveries(
    *,
    now: int | None = None,
    limit: int = 20,
    db_path: Path = WATCH_DB,
) -> list[dict[str, Any]]:
    current = now if now is not None else int(time.time())
    db = await _connect(db_path)
    try:
        async with db.execute(
            """SELECT sequence, event_id, payload_json, delivery_attempts
               FROM watch_events
               WHERE delivery_status IN ('pending', 'retry') AND next_attempt_at<=?
               ORDER BY sequence ASC LIMIT ?""",
            (current, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "sequence": row["sequence"],
                "event_id": row["event_id"],
                "payload": json.loads(row["payload_json"]),
                "attempts": row["delivery_attempts"],
            }
            for row in rows
        ]
    finally:
        await db.close()


async def mark_delivered(
    sequence: int,
    *,
    now: int | None = None,
    db_path: Path = WATCH_DB,
) -> None:
    current = now if now is not None else int(time.time())
    db = await _connect(db_path)
    try:
        await db.execute(
            """UPDATE watch_events
               SET delivery_status='delivered', delivery_attempts=delivery_attempts+1,
                   delivered_at=?, last_error=NULL
               WHERE sequence=?""",
            (current, sequence),
        )
        await db.commit()
    finally:
        await db.close()


async def mark_delivery_failed(
    sequence: int,
    error: str,
    *,
    max_attempts: int,
    now: int | None = None,
    db_path: Path = WATCH_DB,
) -> None:
    current = now if now is not None else int(time.time())
    db = await _connect(db_path)
    try:
        async with db.execute(
            "SELECT delivery_attempts FROM watch_events WHERE sequence=?",
            (sequence,),
        ) as cursor:
            row = await cursor.fetchone()
        if row is None:
            return
        attempts = row["delivery_attempts"] + 1
        status = "failed" if attempts >= max_attempts else "retry"
        backoff = min(300, 2 ** min(attempts, 8))
        await db.execute(
            """UPDATE watch_events
               SET delivery_status=?, delivery_attempts=?, next_attempt_at=?, last_error=?
               WHERE sequence=?""",
            (status, attempts, current + backoff, error[:500], sequence),
        )
        await db.commit()
    finally:
        await db.close()


async def due_codex_deliveries(
    *,
    now: int | None = None,
    limit: int = 20,
    db_path: Path = WATCH_DB,
) -> list[dict[str, Any]]:
    current = now if now is not None else int(time.time())
    db = await _connect(db_path)
    try:
        async with db.execute(
            """WITH due_messages AS (
                   SELECT c.thread_id, e.dialog_id, e.message_id
                   FROM codex_deliveries c
                   JOIN watch_events e ON e.sequence=c.sequence
                   WHERE c.delivery_status IN ('pending', 'retry')
                     AND c.next_attempt_at<=?
                   GROUP BY c.thread_id, e.dialog_id, e.message_id
                   ORDER BY MIN(c.sequence) LIMIT ?
               )
               SELECT c.sequence, c.thread_id, c.delivery_attempts,
                      e.event_id, e.dialog_id, e.message_id, e.payload_json
               FROM codex_deliveries c
               JOIN watch_events e ON e.sequence=c.sequence
               JOIN due_messages d ON d.thread_id=c.thread_id
                   AND d.dialog_id=e.dialog_id AND d.message_id=e.message_id
               WHERE c.delivery_status IN ('pending', 'retry')
               ORDER BY c.sequence ASC""",
            (current, limit),
        ) as cursor:
            rows = await cursor.fetchall()
        return [
            {
                "sequence": row["sequence"],
                "thread_id": row["thread_id"],
                "event_id": row["event_id"],
                "dialog_id": row["dialog_id"],
                "message_id": row["message_id"],
                "payload": json.loads(row["payload_json"]),
                "attempts": row["delivery_attempts"],
            }
            for row in rows
        ]
    finally:
        await db.close()


async def mark_codex_delivered(
    sequences: list[int],
    *,
    method: str,
    now: int | None = None,
    db_path: Path = WATCH_DB,
) -> None:
    if not sequences:
        return
    current = now if now is not None else int(time.time())
    placeholders = ",".join("?" for _ in sequences)
    db = await _connect(db_path)
    try:
        await db.execute(
            f"""UPDATE codex_deliveries
                SET delivery_status='delivered',
                    delivery_attempts=delivery_attempts+1,
                    delivered_at=?, delivery_method=?, last_error=NULL
                WHERE sequence IN ({placeholders})""",
            (current, method, *sequences),
        )
        await db.commit()
    finally:
        await db.close()


async def mark_codex_delivery_failed(
    sequences: list[int],
    error: str,
    *,
    max_attempts: int,
    now: int | None = None,
    db_path: Path = WATCH_DB,
) -> None:
    if not sequences:
        return
    current = now if now is not None else int(time.time())
    db = await _connect(db_path)
    try:
        for sequence in sequences:
            async with db.execute(
                "SELECT delivery_attempts FROM codex_deliveries WHERE sequence=?",
                (sequence,),
            ) as cursor:
                row = await cursor.fetchone()
            if row is None:
                continue
            attempts = row["delivery_attempts"] + 1
            status = "failed" if attempts >= max_attempts else "retry"
            backoff = min(300, 2 ** min(attempts, 8))
            await db.execute(
                """UPDATE codex_deliveries
                   SET delivery_status=?, delivery_attempts=?,
                       next_attempt_at=?, last_error=?
                   WHERE sequence=?""",
                (status, attempts, current + backoff, error[:500], sequence),
            )
        await db.commit()
    finally:
        await db.close()


async def prune_terminal_state(
    *,
    retention_seconds: int = 172800,
    now: int | None = None,
    db_path: Path = WATCH_DB,
) -> tuple[int, int]:
    """Delete delivered/failed events and inactive watches after retention."""
    if retention_seconds < 3600:
        raise ValueError("retention_seconds must be at least 3600")
    current = now if now is not None else int(time.time())
    cutoff = current - retention_seconds
    db = await _connect(db_path)
    try:
        event_cursor = await db.execute(
            """DELETE FROM watch_events
               WHERE received_at<? AND delivery_status IN ('delivered', 'failed')""",
            (cutoff,),
        )
        watch_cursor = await db.execute(
            """DELETE FROM watches
               WHERE status!='active' AND stopped_at<?
                 AND NOT EXISTS(
                     SELECT 1 FROM watch_events WHERE watch_events.watch_id=watches.watch_id
                 )""",
            (cutoff,),
        )
        await db.commit()
        return event_cursor.rowcount, watch_cursor.rowcount
    finally:
        await db.close()


__all__ = [
    "WATCH_DB",
    "due_codex_deliveries",
    "due_deliveries",
    "expire_stale",
    "get_events",
    "init",
    "list_watches",
    "mark_delivered",
    "mark_codex_delivered",
    "mark_codex_delivery_failed",
    "mark_delivery_failed",
    "prune_terminal_state",
    "record_incoming",
    "start_watch",
    "stop_watch",
]
