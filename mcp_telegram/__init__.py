import asyncio
import os
import sys
import time
from typing import Annotated

from typer import Context, Option, Typer

app = Typer()

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
    """Run the mcp-telegram server."""
    from .server import run_mcp_server

    asyncio.run(run_mcp_server())


@app.command()
def listen() -> None:
    """Run the autonomous Telegram listener."""
    from .listener import run_listener

    asyncio.run(run_listener())


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
