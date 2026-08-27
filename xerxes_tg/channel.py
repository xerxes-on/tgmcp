"""Push temporary watch events into a live Claude Code MCP channel."""

from __future__ import annotations

import asyncio
import logging
import sqlite3
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from . import watch_store

logger = logging.getLogger(__name__)

ChannelSender = Callable[[dict[str, Any]], Awaitable[None]]


class ChannelBridge:
    """Route durable watch events to the MCP client that created each watch.

    Watch-to-session binding intentionally lives in this MCP process. Every
    Claude Code session owns a separate stdio server process, so registering a
    watch here prevents a reply from being broadcast into unrelated sessions.
    The SQLite event remains available through webhooks and ``GetWatchEvents``
    if this process exits before writing the channel notification.
    """

    def __init__(
        self,
        sender: ChannelSender,
        *,
        db_path: Path = watch_store.WATCH_DB,
        poll_seconds: float = 0.5,
        dedupe_size: int = 1000,
    ) -> None:
        self._sender = sender
        self._db_path = db_path
        self._poll_seconds = poll_seconds
        self._subscriptions: dict[str, int] = {}
        self._seen_order: deque[tuple[int, int]] = deque()
        self._seen_messages: set[tuple[int, int]] = set()
        self._dedupe_size = dedupe_size
        self._wake = asyncio.Event()
        self._stopped = asyncio.Event()

    @property
    def watch_ids(self) -> tuple[str, ...]:
        return tuple(self._subscriptions)

    def register(self, watch_id: str, *, after_sequence: int = 0) -> None:
        """Attach a durable watch to this MCP session."""
        self._subscriptions[watch_id] = max(0, after_sequence)
        self._wake.set()

    def unregister(self, watch_id: str) -> None:
        self._subscriptions.pop(watch_id, None)
        self._wake.set()

    def stop(self) -> None:
        self._stopped.set()
        self._wake.set()

    async def deliver_due_once(self) -> int:
        """Write all currently available events in global sequence order."""
        queued: list[tuple[int, str, dict[str, Any]]] = []
        for watch_id, after_sequence in tuple(self._subscriptions.items()):
            events = await watch_store.get_events(
                watch_id,
                after_sequence=after_sequence,
                db_path=self._db_path,
            )
            queued.extend((int(event["sequence"]), watch_id, event) for event in events)

        delivered = 0
        completed_once_watches: set[str] = set()
        for sequence, watch_id, event in sorted(queued, key=lambda item: item[0]):
            # A Telegram message can match overlapping watches in one session.
            # Advance both subscriptions but inject the message only once.
            message_key = (int(event["chat_id"]), int(event["message_id"]))
            if message_key not in self._seen_messages:
                await self._sender(_notification_params(event))
                self._remember(message_key)
                delivered += 1

            if watch_id in self._subscriptions:
                self._subscriptions[watch_id] = max(
                    self._subscriptions[watch_id], sequence
                )
            if event.get("watch_mode") == "once":
                completed_once_watches.add(watch_id)

        for watch_id in completed_once_watches:
            self.unregister(watch_id)
        return delivered

    async def run(self) -> None:
        """Poll the outbox until the MCP transport closes."""
        while not self._stopped.is_set():
            if self._subscriptions:
                try:
                    await self.deliver_due_once()
                except (OSError, sqlite3.Error, ValueError):
                    logger.exception("Claude channel delivery failed; will retry")

            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self._poll_seconds)
            except TimeoutError:
                pass

    def _remember(self, key: tuple[int, int]) -> None:
        self._seen_messages.add(key)
        self._seen_order.append(key)
        while len(self._seen_order) > self._dedupe_size:
            expired = self._seen_order.popleft()
            self._seen_messages.discard(expired)


_active_bridge: ChannelBridge | None = None


def install(bridge: ChannelBridge) -> None:
    global _active_bridge  # noqa: PLW0603
    _active_bridge = bridge


def uninstall(bridge: ChannelBridge) -> None:
    global _active_bridge  # noqa: PLW0603
    if _active_bridge is bridge:
        _active_bridge = None


def register_watch(watch_id: str) -> bool:
    """Bind a newly-created watch to the current MCP process, if present."""
    if _active_bridge is None:
        return False
    _active_bridge.register(watch_id)
    return True


def unregister_watch(watch_id: str) -> None:
    if _active_bridge is not None:
        _active_bridge.unregister(watch_id)


def registered_watch_ids() -> tuple[str, ...]:
    if _active_bridge is None:
        return ()
    return _active_bridge.watch_ids


def _notification_params(event: dict[str, Any]) -> dict[str, Any]:
    sender = event.get("sender") or {}
    sender_name = str(sender.get("name") or sender.get("id") or "Unknown")
    text = str(event.get("text") or "")
    content = (
        "Telegram event:\n"
        f"{sender_name} replied: {text!r}\n"
        "Decide whether any action is needed. Use the xerxes-tg tools if a "
        "Telegram reply or more context is needed."
    )
    return {
        "content": content,
        "meta": {
            "event_id": str(event["event_id"]),
            "watch_id": str(event["watch_id"]),
            "chat_id": str(event["chat_id"]),
            "message_id": str(event["message_id"]),
            "sender_id": str(sender.get("id") or ""),
            "sender_name": sender_name,
            "sequence": str(event["sequence"]),
        },
    }


__all__ = [
    "ChannelBridge",
    "install",
    "register_watch",
    "registered_watch_ids",
    "uninstall",
    "unregister_watch",
]
