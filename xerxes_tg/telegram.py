# ruff: noqa: T201
from __future__ import annotations

import logging
import shutil
from functools import cache
from getpass import getpass
from pathlib import Path

from pydantic_settings import BaseSettings
from telethon import TelegramClient  # type: ignore[import-untyped]
from telethon.errors.rpcerrorlist import SessionPasswordNeededError  # type: ignore[import-untyped]
from telethon.tl.types import User  # type: ignore[import-untyped]
from xdg_base_dirs import xdg_config_home, xdg_state_home  # type: ignore[import-error]

logger = logging.getLogger(__name__)

CONFIG_DIR = xdg_config_home() / "xerxes-tg"
CONFIG_ENV = CONFIG_DIR / "config.env"

_LEGACY_CONFIG_DIR = xdg_config_home() / "mcp-telegram"
_LEGACY_STATE_DIR = xdg_state_home() / "mcp-telegram"
_SESSION_NAME = "xerxes_tg_session"
_LEGACY_SESSION_NAME = "mcp_telegram_session"

_MIGRATED = False


def _migrate_legacy_paths() -> None:
    """One-time migration from ~/.config/mcp-telegram → ~/.config/xerxes-tg.

    Copies config and session files, stripping legacy TELEGRAM_LISTENER_* keys.
    Legacy directories are left in place as a safety net.
    """
    global _MIGRATED  # noqa: PLW0603
    if _MIGRATED:
        return
    _MIGRATED = True

    # --- config ---
    legacy_cfg = _LEGACY_CONFIG_DIR / "config.env"
    if not CONFIG_ENV.exists() and legacy_cfg.exists():
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        raw = legacy_cfg.read_text()
        cleaned = "\n".join(
            line for line in raw.splitlines() if not line.lstrip().startswith("TELEGRAM_LISTENER_")
        )
        if not cleaned.endswith("\n"):
            cleaned += "\n"
        CONFIG_ENV.write_text(cleaned)
        logger.info("xerxes-tg: migrated config from %s to %s", _LEGACY_CONFIG_DIR, CONFIG_DIR)

    # --- session ---
    new_state = xdg_state_home() / "xerxes-tg"
    new_session = new_state / f"{_SESSION_NAME}.session"
    legacy_session = _LEGACY_STATE_DIR / f"{_LEGACY_SESSION_NAME}.session"
    if not new_session.exists() and legacy_session.exists():
        new_state.mkdir(parents=True, exist_ok=True)
        shutil.copy2(legacy_session, new_session)
        for suffix in ("-journal", "-wal", "-shm"):
            extra = legacy_session.with_name(legacy_session.name + suffix)
            if extra.exists():
                shutil.copy2(
                    extra,
                    new_state / extra.name.replace(_LEGACY_SESSION_NAME, _SESSION_NAME),
                )
        logger.info("xerxes-tg: migrated session from %s to %s", legacy_session, new_session)


# Run migration at import time so settings pick up the new location.
_migrate_legacy_paths()

# Migrate legacy plaintext SQLite session to encrypted StringSession.
try:
    from . import session as _session_module

    _session_module.migrate_legacy_sqlite()
except Exception:  # keyring or crypto may fail in constrained envs
    logger.exception("xerxes-tg: session migration failed — continuing with plaintext session")


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


class TelegramSettings(BaseSettings):
    api_id: str = ""
    api_hash: str = ""
    read_chats: str = ""
    write_chats: str = ""
    aliases: str = ""

    class Config:
        env_prefix = "TELEGRAM_"
        env_file = str(CONFIG_ENV) if CONFIG_ENV.exists() else ".env"
        extra = "ignore"

    @property
    def read_chat_ids(self) -> list[int]:
        return _parse_chat_ids(self.read_chats)

    @property
    def write_chat_ids(self) -> list[int]:
        return _parse_chat_ids(self.write_chats)

    @property
    def alias_map(self) -> dict[str, int]:
        return _parse_aliases(self.aliases)


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
    from . import session as _session

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

    _session.save_session(user_session)

    user = await user_session.get_me()
    if isinstance(user, User):
        print(f"Hey {user.username}! You are connected!")
    else:
        print("Connected!")
    print("You can now use the xerxes-tg server.")


async def logout_from_telegram() -> None:
    from . import session as _session

    user_session = create_client()
    await user_session.connect()
    await user_session.log_out()
    _session.delete_session()
    print("You are now logged out from Telegram.")


@cache
def create_client(
    api_id: str | None = None,
    api_hash: str | None = None,
    session_name: str = _SESSION_NAME,  # noqa: ARG001 — retained for API compat
) -> TelegramClient:
    return create_ephemeral_client(api_id=api_id, api_hash=api_hash)


def create_ephemeral_client(
    api_id: str | None = None,
    api_hash: str | None = None,
) -> TelegramClient:
    from . import session as _session

    if api_id is not None and api_hash is not None:
        config = TelegramSettings(api_id=api_id, api_hash=api_hash)
    else:
        config = TelegramSettings()
    return TelegramClient(
        _session.load_session(),
        config.api_id,
        config.api_hash,
        base_logger="telethon",
    )


def persist_session(client: TelegramClient) -> None:
    """Encrypt and save the client's current session state."""
    from . import session as _session

    _session.save_session(client)
