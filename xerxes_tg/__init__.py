import asyncio
import os
import sys
import time
from typing import Annotated

from typer import Context, Option, Typer

app = Typer()

# Lazy-register agent subcommand (avoids importing anthropic/rich on every CLI call)
def _register_agent() -> None:
    from .agent.cli import app as _agent_app
    app.add_typer(_agent_app, name="agent")


def _register_watcher() -> None:
    from .watcher_cli import app as _watcher_app
    app.add_typer(_watcher_app, name="watcher")


_register_agent()
_register_watcher()

INTRO_ART = [
    "██╗  ██╗███████╗██████╗ ██╗  ██╗███████╗███████╗",
    "╚██╗██╔╝██╔════╝██╔══██╗╚██╗██╔╝██╔════╝██╔════╝",
    " ╚███╔╝ █████╗  ██████╔╝ ╚███╔╝ █████╗  ███████╗",
    " ██╔██╗ ██╔══╝  ██╔══██╗ ██╔██╗ ██╔══╝  ╚════██║",
    "██╔╝ ██╗███████╗██║  ██║██╔╝ ██╗███████╗███████║",
    "╚═╝  ╚═╝╚══════╝╚═╝  ╚═╝╚═╝  ╚═╝╚══════╝╚══════╝",
    "                ╔╦╗╔═╗                             ",
    "                 ║ ║ ╦                             ",
    "                 ╩ ╚═╝                             ",
]

# Blue gradient — 6 stops from deep to bright
_GRAD = [
    "\033[38;2;0;40;140m",   # deep blue
    "\033[38;2;0;68;204m",   # blue
    "\033[38;2;0;102;255m",  # bright blue
    "\033[38;2;0;136;255m",  # sky blue
    "\033[38;2;0;187;255m",  # cyan-blue
    "\033[38;2;85;221;255m", # light cyan
]
_RESET = "\033[0m"
_HIDE = "\033[?25l"
_SHOW = "\033[?25h"
_CLEAR = "\033[2J\033[H"


