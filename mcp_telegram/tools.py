from __future__ import annotations

import logging
import os
import random
import sys
import typing as t
from functools import singledispatch

from mcp.types import (
    EmbeddedResource,
    ImageContent,
    TextContent,
    Tool,
)
from pydantic import BaseModel, ConfigDict
from telethon import TelegramClient, custom, functions, types  # type: ignore[import-untyped]

from .telegram import check_access, create_client, get_aliases, get_allowed_chat_ids, resolve_dialog_id

logger = logging.getLogger(__name__)


class ToolArgs(BaseModel):
    model_config = ConfigDict()


@singledispatch
async def tool_runner(
    args,  # noqa: ANN001
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    raise NotImplementedError(f"Unsupported type: {type(args)}")


def tool_description(args: type[ToolArgs]) -> Tool:
    return Tool(
        name=args.__name__,
        description=args.__doc__,
        inputSchema=args.model_json_schema(),
    )


def tool_args(tool: Tool, *args, **kwargs) -> ToolArgs:  # noqa: ANN002, ANN003
    return sys.modules[__name__].__dict__[tool.name](*args, **kwargs)


### ListDialogs ###


class ListDialogs(ToolArgs):
    """List available dialogs, chats and channels."""

    unread: bool = False
    archived: bool = False
    ignore_pinned: bool = False


@tool_runner.register
async def list_dialogs(
    args: ListDialogs,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    client: TelegramClient
    logger.info("method[ListDialogs] args[%s]", args)

    allowed = get_allowed_chat_ids()

    response: list[TextContent] = []
    async with create_client() as client:
        dialog: custom.dialog.Dialog
        async for dialog in client.iter_dialogs(archived=args.archived, ignore_pinned=args.ignore_pinned):
            if allowed is not None and dialog.id not in allowed:
                continue
            if args.unread and dialog.unread_count == 0:
                continue
            msg = (
                f"name='{dialog.name}' id={dialog.id} "
                f"unread={dialog.unread_count} mentions={dialog.unread_mentions_count}"
            )
            response.append(TextContent(type="text", text=msg))

    return response


### ListMessages ###


class ListMessages(ToolArgs):
    """
    List messages in a given dialog, chat or channel. The messages are listed in order from newest to oldest.

    If `unread` is set to `True`, only unread messages will be listed. Once a message is read, it will not be
    listed again.

    If `limit` is set, only the last `limit` messages will be listed. If `unread` is set, the limit will be
    the minimum between the unread messages and the limit.
    """

    dialog_id: int | str
    unread: bool = False
    limit: int = 100


@tool_runner.register
async def list_messages(
    args: ListMessages,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    client: TelegramClient
    logger.info("method[ListMessages] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "read")

    response: list[TextContent] = []
    async with create_client() as client:
        result = await client(functions.messages.GetPeerDialogsRequest(peers=[did]))
        if not result:
            raise ValueError(f"Channel not found: {args.dialog_id}")

        if not isinstance(result, types.messages.PeerDialogs):
            raise TypeError(f"Unexpected result: {type(result)}")

        for dialog in result.dialogs:
            logger.debug("dialog: %s", dialog)
        for message in result.messages:
            logger.debug("message: %s", message)

        iter_messages_args: dict[str, t.Any] = {
            "entity": did,
            "reverse": False,
        }
        if args.unread:
            iter_messages_args["limit"] = min(dialog.unread_count, args.limit)
        else:
            iter_messages_args["limit"] = args.limit

        logger.debug("iter_messages_args: %s", iter_messages_args)
        async for message in client.iter_messages(**iter_messages_args):
            logger.debug("message: %s", type(message))
            if isinstance(message, custom.Message):
                text = message.text or ""
                sender = ""
                if message.sender:
                    if hasattr(message.sender, "first_name"):
                        sender = message.sender.first_name or ""
                    elif hasattr(message.sender, "title"):
                        sender = message.sender.title or ""
                prefix = f"[id={message.id}] {sender}: " if sender else f"[id={message.id}] "
                media_note = ""
                if message.media and not text:
                    media_note = f"[media: {type(message.media).__name__}]"
                content = text or media_note
                if content:
                    response.append(TextContent(type="text", text=f"{prefix}{content}"))

    return response


### SendMessage ###


class SendMessage(ToolArgs):
    """Send a text message to a dialog, chat or channel. Use parse_mode='md' for Markdown or 'html' for HTML formatting."""

    dialog_id: int | str
    message: str
    parse_mode: str = "md"
    reply_to: int | None = None
    link_preview: bool = True


@tool_runner.register
async def send_message(
    args: SendMessage,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[SendMessage] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "write")

    msg = args.message.rstrip() + "\n\n`sent via agent`"
    parse_mode = args.parse_mode if args.parse_mode else None
    async with create_client() as client:
        result = await client.send_message(
            entity=did,
            message=msg,
            parse_mode=parse_mode,
            reply_to=args.reply_to,
            link_preview=args.link_preview,
        )
        return [TextContent(type="text", text=f"Message sent. id={result.id}")]


### EditMessage ###


class EditMessage(ToolArgs):
    """Edit a previously sent message. You can only edit your own messages."""

    dialog_id: int | str
    message_id: int
    new_text: str
    parse_mode: str = "md"


@tool_runner.register
async def edit_message(
    args: EditMessage,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[EditMessage] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "write")

    parse_mode = args.parse_mode if args.parse_mode else None
    async with create_client() as client:
        result = await client.edit_message(
            entity=did,
            message=args.message_id,
            text=args.new_text,
            parse_mode=parse_mode,
        )
        return [TextContent(type="text", text=f"Message edited. id={result.id}")]


### DeleteMessages ###


class DeleteMessages(ToolArgs):
    """Delete one or more messages. Provide a list of message IDs to delete."""

    dialog_id: int | str
    message_ids: list[int]
    revoke: bool = True


@tool_runner.register
async def delete_messages(
    args: DeleteMessages,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[DeleteMessages] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "write")

    async with create_client() as client:
        result = await client.delete_messages(
            entity=did,
            message_ids=args.message_ids,
            revoke=args.revoke,
        )
        return [TextContent(type="text", text=f"Deleted {len(args.message_ids)} message(s). result={result}")]


### ForwardMessages ###


class ForwardMessages(ToolArgs):
    """Forward messages from one dialog to another."""

    from_dialog_id: int | str
    to_dialog_id: int | str
    message_ids: list[int]


@tool_runner.register
async def forward_messages(
    args: ForwardMessages,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[ForwardMessages] args[%s]", args)
    from_did = resolve_dialog_id(args.from_dialog_id)
    to_did = resolve_dialog_id(args.to_dialog_id)
    check_access(from_did, "read")
    check_access(to_did, "write")

    async with create_client() as client:
        result = await client.forward_messages(
            entity=to_did,
            messages=args.message_ids,
            from_peer=from_did,
        )
        ids = [m.id for m in result] if isinstance(result, list) else [result.id]
        return [TextContent(type="text", text=f"Forwarded {len(args.message_ids)} message(s). new_ids={ids}")]


### SendReaction ###


class SendReaction(ToolArgs):
    """React to a message with an emoji. Common reactions: thumbs_up, heart, fire, party, laughing, shocked, sad, 100, rocket, eyes, clap, pray."""

    dialog_id: int | str
    message_id: int
    emoji: str = "\U0001f44d"


@tool_runner.register
async def send_reaction(
    args: SendReaction,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[SendReaction] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "write")

    async with create_client() as client:
        await client(
            functions.messages.SendReactionRequest(
                peer=did,
                msg_id=args.message_id,
                reaction=[types.ReactionEmoji(emoticon=args.emoji)],
            )
        )
        return [TextContent(type="text", text=f"Reacted with {args.emoji} to message {args.message_id}")]


### SendFile ###


class SendFile(ToolArgs):
    """Send a file, photo, or video to a dialog. Provide a local file path."""

    dialog_id: int | str
    file_path: str
    caption: str = ""
    reply_to: int | None = None
    force_document: bool = False


@tool_runner.register
async def send_file(
    args: SendFile,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[SendFile] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "write")

    if not os.path.exists(args.file_path):
        raise FileNotFoundError(f"File not found: {args.file_path}")

    async with create_client() as client:
        result = await client.send_file(
            entity=did,
            file=args.file_path,
            caption=args.caption,
            reply_to=args.reply_to,
            force_document=args.force_document,
        )
        return [TextContent(type="text", text=f"File sent. id={result.id}")]


### DownloadMedia ###


class DownloadMedia(ToolArgs):
    """Download media from a message. Returns the local file path of the downloaded file."""

    dialog_id: int | str
    message_id: int
    save_dir: str = ""


@tool_runner.register
async def download_media(
    args: DownloadMedia,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[DownloadMedia] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "read")

    save_dir = args.save_dir or os.path.join(os.path.expanduser("~"), "Downloads")

    async with create_client() as client:
        messages = await client.get_messages(did, ids=args.message_id)
        if not messages:
            raise ValueError(f"Message {args.message_id} not found in dialog {args.dialog_id}")

        message = messages
        if isinstance(messages, list):
            message = messages[0]

        if not message.media:
            raise ValueError(f"Message {args.message_id} has no media")

        path = await client.download_media(message, file=save_dir)
        return [TextContent(type="text", text=f"Downloaded to: {path}")]


### MarkAsRead ###


class MarkAsRead(ToolArgs):
    """Mark messages as read in a dialog up to a given message ID."""

    dialog_id: int | str
    max_id: int = 0


@tool_runner.register
async def mark_as_read(
    args: MarkAsRead,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[MarkAsRead] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "read")

    async with create_client() as client:
        await client.send_read_acknowledge(
            entity=did,
            max_id=args.max_id,
        )
        return [TextContent(type="text", text=f"Marked as read in dialog {args.dialog_id} up to message {args.max_id}")]


### GetMe ###


class GetMe(ToolArgs):
    """Get information about the currently logged-in user account."""


@tool_runner.register
async def get_me(
    args: GetMe,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[GetMe] args[%s]", args)

    async with create_client() as client:
        me = await client.get_me()
        info = f"id={me.id} username={me.username} first_name={me.first_name} last_name={me.last_name} phone={me.phone}"
        return [TextContent(type="text", text=info)]


### SendSticker ###


class SendSticker(ToolArgs):
    """Send a sticker to a dialog. Provide either a sticker file_id (from a previous message), a local .webp/.tgs/.webm file path, or a sticker emoji shortcode to search for a random sticker with that emoji."""

    dialog_id: int | str
    sticker: str
    reply_to: int | None = None


@tool_runner.register
async def send_sticker(
    args: SendSticker,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[SendSticker] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "write")

    async with create_client() as client:
        file = args.sticker
        # If it looks like an emoji, search for a sticker with that emoji
        if len(args.sticker) <= 4 and not os.path.exists(args.sticker):
            try:
                result = await client(functions.messages.GetStickersRequest(emoticon=args.sticker, hash=0))
                if result.stickers:
                    file = result.stickers[random.randint(0, min(len(result.stickers) - 1, 9))]
                else:
                    return [TextContent(type="text", text=f"No stickers found for emoji: {args.sticker}")]
            except Exception:
                pass
        elif os.path.exists(args.sticker):
            pass  # local file, send as-is

        result = await client.send_file(
            entity=did,
            file=file,
            reply_to=args.reply_to,
        )
        return [TextContent(type="text", text=f"Sticker sent. id={result.id}")]


### PinMessage ###


class PinMessage(ToolArgs):
    """Pin a message in a dialog/chat/channel."""

    dialog_id: int | str
    message_id: int
    notify: bool = False


@tool_runner.register
async def pin_message(
    args: PinMessage,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[PinMessage] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "write")

    async with create_client() as client:
        await client.pin_message(entity=did, message=args.message_id, notify=args.notify)
        return [TextContent(type="text", text=f"Pinned message {args.message_id}")]


### UnpinMessage ###


class UnpinMessage(ToolArgs):
    """Unpin a message in a dialog/chat/channel. If message_id is 0, unpins all messages."""

    dialog_id: int | str
    message_id: int = 0


@tool_runner.register
async def unpin_message(
    args: UnpinMessage,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[UnpinMessage] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "write")

    async with create_client() as client:
        if args.message_id:
            await client.unpin_message(entity=did, message=args.message_id)
            return [TextContent(type="text", text=f"Unpinned message {args.message_id}")]
        else:
            await client(functions.messages.UnpinAllMessagesRequest(peer=did))
            return [TextContent(type="text", text="Unpinned all messages")]


### GetMessageInfo ###


class GetMessageInfo(ToolArgs):
    """Get detailed info about a specific message including sender, date, reply chain, media type, forwards, and views."""

    dialog_id: int | str
    message_id: int


@tool_runner.register
async def get_message_info(
    args: GetMessageInfo,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[GetMessageInfo] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "read")

    async with create_client() as client:
        msg = await client.get_messages(did, ids=args.message_id)
        if not msg:
            raise ValueError(f"Message {args.message_id} not found")
        if isinstance(msg, list):
            msg = msg[0]

        sender_name = ""
        if msg.sender:
            if hasattr(msg.sender, "first_name"):
                sender_name = f"{msg.sender.first_name or ''} {msg.sender.last_name or ''}".strip()
            elif hasattr(msg.sender, "title"):
                sender_name = msg.sender.title or ""

        info_parts = [
            f"id={msg.id}",
            f"date={msg.date}",
            f"sender={sender_name} (id={msg.sender_id})",
            f"text={msg.text or ''}",
        ]
        if msg.reply_to:
            info_parts.append(f"reply_to_msg_id={msg.reply_to.reply_to_msg_id}")
        if msg.forward:
            info_parts.append(f"forwarded=true")
        if msg.media:
            info_parts.append(f"media={type(msg.media).__name__}")
        if msg.views is not None:
            info_parts.append(f"views={msg.views}")
        if msg.forwards is not None:
            info_parts.append(f"forwards={msg.forwards}")
        if msg.reactions:
            reactions = []
            for r in msg.reactions.results:
                emoji = r.reaction.emoticon if hasattr(r.reaction, "emoticon") else "custom"
                reactions.append(f"{emoji}x{r.count}")
            info_parts.append(f"reactions={','.join(reactions)}")

        return [TextContent(type="text", text="\n".join(info_parts))]


### SearchMessages ###


class SearchMessages(ToolArgs):
    """Search messages in a dialog by text query. Returns matching messages."""

    dialog_id: int | str
    query: str
    limit: int = 20
    from_user: int | str | None = None


@tool_runner.register
async def search_messages(
    args: SearchMessages,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[SearchMessages] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "read")

    async with create_client() as client:
        kwargs: dict[str, t.Any] = {
            "entity": did,
            "search": args.query,
            "limit": args.limit,
        }
        if args.from_user is not None:
            kwargs["from_user"] = resolve_dialog_id(args.from_user)

        response: list[TextContent] = []
        async for message in client.iter_messages(**kwargs):
            if isinstance(message, custom.Message):
                sender = ""
                if message.sender:
                    if hasattr(message.sender, "first_name"):
                        sender = message.sender.first_name or ""
                    elif hasattr(message.sender, "title"):
                        sender = message.sender.title or ""
                text = message.text or f"[media: {type(message.media).__name__}]" if message.media else message.text or ""
                prefix = f"[id={message.id} date={message.date}] {sender}: "
                response.append(TextContent(type="text", text=f"{prefix}{text}"))

        if not response:
            return [TextContent(type="text", text=f"No messages found matching '{args.query}'")]
        return response


### GetChatInfo ###


class GetChatInfo(ToolArgs):
    """Get information about a chat/channel/user including title, members count, description, and photo."""

    dialog_id: int | str


@tool_runner.register
async def get_chat_info(
    args: GetChatInfo,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[GetChatInfo] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "read")

    async with create_client() as client:
        entity = await client.get_entity(did)
        info_parts = [f"id={entity.id}"]

        if hasattr(entity, "title"):
            info_parts.append(f"title={entity.title}")
        if hasattr(entity, "first_name"):
            info_parts.append(f"name={entity.first_name or ''} {getattr(entity, 'last_name', '') or ''}".strip())
        if hasattr(entity, "username") and entity.username:
            info_parts.append(f"username=@{entity.username}")
        if hasattr(entity, "participants_count") and entity.participants_count:
            info_parts.append(f"members={entity.participants_count}")
        if hasattr(entity, "about") and entity.about:
            info_parts.append(f"about={entity.about}")
        if hasattr(entity, "phone") and entity.phone:
            info_parts.append(f"phone={entity.phone}")
        if hasattr(entity, "bot") and entity.bot:
            info_parts.append("is_bot=true")
        if hasattr(entity, "verified") and entity.verified:
            info_parts.append("verified=true")

        return [TextContent(type="text", text="\n".join(info_parts))]


### GetChatMembers ###


class GetChatMembers(ToolArgs):
    """List members/participants of a group or channel."""

    dialog_id: int | str
    limit: int = 50
    search: str = ""


@tool_runner.register
async def get_chat_members(
    args: GetChatMembers,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[GetChatMembers] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "read")

    async with create_client() as client:
        response: list[TextContent] = []
        async for user in client.iter_participants(did, limit=args.limit, search=args.search):
            name = f"{user.first_name or ''} {user.last_name or ''}".strip()
            username = f" @{user.username}" if user.username else ""
            status = ""
            if user.status:
                status = f" status={type(user.status).__name__.replace('UserStatus', '').lower()}"
            response.append(TextContent(type="text", text=f"id={user.id} {name}{username}{status}"))

        if not response:
            return [TextContent(type="text", text="No members found")]
        return response


### SendVoice ###


class SendVoice(ToolArgs):
    """Send a voice message (.ogg opus) to a dialog. Provide a local file path to an audio file."""

    dialog_id: int | str
    file_path: str
    reply_to: int | None = None


@tool_runner.register
async def send_voice(
    args: SendVoice,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[SendVoice] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "write")

    if not os.path.exists(args.file_path):
        raise FileNotFoundError(f"File not found: {args.file_path}")

    async with create_client() as client:
        result = await client.send_file(
            entity=did,
            file=args.file_path,
            voice_note=True,
            reply_to=args.reply_to,
        )
        return [TextContent(type="text", text=f"Voice sent. id={result.id}")]


### SendLocation ###


class SendLocation(ToolArgs):
    """Send a GPS location to a dialog."""

    dialog_id: int | str
    latitude: float
    longitude: float
    reply_to: int | None = None


@tool_runner.register
async def send_location(
    args: SendLocation,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[SendLocation] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "write")

    async with create_client() as client:
        geo = types.InputGeoPoint(lat=args.latitude, long=args.longitude)
        media = types.InputMediaGeoPoint(geo_point=geo)
        result = await client.send_file(
            entity=did,
            file=media,
            reply_to=args.reply_to,
        )
        return [TextContent(type="text", text=f"Location sent. id={result.id}")]


### SendContact ###


class SendContact(ToolArgs):
    """Send a contact card to a dialog."""

    dialog_id: int | str
    phone_number: str
    first_name: str
    last_name: str = ""
    reply_to: int | None = None


@tool_runner.register
async def send_contact(
    args: SendContact,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[SendContact] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "write")

    async with create_client() as client:
        media = types.InputMediaContact(
            phone_number=args.phone_number,
            first_name=args.first_name,
            last_name=args.last_name,
            vcard="",
        )
        result = await client.send_file(
            entity=did,
            file=media,
            reply_to=args.reply_to,
        )
        return [TextContent(type="text", text=f"Contact sent. id={result.id}")]


### SendPoll ###


class SendPoll(ToolArgs):
    """Send a poll to a dialog. Set quiz=true and correct_answer index for quiz mode."""

    dialog_id: int | str
    question: str
    answers: list[str]
    quiz: bool = False
    correct_answer: int | None = None
    multiple_choice: bool = False


@tool_runner.register
async def send_poll(
    args: SendPoll,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[SendPoll] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "write")

    async with create_client() as client:
        poll_answers = [types.PollAnswer(text=types.TextWithEntities(text=a, entities=[]), option=bytes([i])) for i, a in enumerate(args.answers)]
        poll = types.Poll(
            id=0,
            question=types.TextWithEntities(text=args.question, entities=[]),
            answers=poll_answers,
            quiz=args.quiz,
            multiple_choice=args.multiple_choice,
        )
        correct = None
        if args.quiz and args.correct_answer is not None:
            correct = [bytes([args.correct_answer])]
        media = types.InputMediaPoll(poll=poll, correct_answers=correct)
        result = await client(functions.messages.SendMediaRequest(
            peer=did,
            media=media,
            message="",
            random_id=int.from_bytes(os.urandom(8), "big", signed=True),
        ))
        return [TextContent(type="text", text=f"Poll sent.")]


### ListAliases ###


class ListAliases(ToolArgs):
    """List configured chat aliases. Use alias names instead of numeric IDs in any dialog_id field."""


@tool_runner.register
async def list_aliases(
    args: ListAliases,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[ListAliases] args[%s]", args)

    aliases = get_aliases()
    if not aliases:
        return [TextContent(type="text", text="No aliases configured. Run 'xerxes-tg setup' to add them.")]

    lines = [f"{name} => {cid}" for name, cid in aliases.items()]
    return [TextContent(type="text", text="\n".join(lines))]
