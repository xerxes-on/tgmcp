"""Autonomous Telegram listener with agent CLI integration.

Listens for incoming messages, fetches conversation context,
runs per-chat agent sessions, and handles reply and approval flows.
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import shutil
from datetime import UTC, datetime

from telethon import TelegramClient, events  # type: ignore[import-untyped]
from telethon.tl.custom import Message  # type: ignore[import-untyped]

from .state import (
    LoopGuard,
    PendingApproval,
    PendingApprovalStore,
    RateLimiter,
    SentMessageTracker,
    SessionStore,
    is_acknowledgment,
)
from .telegram import check_access, create_listener_client, get_settings, resolve_dialog_id

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# System prompt — dedicated for Telegram listener decisions
# ---------------------------------------------------------------------------

LISTENER_SYSTEM_PROMPT = """\
You are a Telegram listener agent. You receive messages from Telegram chats \
and decide how to respond.

You receive the conversation context (recent messages) and the latest incoming message.

## Decision Rules

1. If the message is noise, spam, a sticker reaction, or a simple acknowledgment \
("ok", "thanks"), IGNORE it.
2. If the message is a direct question to you, or a reply to one of your previous \
messages, you SHOULD reply.
3. If the message is a group conversation between other people and does not mention \
you or need your input, IGNORE it.
4. Reply in the same language the sender used.
5. Be concise. Telegram messages should be short and practical.
6. Do not use markdown code fences in your response — plain text only.
7. If you are unsure whether to reply or the topic is sensitive/risky, keep your \
reply cautious.

## Output Format

Reply with ONLY the text you want to send. Nothing else. No JSON, no markdown \
fences, no "Reply:" prefix.
If you want to ignore the message, reply with exactly: IGNORE
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _strip_code_fences(text: str) -> str:
    cleaned = text.strip()
    if cleaned.startswith("```") and cleaned.endswith("```"):
        lines = cleaned.splitlines()
        if len(lines) >= 3:
            return "\n".join(lines[1:-1]).strip()
    return cleaned


def _truncate(text: str, limit: int = 700) -> str:
    value = text.strip()
    if len(value) <= limit:
        return value
    return value[: limit - 3] + "..."


# ---------------------------------------------------------------------------
# AgentDecision
# ---------------------------------------------------------------------------


class AgentDecision:
    __slots__ = ("action", "reply", "reason")

    def __init__(self, action: str, reply: str = "", reason: str = "") -> None:
        self.action = action
        self.reply = reply
        self.reason = reason


# ---------------------------------------------------------------------------
# TelegramAutoListener
# ---------------------------------------------------------------------------


