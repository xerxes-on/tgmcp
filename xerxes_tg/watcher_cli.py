"""CLI commands for the temporary Telegram watcher daemon."""

from __future__ import annotations

import asyncio
import json
import os
from getpass import getpass
from typing import Annotated

from rich.console import Console
from typer import Option, Typer

from . import watch_store, watcher
from .telegram import CONFIG_ENV
from .webhook import WebhookConfig

app = Typer(help="Temporary Telegram reply watcher and webhook dispatcher.")
console = Console()


def _set_config_values(values: dict[str, str | None]) -> None:
    CONFIG_ENV.parent.mkdir(parents=True, exist_ok=True)
    existing = CONFIG_ENV.read_text().splitlines() if CONFIG_ENV.exists() else []
    keys = set(values)
    lines = [line for line in existing if line.partition("=")[0].strip() not in keys]
    for key, value in values.items():
        if value is not None:
            if "\n" in value or "\r" in value:
                raise ValueError(f"{key} cannot contain newlines")
            escaped = value.replace("'", "'\\''")
            lines.append(f"{key}='{escaped}'")
    CONFIG_ENV.write_text("\n".join(lines) + "\n")
    os.chmod(CONFIG_ENV, 0o600)


@app.command()
def start(foreground: bool = False) -> None:
    """Start the watcher, detached by default."""
    if foreground:
        asyncio.run(watcher.run_daemon())
        return
    pid = watcher.ensure_running()
    console.print(f"[green]Watcher running[/green] (PID {pid}).")


@app.command()
def stop() -> None:
    """Stop the watcher daemon without deleting subscriptions or events."""
    if watcher.stop_process():
        console.print("[green]Watcher stopped.[/green]")
    else:
        console.print("[yellow]Watcher is not running or did not stop cleanly.[/yellow]")


@app.command()
def status() -> None:
    """Show watcher, webhook, and active-subscription status."""
    config = WebhookConfig.load()
    watches = asyncio.run(watch_store.list_watches())
    payload = {
        "running": watcher.is_running(),
        "ready": watcher.is_ready(),
        "pid": watcher.read_pid(),
        "webhook_configured": config.enabled,
        "webhook_url": config.display_url if config.url else None,
        "active_watches": len(watches),
        "database": str(watch_store.WATCH_DB),
        "log": str(watcher.WATCHER_LOG),
    }
    console.print_json(json.dumps(payload))


@app.command()
def configure(
    url: Annotated[str | None, Option(help="HTTPS endpoint that receives watch events")] = None,
    disable: Annotated[bool, Option(help="Remove webhook URL and secret")] = False,
) -> None:
    """Configure a global signed webhook without exposing its secret in shell history."""
    if disable:
        _set_config_values(
            {
                "XERXES_TG_WEBHOOK_URL": None,
                "XERXES_TG_WEBHOOK_SECRET": None,
            }
        )
        console.print("[green]Webhook delivery disabled.[/green]")
        return

    current = WebhookConfig.load()
    target_url = url or current.url
    if not target_url:
        raise ValueError("pass --url or configure XERXES_TG_WEBHOOK_URL")
    secret = getpass("Webhook signing secret (Enter to keep existing): ").strip()
    if not secret:
        secret = current.secret
    config = WebhookConfig(url=target_url, secret=secret)
    config.validate()
    _set_config_values(
        {
            "XERXES_TG_WEBHOOK_URL": target_url,
            "XERXES_TG_WEBHOOK_SECRET": secret,
        }
    )
    console.print("[green]Webhook configured.[/green] Restart the watcher to reload it.")