def _play_intro() -> None:
    if not sys.stdout.isatty():
        return

    try:
        cols, rows = os.get_terminal_size()
    except OSError:
        cols, rows = 120, 40

    art = INTRO_ART
    art_h = len(art)
    art_w = max(len(line) for line in art)

    # Centering offsets
    pad_x = max(0, (cols - art_w) // 2)
    pad_y = max(0, (rows - art_h) // 2)

    sys.stdout.write(_HIDE + _CLEAR)
    sys.stdout.flush()

    try:
        # Phase 1: reveal line by line with gradient sweep (~2s)
        for i, line in enumerate(art):
            y = pad_y + i
            grad_idx = int(i / max(art_h - 1, 1) * (len(_GRAD) - 1))
            color = _GRAD[grad_idx]

            # Typewrite each line
            sys.stdout.write(f"\033[{y};{pad_x + 1}H")
            for j, ch in enumerate(line):
                sys.stdout.write(f"{color}{ch}")
                if j % 4 == 0:
                    sys.stdout.flush()
                    time.sleep(0.004)
            sys.stdout.write(_RESET)
            sys.stdout.flush()
            time.sleep(0.08)

        # Phase 2: gradient color sweep across all text (~1.5s)
        steps = len(_GRAD)
        for step in range(steps):
            for i, line in enumerate(art):
                y = pad_y + i
                # Shift gradient based on step + line position
                grad_idx = (step + i) % len(_GRAD)
                color = _GRAD[grad_idx]
                sys.stdout.write(f"\033[{y};{pad_x + 1}H{color}{line}{_RESET}")
            sys.stdout.flush()
            time.sleep(0.15)

        # Phase 3: hold final bright state (~0.5s)
        for i, line in enumerate(art):
            y = pad_y + i
            sys.stdout.write(f"\033[{y};{pad_x + 1}H{_GRAD[-1]}{line}{_RESET}")
        sys.stdout.flush()
        time.sleep(0.5)

    finally:
        # Clear and restore
        sys.stdout.write(_CLEAR + _SHOW)
        sys.stdout.flush()


@app.callback(invoke_without_command=True)
def _run(ctx: Context) -> None:
    if ctx.invoked_subcommand is None:
        run()
    else:
        _play_intro()


@app.command()
def sign_in(
    api_id: Annotated[str, Option(help="Telegram API id")],
    api_hash: Annotated[str, Option(help="Telegram API hash")],
    phone_number: Annotated[str, Option(help="Phone number with country code")],
) -> None:
    """Connect to Telegram API."""
    from .telegram import connect_to_telegram

    asyncio.run(connect_to_telegram(api_id, api_hash, phone_number))


@app.command()
def run() -> None:
    """Run the xerxes-tg MCP server."""
    from .server import run_mcp_server

    asyncio.run(run_mcp_server())


@app.command()
def logout() -> None:
    """Logout from Telegram API."""
    from .telegram import logout_from_telegram

    asyncio.run(logout_from_telegram())


@app.command()
def setup() -> None:
    """Interactive setup wizard — configure API keys, login, and chat ACL."""
    from .setup import run_setup

    run_setup()


@app.command()
def doctor() -> None:
    """Run health checks against the local install and Telegram session."""
    from .doctor import run_doctor

    raise SystemExit(run_doctor())


@app.command()
def install() -> None:
    """(Re)select coding agents and write xerxes-tg entries to their configs."""
    from .setup import run_install_agents

    raise SystemExit(run_install_agents())


@app.command()
def uninstall(
    purge: Annotated[bool, Option(help="Also delete config, session, audit log, mirror db, and keychain entry")] = False,
) -> None:
    """Remove xerxes-tg entries from coding agent configs."""
    from .uninstall import run_uninstall

    raise SystemExit(run_uninstall(purge=purge))


@app.command(name="audit")
def audit_cmd(
    tail: Annotated[int, Option("--tail", help="Show the last N records")] = 20,
    tool: Annotated[str | None, Option(help="Filter by tool name (e.g. SendMessage)")] = None,
    since_hours: Annotated[float | None, Option("--since-hours", help="Only show records newer than N hours")] = None,
    json_output: Annotated[bool, Option("--json", help="Emit raw JSON lines")] = False,
) -> None:
    """View the tool-call audit log (last 48 hours)."""
    import json as _json

    from . import audit as _audit

    records = _audit.tail(limit=tail, tool=tool, since_hours=since_hours)
    if not records:
        print("(no records)")
        return
    if json_output:
        for r in records:
            print(_json.dumps(r, ensure_ascii=False))
        return
    for r in records:
        marker = "✓" if r.get("ok") else "✗"
        args_preview = _json.dumps(r.get("args", {}), ensure_ascii=False)[:80]
        line = f"{r['ts']}  {marker} {r['tool']:<20} {args_preview}"
        if r.get("error"):
            line += f"  err={r['error']}"
        elif r.get("result_preview"):
            line += f"  → {r['result_preview'][:60]}"
        print(line)


@app.command()
def sync(
    dialog: Annotated[str | None, Option(help="Alias or chat id to sync (repeat with commas)")] = None,
    all_chats: Annotated[bool, Option("--all", help="Sync every chat in the read ACL")] = False,
    since_hours: Annotated[int | None, Option("--since-hours", help="Only fetch messages newer than N hours")] = 24,
    max_messages: Annotated[int, Option("--max", help="Cap per-dialog message fetch")] = 5000,
) -> None:
    """Pull recent messages into the local mirror db for cross-chat search."""
    from . import mirror
    from .telegram import create_client, get_allowed_chat_ids, resolve_dialog_id

    async def _run() -> None:
        targets: list[int] = []
        if dialog:
            for part in dialog.split(","):
                p = part.strip()
                if p:
                    targets.append(resolve_dialog_id(p))
        if all_chats:
            allowed = get_allowed_chat_ids()
            if allowed is None:
                print("ACL is unrestricted. Specify --dialog to avoid syncing everything.")
                raise SystemExit(2)
            targets.extend(sorted(allowed))
        if not targets:
            print("Nothing to sync. Pass --dialog <alias|id> or --all.")
            raise SystemExit(2)

        client = create_client()
        await client.connect()
        try:
            total = 0
            for did in targets:
                try:
                    n = await mirror.sync_dialog(
                        client, did, since_hours=since_hours, max_messages=max_messages
                    )
                except Exception as e:  # noqa: BLE001
                    print(f"  ✗ {did}: {type(e).__name__}: {e}")
                    continue
                total += n
                print(f"  ✓ {did}: +{n} messages")
            s = mirror.stats()
            print(f"\n  mirror: {s['messages']} messages across {s['dialogs']} dialogs")
        finally:
            await client.disconnect()

    asyncio.run(_run())
