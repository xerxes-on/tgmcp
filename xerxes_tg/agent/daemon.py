from __future__ import annotations

import asyncio
import logging
import os
import signal
import time

import anthropic
from rich.console import Console

from .. import telegram as tg_module
from . import memory, notifier, personality, pidfile
from .buffer import DebounceBuffer
from .config import AgentConfig
from .listener import Deps, make_flush_callback, register

console = Console()
logger = logging.getLogger(__name__)


def _read_config_env_var(name: str) -> str:
    """Read a variable from config.env, stripping quotes."""
    from ..telegram import CONFIG_ENV
    if CONFIG_ENV.exists():
        for line in CONFIG_ENV.read_text().splitlines():
            line = line.strip()
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip("'\"")
    return ""


def _resolve_anthropic_key() -> str:
    return os.environ.get("ANTHROPIC_API_KEY", "").strip() or _read_config_env_var("ANTHROPIC_API_KEY")


def _resolve_anthropic_base_url() -> str | None:
    url = os.environ.get("ANTHROPIC_BASE_URL", "").strip() or _read_config_env_var("ANTHROPIC_BASE_URL")
    return url or None


async def _approval_loop(deps: Deps) -> None:
    """Single-flight approval worker — processes one pending approval at a time."""
    while True:
        await deps.approval_event.wait()
        deps.approval_event.clear()

        while True:
            approval = await memory.get_pending_approval(deps.db)
            if approval is None:
                break

            status, final_text = await notifier.prompt_approval(approval)
            await memory.update_approval_status(deps.db, approval["id"], status)

            if status == "approved" and final_text:
                chat_id = approval["chat_id"]
                ctx = approval.get("context", {})
                reaction = ctx.get("reaction_emoji")
                sticker = ctx.get("sticker_emoji")

                if reaction:
                    last_msg_id = ctx.get("last_tg_msg_id", 0)
                    if last_msg_id:
                        await personality.send_reaction(deps.client, chat_id, last_msg_id, reaction)

                await personality.show_typing(
                    deps.client, chat_id, personality.typing_duration(final_text)
                )
                sent = await deps.client.send_message(chat_id, final_text)
                await memory.save_message(
                    deps.db,
                    chat_id=chat_id,
                    tg_msg_id=sent.id,
                    sender_id=deps.me.id,
                    sender_name="Me",
                    text=final_text,
                    ts=int(time.time()),
                    is_outgoing=True,
                )
                if sticker:
                    await personality.send_sticker(deps.client, chat_id, sticker)


async def _expiry_loop(deps: Deps) -> None:
    """Periodically expire stale approvals."""
    while True:
        await asyncio.sleep(300)
        await memory.expire_old_approvals(deps.db)


async def run_daemon() -> None:
    cfg = AgentConfig.load()

    if not cfg.chats:
        console.print(
            "[yellow]Warning:[/yellow] No chats configured in agent.yaml. "
            "Agent will run but won't respond to anything."
        )

    api_key = _resolve_anthropic_key()
    if not api_key:
        console.print(
            "[red]Error:[/red] ANTHROPIC_API_KEY is not set.\n"
            "  Add it to [dim]~/.config/xerxes-tg/config.env[/dim]: "
            "[bold]ANTHROPIC_API_KEY=sk-ant-...[/bold]"
        )
        raise SystemExit(1)

    # Prevent double-run
    if pidfile.is_running():
        existing = pidfile.read_pidfile()
        console.print(f"[red]Agent is already running[/red] (PID {existing}). Use [bold]stop[/bold] first.")
        raise SystemExit(1)

    pidfile.write_pidfile()

    db = await memory.init()
    client = tg_module.create_client()
    await client.connect()

    if not await client.is_user_authorized():
        console.print("[red]Not logged in.[/red] Run [bold]xerxes-tg sign-in[/bold] first.")
        pidfile.remove_pidfile()
        await db.close()
        raise SystemExit(1)

    me = await client.get_me()
    base_url = _resolve_anthropic_base_url()
    anthropic_client = anthropic.AsyncAnthropic(
        api_key=api_key,
        **({"base_url": base_url} if base_url else {}),
    )
    approval_event = asyncio.Event()

    buffer = DebounceBuffer(debounce_s=cfg.debounce_seconds, on_flush=None)  # type: ignore[arg-type]

    deps = Deps(
        client=client,
        me=me,
        cfg=cfg,
        db=db,
        anthropic=anthropic_client,
        buffer=buffer,
        approval_event=approval_event,
    )

    # Wire the buffer flush callback now that deps exists
    buffer._on_flush = make_flush_callback(deps)  # noqa: SLF001

    register(client, deps)

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop_event.set)

    approval_task = asyncio.create_task(_approval_loop(deps))
    expiry_task = asyncio.create_task(_expiry_loop(deps))

    name = getattr(me, "first_name", None) or getattr(me, "username", "?")
    chat_count = len(cfg.chats)
    console.print(
        f"[green]Agent ready[/green] — logged in as [bold]{name}[/bold], "
        f"watching [bold]{chat_count}[/bold] chat(s). "
        "Press [bold]Ctrl+C[/bold] to stop."
    )

    try:
        # Run client until stop signal
        await asyncio.gather(
            client.run_until_disconnected(),
            stop_event.wait(),
            return_exceptions=True,
        )
    finally:
        approval_task.cancel()
        expiry_task.cancel()
        await buffer.flush_all()
        await db.close()
        await client.disconnect()
        pidfile.remove_pidfile()
        console.print("[dim]Agent stopped.[/dim]")
