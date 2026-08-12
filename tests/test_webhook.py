from __future__ import annotations

import json
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import ClassVar

from xerxes_tg import watch_store
from xerxes_tg.webhook import (
    WebhookConfig,
    deliver_due_once,
    signed_headers,
    verify_signature,
)


class _WebhookHandler(BaseHTTPRequestHandler):
    received: ClassVar[list[tuple[bytes, dict[str, str]]]] = []
    response_status: ClassVar[int] = 204

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        self.received.append((body, dict(self.headers)))
        self.send_response(self.response_status)
        self.end_headers()

    def log_message(self, format: str, *args) -> None:
        return


class WebhookTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "watches.db"
        _WebhookHandler.received = []
        _WebhookHandler.response_status = 204
        self.server = HTTPServer(("127.0.0.1", 0), _WebhookHandler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp_dir.cleanup()

    async def test_signature_verification_and_replay_window(self) -> None:
        body = b'{"hello":"world"}'
        headers = signed_headers(secret="secret", body=body, event_id="event", timestamp=1000)
        signature = headers["X-Xerxes-TG-Signature"]
        self.assertTrue(
            verify_signature(
                secret="secret",
                body=body,
                timestamp="1000",
                signature=signature,
                now=1001,
            )
        )
        self.assertFalse(
            verify_signature(
                secret="secret",
                body=body,
                timestamp="1000",
                signature=signature,
                now=1400,
            )
        )
        self.assertFalse(
            verify_signature(
                secret="wrong",
                body=body,
                timestamp="1000",
                signature=signature,
                now=1001,
            )
        )

    async def test_due_event_posts_signed_json_and_is_marked_delivered(self) -> None:
        current = int(time.time())
        watch = await watch_store.start_watch(
            dialog_id=42,
            idle_timeout_seconds=60,
            max_duration_seconds=120,
            now=current,
            db_path=self.db_path,
        )
        await watch_store.record_incoming(
            dialog_id=42,
            message_id=1,
            reply_to_message_id=None,
            sender_id=7,
            sender_name="User",
            text="hello",
            received_at=current,
            db_path=self.db_path,
        )
        port = self.server.server_address[1]
        config = WebhookConfig(
            url=f"http://127.0.0.1:{port}/telegram",
            secret="test-secret",
        )

        delivered, failed = await deliver_due_once(config, db_path=self.db_path)
        self.assertEqual((delivered, failed), (1, 0))
        self.assertEqual(len(_WebhookHandler.received), 1)

        body, headers = _WebhookHandler.received[0]
        payload = json.loads(body)
        self.assertEqual(payload["watch_id"], watch["watch_id"])
        self.assertTrue(
            verify_signature(
                secret="test-secret",
                body=body,
                timestamp=headers["X-Xerxes-Tg-Timestamp"],
                signature=headers["X-Xerxes-Tg-Signature"],
            )
        )
        events = await watch_store.get_events(watch["watch_id"], db_path=self.db_path)
        self.assertEqual(events[0]["delivery"]["status"], "delivered")
        self.assertEqual(events[0]["delivery"]["attempts"], 1)

    async def test_failed_delivery_is_retried_and_remains_pollable(self) -> None:
        current = int(time.time())
        watch = await watch_store.start_watch(
            dialog_id=42,
            idle_timeout_seconds=60,
            max_duration_seconds=120,
            now=current,
            db_path=self.db_path,
        )
        await watch_store.record_incoming(
            dialog_id=42,
            message_id=2,
            reply_to_message_id=None,
            sender_id=7,
            sender_name="User",
            text="retry me",
            received_at=current,
            db_path=self.db_path,
        )
        _WebhookHandler.response_status = 500
        port = self.server.server_address[1]
        config = WebhookConfig(
            url=f"http://127.0.0.1:{port}/telegram",
            secret="test-secret",
            max_attempts=2,
        )

        delivered, failed = await deliver_due_once(config, db_path=self.db_path)
        self.assertEqual((delivered, failed), (0, 1))
        events = await watch_store.get_events(watch["watch_id"], db_path=self.db_path)
        self.assertEqual(events[0]["delivery"]["status"], "retry")
        self.assertEqual(events[0]["delivery"]["attempts"], 1)
        self.assertIn("HTTP 500", events[0]["delivery"]["last_error"])

        due = await watch_store.due_deliveries(now=current + 10, db_path=self.db_path)
        self.assertEqual(len(due), 1)
        await watch_store.mark_delivery_failed(
            due[0]["sequence"],
            "still failing",
            max_attempts=2,
            now=current + 10,
            db_path=self.db_path,
        )
        events = await watch_store.get_events(watch["watch_id"], db_path=self.db_path)
        self.assertEqual(events[0]["delivery"]["status"], "failed")
        self.assertEqual(events[0]["delivery"]["attempts"], 2)


if __name__ == "__main__":
    unittest.main()
