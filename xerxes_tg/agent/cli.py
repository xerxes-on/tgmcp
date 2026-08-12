from __future__ import annotations

import asyncio
import os
import signal
import time

from rich.console import Console
from rich.table import Table
from typer import Typer

from . import memory, pidfile
from .config import AGENT_YAML, AgentConfig

app = Typer(help="Autonomous Telegram agent daemon.")
console = Console()


@app.command()
def start() -> None:
    """Start the agent daemon (foreground). Ctrl+C or `agent stop` to quit."""
    from .daemon import run_daemon

    if not AGENT_YAML.exists():
        cfg = AgentConfig()
        cfg.write_example()
        console.print(
            f"[yellow]Created example config at[/yellow] {AGENT_YAML}\n"
            "Edit it to add your chats and owned services, then re-run."
        )
        raise SystemExit(0)

    asyncio.run(run_daemon())


@app.command()
def stop() -> None:
    """Send SIGTERM to a running daemon."""
    pid = pidfile.read_pidfile()
    if pid is None or not pidfile.is_running():
        console.print("[yellow]Agent is not running.[/yellow]")
        raise SystemExit(1)
    os.kill(pid, signal.SIGTERM)
    console.print(f"[green]Sent SIGTERM[/green] to PID {pid}.")

    # Wait up to 5s for clean exit
    for _ in range(10):
        time.sleep(0.5)
        if not pidfile.is_running():
            console.print("[green]Agent stopped.[/green]")
            return
    console.print("[yellow]Agent did not stop within 5s — may still be shutting down.[/yellow]")


@app.command()
def status() -> None:
    """Show agent running status and DB stats."""
    running = pidfile.is_running()
    pid = pidfile.read_pidfile()

    table = Table(show_header=False, box=None)
    table.add_column(style="dim")
    table.add_column()

    if running:
        table.add_row("Status", f"[green]Running[/green] (PID {pid})")
    else:
        table.add_row("Status", "[red]Stopped[/red]")

    cfg_exists = AGENT_YAML.exists()
    table.add_row("Config", str(AGENT_YAML) if cfg_exists else "[yellow]Not found[/yellow]")

    if cfg_exists:
        cfg = AgentConfig.load()
        table.add_row("Watching", f"{len(cfg.chats)} chat(s)")
        table.add_row("Model", cfg.orchestrator_model)
        table.add_row("Confidence threshold", f"{cfg.confidence_threshold:.0%}")

    from .config import AGENT_DB

    if AGENT_DB.exists():

        async def _stats() -> tuple[int, int]:
            db = await memory.init()
            today = await memory.messages_today(db)
            pending = await memory.pending_count(db)
            await db.close()
            return today, pending

        today, pending = asyncio.run(_stats())
        table.add_row("Messages today", str(today))
        table.add_row("Pending approvals", str(pending))
    else:
        table.add_row("DB", "[dim]Not initialized[/dim]")

    console.print(table)
