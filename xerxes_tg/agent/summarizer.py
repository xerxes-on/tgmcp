from __future__ import annotations

import logging
from typing import Any

import anthropic

from .config import AgentConfig

logger = logging.getLogger(__name__)


async def summarize_history(
    anthropic_client: anthropic.AsyncAnthropic,
    *,
    cfg: AgentConfig,
    chat_id: int,
    existing_summary: str,
    messages: list[dict[str, Any]],
) -> str:
    """Compress conversation history into an updated summary."""
    history_text = "\n".join(
        f"{'Me' if m.get('outgoing') else (m.get('sender') or 'Them')}: {m.get('text', '')}"
        for m in messages
    )

    prompt = "Summarize this conversation concisely, preserving key facts, decisions, and open questions.\n\n"
    if existing_summary:
        prompt += f"Previous summary:\n{existing_summary}\n\n"
    prompt += f"New messages:\n{history_text}\n\nReturn only the updated summary."

    try:
        response = await anthropic_client.messages.create(
            model=cfg.summarizer_model,
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text.strip()
    except Exception:
        logger.exception("Summarizer API call failed")
        return existing_summary
