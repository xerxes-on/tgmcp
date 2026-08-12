from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import aiosqlite
import anthropic
from telethon import TelegramClient, events  # type: ignore[import-untyped]
from telethon.tl.types import User  # type: ignore[import-untyped]

from . import memory, notifier, personality, summarizer
from .buffer import DebounceBuffer
from .config import AgentConfig
from .orchestrator import OrchestratorDecision, decide
from .relevance import is_relevant

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass
class Deps:
    client: TelegramClient
    me: User
    cfg: AgentConfig
    db: aiosqlite.Connection
    anthropic: anthropic.AsyncAnthropic
    buffer: DebounceBuffer
    approval_event: Any  # asyncio.Event


def register(client: TelegramClient, deps: Deps) -> None:
    """Register Telethon event handlers on the client."""

    @client.on(events.NewMessage())
    async def _on_message(event: events.NewMessage.Event) -> None:
        await _handle_event(event, deps)


async def _handle_event(event: events.NewMessage.Event, deps: Deps) -> None:
    # Skip outgoing messages (sent by us)
    if getattr(event, "out", False):
        return

    msg = event.message
    if msg is None:
        return

    chat_id: int = event.chat_id
    tg_msg_id: int = msg.id

    # Only process watched chats
    if not deps.cfg.is_watched(chat_id):
        return

    # Dedup
    if await memory.is_processed(deps.db, chat_id, tg_msg_id):
        return

    # Relevance check (groups only; DMs always pass)
    me_username = getattr(deps.me, "username", None)
    if not await is_relevant(
        event,
        me_id=deps.me.id,
        me_username=me_username,
        owned_services=deps.cfg.owned_services,
    ):
        return

    # Mark and store
    await memory.mark_processed(deps.db, chat_id, tg_msg_id)
    sender = await event.get_sender()
    sender_name = _sender_name(sender)
    text: str = msg.text or msg.message or ""

    await memory.save_message(
        deps.db,
        chat_id=chat_id,
        tg_msg_id=tg_msg_id,
        sender_id=getattr(sender, "id", None),
        sender_name=sender_name,
        text=text,
        ts=int(msg.date.timestamp()) if msg.date else int(time.time()),
    )

    chat_cfg = deps.cfg.chat_config(chat_id)
    is_dm = getattr(event, "is_private", False)

    event_payload = {
        "tg_msg_id": tg_msg_id,
        "sender_name": sender_name,
        "text": text,
        "is_dm": is_dm,
        "chat_cfg": chat_cfg,
    }

    if is_dm:
        # Debounce DMs — wait for message burst to complete
        await deps.buffer.append(chat_id, event_payload)
    else:
        # Groups: process immediately
        await _process_messages(chat_id, [event_payload], deps)