class TelegramAutoListener:
    def __init__(self, client: TelegramClient) -> None:
        self.client = client
        self.settings = get_settings()
        self.chat_modes = self.settings.listener_chat_modes
        self.system_prompt = self.settings.listener_system_prompt.strip() or LISTENER_SYSTEM_PROMPT
        self.approval_chat_id = (
            resolve_dialog_id(self.settings.listener_approval_chat)
            if self.settings.listener_approval_chat.strip()
            else None
        )
        self.self_id: int | None = None

        # Agent CLI config
        self.claude_path = self.settings.listener_claude_path
        self.claude_cwd = self.settings.listener_claude_cwd or os.getcwd()
        self.timeout = self.settings.listener_timeout
        self.context_count = self.settings.listener_context_messages

        # State management
        self.sessions = SessionStore(cwd=self.claude_cwd)
        self.pending_store = PendingApprovalStore(ttl_seconds=self.settings.listener_approval_ttl)
        self.sent_tracker = SentMessageTracker()
        self.rate_limiter = RateLimiter(
            max_count=self.settings.listener_rate_limit_count,
            window_seconds=self.settings.listener_rate_limit_window,
            cooldown_seconds=self.settings.listener_cooldown,
        )
        self.loop_guard = LoopGuard()

    # ------------------------------------------------------------------
    # Validation
    # ------------------------------------------------------------------

    def _validate(self) -> None:
        if not self.settings.listener_enabled:
            raise ValueError("TELEGRAM_LISTENER_ENABLED is false")
        if not self.chat_modes:
            raise ValueError("No listener chats configured in TELEGRAM_LISTENER_CHATS")

        requires_agent = any(mode in {"ask", "auto", "decide"} for mode in self.chat_modes.values())
        requires_approval = any(mode in {"ask", "decide"} for mode in self.chat_modes.values())

        for chat_id, mode in self.chat_modes.items():
            check_access(chat_id, "read")
            if mode in {"ask", "auto", "decide"}:
                check_access(chat_id, "write")

        if requires_agent:
            resolved = shutil.which(self.claude_path)
            if not resolved:
                raise ValueError(
                    f"Agent CLI not found at '{self.claude_path}'. "
                    "Set TELEGRAM_LISTENER_CLAUDE_PATH to the correct path."
                )
            logger.info("Agent CLI resolved to: %s", resolved)

        if requires_approval:
            if self.approval_chat_id is None:
                raise ValueError("TELEGRAM_LISTENER_APPROVAL_CHAT is required for ask/decide modes")
            check_access(self.approval_chat_id, "write")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def run(self) -> None:
        self._validate()
        await self.client.connect()
        me = await self.client.get_me()
        self.self_id = getattr(me, "id", None)

        chats = set(self.chat_modes.keys())
        if self.approval_chat_id is not None:
            chats.add(self.approval_chat_id)

        @self.client.on(events.NewMessage(chats=list(chats)))
        async def on_new_message(event: events.NewMessage.Event) -> None:
            try:
                await self._handle_event(event)
            except Exception:
                logger.exception("Listener failed to handle message")

        # Background cleanup for expired approvals
        asyncio.create_task(self._approval_cleanup_loop())

        logger.info("Listener started for chats: %s", self.chat_modes)
        if self.approval_chat_id is not None:
            logger.info("Approval chat: %s", self.approval_chat_id)

        await self.client.run_until_disconnected()

    async def _approval_cleanup_loop(self) -> None:
        while True:
            await asyncio.sleep(600)  # every 10 minutes
            try:
                self.pending_store.cleanup_expired()
            except Exception:
                logger.exception("Approval cleanup failed")

    # ------------------------------------------------------------------
    # Event dispatch
    # ------------------------------------------------------------------

    async def _handle_event(self, event: events.NewMessage.Event) -> None:
        message = event.message
        chat_id = event.chat_id
        if chat_id is None:
            return

        # Handle approval commands in the approval chat
        if self.approval_chat_id is not None and chat_id == self.approval_chat_id:
            if await self._handle_approval_command(message):
                return

        mode = self.chat_modes.get(chat_id)
        if mode is None:
            return

        # Skip own messages
        if message.out:
            return
        if self.self_id is not None and message.sender_id == self.self_id:
            return
        if not message.text and not message.media:
            return

        # Detect replies to our own messages
        is_reply_to_own = False
        if message.reply_to and getattr(message.reply_to, "reply_to_msg_id", None):
            reply_target_id = message.reply_to.reply_to_msg_id
            is_reply_to_own = self.sent_tracker.is_own_message(chat_id, reply_target_id)

        await self._handle_incoming_message(message, mode, is_reply_to_own)

    # ------------------------------------------------------------------
    # Incoming message processing
    # ------------------------------------------------------------------

    async def _handle_incoming_message(
        self,
        message: Message,
        mode: str,
        is_reply_to_own: bool,
    ) -> None:
        chat = await message.get_chat()
        sender = await message.get_sender()
        chat_id = message.chat_id or 0
        chat_name = getattr(chat, "title", None) or getattr(chat, "first_name", None) or str(chat_id)
        sender_name = (
            getattr(sender, "first_name", None)
            or getattr(sender, "title", None)
            or getattr(sender, "username", None)
            or str(message.sender_id or "unknown")
        )

        original_text = message.text or f"[media: {type(message.media).__name__}]"

        # --- Read mode: log only ---
        if mode == "read":
            logger.info("Read-only: chat=%s id=%s from=%s", chat_id, message.id, sender_name)
            return

        # --- Guard chain ---

        # 1. Ack detection (skip if this is a direct reply to us)
        if not is_reply_to_own and is_acknowledgment(original_text):
            logger.debug("Dropped ack: chat=%s id=%s text='%s'", chat_id, message.id, original_text[:50])
            return

        # 2. Rate limit
        if not self.rate_limiter.is_allowed(chat_id):
            logger.warning("Rate limit hit: chat=%s, skipping id=%s", chat_id, message.id)
            return

        # 3. Loop guard (bot senders only)
        sender_is_bot = getattr(sender, "bot", False)
        if sender_is_bot:
            allowed, reason = self.loop_guard.check(message.sender_id or 0)
            if not allowed:
                logger.warning("Loop guard blocked sender=%s: %s", message.sender_id, reason)
                return

        # --- Run agent ---
        decision = await self._run_agent(
            mode=mode,
            chat_id=chat_id,
            chat_name=chat_name,
            sender_name=sender_name,
            message=message,
            original_text=original_text,
            is_reply_to_own=is_reply_to_own,
        )

        if decision.action == "ignore":
            logger.info("Ignored: chat=%s id=%s reason=%s", chat_id, message.id, decision.reason or "agent")
            return

        # --- Send or queue reply ---
        if mode == "auto" or mode == "decide":
            if not decision.reply:
                return
            sent = await self.client.send_message(chat_id, decision.reply, reply_to=message.id)
            self.sent_tracker.record(chat_id, sent.id)
            self.rate_limiter.record(chat_id)
            if sender_is_bot:
                self.loop_guard.record(message.sender_id or 0)
            logger.info("Replied: chat=%s id=%s", chat_id, message.id)

        elif mode == "ask":
            if not decision.reply:
                return
            await self._queue_approval(
                chat_id=chat_id,
                message_id=message.id,
                draft_reply=decision.reply,
                original_text=original_text,
                chat_name=chat_name,
                sender_name=sender_name,
                reason=decision.reason or "Always-ask mode",
            )

    # ------------------------------------------------------------------
    # Context fetching
    # ------------------------------------------------------------------

    async def _fetch_context(self, chat_id: int, before_id: int) -> str:
        """Fetch recent messages from the chat for conversation context."""
        lines: list[str] = []
        try:
            async for msg in self.client.iter_messages(
                chat_id,
                limit=self.context_count,
                max_id=before_id,
            ):
                if not isinstance(msg, Message):
                    continue
                sender = await msg.get_sender()
                name = (
                    getattr(sender, "first_name", None)
                    or getattr(sender, "title", None)
                    or str(msg.sender_id or "?")
                )
                is_me = msg.sender_id == self.self_id
                label = f"{name} (you)" if is_me else name
                ts = msg.date.strftime("%H:%M") if msg.date else "?"
                text = msg.text or "[media]"
                lines.append(f"[{ts}] {label}: {text}")
        except Exception:
            logger.exception("Failed to fetch context for chat %s", chat_id)

        lines.reverse()  # oldest first
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Agent CLI runner
    # ------------------------------------------------------------------

    async def _run_agent(
        self,
        *,
        mode: str,
        chat_id: int,
        chat_name: str,
        sender_name: str,
        message: Message,
        original_text: str,
        is_reply_to_own: bool,
    ) -> AgentDecision:
        # 1. Fetch conversation context
        context = await self._fetch_context(chat_id, before_id=message.id)

        # 2. Get or create agent session
        session_id, is_new = self.sessions.get_or_create(chat_id)

        # 3. Build prompt
        prompt_parts: list[str] = []
        if context:
            prompt_parts.append(f'=== Recent conversation in "{chat_name}" ===')
            prompt_parts.append(context)
            prompt_parts.append("")
        prompt_parts.append("=== New message ===")
        prompt_parts.append(f"From: {sender_name}")
        if is_reply_to_own:
            prompt_parts.append("(This is a direct reply to one of YOUR previous messages)")
        prompt_parts.append(f"Text: {original_text}")

        if mode == "ask":
            prompt_parts.append("\nMode: ask — always draft a reply (it goes through approval).")
        elif mode == "auto":
            prompt_parts.append("\nMode: auto — reply if appropriate, ignore noise.")
        elif mode == "decide":
            prompt_parts.append("\nMode: decide — reply if appropriate, ignore if not.")

        prompt = "\n".join(prompt_parts)

        # 4. Build agent CLI arguments
        args = [self.claude_path, "--dangerously-skip-permissions"]
        if is_new:
            args.extend(["--session-id", session_id])
            args.extend(["--system-prompt", self.system_prompt])
        else:
            args.extend(["--resume", session_id])
        args.append("-p")

        # 5. Build environment
        env = {**os.environ}
        env.pop("CLAUDECODE", None)

        # 6. Run subprocess with timeout
        logger.info(
            "Agent CLI: %s session=%s chat=%s",
            "new" if is_new else "resume",
            session_id[:8],
            chat_id,
        )

        try:
            process = await asyncio.create_subprocess_exec(
                *args,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.claude_cwd,
                env=env,
            )
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                process.communicate(prompt.encode()),
                timeout=self.timeout,
            )
        except asyncio.TimeoutError:
            logger.error("Agent CLI timeout (%ds) chat=%s", self.timeout, chat_id)
            try:
                process.kill()
                await process.wait()
            except Exception:
                pass
            return AgentDecision(action="ignore", reason="timeout")
        except Exception as exc:
            logger.exception("Agent CLI error chat=%s: %s", chat_id, exc)
            return AgentDecision(action="ignore", reason=str(exc))

        if process.returncode != 0:
            error = (stderr_bytes or stdout_bytes or b"").decode().strip()
            logger.error("Agent CLI exit=%d chat=%s: %s", process.returncode, chat_id, error[:300])
            # Reset broken sessions
            if any(kw in error.lower() for kw in ("session", "resume", "not found")):
                logger.info("Resetting broken session %s for chat %s", session_id[:8], chat_id)
                self.sessions.clear(chat_id)
            return AgentDecision(action="ignore", reason="process error")

        # 7. Parse output
        output = _strip_code_fences((stdout_bytes or b"").decode().strip())
        if not output:
            return AgentDecision(action="ignore", reason="empty output")

        if output.upper().strip() == "IGNORE":
            return AgentDecision(action="ignore")

        # For ask mode, output always goes through approval
        action = "ask" if mode == "ask" else "reply"
        return AgentDecision(action=action, reply=output)

    # ------------------------------------------------------------------
    # Approval queue
    # ------------------------------------------------------------------

    async def _queue_approval(
        self,
        *,
        chat_id: int,
        message_id: int,
        draft_reply: str,
        original_text: str,
        chat_name: str,
        sender_name: str,
        reason: str,
    ) -> None:
        if self.approval_chat_id is None:
            raise RuntimeError("Approval chat is not configured")

        token = secrets.token_hex(3)
        pending = PendingApproval(
            token=token,
            chat_id=chat_id,
            message_id=message_id,
            draft_reply=draft_reply,
            original_text=original_text,
            chat_name=chat_name,
            sender_name=sender_name,
            reason=reason,
            created_at=datetime.now(UTC),
        )
        self.pending_store.add(pending)

        approval_text = "\n".join(
            [
                f"Approval needed [{token}]",
                f"Chat: {chat_name} ({chat_id})",
                f"From: {sender_name}",
                f"Message ID: {message_id}",
                f"Reason: {reason}",
                "",
                "Original:",
                _truncate(original_text),
                "",
                "Draft reply:",
                _truncate(draft_reply),
                "",
                "Commands:",
                f"/approve {token}",
                f"/reply {token} <custom text>",
                f"/reject {token}",
            ]
        )

        await self.client.send_message(self.approval_chat_id, approval_text)
        logger.info("Queued approval token=%s chat=%s msg=%s", token, chat_id, message_id)

    async def _handle_approval_command(self, message: Message) -> bool:
        text = (message.text or "").strip()
        if not text.startswith("/"):
            return False

        parts = text.split(maxsplit=2)
        command = parts[0].lower()
        if command not in {"/approve", "/reply", "/reject"}:
            return False

        if len(parts) < 2:
            await self.client.send_message(message.chat_id, "Missing approval token.", reply_to=message.id)
            return True

        token = parts[1].strip().lower()
        pending = self.pending_store.get(token)
        if not pending:
            await self.client.send_message(message.chat_id, f"Unknown or expired token: {token}", reply_to=message.id)
            return True

        if command == "/reject":
            self.pending_store.remove(token)
            await self.client.send_message(message.chat_id, f"Rejected {token}.", reply_to=message.id)
            return True

        reply_text = pending.draft_reply
        if command == "/reply":
            if len(parts) < 3 or not parts[2].strip():
                await self.client.send_message(message.chat_id, "Custom reply text is required.", reply_to=message.id)
                return True
            reply_text = parts[2].strip()
        elif len(parts) >= 3 and parts[2].strip():
            reply_text = parts[2].strip()

        sent = await self.client.send_message(pending.chat_id, reply_text, reply_to=pending.message_id)
        self.sent_tracker.record(pending.chat_id, sent.id)
        self.rate_limiter.record(pending.chat_id)
        self.pending_store.remove(token)
        await self.client.send_message(message.chat_id, f"Sent reply for {token}.", reply_to=message.id)
        logger.info("Approved token=%s chat=%s msg=%s", token, pending.chat_id, pending.message_id)
        return True


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


async def run_listener() -> None:
    """Standalone entry point for ``xerxes-tg listen``."""
    client = create_listener_client()
    listener = TelegramAutoListener(client)
    await listener.run()
