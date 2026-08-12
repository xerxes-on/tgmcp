from __future__ import annotations

import asyncio
import json
import logging
import os
import random
import re
import sys
import typing as t
from datetime import datetime, timedelta, timezone
from functools import singledispatch

from mcp.types import (
    EmbeddedResource,
    ImageContent,
    TextContent,
    Tool,
)
from pydantic import BaseModel, ConfigDict
from telethon import TelegramClient, custom, functions, types, utils  # type: ignore[import-untyped]

from .telegram import (
    check_access,
    create_client,
    create_ephemeral_client,
    get_aliases,
    get_allowed_chat_ids,
    resolve_dialog_id,
)


def _parse_send_at(raw: str) -> datetime:
    """Parse a schedule spec into a timezone-aware datetime.

    Accepts ISO-8601 (``2026-04-14T10:00``), relative offsets (``+10m``,
    ``+2h``, ``+3d``), or natural forms (``tomorrow 09:00``, ``today 18:30``).
    Returns a ``datetime`` in the local timezone.
    """
    raw = raw.strip()
    now = datetime.now().astimezone()

    # Relative: +<N><unit>
    m = re.fullmatch(r"\+(\d+)\s*([smhd])", raw, re.IGNORECASE)
    if m:
        n = int(m.group(1))
        unit = m.group(2).lower()
        delta = {"s": timedelta(seconds=n), "m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]
        return now + delta

    # today/tomorrow HH:MM
    m = re.fullmatch(r"(today|tomorrow)\s+(\d{1,2}):(\d{2})", raw, re.IGNORECASE)
    if m:
        offset = 0 if m.group(1).lower() == "today" else 1
        target = now.replace(hour=int(m.group(2)), minute=int(m.group(3)), second=0, microsecond=0) + timedelta(days=offset)
        return target

    # ISO-8601
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as e:
        raise ValueError(
            f"Could not parse send_at {raw!r}. Use ISO-8601 (2026-04-14T10:00), "
            f"relative (+10m, +2h, +3d), or 'tomorrow HH:MM'."
        ) from e
    if dt.tzinfo is None:
        dt = dt.astimezone()
    return dt


def _resolve_entity_ref(entity: int | str) -> int | str:
    if isinstance(entity, int):
        return entity

    raw = entity.strip()
    try:
        return resolve_dialog_id(raw)
    except ValueError:
        if raw.lstrip("-").isdigit():
            return int(raw)
        return raw


async def _get_entity(client: TelegramClient, entity: int | str):  # noqa: ANN202
    if isinstance(entity, str) and not entity.strip().lstrip("-").isdigit():
        try:
            return await client.get_entity(entity.strip())
        except ValueError:
            pass

    return await client.get_entity(_resolve_entity_ref(entity))


def _theme_params(raw: str | None) -> types.DataJSON | None:
    if not raw:
        return None

    try:
        json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError("theme_params_json must be valid JSON") from e

    return types.DataJSON(data=raw)

logger = logging.getLogger(__name__)
_BACKGROUND_UPLOADS: set[asyncio.Task[None]] = set()
_DEFAULT_BACKGROUND_UPLOAD_MB = 0
_AGENT_FOOTER = "\n\n`sent via agent`"


def _add_footer(text: str) -> str:
    return text.rstrip() + _AGENT_FOOTER if text.strip() else "`sent via agent`"


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
    """Send a text message to a dialog, chat or channel.

    Use parse_mode='md' for Markdown or 'html' for HTML formatting.
    Set send_at to schedule the message for later: accepts ISO-8601
    ('2026-04-14T10:00'), relative offsets ('+10m', '+2h', '+3d'),
    or 'tomorrow HH:MM'/'today HH:MM'.
    """

    dialog_id: int | str
    message: str
    parse_mode: str = "md"
    reply_to: int | None = None
    link_preview: bool = True
    send_at: str | None = None


@tool_runner.register
async def send_message(
    args: SendMessage,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[SendMessage] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "write")

    msg = args.message.rstrip() + "\n\n`sent via agent`"
    parse_mode = args.parse_mode if args.parse_mode else None

    schedule_dt: datetime | None = None
    if args.send_at:
        schedule_dt = _parse_send_at(args.send_at)
        if schedule_dt <= datetime.now().astimezone():
            raise ValueError(f"send_at must be in the future (got {schedule_dt.isoformat()})")

    async with create_client() as client:
        result = await client.send_message(
            entity=did,
            message=msg,
            parse_mode=parse_mode,
            reply_to=args.reply_to,
            link_preview=args.link_preview,
            schedule=schedule_dt,
        )
        if schedule_dt is not None:
            return [TextContent(type="text", text=f"Scheduled for {schedule_dt.isoformat()}. id={result.id}")]
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
            text=_add_footer(args.new_text),
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
    wait_for_upload: bool = False


@tool_runner.register
async def send_file(
    args: SendFile,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[SendFile] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "write")

    if not os.path.exists(args.file_path):
        raise FileNotFoundError(f"File not found: {args.file_path}")

    size = os.path.getsize(args.file_path)
    threshold = _background_upload_threshold_bytes()
    if not args.wait_for_upload:
        task = asyncio.create_task(_send_file_background(did, args), name=f"SendFile:{args.file_path}")
        _BACKGROUND_UPLOADS.add(task)
        task.add_done_callback(_finish_background_upload)

        size_mb = size / 1024 / 1024
        threshold_mb = threshold / 1024 / 1024
        return [
            TextContent(
                type="text",
                text=(
                    f"File upload queued in background ({size_mb:.1f} MB; "
                    f"sync threshold {threshold_mb:.1f} MB)."
                ),
            )
        ]

    async with create_client() as client:
        result = await client.send_file(
            entity=did,
            file=args.file_path,
            caption=_add_footer(args.caption),
            reply_to=args.reply_to,
            force_document=args.force_document,
        )
        return [TextContent(type="text", text=f"File sent. id={result.id}")]


def _background_upload_threshold_bytes() -> int:
    raw = os.environ.get("TELEGRAM_BACKGROUND_UPLOAD_THRESHOLD_MB")
    if raw and raw.strip().isdigit():
        return max(1, int(raw.strip())) * 1024 * 1024

    return _DEFAULT_BACKGROUND_UPLOAD_MB * 1024 * 1024


async def _send_file_background(did: int, args: SendFile) -> None:
    async with create_ephemeral_client() as client:
        result = await client.send_file(
            entity=did,
            file=args.file_path,
            caption=_add_footer(args.caption),
            reply_to=args.reply_to,
            force_document=args.force_document,
        )
        logger.info("background SendFile completed file[%s] message_id[%s]", args.file_path, result.id)


def _finish_background_upload(task: asyncio.Task[None]) -> None:
    _BACKGROUND_UPLOADS.discard(task)
    try:
        task.result()
    except Exception:
        logger.exception("background SendFile failed")


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


### LaunchMiniApp ###


class LaunchMiniApp(ToolArgs):
    """Generate an authenticated Telegram Mini App WebView URL.

    Use this when the user asks to open or work with a Telegram Mini App.
    The returned URL is authenticated as the user's Telegram account and
    should be treated as a secret. Open it with browser automation to interact
    with the Mini App UI.

    Modes:
    - auto: choose app if app_short_name is set, web if peer+url are set,
      simple if no peer is set, otherwise main.
    - app: launch a bot app by short name, e.g. t.me/<bot>/<short_name>.
    - main: launch the bot's main/menu Mini App in a peer context.
    - web: launch a specific bot WebView URL in a peer context.
    - simple: launch a simple bot WebView without a peer context.
    """

    bot: int | str
    peer: int | str | None = None
    mode: t.Literal["auto", "app", "main", "web", "simple"] = "auto"
    app_short_name: str | None = None
    url: str | None = None
    start_param: str | None = None
    platform: str = "web"
    write_allowed: bool = False
    compact: bool = False
    fullscreen: bool = True
    theme_params_json: str | None = None


@tool_runner.register
async def launch_mini_app(
    args: LaunchMiniApp,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[LaunchMiniApp] args[%s]", args)

    if args.mode == "app" and not args.app_short_name:
        raise ValueError("app_short_name is required when mode='app'")
    if args.mode in {"main", "web"} and args.peer is None:
        raise ValueError(f"peer is required when mode='{args.mode}'")
    if args.mode == "web" and not args.url:
        raise ValueError("url is required when mode='web'")

    selected_mode = args.mode
    if selected_mode == "auto":
        if args.app_short_name:
            selected_mode = "app"
        elif args.peer is not None and args.url:
            selected_mode = "web"
        elif args.peer is None:
            selected_mode = "simple"
        else:
            selected_mode = "main"

    async with create_client() as client:
        bot_entity = await _get_entity(client, args.bot)
        check_access(utils.get_peer_id(bot_entity), "read")
        bot_input = utils.get_input_user(bot_entity)

        peer_entity = None
        peer_input = None
        if args.peer is not None:
            peer_entity = await _get_entity(client, args.peer)
            check_access(utils.get_peer_id(peer_entity), "read")
            peer_input = utils.get_input_peer(peer_entity)
        elif selected_mode in {"app", "main", "web"}:
            peer_entity = bot_entity
            peer_input = utils.get_input_peer(bot_entity)

        theme_params = _theme_params(args.theme_params_json)

        if selected_mode == "app":
            if peer_input is None:
                raise ValueError("peer is required to launch a bot app by short name")
            result = await client(
                functions.messages.RequestAppWebViewRequest(
                    peer=peer_input,
                    app=types.InputBotAppShortName(
                        bot_id=bot_input,
                        short_name=args.app_short_name or "",
                    ),
                    platform=args.platform,
                    write_allowed=args.write_allowed,
                    compact=args.compact,
                    fullscreen=args.fullscreen,
                    start_param=args.start_param,
                    theme_params=theme_params,
                )
            )
        elif selected_mode == "main":
            if peer_input is None:
                raise ValueError("peer is required to launch the main bot Mini App")
            result = await client(
                functions.messages.RequestMainWebViewRequest(
                    peer=peer_input,
                    bot=bot_input,
                    platform=args.platform,
                    compact=args.compact,
                    fullscreen=args.fullscreen,
                    start_param=args.start_param,
                    theme_params=theme_params,
                )
            )
        elif selected_mode == "web":
            if peer_input is None:
                raise ValueError("peer is required to launch a peer WebView")
            result = await client(
                functions.messages.RequestWebViewRequest(
                    peer=peer_input,
                    bot=bot_input,
                    platform=args.platform,
                    url=args.url,
                    compact=args.compact,
                    fullscreen=args.fullscreen,
                    start_param=args.start_param,
                    theme_params=theme_params,
                )
            )
        elif selected_mode == "simple":
            result = await client(
                functions.messages.RequestSimpleWebViewRequest(
                    bot=bot_input,
                    platform=args.platform,
                    url=args.url,
                    start_param=args.start_param,
                    compact=args.compact,
                    fullscreen=args.fullscreen,
                    theme_params=theme_params,
                )
            )
        else:
            raise ValueError(f"Unsupported Mini App mode: {selected_mode}")

        webview_url = getattr(result, "url", None)
        if not webview_url:
            raise TypeError(f"Telegram returned {type(result).__name__} without a url")

        peer_id = utils.get_peer_id(peer_entity) if peer_entity is not None else ""
        return [
            TextContent(
                type="text",
                text=(
                    "Authenticated Telegram Mini App URL generated.\n"
                    "Treat this URL as secret; it is authenticated as the Telegram account.\n"
                    f"mode={selected_mode} bot_id={utils.get_peer_id(bot_entity)} peer_id={peer_id}\n"
                    f"url={webview_url}"
                ),
            )
        ]


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


### SearchAllMessages ###


class SearchAllMessages(ToolArgs):
    """Global search across every chat reachable by the configured ACL.

    Wraps Telegram's server-side global search and filters out results from
    dialogs outside the read/write allowlist.
    """

    query: str
    limit: int = 30


@tool_runner.register
async def search_all_messages(
    args: SearchAllMessages,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[SearchAllMessages] args[%s]", args)

    allowed = get_allowed_chat_ids()

    response: list[TextContent] = []
    async with create_client() as client:
        async for message in client.iter_messages(entity=None, search=args.query, limit=args.limit):
            chat_id = message.chat_id if hasattr(message, "chat_id") else None
            if allowed is not None and chat_id not in allowed:
                continue
            sender = ""
            if message.sender:
                if hasattr(message.sender, "first_name"):
                    sender = message.sender.first_name or ""
                elif hasattr(message.sender, "title"):
                    sender = message.sender.title or ""
            text = message.text or (f"[media: {type(message.media).__name__}]" if message.media else "")
            response.append(
                TextContent(
                    type="text",
                    text=f"[chat={chat_id} id={message.id} date={message.date}] {sender}: {text}",
                )
            )

    if not response:
        return [TextContent(type="text", text=f"No messages found matching '{args.query}'")]
    return response


### GetThread ###


class GetThread(ToolArgs):
    """Return the full reply-thread containing a given message.

    Walks upward from ``message_id`` to find the thread root, then fetches the
    discussion under it. Output is ordered from oldest to newest, indented by
    reply depth.
    """

    dialog_id: int | str
    message_id: int
    depth: int = 50


@tool_runner.register
async def get_thread(
    args: GetThread,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[GetThread] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "read")

    async with create_client() as client:
        # Walk upward to find root.
        root_id = args.message_id
        seen: set[int] = set()
        while True:
            if root_id in seen:
                break
            seen.add(root_id)
            msg = await client.get_messages(did, ids=root_id)
            if not msg:
                break
            if isinstance(msg, list):
                msg = msg[0]
            if not getattr(msg, "reply_to", None) or not msg.reply_to.reply_to_msg_id:
                break
            root_id = msg.reply_to.reply_to_msg_id

        # Collect the root + its replies (if any) via GetRepliesRequest.
        collected: list[custom.Message] = []
        try:
            root_msg = await client.get_messages(did, ids=root_id)
            if root_msg:
                if isinstance(root_msg, list):
                    root_msg = root_msg[0]
                collected.append(root_msg)
        except Exception:
            pass

        try:
            async for m in client.iter_messages(did, reply_to=root_id, limit=args.depth):
                collected.append(m)
        except Exception:
            # Not all chats support threaded replies (e.g. private DMs).
            # Fall back to scanning recent messages for reply_to == root_id.
            async for m in client.iter_messages(did, limit=200):
                if m.reply_to and m.reply_to.reply_to_msg_id == root_id:
                    collected.append(m)

        # De-dup and sort by id asc.
        uniq: dict[int, custom.Message] = {}
        for m in collected:
            uniq[m.id] = m
        ordered = sorted(uniq.values(), key=lambda m: m.id)

        # Compute depth per message (1 for root, 2+ for nested replies).
        depth_by_id: dict[int, int] = {root_id: 0}
        for m in ordered:
            if m.id == root_id:
                continue
            parent = m.reply_to.reply_to_msg_id if m.reply_to else root_id
            depth_by_id[m.id] = depth_by_id.get(parent, 0) + 1

        response: list[TextContent] = []
        for m in ordered:
            sender = ""
            if m.sender:
                if hasattr(m.sender, "first_name"):
                    sender = m.sender.first_name or ""
                elif hasattr(m.sender, "title"):
                    sender = m.sender.title or ""
            text = m.text or (f"[media: {type(m.media).__name__}]" if m.media else "")
            indent = "  " * depth_by_id.get(m.id, 0)
            response.append(
                TextContent(
                    type="text",
                    text=f"{indent}[id={m.id}] {sender}: {text}",
                )
            )

    if not response:
        return [TextContent(type="text", text=f"No thread found for message {args.message_id}")]
    return response


### ReplyTo ###


class ReplyTo(ToolArgs):
    """Find the most recent message matching a predicate and reply to it.

    Specify any combination of:
    - ``from_user``: only messages from this user (alias or numeric id)
    - ``contains``: substring that must appear in the message text
    - ``since_seconds``: only messages newer than N seconds ago
    - ``unread_only``: only consider unread messages

    The latest match wins. Fails with a clear error if no message matches.
    """

    dialog_id: int | str
    text: str
    from_user: int | str | None = None
    contains: str | None = None
    since_seconds: int | None = None
    unread_only: bool = False
    parse_mode: str = "md"


@tool_runner.register
async def reply_to(
    args: ReplyTo,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[ReplyTo] args[%s]", args)
    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "write")

    iter_kwargs: dict[str, t.Any] = {"entity": did, "limit": 200}
    if args.from_user is not None:
        iter_kwargs["from_user"] = resolve_dialog_id(args.from_user)
    if args.contains:
        iter_kwargs["search"] = args.contains

    cutoff_dt: datetime | None = None
    if args.since_seconds is not None:
        cutoff_dt = datetime.now(timezone.utc) - timedelta(seconds=args.since_seconds)

    matched: custom.Message | None = None
    async with create_client() as client:
        if args.unread_only:
            # Limit to actually unread messages by consulting dialog unread_count.
            async for dialog in client.iter_dialogs():
                if dialog.id == did:
                    iter_kwargs["limit"] = max(1, min(dialog.unread_count or 1, 200))
                    break

        async for msg in client.iter_messages(**iter_kwargs):
            if cutoff_dt and msg.date < cutoff_dt:
                break  # iter_messages is newest-first; older messages can't match.
            matched = msg
            break

        if matched is None:
            raise ValueError("No message matched the given predicate.")

        parse_mode = args.parse_mode if args.parse_mode else None
        body = args.text.rstrip() + "\n\n`sent via agent`"
        result = await client.send_message(
            entity=did,
            message=body,
            parse_mode=parse_mode,
            reply_to=matched.id,
        )
        return [
            TextContent(
                type="text",
                text=f"Replied to id={matched.id}. new_id={result.id}",
            )
        ]


### SearchLocal ###


class SearchLocal(ToolArgs):
    """Full-text search over the local mirror db.

    Run ``xerxes-tg sync`` first to populate the mirror. Results include
    messages from any chat the mirror has indexed (within your read ACL).
    """

    query: str
    dialog_id: int | str | None = None
    limit: int = 50
    since_hours: int | None = None


@tool_runner.register
async def search_local(
    args: SearchLocal,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    logger.info("method[SearchLocal] args[%s]", args)
    from . import mirror

    did = resolve_dialog_id(args.dialog_id) if args.dialog_id is not None else None
    allowed = get_allowed_chat_ids()

    rows = mirror.search(
        query=args.query,
        dialog_id=did,
        limit=args.limit,
        since_hours=args.since_hours,
    )
    response: list[TextContent] = []
    for row in rows:
        if allowed is not None and row["dialog_id"] not in allowed:
            continue
        ts = datetime.fromtimestamp(row["ts"], timezone.utc).isoformat()
        response.append(
            TextContent(
                type="text",
                text=f"[chat={row['dialog_id']} id={row['message_id']} date={ts}] {row['sender_name']}: {row['text']}",
            )
        )
    if not response:
        return [TextContent(type="text", text=f"No local matches for {args.query!r}. Run `xerxes-tg sync --all` first.")]
    return response


### Temporary Watches ###


class StartWatch(ToolArgs):
    """Start a durable temporary watch for incoming Telegram messages.

    The watch survives the MCP process and is handled by a background daemon.
    It expires after ``idle_timeout_seconds`` without a matching message and
    always stops at ``max_duration_seconds``. Use ``mode='once'`` to stop after
    the first match, or ``mode='conversation'`` to renew the idle deadline on
    every match. Webhook destinations and secrets come only from local config.
    """

    dialog_id: int | str
    after_message_id: int | None = None
    reply_to_message_id: int | None = None
    sender_id: int | None = None
    mode: t.Literal["once", "conversation"] = "conversation"
    idle_timeout_seconds: int = 3600
    max_duration_seconds: int = 86400


@tool_runner.register
async def start_watch_tool(
    args: StartWatch,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    from . import watch_store, watcher
    from .webhook import WebhookConfig

    did = resolve_dialog_id(args.dialog_id)
    check_access(did, "read")
    if args.after_message_id is not None and args.after_message_id < 1:
        raise ValueError("after_message_id must be positive")
    if args.reply_to_message_id is not None and args.reply_to_message_id < 1:
        raise ValueError("reply_to_message_id must be positive")
    if args.idle_timeout_seconds < 30:
        raise ValueError("idle_timeout_seconds must be at least 30")
    if args.max_duration_seconds < args.idle_timeout_seconds:
        raise ValueError("max_duration_seconds must be at least idle_timeout_seconds")

    watch = await watch_store.start_watch(
        dialog_id=did,
        after_message_id=args.after_message_id,
        reply_to_message_id=args.reply_to_message_id,
        sender_id=args.sender_id,
        mode=args.mode,
        idle_timeout_seconds=args.idle_timeout_seconds,
        max_duration_seconds=args.max_duration_seconds,
    )
    try:
        pid = await asyncio.to_thread(watcher.ensure_running)
    except Exception:
        await watch_store.stop_watch(watch["watch_id"], reason="watcher_start_failed")
        raise

    config = WebhookConfig.load()
    result = {
        **watch,
        "watcher_pid": pid,
        "webhook_configured": config.enabled,
        "polling_fallback": {
            "tool": "GetWatchEvents",
            "watch_id": watch["watch_id"],
            "after_sequence": 0,
        },
    }
    if not config.enabled:
        result["warning"] = (
            "Webhook delivery is disabled until XERXES_TG_WEBHOOK_URL and "
            "XERXES_TG_WEBHOOK_SECRET are configured; events remain pollable."
        )
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


class StopWatch(ToolArgs):
    """Stop one temporary Telegram watch immediately."""

    watch_id: str


@tool_runner.register
async def stop_watch_tool(
    args: StopWatch,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    from . import watch_store

    watch = await watch_store.stop_watch(args.watch_id)
    if watch is None:
        raise ValueError(f"Unknown watch_id: {args.watch_id}")
    return [TextContent(type="text", text=json.dumps(watch, ensure_ascii=False))]


class ListWatches(ToolArgs):
    """List temporary Telegram watches and their lifecycle state."""

    include_inactive: bool = False


@tool_runner.register
async def list_watches_tool(
    args: ListWatches,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    from . import watch_store, watcher
    from .webhook import WebhookConfig

    watches = await watch_store.list_watches(include_inactive=args.include_inactive)
    result = {
        "watcher_running": watcher.is_running(),
        "watcher_ready": watcher.is_ready(),
        "webhook_configured": WebhookConfig.load().enabled,
        "watches": watches,
    }
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]


class GetWatchEvents(ToolArgs):
    """Poll durable events for a watch when webhook delivery is unavailable.

    Pass the highest previously seen ``sequence`` as ``after_sequence`` to
    receive only newer events. This operation does not delete or acknowledge
    events, so repeated calls are safe.
    """

    watch_id: str
    after_sequence: int = 0
    limit: int = 100


@tool_runner.register
async def get_watch_events_tool(
    args: GetWatchEvents,
) -> t.Sequence[TextContent | ImageContent | EmbeddedResource]:
    from . import watch_store

    if args.after_sequence < 0:
        raise ValueError("after_sequence cannot be negative")
    if not 1 <= args.limit <= 500:
        raise ValueError("limit must be between 1 and 500")
    events = await watch_store.get_events(
        args.watch_id,
        after_sequence=args.after_sequence,
        limit=args.limit,
    )
    result = {
        "watch_id": args.watch_id,
        "events": events,
        "next_after_sequence": events[-1]["sequence"] if events else args.after_sequence,
    }
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]
