from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from xerxes_tg import watch_store


class WatchStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "watches.db"

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    async def test_conversation_watch_matches_deduplicates_and_renews(self) -> None:
        watch = await watch_store.start_watch(
            dialog_id=42,
            after_message_id=10,
            idle_timeout_seconds=60,
            max_duration_seconds=120,
            now=100,
            db_path=self.db_path,
        )

        events = await watch_store.record_incoming(
            dialog_id=42,
            message_id=11,
            reply_to_message_id=None,
            sender_id=7,
            sender_name="User",
            text="hello",
            received_at=150,
            db_path=self.db_path,
        )
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["watch_id"], watch["watch_id"])
        self.assertEqual(events[0]["sequence"], 1)

        duplicate = await watch_store.record_incoming(
            dialog_id=42,
            message_id=11,
            reply_to_message_id=None,
            sender_id=7,
            sender_name="User",
            text="hello",
            received_at=151,
            db_path=self.db_path,
        )
        self.assertEqual(duplicate, [])

        active = await watch_store.list_watches(now=209, db_path=self.db_path)
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["watch_id"], watch["watch_id"])

        expired = await watch_store.list_watches(
            include_inactive=True,
            now=211,
            db_path=self.db_path,
        )
        self.assertEqual(expired[0]["status"], "expired")
        self.assertEqual(expired[0]["stop_reason"], "idle_timeout")

    async def test_equal_idle_and_hard_deadline_reports_max_duration(self) -> None:
        await watch_store.start_watch(
            dialog_id=42,
            idle_timeout_seconds=60,
            max_duration_seconds=60,
            now=100,
            db_path=self.db_path,
        )
        watches = await watch_store.list_watches(
            include_inactive=True,
            now=160,
            db_path=self.db_path,
        )
        self.assertEqual(watches[0]["status"], "expired")
        self.assertEqual(watches[0]["stop_reason"], "max_duration")

    async def test_once_watch_applies_reply_and_sender_filters(self) -> None:
        watch = await watch_store.start_watch(
            dialog_id=-1001,
            after_message_id=20,
            reply_to_message_id=20,
            sender_id=99,
            mode="once",
            idle_timeout_seconds=60,
            max_duration_seconds=120,
            now=100,
            db_path=self.db_path,
        )
        mismatch = await watch_store.record_incoming(
            dialog_id=-1001,
            message_id=21,
            reply_to_message_id=20,
            sender_id=98,
            sender_name="Other",
            text="not a match",
            received_at=110,
            db_path=self.db_path,
        )
        self.assertEqual(mismatch, [])

        matched = await watch_store.record_incoming(
            dialog_id=-1001,
            message_id=22,
            reply_to_message_id=20,
            sender_id=99,
            sender_name="Expected",
            text="a match",
            received_at=111,
            db_path=self.db_path,
        )
        self.assertEqual(len(matched), 1)

        watches = await watch_store.list_watches(
            include_inactive=True,
            now=112,
            db_path=self.db_path,
        )
        self.assertEqual(watches[0]["watch_id"], watch["watch_id"])
        self.assertEqual(watches[0]["status"], "completed")
        self.assertEqual(watches[0]["stop_reason"], "first_response")

    async def test_stop_and_event_polling_cursor(self) -> None:
        watch = await watch_store.start_watch(
            dialog_id=1,
            idle_timeout_seconds=60,
            max_duration_seconds=120,
            now=100,
            db_path=self.db_path,
        )
        for message_id in (1, 2):
            await watch_store.record_incoming(
                dialog_id=1,
                message_id=message_id,
                reply_to_message_id=None,
                sender_id=2,
                sender_name="User",
                text=str(message_id),
                received_at=100 + message_id,
                db_path=self.db_path,
            )

        events = await watch_store.get_events(
            watch["watch_id"], after_sequence=1, db_path=self.db_path
        )
        self.assertEqual([event["sequence"] for event in events], [2])
        self.assertEqual(events[0]["delivery"]["status"], "pending")

        stopped = await watch_store.stop_watch(watch["watch_id"], now=110, db_path=self.db_path)
        self.assertIsNotNone(stopped)
        self.assertEqual(stopped["status"], "stopped")

    async def test_codex_bound_watch_enqueues_and_tracks_delivery(self) -> None:
        watch = await watch_store.start_watch(
            dialog_id=42,
            codex_thread_id="thread-123",
            idle_timeout_seconds=60,
            max_duration_seconds=120,
            now=100,
            db_path=self.db_path,
        )
        self.assertEqual(watch["codex_thread_id"], "thread-123")

        events = await watch_store.record_incoming(
            dialog_id=42,
            message_id=11,
            reply_to_message_id=None,
            sender_id=7,
            sender_name="User",
            text="hello Codex",
            received_at=101,
            db_path=self.db_path,
        )
        self.assertEqual(len(events), 1)

        due = await watch_store.due_codex_deliveries(now=101, db_path=self.db_path)
        self.assertEqual(len(due), 1)
        self.assertEqual(due[0]["thread_id"], "thread-123")
        self.assertEqual(due[0]["payload"]["text"], "hello Codex")

        polled = await watch_store.get_events(watch["watch_id"], db_path=self.db_path)
        self.assertEqual(polled[0]["codex_delivery"]["status"], "pending")

        await watch_store.mark_codex_delivered(
            [events[0]["sequence"]],
            method="steered",
            now=102,
            db_path=self.db_path,
        )
        self.assertEqual(
            await watch_store.due_codex_deliveries(now=102, db_path=self.db_path), []
        )
        delivered = await watch_store.get_events(
            watch["watch_id"], db_path=self.db_path
        )
        self.assertEqual(delivered[0]["codex_delivery"]["status"], "delivered")
        self.assertEqual(delivered[0]["codex_delivery"]["method"], "steered")

    async def test_prune_removes_only_old_terminal_state(self) -> None:
        delivered_watch = await watch_store.start_watch(
            dialog_id=1,
            idle_timeout_seconds=60,
            max_duration_seconds=120,
            now=100,
            db_path=self.db_path,
        )
        pending_watch = await watch_store.start_watch(
            dialog_id=2,
            idle_timeout_seconds=60,
            max_duration_seconds=120,
            now=100,
            db_path=self.db_path,
        )
        for watch, dialog_id in ((delivered_watch, 1), (pending_watch, 2)):
            events = await watch_store.record_incoming(
                dialog_id=dialog_id,
                message_id=1,
                reply_to_message_id=None,
                sender_id=3,
                sender_name="User",
                text="message",
                received_at=101,
                db_path=self.db_path,
            )
            self.assertEqual(len(events), 1)
            await watch_store.stop_watch(watch["watch_id"], now=102, db_path=self.db_path)

        await watch_store.mark_delivered(1, now=102, db_path=self.db_path)
        deleted_events, deleted_watches = await watch_store.prune_terminal_state(
            retention_seconds=3600,
            now=4000,
            db_path=self.db_path,
        )
        self.assertEqual((deleted_events, deleted_watches), (1, 1))

        remaining = await watch_store.list_watches(
            include_inactive=True,
            now=4000,
            db_path=self.db_path,
        )
        self.assertEqual([watch["watch_id"] for watch in remaining], [pending_watch["watch_id"]])
        pending_events = await watch_store.get_events(
            pending_watch["watch_id"], db_path=self.db_path
        )
        self.assertEqual(pending_events[0]["delivery"]["status"], "pending")


if __name__ == "__main__":
    unittest.main()
