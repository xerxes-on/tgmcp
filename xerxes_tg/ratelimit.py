"""In-process token-bucket rate limiter for Telegram write tools.

Defends against accidental bursts before Telegram's own flood protection kicks
in. Configurable via env:
    TELEGRAM_RATE_LIMIT_WRITE_PER_MIN   (default 10)
    TELEGRAM_RATE_LIMIT_REACT_PER_MIN   (default 30)
    TELEGRAM_RATE_LIMIT_FORWARD_PER_MIN (default 10)
"""

from __future__ import annotations

import functools
import os
import time
from collections import defaultdict
from typing import Callable

_DEFAULTS = {
    "write": 10,
    "react": 30,
    "forward": 10,
}


def _limit_for(action: str) -> int:
    env = os.environ.get(f"TELEGRAM_RATE_LIMIT_{action.upper()}_PER_MIN")
    if env and env.strip().isdigit():
        return max(1, int(env))
    return _DEFAULTS.get(action, 10)


# Key: (action, dialog_id) → (tokens, last_refill_ts)
_BUCKETS: dict[tuple[str, int | None], tuple[float, float]] = defaultdict(
    lambda: (0.0, 0.0)
)


class RateLimitExceeded(RuntimeError):
    """Raised when the local rate limit would be exceeded."""


def check_and_consume(action: str, dialog_id: int | None = None) -> None:
    """Consume one token from the bucket for ``(action, dialog_id)``.

    Raises ``RateLimitExceeded`` if none are available. Buckets refill
    linearly at ``limit / 60 tokens/sec``.
    """
    limit = _limit_for(action)
    capacity = float(limit)
    refill_per_sec = capacity / 60.0

    key = (action, dialog_id)
    tokens, last = _BUCKETS[key]
    now = time.monotonic()
    if last == 0.0:
        tokens = capacity
    else:
        tokens = min(capacity, tokens + (now - last) * refill_per_sec)

    if tokens < 1.0:
        reset_in = max(1, int((1.0 - tokens) / refill_per_sec))
        _BUCKETS[key] = (tokens, now)
        raise RateLimitExceeded(
            f"Local rate limit: {limit} {action}/min for chat {dialog_id}. "
            f"Try again in {reset_in}s."
        )
    _BUCKETS[key] = (tokens - 1.0, now)


def rate_limit_tool(action: str) -> Callable:
    """Decorator factory. Applies ``check_and_consume(action, args.dialog_id)``
    before the wrapped tool runs. Expects the tool args object to carry a
    ``dialog_id`` attribute; omits the check if not present."""

    def decorator(func: Callable) -> Callable:
        @functools.wraps(func)
        async def wrapper(args):  # noqa: ANN001
            did = getattr(args, "dialog_id", None)
            if isinstance(did, str):
                # The tool will later call resolve_dialog_id; here we only need
                # a stable key, so hash the alias consistently.
                check_and_consume(action, hash(did.lower()))
            elif isinstance(did, int):
                check_and_consume(action, did)
            else:
                check_and_consume(action, None)
            return await func(args)

        return wrapper

    return decorator


_WRITE_TOOLS = {
    "SendMessage",
    "EditMessage",
    "SendFile",
    "SendVoice",
    "SendLocation",
    "SendContact",
    "SendPoll",
    "SendSticker",
    "PinMessage",
    "UnpinMessage",
    "DeleteMessages",
}
_REACT_TOOLS = {"SendReaction"}
_FORWARD_TOOLS = {"ForwardMessages"}


def classify_tool(tool_name: str) -> str | None:
    """Return the action bucket for a tool, or None for unlimited read tools."""
    if tool_name in _WRITE_TOOLS:
        return "write"
    if tool_name in _REACT_TOOLS:
        return "react"
    if tool_name in _FORWARD_TOOLS:
        return "forward"
    return None


__all__ = [
    "rate_limit_tool",
    "check_and_consume",
    "classify_tool",
    "RateLimitExceeded",
]
