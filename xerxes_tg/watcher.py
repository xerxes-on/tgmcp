"""Background Telethon watcher for durable temporary subscriptions."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sqlite3
import subprocess
import sys
import time

from telethon import events  # type: ignore[import-untyped]
from xdg_base_dirs import xdg_state_home  # type: ignore[import-error]

from . import watch_store
from .telegram import create_client
from .webhook import WebhookConfig, deliver_due_once

logger = logging.getLogger(__name__)

STATE_DIR = xdg_state_home() / "xerxes-tg"
WATCHER_PID = STATE_DIR / "watcher.pid"
WATCHER_LOG = STATE_DIR / "watcher.log"
WATCHER_READY = STATE_DIR / "watcher.ready"


def read_pid() -> int | None:
    try:
        return int(WATCHER_PID.read_text().strip())
    except (FileNotFoundError, OSError, ValueError):
        return None


def is_running() -> bool:
    pid = read_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        WATCHER_PID.unlink(missing_ok=True)
        return False
    except PermissionError:
        return True


def _write_pid() -> None:
    STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(STATE_DIR, 0o700)
    WATCHER_READY.unlink(missing_ok=True)
    WATCHER_PID.write_text(str(os.getpid()))
    os.chmod(WATCHER_PID, 0o600)


def _remove_pid() -> None:
    WATCHER_PID.unlink(missing_ok=True)
    WATCHER_READY.unlink(missing_ok=True)


def is_ready() -> bool:
    if not is_running() or not WATCHER_READY.exists():
        return False
    try:
        return int(WATCHER_READY.read_text().strip()) == read_pid()
    except (OSError, TypeError, ValueError):
        return False


def ensure_running(*, wait_seconds: float = 10.0) -> int:
    """Start a detached watcher process if needed and return its PID."""
    if is_ready():
        pid = read_pid()
        if pid is None:  # pragma: no cover - race guard
            raise RuntimeError("watcher is running but its PID is unavailable")
        return pid

    if not is_running():
        STATE_DIR.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(STATE_DIR, 0o700)
        log_handle = WATCHER_LOG.open("ab")
        os.chmod(WATCHER_LOG, 0o600)
        try:
            subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "from xerxes_tg.watcher import main; main()",
                ],
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=log_handle,
                start_new_session=True,
                close_fds=True,
            )
        finally:
            log_handle.close()

    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if is_ready():
            pid = read_pid()
            if pid is not None:
                return pid
        time.sleep(0.05)
    if is_running():
        stop_process()
    raise RuntimeError(f"watcher failed to become ready; see {WATCHER_LOG}")


def stop_process(*, wait_seconds: float = 5.0) -> bool:
    pid = read_pid()
    if pid is None or not is_running():
        return False
    os.kill(pid, signal.SIGTERM)
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if not is_running():
            return True
        time.sleep(0.1)
    return not is_running()


async def _delivery_loop(stop_event: asyncio.Event) -> None:
    config = WebhookConfig.load()
    if not config.enabled:
        logger.warning(
            "webhook delivery disabled; configure XERXES_TG_WEBHOOK_URL and "
            "XERXES_TG_WEBHOOK_SECRET (events remain pollable)"
        )
        await stop_event.wait()
        return

    try:
        config.validate()
    except ValueError:
        logger.exception("invalid webhook configuration; events remain pollable")
        await stop_event.wait()
        return
    while not stop_event.is_set():
        try:
            delivered, failed = await deliver_due_once(config)
        except (OSError, RuntimeError, sqlite3.Error, ValueError):
            logger.exception("webhook delivery batch failed; will retry")
            delivered, failed = 0, 0
        if delivered or failed:
            logger.info("webhook batch delivered=%d failed=%d", delivered, failed)
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=1.0)
        except TimeoutError:
            pass


async def _expiry_loop(stop_event: asyncio.Event) -> None:
    last_pruned = 0.0
    while not stop_event.is_set():
        try:
            expired = await watch_store.expire_stale()
            if expired:
                logger.info("expired %d inactive watch(es)", expired)
            if time.monotonic() - last_pruned >= 3600:
                events, watches = await watch_store.prune_terminal_state()
                if events or watches:
                    logger.info("pruned old watcher state events=%d watches=%d", events, watches)
                last_pruned = time.monotonic()
        except (OSError, sqlite3.Error):
            logger.exception("watcher state maintenance failed; will retry")
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=30.0)
        except TimeoutError:
            pass


async def run_daemon() -> None:
    if is_running():
        raise RuntimeError(f"watcher is already running (PID {read_pid()})")
    _write_pid()
    client = create_client()
    try:
        await watch_store.init()
        await client.connect()
        if not await client.is_user_authorized():
            raise RuntimeError("Telegram session is not authorized; run xerxes-tg sign-in first")
    except Exception:
        await client.disconnect()
        _remove_pid()
        raise

    @client.on(events.NewMessage())
    async def _on_message(event: events.NewMessage.Event) -> None:
        if getattr(event, "out", False) or event.message is None or event.chat_id is None:
            return
        message = event.message
        sender = await event.get_sender()
        first = getattr(sender, "first_name", "") or ""
        last = getattr(sender, "last_name", "") or ""
        username = getattr(sender, "username", "") or ""
        sender_name = f"{first} {last}".strip() or username or str(getattr(sender, "id", "Unknown"))
        matched = await watch_store.record_incoming(
            dialog_id=int(event.chat_id),
            message_id=message.id,
            reply_to_message_id=message.reply_to_msg_id,
            sender_id=getattr(sender, "id", None),
            sender_name=sender_name,
            text=message.text or message.message or "",
            received_at=int(time.time()),
        )
        if matched:
            logger.info(
                "matched message chat=%s message=%s watches=%d",
                event.chat_id,
                message.id,
                len(matched),
            )

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:  # pragma: no cover - Windows fallback
            signal.signal(sig, lambda *_: loop.call_soon_threadsafe(stop_event.set))

    delivery_task = asyncio.create_task(_delivery_loop(stop_event))
    expiry_task = asyncio.create_task(_expiry_loop(stop_event))
    client_task = asyncio.create_task(client.run_until_disconnected())
    stop_task = asyncio.create_task(stop_event.wait())
    WATCHER_READY.write_text(str(os.getpid()))
    os.chmod(WATCHER_READY, 0o600)
    logger.info("watcher ready pid=%d", os.getpid())
    try:
        await asyncio.wait({client_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
    finally:
        stop_event.set()
        await client.disconnect()
        for task in (delivery_task, expiry_task, client_task, stop_task):
            task.cancel()
        await asyncio.gather(delivery_task, expiry_task, client_task, stop_task, return_exceptions=True)
        _remove_pid()
        logger.info("watcher stopped")


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(run_daemon())


if __name__ == "__main__":
    main()


__all__ = [
    "WATCHER_LOG",
    "WATCHER_PID",
    "WATCHER_READY",
    "ensure_running",
    "is_ready",
    "is_running",
    "read_pid",
    "run_daemon",
    "stop_process",
]
