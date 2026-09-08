from __future__ import annotations

import tempfile
import time
import unittest
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from telethon import errors

from xerxes_tg import watch_store, watcher


def _message(message_id: int, sent_at: int, *, outgoing: bool = False, sender_id: int = 7):
    return SimpleNamespace(
        id=message_id, date=datetime.fromtimestamp(sent_at, UTC), out=outgoing,
        sender_id=sender_id, reply_to_msg_id=10, text="message", message="message",
        get_sender=AsyncMock(return_value=SimpleNamespace(first_name="Sender")),
    )


class _HistoryClient:
    def __init__(self, messages):
        self.messages = messages
        self.calls = []

    async def iter_messages(self, dialog_id, **kwargs):
        self.calls.append((dialog_id, kwargs))
        messages = [m for m in self.messages if m.id > kwargs["min_id"]]
        for message in messages[:kwargs["limit"]]:
            yield message


class WatcherRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovers_missed_messages_with_filters_and_live_deduplication(self):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "watches.db"
            start = int(time.time()) - 10
            old_watch = await watch_store.start_watch(
                dialog_id=42, after_message_id=10, reply_to_message_id=10,
                sender_id=7, now=start, db_path=db_path,
            )
            new_watch = await watch_store.start_watch(
                dialog_id=42, now=start + 5, db_path=db_path,
            )
            messages = [
                _message(9, start - 1),
                _message(11, start + 1, outgoing=True),
                _message(12, start + 2, sender_id=8),
                _message(13, start + 3),
                _message(14, start + 6),
            ]
            await watcher._record_message(42, messages[3], db_path=db_path)
            client = _HistoryClient(messages)
            cursors = {}
            with patch.object(watcher, "check_access") as access:
                await watcher._reconcile_once(client, cursors, db_path=db_path)
                await watcher._reconcile_once(client, cursors, db_path=db_path)
            access.assert_called_with(42, "read")
            self.assertEqual(cursors, {42: 14})
            old_events = await watch_store.get_events(old_watch["watch_id"], db_path=db_path)
            new_events = await watch_store.get_events(new_watch["watch_id"], db_path=db_path)
            self.assertEqual([e["message_id"] for e in old_events], [13, 14])
            self.assertEqual([e["message_id"] for e in new_events], [14])
            await watch_store.stop_watch(old_watch["watch_id"], db_path=db_path)
            await watch_store.stop_watch(new_watch["watch_id"], db_path=db_path)
            await watcher._reconcile_once(client, cursors, db_path=db_path)
            self.assertEqual(cursors, {})
            self.assertEqual(len(client.calls), 2)

    async def test_paginates_backlog_without_skipping_messages(self):
        start = int(time.time()) - 1
        client = _HistoryClient([_message(i, start) for i in range(1, 206)])
        active = [{"dialog_id": 42, "created_at": datetime.fromtimestamp(start, UTC).isoformat()}]
        cursors = {}
        with (
            patch.object(watch_store, "list_watches", new=AsyncMock(return_value=active)),
            patch.object(watcher, "check_access"),
            patch.object(watcher, "_record_message", new=AsyncMock()) as record,
        ):
            for _ in range(3):
                await watcher._reconcile_once(client, cursors)
        self.assertEqual([c.args[1].id for c in record.await_args_list], list(range(1, 206)))
        self.assertEqual([kwargs["min_id"] for _, kwargs in client.calls], [0, 100, 200])

    async def test_rate_limit_preserves_cursor_for_retry(self):
        start = int(time.time()) - 1
        client = _HistoryClient([_message(1, start)])
        active = [{"dialog_id": 42, "created_at": datetime.fromtimestamp(start, UTC).isoformat()}]
        cursors = {}
        with (
            patch.object(watch_store, "list_watches", new=AsyncMock(return_value=active)),
            patch.object(watcher, "check_access"),
            patch.object(watcher, "_record_message", new=AsyncMock(
                side_effect=errors.FloodWaitError(request=None, capture=30),
            )),
        ):
            with self.assertRaises(errors.FloodWaitError):
                await watcher._reconcile_once(client, cursors)
        self.assertEqual(cursors, {})
