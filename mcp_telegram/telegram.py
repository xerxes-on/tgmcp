# ruff: noqa: T201
from __future__ import annotations

from functools import cache
from getpass import getpass
from pathlib import Path

from pydantic_settings import BaseSettings
from telethon import TelegramClient  # type: ignore[import-untyped]
from telethon.errors.rpcerrorlist import SessionPasswordNeededError  # type: ignore[import-untyped]
from telethon.tl.types import User  # type: ignore[import-untyped]
from xdg_base_dirs import xdg_config_home, xdg_state_home  # type: ignore[import-error]

CONFIG_DIR = xdg_config_home() / "mcp-telegram"
CONFIG_ENV = CONFIG_DIR / "config.env"
LISTENER_MODE_ALIASES = {
    "read": "read",
    "read-only": "read",
    "readonly": "read",
    "ask": "ask",
    "ask-first": "ask",
    "always-ask-first": "ask",
    "auto": "auto",
    "auto-reply": "auto",
    "always-auto-reply": "auto",
    "decide": "decide",
    "agent-decides": "decide",
}


def _parse_chat_ids(raw: str) -> list[int]:
    if not raw or not raw.strip():
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def _parse_aliases(raw: str) -> dict[str, int]:
    """Parse 'name:id,name:id' into {name: id}."""
    aliases: dict[str, int] = {}
    if not raw or not raw.strip():
        return aliases
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" in pair:
            name, _, cid = pair.partition(":")
            name = name.strip().lower()
            if name and cid.strip().lstrip("-").isdigit():
                aliases[name] = int(cid.strip())
    return aliases


def _resolve_dialog_id_raw(dialog_id: int | str, aliases: dict[str, int]) -> int:
    if isinstance(dialog_id, int):
        return dialog_id

    key = dialog_id.strip().lower()
    if key in aliases:
        return aliases[key]

    return int(dialog_id)


def _parse_listener_chats(raw: str, aliases: dict[str, int]) -> dict[int, str]:
    mapping: dict[int, str] = {}
    if not raw or not raw.strip():
        return mapping

    for pair in raw.split(","):
        item = pair.strip()
        if not item or "=" not in item:
            continue

        dialog_id, _, mode = item.partition("=")
        normalized_mode = LISTENER_MODE_ALIASES.get(mode.strip().lower())
        if not normalized_mode:
            continue

        try:
            resolved_id = _resolve_dialog_id_raw(dialog_id.strip(), aliases)
        except ValueError:
            continue

        mapping[resolved_id] = normalized_mode

    return mapping


class TelegramSettings(BaseSettings):
    api_id: str = ""
    api_hash: str = ""
    read_chats: str = ""
    write_chats: str = ""
    aliases: str = ""
    listener_enabled: bool = False
    listener_chats: str = ""
    listener_approval_chat: str = ""
    listener_agent_cmd: str = ""
    listener_system_prompt: str = ""
    listener_claude_path: str = "claude"
    listener_claude_cwd: str = ""
    listener_timeout: int = 120
    listener_context_messages: int = 7
    listener_rate_limit_count: int = 5
    listener_rate_limit_window: int = 300
    listener_cooldown: int = 5
    listener_approval_ttl: int = 3600

    class Config:
        env_prefix = "TELEGRAM_"
        env_file = str(CONFIG_ENV) if CONFIG_ENV.exists() else ".env"

    @property
    def read_chat_ids(self) -> list[int]:
        return _parse_chat_ids(self.read_chats)

    @property
    def write_chat_ids(self) -> list[int]:
        return _parse_chat_ids(self.write_chats)

    @property
    def alias_map(self) -> dict[str, int]:
        return _parse_aliases(self.aliases)

    @property
    def listener_chat_modes(self) -> dict[int, str]:
        return _parse_listener_chats(self.listener_chats, self.alias_map)


@cache
def get_settings() -> TelegramSettings:
    return TelegramSettings()


def check_access(dialog_id: int, mode: str) -> None:
    """Raise PermissionError if dialog_id is not allowed for the given mode ('read' or 'write')."""
    settings = get_settings()
    read_ids = settings.read_chat_ids
    write_ids = settings.write_chat_ids
    if not read_ids and not write_ids:
        return  # unrestricted

    if mode == "write":
        if dialog_id not in write_ids:
            raise PermissionError(f"Chat {dialog_id} is not in TELEGRAM_WRITE_CHATS")
    elif mode == "read":
        allowed = set(read_ids) | set(write_ids)  # write implies read
        if dialog_id not in allowed:
            raise PermissionError(f"Chat {dialog_id} is not in TELEGRAM_READ_CHATS or TELEGRAM_WRITE_CHATS")


def resolve_dialog_id(dialog_id: int | str) -> int:
    """Resolve a dialog_id that may be an alias string or an int."""
    try:
        return _resolve_dialog_id_raw(dialog_id, get_settings().alias_map)
    except ValueError:
        aliases = get_settings().alias_map
        available = ", ".join(aliases.keys()) if aliases else "(none configured)"
        raise ValueError(f"Unknown alias '{dialog_id}'. Available aliases: {available}")


def get_aliases() -> dict[str, int]:
    """Return the alias map {name: chat_id}."""
    return get_settings().alias_map


def get_allowed_chat_ids() -> set[int] | None:
    """Return union of read+write chat IDs, or None if unrestricted."""
    settings = get_settings()
    read_ids = settings.read_chat_ids
    write_ids = settings.write_chat_ids
    if not read_ids and not write_ids:
        return None
    return set(read_ids) | set(write_ids)


async def connect_to_telegram(api_id: str, api_hash: str, phone_number: str) -> None:
    user_session = create_client(api_id=api_id, api_hash=api_hash)
    await user_session.connect()

    result = await user_session.send_code_request(phone_number)
    code = input("Enter login code: ")
    try:
        await user_session.sign_in(
            phone=phone_number,
            code=code,
            phone_code_hash=result.phone_code_hash,
        )
    except SessionPasswordNeededError:
        password = getpass("Enter 2FA password: ")
        await user_session.sign_in(password=password)

    user = await user_session.get_me()
    if isinstance(user, User):
        print(f"Hey {user.username}! You are connected!")
    else:
        print("Connected!")
    print("You can now use the mcp-telegram server.")


async def logout_from_telegram() -> None:
    user_session = create_client()
    await user_session.connect()
    await user_session.log_out()
    print("You are now logged out from Telegram.")


@cache
def create_client(
    api_id: str | None = None,
    api_hash: str | None = None,
    session_name: str = "mcp_telegram_session",
) -> TelegramClient:
    if api_id is not None and api_hash is not None:
        config = TelegramSettings(api_id=api_id, api_hash=api_hash)
    else:
        config = TelegramSettings()
    state_home = xdg_state_home() / "mcp-telegram"
    state_home.mkdir(parents=True, exist_ok=True)
    return TelegramClient(state_home / session_name, config.api_id, config.api_hash, base_logger="telethon")


def create_listener_client() -> TelegramClient:
    """Create a separate (non-cached) Telethon client for the listener.

    Uses the same session file so no re-authentication is needed,
    but a distinct client instance so connect/disconnect from MCP tools
    doesn't kill the listener's long-lived connection.
    """
    config = TelegramSettings()
    state_home = xdg_state_home() / "mcp-telegram"
    state_home.mkdir(parents=True, exist_ok=True)
    return TelegramClient(
        state_home / "mcp_telegram_session",
        config.api_id,
        config.api_hash,
        base_logger="telethon.listener",
    )
