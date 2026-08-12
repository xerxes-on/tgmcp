"""Health-check command for xerxes-tg.

Runs a series of checks against the local install and Telegram session,
prints a summary table, and exits with a non-zero code if anything failed.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from rich.console import Console
from rich.table import Table

from .telegram import CONFIG_ENV, create_client, get_settings

console = Console()


@dataclass
class Check:
    name: str
    status: str  # "pass" | "warn" | "fail"
    detail: str = ""


async def _check_session() -> Check:
    try:
        client = create_client()
        await client.connect()
        try:
            if await client.is_user_authorized():
                me = await client.get_me()
                who = getattr(me, "first_name", None) or getattr(me, "username", None) or "user"
                return Check("telegram session", "pass", f"authorized as {who}")
            return Check("telegram session", "fail", "not authorized — run `xerxes-tg setup`")
        finally:
            await client.disconnect()
    except Exception as e:
        return Check("telegram session", "fail", f"{type(e).__name__}: {e}")


async def _check_aliases() -> list[Check]:
    settings = get_settings()
    aliases = settings.alias_map
    if not aliases:
        return [Check("aliases", "warn", "no aliases configured")]

    checks: list[Check] = []
    try:
        client = create_client()
        await client.connect()
        try:
            for name, cid in aliases.items():
                try:
                    await client.get_entity(cid)
                    checks.append(Check(f"alias {name}", "pass", f"→ {cid}"))
                except Exception as e:
                    checks.append(
                        Check(f"alias {name}", "fail", f"{cid} not reachable ({type(e).__name__})")
                    )
        finally:
            await client.disconnect()
    except Exception as e:
        checks.append(Check("aliases", "fail", f"could not connect: {e}"))
    return checks


def _check_config() -> Check:
    if not CONFIG_ENV.exists():
        return Check("config file", "fail", f"{CONFIG_ENV} missing — run `xerxes-tg setup`")
    s = get_settings()
    if not s.api_id or not s.api_hash:
        return Check("config file", "fail", "TELEGRAM_API_ID or TELEGRAM_API_HASH empty")
    return Check("config file", "pass", str(CONFIG_ENV))


def _check_keychain() -> Check:
    try:
        from . import session as _session
    except Exception as e:
        return Check("keychain session", "fail", f"import failed: {e}")
    if _session.keychain_has_key() and _session.encrypted_session_exists():
        return Check("keychain session", "pass", "encrypted session present")
    if _session.keychain_has_key():
        return Check("keychain session", "warn", "key stored but session.enc missing")
    return Check("keychain session", "warn", "not yet created — login to initialize")


def _check_watcher() -> list[Check]:
    from . import watcher
    from .webhook import WebhookConfig

    config = WebhookConfig.load()
    checks = [
        Check(
            "watcher daemon",
            "pass" if watcher.is_ready() else "warn",
            (
                f"ready (PID {watcher.read_pid()})"
                if watcher.is_ready()
                else "starting" if watcher.is_running() else "not running"
            ),
        )
    ]
    if not config.url and not config.secret:
        checks.append(Check("watcher webhook", "warn", "not configured; events are poll-only"))
    else:
        try:
            config.validate()
        except ValueError as exc:
            checks.append(Check("watcher webhook", "fail", str(exc)))
        else:
            checks.append(Check("watcher webhook", "pass", config.display_url))
    return checks


def _check_agent_configs() -> list[Check]:
    from .setup import ALL_AGENTS  # local import to avoid cycle

    checks: list[Check] = []
    for agent in ALL_AGENTS:
        path: Path = agent.global_path()
        if not path.exists():
            checks.append(Check(f"{agent.display}", "warn", "not installed"))
            continue
        try:
            text = path.read_text()
            has_new = '"xerxes-tg"' in text or "'xerxes-tg'" in text
            has_legacy = '"mcp-telegram"' in text or "'mcp-telegram'" in text
            if has_new and not has_legacy:
                checks.append(Check(f"{agent.display}", "pass", str(path)))
            elif has_new and has_legacy:
                checks.append(
                    Check(f"{agent.display}", "warn", "both xerxes-tg and legacy mcp-telegram present")
                )
            elif has_legacy:
                checks.append(
                    Check(f"{agent.display}", "warn", "legacy mcp-telegram key — re-run setup")
                )
            else:
                checks.append(Check(f"{agent.display}", "warn", "not configured for xerxes-tg"))
        except Exception as e:
            checks.append(Check(f"{agent.display}", "fail", f"could not read: {e}"))
    return checks


async def _run_checks() -> list[Check]:
    results: list[Check] = []
    results.append(_check_config())
    results.append(_check_keychain())
    results.append(await _check_session())
    results.extend(await _check_aliases())
    results.extend(_check_watcher())
    results.extend(_check_agent_configs())
    return results


def _render(results: list[Check]) -> int:
    table = Table(show_header=True, header_style="bold bright_cyan", border_style="dim cyan", padding=(0, 1))
    table.add_column("", width=3)
    table.add_column("Check", min_width=22)
    table.add_column("Detail", style="dim", overflow="fold")

    fail_count = 0
    for c in results:
        if c.status == "pass":
            icon = "[bright_green]✓[/bright_green]"
        elif c.status == "warn":
            icon = "[yellow]![/yellow]"
        else:
            icon = "[red]✗[/red]"
            fail_count += 1
        table.add_row(icon, c.name, c.detail)
    console.print(table)
    console.print()
    if fail_count:
        console.print(f"  [red]{fail_count} failing check(s).[/red]")
        return 1
    console.print("  [bright_green]all checks passed[/bright_green]")
    return 0


def run_doctor() -> int:
    results = asyncio.run(_run_checks())
    return _render(results)
