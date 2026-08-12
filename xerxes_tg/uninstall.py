"""Remove xerxes-tg entries from coding agent configs.

Mirrors the agent rewriting done by ``setup.py``. Can optionally purge local
state (config, encrypted session, audit log, mirror db, keychain entry).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from rich.console import Console
from rich.table import Table

from .setup import ALL_AGENTS, CodexCLI, Zed, _Agent, _select_agents
from .telegram import CONFIG_DIR

console = Console()


def _clean_json(path: Path, servers_key: str) -> bool:
    """Remove xerxes-tg + legacy mcp-telegram from a JSON-style agent config."""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, ValueError):
        return False
    servers = data.get(servers_key)
    if not isinstance(servers, dict):
        return False
    changed = servers.pop("xerxes-tg", None) is not None
    changed = servers.pop("mcp-telegram", None) is not None or changed
    if changed:
        path.write_text(json.dumps(data, indent=4) + "\n")
    return changed


def _clean_codex(path: Path) -> bool:
    if not path.exists():
        return False
    lines = path.read_text().splitlines()
    headers = {
        '[mcp_servers."xerxes-tg"]',
        "[mcp_servers.xerxes-tg]",
        '[mcp_servers."mcp-telegram"]',
        "[mcp_servers.mcp-telegram]",
    }
    cleaned: list[str] = []
    skip = False
    changed = False
    for line in lines:
        stripped = line.strip()
        if stripped in headers:
            skip = True
            changed = True
            continue
        if skip:
            if stripped.startswith("["):
                skip = False
            else:
                continue
        cleaned.append(line)
    if changed:
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()
        path.write_text("\n".join(cleaned) + "\n")
    return changed


def _clean_agent(agent: _Agent) -> list[tuple[Path, bool]]:
    """Clean the global config for an agent. Returns a list of (path, removed) pairs."""
    path = agent.global_path()
    if isinstance(agent, CodexCLI):
        return [(path, _clean_codex(path))]
    if isinstance(agent, Zed):
        return [(path, _clean_json(path, "context_servers"))]
    servers_key = getattr(agent, "servers_key", "mcpServers")
    return [(path, _clean_json(path, servers_key))]


def _purge_local_state() -> list[str]:
    from . import session as _session
    from . import audit as _audit
    from . import mirror as _mirror
    from . import watch_store, watcher

    removed: list[str] = []
    watcher.stop_process()
    _session.delete_session()
    removed.append("keychain + session.enc")

    if _audit.AUDIT_PATH.exists():
        _audit.AUDIT_PATH.unlink()
        removed.append(str(_audit.AUDIT_PATH))
    _mirror.purge()
    removed.append(str(_mirror.DB_PATH))
    for path in (
        watch_store.WATCH_DB,
        watcher.WATCHER_LOG,
        watcher.WATCHER_PID,
        watcher.WATCHER_READY,
    ):
        if path.exists():
            path.unlink()
            removed.append(str(path))
    if CONFIG_DIR.exists():
        shutil.rmtree(CONFIG_DIR)
        removed.append(str(CONFIG_DIR))
    return removed


def run_uninstall(purge: bool = False) -> int:
    console.print("[bold bright_cyan]xerxes-tg uninstall[/bold bright_cyan]")
    console.print("  [dim]Choose which coding agents to clean up.[/dim]")
    console.print()

    agents = _select_agents()
    if not agents:
        console.print("  [yellow]No agents selected. Nothing changed.[/yellow]")
        return 0

    table = Table(show_header=True, header_style="bold bright_cyan", border_style="dim cyan", padding=(0, 1))
    table.add_column("", width=3)
    table.add_column("Agent", min_width=20)
    table.add_column("Path", style="dim")

    any_changed = False
    for agent in agents:
        for path, changed in _clean_agent(agent):
            if changed:
                icon = "[bright_green]−[/bright_green]"
                any_changed = True
            else:
                icon = "[dim]·[/dim]"
            table.add_row(icon, agent.display, str(path))
    console.print(table)

    if purge:
        console.print()
        console.print("  [bold red]purging local state[/bold red]")
        for line in _purge_local_state():
            console.print(f"    [red]−[/red] {line}")

    if not any_changed and not purge:
        console.print("  [yellow]No changes applied (nothing to remove).[/yellow]")
    return 0
