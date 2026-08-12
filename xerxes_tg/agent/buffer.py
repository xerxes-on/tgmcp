from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any


class DebounceBuffer:
    """Collect per-chat events and flush after a silence window."""

    def __init__(
        self,
        debounce_s: float,
        on_flush: Callable[[int, list[Any]], Awaitable[None]],
    ) -> None:
        self._delay = debounce_s
        self._on_flush = on_flush
        self._buffers: dict[int, list[Any]] = {}
        self._tasks: dict[int, asyncio.Task[None]] = {}

    async def append(self, chat_id: int, event: Any) -> None:
        self._buffers.setdefault(chat_id, []).append(event)
        existing = self._tasks.get(chat_id)
        if existing and not existing.done():
            existing.cancel()
        self._tasks[chat_id] = asyncio.create_task(self._flush_later(chat_id))

    async def _flush_later(self, chat_id: int) -> None:
        try:
            await asyncio.sleep(self._delay)
        except asyncio.CancelledError:
            return
        events = self._buffers.pop(chat_id, [])
        self._tasks.pop(chat_id, None)
        if events:
            await self._on_flush(chat_id, events)

    async def flush_all(self) -> None:
        """Flush all pending buffers immediately (called on shutdown)."""
        for chat_id in list(self._tasks):
            t = self._tasks.pop(chat_id)
            if not t.done():
                t.cancel()
            events = self._buffers.pop(chat_id, [])
            if events:
                await self._on_flush(chat_id, events)
