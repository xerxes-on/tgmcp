from __future__ import annotations

import logging
import os
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

from .telegram import create_client

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

    response: list[TextContent] = []
    async with create_client() as client:
        dialog: custom.dialog.Dialog
        async for dialog in client.iter_dialogs(archived=args.archived, ignore_pinned=args.ignore_pinned):
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

    dialog_id: int
    unread: bool = False
    limit: int = 100


@tool_runner.register
async def list_messages(
    args: ListMessages,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    client: TelegramClient
    logger.info("method[ListMessages] args[%s]", args)

    response: list[TextContent] = []
    async with create_client() as client:
        result = await client(functions.messages.GetPeerDialogsRequest(peers=[args.dialog_id]))
        if not result:
            raise ValueError(f"Channel not found: {args.dialog_id}")

        if not isinstance(result, types.messages.PeerDialogs):
            raise TypeError(f"Unexpected result: {type(result)}")

        for dialog in result.dialogs:
            logger.debug("dialog: %s", dialog)
        for message in result.messages:
            logger.debug("message: %s", message)

        iter_messages_args: dict[str, t.Any] = {
            "entity": args.dialog_id,
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

    dialog_id: int
    message: str
    parse_mode: str = "md"
    reply_to: int | None = None
    link_preview: bool = True


@tool_runner.register
async def send_message(
    args: SendMessage,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[SendMessage] args[%s]", args)

    parse_mode = args.parse_mode if args.parse_mode else None
    async with create_client() as client:
        result = await client.send_message(
            entity=args.dialog_id,
            message=args.message,
            parse_mode=parse_mode,
            reply_to=args.reply_to,
            link_preview=args.link_preview,
        )
        return [TextContent(type="text", text=f"Message sent. id={result.id}")]


### EditMessage ###


class EditMessage(ToolArgs):
    """Edit a previously sent message. You can only edit your own messages."""

    dialog_id: int
    message_id: int
    new_text: str
    parse_mode: str = "md"


@tool_runner.register
async def edit_message(
    args: EditMessage,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[EditMessage] args[%s]", args)

    parse_mode = args.parse_mode if args.parse_mode else None
    async with create_client() as client:
        result = await client.edit_message(
            entity=args.dialog_id,
            message=args.message_id,
            text=args.new_text,
            parse_mode=parse_mode,
        )
        return [TextContent(type="text", text=f"Message edited. id={result.id}")]


### DeleteMessages ###


class DeleteMessages(ToolArgs):
    """Delete one or more messages. Provide a list of message IDs to delete."""

    dialog_id: int
    message_ids: list[int]
    revoke: bool = True


@tool_runner.register
async def delete_messages(
    args: DeleteMessages,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[DeleteMessages] args[%s]", args)

    async with create_client() as client:
        result = await client.delete_messages(
            entity=args.dialog_id,
            message_ids=args.message_ids,
            revoke=args.revoke,
        )
        return [TextContent(type="text", text=f"Deleted {len(args.message_ids)} message(s). result={result}")]


### ForwardMessages ###


class ForwardMessages(ToolArgs):
    """Forward messages from one dialog to another."""

    from_dialog_id: int
    to_dialog_id: int
    message_ids: list[int]


@tool_runner.register
async def forward_messages(
    args: ForwardMessages,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[ForwardMessages] args[%s]", args)

    async with create_client() as client:
        result = await client.forward_messages(
            entity=args.to_dialog_id,
            messages=args.message_ids,
            from_peer=args.from_dialog_id,
        )
        ids = [m.id for m in result] if isinstance(result, list) else [result.id]
        return [TextContent(type="text", text=f"Forwarded {len(args.message_ids)} message(s). new_ids={ids}")]


### SendReaction ###


class SendReaction(ToolArgs):
    """React to a message with an emoji. Common reactions: thumbs_up, heart, fire, party, laughing, shocked, sad, 100, rocket, eyes, clap, pray."""

    dialog_id: int
    message_id: int
    emoji: str = "\U0001f44d"


@tool_runner.register
async def send_reaction(
    args: SendReaction,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[SendReaction] args[%s]", args)

    async with create_client() as client:
        await client(
            functions.messages.SendReactionRequest(
                peer=args.dialog_id,
                msg_id=args.message_id,
                reaction=[types.ReactionEmoji(emoticon=args.emoji)],
            )
        )
        return [TextContent(type="text", text=f"Reacted with {args.emoji} to message {args.message_id}")]


### SendFile ###


class SendFile(ToolArgs):
    """Send a file, photo, or video to a dialog. Provide a local file path."""

    dialog_id: int
    file_path: str
    caption: str = ""
    reply_to: int | None = None
    force_document: bool = False


@tool_runner.register
async def send_file(
    args: SendFile,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[SendFile] args[%s]", args)

    if not os.path.exists(args.file_path):
        raise FileNotFoundError(f"File not found: {args.file_path}")

    async with create_client() as client:
        result = await client.send_file(
            entity=args.dialog_id,
            file=args.file_path,
            caption=args.caption,
            reply_to=args.reply_to,
            force_document=args.force_document,
        )
        return [TextContent(type="text", text=f"File sent. id={result.id}")]


### DownloadMedia ###


class DownloadMedia(ToolArgs):
    """Download media from a message. Returns the local file path of the downloaded file."""

    dialog_id: int
    message_id: int
    save_dir: str = ""


@tool_runner.register
async def download_media(
    args: DownloadMedia,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[DownloadMedia] args[%s]", args)

    save_dir = args.save_dir or os.path.join(os.path.expanduser("~"), "Downloads")

    async with create_client() as client:
        messages = await client.get_messages(args.dialog_id, ids=args.message_id)
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

    dialog_id: int
    max_id: int = 0


@tool_runner.register
async def mark_as_read(
    args: MarkAsRead,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[MarkAsRead] args[%s]", args)

    async with create_client() as client:
        await client.send_read_acknowledge(
            entity=args.dialog_id,
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
