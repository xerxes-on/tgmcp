from __future__ import annotations

import re


async def is_relevant(
    event: object,
    *,
    me_id: int,
    me_username: str | None,
    owned_services: list[str],
) -> bool:
    """Return True if the message warrants agent attention."""
    # Private chats (DMs) are always relevant
    if getattr(event, "is_private", False):
        return True

    msg = getattr(event, "message", None)
    if msg is None:
        return False

    # Telegram-native mention flag
    if getattr(msg, "mentioned", False):
        return True

    # Fallback: username literal in text
    text: str = getattr(msg, "text", None) or getattr(msg, "message", None) or ""
    if me_username and f"@{me_username.lower()}" in text.lower():
        return True

    # Reply to one of our own messages
    reply_to = getattr(msg, "reply_to", None)
    if reply_to is not None:
        try:
            replied = await msg.get_reply_message()
            if replied is not None and getattr(replied, "sender_id", None) == me_id:
                return True
        except Exception:
            pass

    # Owned-service keyword match (whole-word)
    text_lower = text.lower()
    for svc in owned_services:
        if re.search(rf"\b{re.escape(svc.lower())}\b", text_lower):
            return True

    return False
