from __future__ import annotations

import logging
import time
from typing import Any, Literal

import anthropic
from pydantic import BaseModel

from .config import AgentConfig

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
You are an autonomous Telegram agent acting on behalf of your owner. You manage their Telegram \
communications — replying to messages, answering questions, and delegating complex tasks.

You have deep knowledge of the UFarm ecosystem (ufarm-api, ufarm-market, ufarm-billing, \
ufarm-auth, ufarm-notifications, ufarm-chat, ufarm-sms, ufarm-bnpl, ufarm-scoring, \
ufarm-ai-calendar) and general software engineering.

Personality:
- Professional tone in team/work chats (technical, concise, helpful)
- Friendly and warm in personal DMs (casual, emoji OK, stickers welcome)
- Always honest — if you don't know, say so and escalate

Decision rules:
- action=send: you are confident and have clear evidence/context to reply correctly
- action=skip: message does not need a reply (spam, irrelevant, already answered)
- action=clarify: you need more info from the sender before answering — ask for a screenshot, \
curl command, error log, steps to reproduce, API response, etc. Be specific in clarify_question.
- action=escalate: you are unsure, the topic is sensitive, or consequences of a wrong answer are high

Confidence guidelines:
- >= 0.85: factual, technical answer you know well
- 0.65–0.84: likely correct but worth verifying
- < 0.65: uncertain — escalate with a draft

Social signals:
- sticker_emoji: suggest an emoji for a sticker when contextually appropriate (fun chats, celebrations)
- reaction_emoji: suggest a reaction emoji when a brief acknowledgment beats a full reply
- Both are optional — null when not needed

Always respond by calling the `respond` tool.
"""

_RESPOND_TOOL: dict[str, Any] = {
    "name": "respond",
    "description": "Emit your decision for this Telegram message.",
    "input_schema": {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["send", "skip", "escalate", "clarify"],
                "description": (
                    "send: reply immediately. "
                    "skip: no action needed. "
                    "escalate: needs owner review. "
                    "clarify: ask the sender for more info (screenshot, curl, logs, steps to reproduce, etc.)."
                ),
            },
            "reply_text": {
                "type": "string",
                "description": "Reply to send or draft for owner review. Required for send/escalate.",
            },
            "clarify_question": {
                "type": ["string", "null"],
                "description": (
                    "When action=clarify, the question to ask the sender. "
                    "Be specific: ask for a screenshot, a curl command, error logs, "
                    "steps to reproduce, the exact API response, etc."
                ),
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
                "description": "Confidence in this decision (0–1).",
            },
            "sticker_emoji": {
                "type": ["string", "null"],
                "description": "Emoji to send as a sticker alongside the reply. Null if not needed.",
            },
            "reaction_emoji": {
                "type": ["string", "null"],
                "description": "Emoji to react to the message with. Null if not needed.",
            },
            "reasoning": {
                "type": "string",
                "description": "Brief explanation of the decision and confidence.",
            },
        },
        "required": ["action", "confidence", "reasoning"],
    },
}


class OrchestratorDecision(BaseModel):
    action: Literal["send", "skip", "escalate", "clarify"]
    reply_text: str = ""
    clarify_question: str | None = None
    confidence: float
    sticker_emoji: str | None = None
    reaction_emoji: str | None = None
    reasoning: str


async def decide(
    anthropic_client: anthropic.AsyncAnthropic,
    *,
    cfg: AgentConfig,
    chat_id: int,
    incoming_messages: list[dict[str, Any]],
    history: list[dict[str, Any]],
    summary: str,
    tone: str,
) -> OrchestratorDecision:
    """Call Claude to decide what to do with the incoming message(s)."""

    # Build message history for the prompt
    history_text = _format_history(history)
    incoming_text = _format_incoming(incoming_messages)

    user_content = f"Chat tone: {tone}\n\n"
    if summary:
        user_content += f"Conversation summary so far:\n{summary}\n\n"
    if history_text:
        user_content += f"Recent history:\n{history_text}\n\n"
    user_content += f"New message(s) to handle:\n{incoming_text}"

    services_note = ""
    if cfg.owned_services:
        services_note = f"\n\nOwned services context: {', '.join(cfg.owned_services)}"

    messages = [{"role": "user", "content": user_content}]

    try:
        response = await anthropic_client.messages.create(
            model=cfg.orchestrator_model,
            max_tokens=1024,
            system=[
                {
                    "type": "text",
                    "text": _SYSTEM_PROMPT + services_note,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            tools=[_RESPOND_TOOL],
            tool_choice={"type": "tool", "name": "respond"},
            messages=messages,
        )
    except Exception:
        logger.exception("Orchestrator API call failed")
        return OrchestratorDecision(
            action="escalate",
            reply_text="(agent error — please review manually)",
            confidence=0.0,
            reasoning="API call failed",
        )

    # Extract tool use block
    for block in response.content:
        if block.type == "tool_use" and block.name == "respond":
            inp = block.input
            return OrchestratorDecision(
                action=inp.get("action", "escalate"),
                reply_text=inp.get("reply_text", ""),
                clarify_question=inp.get("clarify_question"),
                confidence=float(inp.get("confidence", 0.5)),
                sticker_emoji=inp.get("sticker_emoji"),
                reaction_emoji=inp.get("reaction_emoji"),
                reasoning=inp.get("reasoning", ""),
            )

    return OrchestratorDecision(
        action="escalate",
        reply_text="",
        confidence=0.0,
        reasoning="No tool_use block in response",
    )


def _format_history(history: list[dict[str, Any]]) -> str:
    lines = []
    for msg in history:
        role = "Me" if msg.get("outgoing") else (msg.get("sender") or "Them")
        ts = time.strftime("%H:%M", time.localtime(msg.get("ts", 0)))
        lines.append(f"[{ts}] {role}: {msg.get('text', '')}")
    return "\n".join(lines)


def _format_incoming(messages: list[dict[str, Any]]) -> str:
    lines = []
    for msg in messages:
        sender = msg.get("sender_name") or "Unknown"
        text = msg.get("text", "")
        lines.append(f"{sender}: {text}")
    return "\n".join(lines)
