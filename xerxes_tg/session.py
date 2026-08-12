"""Encrypted Telethon session storage.

The Telethon ``StringSession`` (a base64-ish blob of auth state) is encrypted
with Fernet. The Fernet key lives in a local file (``session.key``, mode 600)
next to the ciphertext -- not in the OS keychain. Keychain-backed storage was
tried first but proved unworkable: macOS refuses keychain item access with
errSecInteractionNotAllowed (-25308) for any process without an interactive
GUI session to show the authorization prompt, which is exactly the situation
for MCP-server subprocesses and background daemons. File permissions (600,
owner-only) are this tool's actual security boundary anyway, same as every
other credential file in this project (e.g. ufarm-incident-daemon's
watcher-bot.env) -- keychain added a second, less reliable layer on top for
no real benefit in a single-user local-automation context.

The ciphertext is written to ``~/.local/state/xerxes-tg/session.enc``.

If the legacy SQLite session file is present on first use, it is loaded once,
re-saved as an encrypted StringSession, and the plaintext file is deleted.
"""

from __future__ import annotations

import logging
import os

from cryptography.fernet import Fernet, InvalidToken
from telethon import TelegramClient  # type: ignore[import-untyped]
from telethon.sessions import (  # type: ignore[import-untyped]
    SQLiteSession,
    StringSession,
)
from xdg_base_dirs import xdg_state_home  # type: ignore[import-error]

logger = logging.getLogger(__name__)

_STATE_DIR = xdg_state_home() / "xerxes-tg"
_ENC_PATH = _STATE_DIR / "session.enc"
_KEY_PATH = _STATE_DIR / "session.key"
_LEGACY_SQLITE = _STATE_DIR / "xerxes_tg_session.session"
_KEYRING_SERVICE = "xerxes-tg"
_KEYRING_USER = "session-fernet"


def _get_or_create_key() -> bytes:
    if _KEY_PATH.exists():
        return _KEY_PATH.read_bytes()
    new_key = Fernet.generate_key()
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    _KEY_PATH.write_bytes(new_key)
    os.chmod(_KEY_PATH, 0o600)
    logger.info("xerxes-tg: generated new session key at %s", _KEY_PATH)
    return new_key


def _get_key_or_none() -> bytes | None:
    if _KEY_PATH.exists():
        return _KEY_PATH.read_bytes()
    return None


def _get_legacy_keyring_key() -> bytes | None:
    """Read the key used by xerxes-tg <=0.5.1, if the keyring is available."""
    try:
        import keyring
    except ImportError:
        return None
    try:
        value = keyring.get_password(_KEYRING_SERVICE, _KEYRING_USER)
    except keyring.errors.KeyringError:
        logger.debug("xerxes-tg: legacy keyring key is unavailable", exc_info=True)
        return None
    return value.encode() if value else None


def load_session() -> StringSession:
    """Return a ``StringSession`` — empty if nothing is stored yet."""
    if not _ENC_PATH.exists():
        return _load_legacy_session() or StringSession()
    encrypted = _ENC_PATH.read_bytes()
    file_key = _get_key_or_none()
    keyring_key = _get_legacy_keyring_key()
    for source, key in (("session.key", file_key), ("legacy keyring", keyring_key)):
        if not key:
            continue
        try:
            plaintext = Fernet(key).decrypt(encrypted)
        except (InvalidToken, ValueError):
            continue
        if source == "legacy keyring":
            logger.info("xerxes-tg: loaded session.enc with the legacy keyring key")
        return StringSession(plaintext.decode())

    legacy = _load_legacy_session()
    if legacy is not None:
        logger.warning(
            "xerxes-tg: session.enc failed to decrypt — using the legacy SQLite "
            "session in-memory without modifying either file"
        )
        return legacy
    logger.warning("xerxes-tg: session.enc failed to decrypt — starting fresh")
    return StringSession()


def _load_legacy_session() -> StringSession | None:
    """Load the legacy SQLite session without mutating or deleting it."""
    if not _LEGACY_SQLITE.exists():
        return None
    sq_path = _LEGACY_SQLITE.with_suffix("")
    sq = SQLiteSession(str(sq_path))
    try:
        if not sq.auth_key:
            return None
        return StringSession(StringSession.save(sq))
    finally:
        sq.close()


def save_session(client: TelegramClient) -> None:
    """Persist the client's current session as encrypted bytes."""
    key = _get_or_create_key()
    session_str = StringSession.save(client.session)
    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    enc = Fernet(key).encrypt(session_str.encode())
    _ENC_PATH.write_bytes(enc)
    os.chmod(_ENC_PATH, 0o600)


def delete_session() -> None:
    """Remove encrypted session keys from both current and legacy storage."""
    try:
        import keyring
    except ImportError:
        keyring = None
    try:
        if keyring is not None:
            keyring.delete_password(_KEYRING_SERVICE, _KEYRING_USER)
    except keyring.errors.KeyringError:
        logger.debug("xerxes-tg: legacy keyring key was already absent", exc_info=True)
    if _KEY_PATH.exists():
        _KEY_PATH.unlink()
    if _ENC_PATH.exists():
        _ENC_PATH.unlink()


def migrate_legacy_sqlite() -> bool:
    """If a plaintext SQLite session is present and no encrypted one exists,
    convert it once and delete the plaintext file. Returns True if migrated."""
    if _ENC_PATH.exists() or not _LEGACY_SQLITE.exists():
        return False

    _STATE_DIR.mkdir(parents=True, exist_ok=True)
    # SQLiteSession path argument omits the ".session" suffix.
    sq_path = _LEGACY_SQLITE.with_suffix("")
    sq = SQLiteSession(str(sq_path))
    if not sq.auth_key:
        # Session exists but never authenticated — nothing to migrate.
        sq.close()
        return False

    key = _get_or_create_key()
    session_str = StringSession.save(sq)
    enc = Fernet(key).encrypt(session_str.encode())
    _ENC_PATH.write_bytes(enc)
    sq.close()

    # Remove the plaintext artefacts.
    for suffix in ("", "-journal", "-wal", "-shm"):
        p = _LEGACY_SQLITE.with_name(_LEGACY_SQLITE.name + suffix)
        if p.exists():
            p.unlink()
    logger.info("xerxes-tg: migrated sqlite session → encrypted session.enc")
    return True


def keychain_has_key() -> bool:
    return _get_key_or_none() is not None


def encrypted_session_exists() -> bool:
    return _ENC_PATH.exists()


__all__ = [
    "delete_session",
    "encrypted_session_exists",
    "keychain_has_key",
    "load_session",
    "migrate_legacy_sqlite",
    "save_session",
]
