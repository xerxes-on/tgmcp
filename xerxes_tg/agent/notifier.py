from __future__ import annotations

import asyncio
import sys
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.panel import Panel
from rich.text import Text

if TYPE_CHECKING:
    pass

console = Console()


def iterm_notify(title: str, body: str = "") -> None:
    """Send an iTerm2 / terminal-notifier banner. Degrades silently elsewhere."""
    msg = f"{title}: {body}" if body else title
    # OSC 9 — works in iTerm2, Wezterm, and some other terminals
    sys.stdout.write(f"\033]9;{msg}\007")
    sys.stdout.flush()
    # Also ring the terminal bell for non-iTerm2 environments
    sys.stdout.write("\a")
    sys.stdout.flush()


def _make_panel(
    chat_label: str,
    draft: str,
    confidence: float,
    reasoning: str,
    incoming: str,
) -> Panel:
    lines = Text()
    lines.append(f"Chat: {chat_label}\n", style="bold cyan")
    lines.append(f"Message: {incoming[:200]}\n\n", style="white")
    lines.append("Draft reply:\n", style="bold green")
    lines.append(f"{draft}\n\n", style="green")
    lines.append(f"Confidence: {confidence:.0%}", style="yellow")
    if reasoning:
        lines.append(f"  — {reasoning}", style="dim")
    lines.append("\n\n")
    lines.append("[A]pprove  [R]eject  [C]hange  [I]gnore", style="bold yellow")
    return Panel(lines, title="[yellow]⚡ Approval needed[/yellow]", border_style="yellow")


def _make_clarify_panel(
    chat_label: str,
    question: str,
    incoming: str,
) -> Panel:
    lines = Text()
    lines.append(f"Chat: {chat_label}\n", style="bold cyan")
    lines.append(f"Message: {incoming[:200]}\n\n", style="white")
    lines.append("Clarification question to send:\n", style="bold blue")
    lines.append(f"{question}\n\n", style="blue")
    lines.append("[A]pprove  [R]eject  [C]hange  [I]gnore", style="bold yellow")
    return Panel(lines, title="[blue]❓ Clarification needed[/blue]", border_style="blue")


async def prompt_approval(approval: dict[str, Any]) -> tuple[str, str]:
    """
    Show the terminal approval UI and wait for user input.
    Returns (status, final_text) — status is one of: approved, rejected, ignored.
    Runs the blocking prompt in a thread so the event loop stays alive.
    """
    ctx = approval.get("context", {})
    chat_label = ctx.get("chat_label", str(approval["chat_id"]))
    draft = approval["draft_text"]
    incoming = ctx.get("incoming", "")
    confidence = ctx.get("confidence", 0.0)
    reasoning = ctx.get("reasoning", "")
    is_clarify = ctx.get("is_clarify", False)

    if is_clarify:
        panel = _make_clarify_panel(chat_label, draft, incoming)
    else:
        panel = _make_panel(chat_label, draft, confidence, reasoning, incoming)

    def _blocking_prompt() -> tuple[str, str]:
        console.print()
        console.print(panel)
        while True:
            choice = input("> ").strip().lower()
            if choice == "a":
                return ("approved", draft)
            elif choice == "r":
                return ("rejected", "")
            elif choice == "c":
                new_text = input("New reply: ").strip()
                return ("approved", new_text) if new_text else ("ignored", "")
            elif choice == "i":
                return ("ignored", "")
            else:
                console.print("[dim]Type a/r/c/i[/dim]")

    return await asyncio.to_thread(_blocking_prompt)
