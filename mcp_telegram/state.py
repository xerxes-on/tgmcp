"""State management for the Telegram listener.

Handles: agent sessions, pending approvals (persistent),
sent message tracking, rate limiting, loop guards, ack detection.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from xdg_base_dirs import xdg_state_home  # type: ignore[import-error]

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# State directory
# ---------------------------------------------------------------------------

_STATE_DIR: Path | None = None


def state_dir() -> Path:
    global _STATE_DIR  # noqa: PLW0603
    if _STATE_DIR is None:
        _STATE_DIR = xdg_state_home() / "mcp-telegram"
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
    return _STATE_DIR


def _atomic_write_json(path: Path, data: object) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2, default=str) + "\n")
    tmp.rename(path)


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())  # type: ignore[no-any-return]
    except (FileNotFoundError, json.JSONDecodeError, ValueError):
        return {}


# ---------------------------------------------------------------------------
# SessionStore — per-chat agent session management
# ---------------------------------------------------------------------------


class SessionStore:
    """Manages agent session UUIDs per Telegram chat.

    Sessions file: ``~/.local/state/mcp-telegram/sessions.json``
    The agent CLI stores session data at: ``~/.claude/projects/<project-dir>/<id>.jsonl``
    """

    def __init__(self, cwd: str, max_size_bytes: int = 500 * 1024) -> None:
        self._path = state_dir() / "sessions.json"
        self._max_size = max_size_bytes
        self._cwd = cwd
        self._project_dir = self._cwd_to_project_dir(cwd)
        self._data: dict[str, dict] = _load_json(self._path)

    @staticmethod
    def _cwd_to_project_dir(cwd: str) -> str:
        """Convert CWD to the agent project directory name.

        ``/Users/xerxes/Projects/Ufarm`` → ``-Users-xerxes-Projects-Ufarm``
        """
        return cwd.replace("/", "-")

    def _session_file(self, session_id: str) -> Path:
        home = Path.home()
        return home / ".claude" / "projects" / self._project_dir / f"{session_id}.jsonl"

    def _is_oversized(self, session_id: str) -> bool:
        try:
            return self._session_file(session_id).stat().st_size > self._max_size
        except (FileNotFoundError, OSError):
            return False

    def _save(self) -> None:
        _atomic_write_json(self._path, self._data)

    def get_or_create(self, chat_id: int) -> tuple[str, bool]:
        """Return ``(session_id, is_new)``. Auto-resets oversized sessions."""
        key = str(chat_id)
        entry = self._data.get(key)

        if entry:
            sid = entry["session_id"]
            if self._is_oversized(sid):
                logger.info("Session %s oversized — resetting for chat %s", sid, chat_id)
                del self._data[key]
                self._save()
            else:
                entry["last_used"] = time.time()
                self._save()
                return sid, False

        sid = str(uuid.uuid4())
        self._data[key] = {"session_id": sid, "last_used": time.time()}
        self._save()
        return sid, True

    def clear(self, chat_id: int) -> None:
        key = str(chat_id)
        if key in self._data:
            del self._data[key]
            self._save()


# ---------------------------------------------------------------------------
# PendingApproval — persistent approval queue
# ---------------------------------------------------------------------------


@dataclass
class PendingApproval:
    token: str
    chat_id: int
    message_id: int
    draft_reply: str
    original_text: str
    chat_name: str
    sender_name: str
    reason: str
    created_at: datetime

    def to_dict(self) -> dict:
        return {
            "token": self.token,
            "chat_id": self.chat_id,
            "message_id": self.message_id,
            "draft_reply": self.draft_reply,
            "original_text": self.original_text,
            "chat_name": self.chat_name,
            "sender_name": self.sender_name,
            "reason": self.reason,
            "created_at": self.created_at.isoformat(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> PendingApproval:
        return cls(
            token=d["token"],
            chat_id=d["chat_id"],
            message_id=d["message_id"],
            draft_reply=d["draft_reply"],
            original_text=d["original_text"],
            chat_name=d["chat_name"],
            sender_name=d["sender_name"],
            reason=d["reason"],
            created_at=datetime.fromisoformat(d["created_at"]),
        )


class PendingApprovalStore:
    """Persistent approval queue backed by JSON file."""

    def __init__(self, ttl_seconds: int = 3600) -> None:
        self._path = state_dir() / "pending.json"
        self._ttl = ttl_seconds
        self._data: dict[str, PendingApproval] = {}
        self._load()
        self.cleanup_expired()

    def _load(self) -> None:
        raw = _load_json(self._path)
        for token, entry in raw.items():
            try:
                self._data[token] = PendingApproval.from_dict(entry)
            except (KeyError, TypeError, ValueError):
                logger.warning("Skipping corrupt pending entry: %s", token)

    def _save(self) -> None:
        raw = {token: pa.to_dict() for token, pa in self._data.items()}
        _atomic_write_json(self._path, raw)

    def add(self, pending: PendingApproval) -> None:
        self._data[pending.token] = pending
        self._save()

    def get(self, token: str) -> PendingApproval | None:
        pa = self._data.get(token)
        if pa and self._is_expired(pa):
            self.remove(token)
            return None
        return pa

    def remove(self, token: str) -> None:
        if token in self._data:
            del self._data[token]
            self._save()

    def cleanup_expired(self) -> int:
        now = datetime.now(UTC)
        expired = [
            token
            for token, pa in self._data.items()
            if (now - pa.created_at).total_seconds() > self._ttl
        ]
        for token in expired:
            del self._data[token]
        if expired:
            self._save()
            logger.info("Cleaned up %d expired approvals", len(expired))
        return len(expired)

    def _is_expired(self, pa: PendingApproval) -> bool:
        return (datetime.now(UTC) - pa.created_at).total_seconds() > self._ttl


# ---------------------------------------------------------------------------
# SentMessageTracker — track messages we sent (in-memory)
# ---------------------------------------------------------------------------


class SentMessageTracker:
    """Tracks message IDs sent by the listener, per chat. In-memory only."""

    MAX_PER_CHAT = 100

    def __init__(self) -> None:
        self._data: dict[int, dict[int, float]] = {}

    def record(self, chat_id: int, message_id: int) -> None:
        bucket = self._data.setdefault(chat_id, {})
        bucket[message_id] = time.time()
        self._prune(chat_id)

    def is_own_message(self, chat_id: int, message_id: int) -> bool:
        return message_id in self._data.get(chat_id, {})

    def _prune(self, chat_id: int) -> None:
        bucket = self._data.get(chat_id)
        if not bucket or len(bucket) <= self.MAX_PER_CHAT:
            return
        sorted_ids = sorted(bucket, key=bucket.get)  # type: ignore[arg-type]
        for mid in sorted_ids[: len(sorted_ids) - self.MAX_PER_CHAT]:
            del bucket[mid]


# ---------------------------------------------------------------------------
# RateLimiter — per-chat sliding window
# ---------------------------------------------------------------------------


class RateLimiter:
    """Per-chat rate limiter with sliding window and cooldown."""

    def __init__(self, max_count: int, window_seconds: int, cooldown_seconds: int) -> None:
        self._max_count = max_count
        self._window = window_seconds
        self._cooldown = cooldown_seconds
        self._timestamps: dict[int, list[float]] = {}

    def is_allowed(self, chat_id: int) -> bool:
        now = time.time()
        self._prune(chat_id, now)
        stamps = self._timestamps.get(chat_id, [])

        # Cooldown check
        if stamps and (now - stamps[-1]) < self._cooldown:
            return False

        # Window count check
        return len(stamps) < self._max_count

    def record(self, chat_id: int) -> None:
        self._timestamps.setdefault(chat_id, []).append(time.time())

    def _prune(self, chat_id: int, now: float) -> None:
        stamps = self._timestamps.get(chat_id)
        if not stamps:
            return
        cutoff = now - self._window
        self._timestamps[chat_id] = [ts for ts in stamps if ts > cutoff]


# ---------------------------------------------------------------------------
# LoopGuard — bot-to-bot loop prevention
# ---------------------------------------------------------------------------


class LoopGuard:
    """Prevents bot-to-bot reply loops by tracking exchange depth per sender."""

    MAX_DEPTH = 10
    RESET_AFTER_SECONDS = 120  # 2 minutes

    def __init__(self) -> None:
        # {sender_id: (depth, last_seen_timestamp)}
        self._data: dict[int, tuple[int, float]] = {}

    def check(self, sender_id: int) -> tuple[bool, str]:
        """Return ``(allowed, reason)``."""
        now = time.time()
        depth, last_seen = self._data.get(sender_id, (0, 0.0))

        if now - last_seen > self.RESET_AFTER_SECONDS:
            self._data[sender_id] = (0, now)
            return True, ""

        if depth >= self.MAX_DEPTH:
            return False, f"depth limit ({depth}/{self.MAX_DEPTH} exchanges)"

        return True, ""

    def record(self, sender_id: int) -> None:
        now = time.time()
        depth, _ = self._data.get(sender_id, (0, 0.0))
        self._data[sender_id] = (depth + 1, now)


# ---------------------------------------------------------------------------
# Acknowledgment detection
# ---------------------------------------------------------------------------

_ACK_PATTERNS: list[re.Pattern[str]] = [
    re.compile(
        r"^(thanks?|thx|ty|thank you|cheers?|got it|noted|ok(ay)?|👍|✅|roger|received|sure|sounds good|perfect|great|awesome|cool|nice|understood)[!.\s]*$",
        re.IGNORECASE,
    ),
    re.compile(
        r"^(on it|will do|done|completed|finished|handled|no problem|np|👌|🙏|lgtm|ack)[!.\s]*$",
        re.IGNORECASE,
    ),
]

_MENTION_RE = re.compile(r"@\w+")


def is_acknowledgment(text: str) -> bool:
    """Return True if the message is a low-value ack that shouldn't trigger a reply."""
    clean = _MENTION_RE.sub("", text).strip()
    return any(p.search(clean) for p in _ACK_PATTERNS)