async def _process_messages(
    chat_id: int,
    payloads: list[dict[str, Any]],
    deps: Deps,
) -> None:
    """Run the orchestrator and execute the decision."""
    chat_cfg = deps.cfg.chat_config(chat_id)
    tone = chat_cfg.tone if chat_cfg else "friendly"
    last_tg_msg_id = payloads[-1]["tg_msg_id"]

    history = await memory.get_history(deps.db, chat_id, limit=deps.cfg.max_history_messages)
    summary = await memory.get_summary(deps.db, chat_id)

    # Compress history if it's getting long
    if len(history) >= deps.cfg.max_history_messages:
        new_summary = await summarizer.summarize_history(
            deps.anthropic,
            cfg=deps.cfg,
            chat_id=chat_id,
            existing_summary=summary,
            messages=history[: deps.cfg.max_history_messages // 2],
        )
        await memory.save_summary(deps.db, chat_id, new_summary)
        summary = new_summary

    incoming_messages = [
        {"sender_name": p["sender_name"], "text": p["text"]} for p in payloads
    ]
    incoming_preview = "; ".join(p["text"] for p in payloads)[:300]

    decision = await decide(
        deps.anthropic,
        cfg=deps.cfg,
        chat_id=chat_id,
        incoming_messages=incoming_messages,
        history=history,
        summary=summary,
        tone=tone,
    )

    logger.info(
        "decision chat=%d action=%s confidence=%.2f",
        chat_id, decision.action, decision.confidence,
    )

    await _execute_decision(
        decision=decision,
        chat_id=chat_id,
        last_tg_msg_id=last_tg_msg_id,
        incoming_preview=incoming_preview,
        deps=deps,
    )


async def _execute_decision(
    *,
    decision: OrchestratorDecision,
    chat_id: int,
    last_tg_msg_id: int,
    incoming_preview: str,
    deps: Deps,
) -> None:
    chat_cfg = deps.cfg.chat_config(chat_id)
    chat_label = f"chat {chat_id}"
    try:
        entity = await deps.client.get_entity(chat_id)
        chat_label = getattr(entity, "title", None) or getattr(entity, "first_name", None) or chat_label
    except Exception:
        pass

    if decision.action == "skip":
        if decision.reaction_emoji:
            await personality.send_reaction(
                deps.client, chat_id, last_tg_msg_id, decision.reaction_emoji
            )
        return

    if decision.action == "clarify":
        question = decision.clarify_question or decision.reply_text
        if not question:
            return
        # Clarify with confidence below threshold → need approval
        if decision.confidence < deps.cfg.confidence_threshold:
            await _queue_for_approval(
                chat_id=chat_id,
                draft=question,
                incoming=incoming_preview,
                chat_label=chat_label,
                decision=decision,
                is_clarify=True,
                deps=deps,
            )
        else:
            await _send_reply(deps, chat_id, last_tg_msg_id, question, decision)
        return

    if decision.action == "escalate" or decision.confidence < deps.cfg.confidence_threshold:
        draft = decision.reply_text
        await _queue_for_approval(
            chat_id=chat_id,
            draft=draft,
            incoming=incoming_preview,
            chat_label=chat_label,
            decision=decision,
            is_clarify=False,
            deps=deps,
        )
        return

    # action == "send" with sufficient confidence
    await _send_reply(deps, chat_id, last_tg_msg_id, decision.reply_text, decision)


async def _send_reply(
    deps: Deps,
    chat_id: int,
    reply_to_msg_id: int,
    text: str,
    decision: OrchestratorDecision,
) -> None:
    if not text:
        return

    # React first if suggested
    if decision.reaction_emoji:
        await personality.send_reaction(deps.client, chat_id, reply_to_msg_id, decision.reaction_emoji)

    # Typing indicator
    await personality.show_typing(deps.client, chat_id, personality.typing_duration(text))

    # Send the message
    sent = await deps.client.send_message(chat_id, text, reply_to=reply_to_msg_id)

    # Store our reply in history
    await memory.save_message(
        deps.db,
        chat_id=chat_id,
        tg_msg_id=sent.id,
        sender_id=deps.me.id,
        sender_name="Me",
        text=text,
        ts=int(time.time()),
        is_outgoing=True,
    )

    # Optional sticker follow-up
    if decision.sticker_emoji:
        await personality.send_sticker(deps.client, chat_id, decision.sticker_emoji)


async def _queue_for_approval(
    *,
    chat_id: int,
    draft: str,
    incoming: str,
    chat_label: str,
    decision: OrchestratorDecision,
    is_clarify: bool,
    deps: Deps,
) -> None:
    context = {
        "chat_label": chat_label,
        "incoming": incoming,
        "confidence": decision.confidence,
        "reasoning": decision.reasoning,
        "sticker_emoji": decision.sticker_emoji,
        "reaction_emoji": decision.reaction_emoji,
        "is_clarify": is_clarify,
        "last_tg_msg_id": 0,  # stored in draft flow; approval sender will reply to latest
    }
    await memory.queue_approval(
        deps.db,
        chat_id=chat_id,
        draft_text=draft,
        context=context,
        ttl_minutes=deps.cfg.approval_ttl_minutes,
    )
    notifier.iterm_notify(f"Approval needed — {chat_label}", draft[:80])
    deps.approval_event.set()


def make_flush_callback(deps: Deps):
    async def _flush(chat_id: int, payloads: list[dict]) -> None:
        await _process_messages(chat_id, payloads, deps)
    return _flush


def _sender_name(sender: object) -> str:
    if sender is None:
        return "Unknown"
    first = getattr(sender, "first_name", "") or ""
    last = getattr(sender, "last_name", "") or ""
    username = getattr(sender, "username", "") or ""
    full = f"{first} {last}".strip()
    return full or username or str(getattr(sender, "id", "Unknown"))
