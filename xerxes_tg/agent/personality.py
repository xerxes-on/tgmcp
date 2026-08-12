from __future__ import annotations

import asyncio
import random
from typing import TYPE_CHECKING

from telethon import functions, types  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from telethon import TelegramClient  # type: ignore[import-untyped]

# Sticker packs mapped by mood/context
_STICKER_MAP: dict[str, list[str]] = {
    "thumbs_up": ["👍"],
    "celebrate": ["🎉", "🥳"],
    "thinking": ["🤔"],
    "wave": ["👋"],
    "heart": ["❤️"],
    "laugh": ["😂", "😄"],
    "ok": ["✅", "👌"],
    "sad": ["😔"],
    "fire": ["🔥"],
    "working": ["💪", "🚀"],
}


async def show_typing(client: TelegramClient, chat_id: int, duration_s: float = 1.5) -> None:
    """Send typing indicator for a short duration before a reply."""
    try:
        entity = await client.get_input_entity(chat_id)
        await client(
            functions.messages.SetTypingRequest(
                peer=entity,
                action=types.SendMessageTypingAction(),
            )
        )
        await asyncio.sleep(duration_s)
    except Exception:
        pass


async def send_reaction(
    client: TelegramClient,
    chat_id: int,
    message_id: int,
    emoji: str,
) -> None:
    try:
        entity = await client.get_input_entity(chat_id)
        await client(
            functions.messages.SendReactionRequest(
                peer=entity,
                msg_id=message_id,
                reaction=[types.ReactionEmoji(emoticon=emoji)],
            )
        )
    except Exception:
        pass


async def send_sticker(
    client: TelegramClient,
    chat_id: int,
    emoji: str,
    reply_to: int | None = None,
) -> None:
    """Find and send a random sticker matching emoji."""
    try:
        result = await client(
            functions.messages.GetStickersRequest(emoticon=emoji, hash=0)
        )
        stickers = getattr(result, "stickers", [])
        if not stickers:
            return
        file = stickers[random.randint(0, min(len(stickers) - 1, 9))]
        await client.send_file(chat_id, file, reply_to=reply_to)
    except Exception:
        pass


def typing_duration(text: str) -> float:
    """Simulate realistic typing speed (chars/sec), capped at 3s."""
    return min(len(text) / 40.0, 3.0)
