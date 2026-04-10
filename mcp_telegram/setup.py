# ruff: noqa: T201
"""Interactive setup wizard for mcp-telegram — with terminal animations."""
from __future__ import annotations

import asyncio
import json
import platform
import sys
import time
from getpass import getpass
from pathlib import Path

from rich.align import Align
from rich.console import Console
from rich.live import Live
from rich.panel import Panel
from rich.style import Style
from rich.table import Table
from rich.text import Text
from telethon import TelegramClient  # type: ignore[import-untyped]
from telethon.errors.rpcerrorlist import SessionPasswordNeededError  # type: ignore[import-untyped]
from telethon.tl.types import User  # type: ignore[import-untyped]
from xdg_base_dirs import xdg_state_home  # type: ignore[import-error]

from .telegram import CONFIG_DIR, CONFIG_ENV

console = Console()

_LAUNCH_CMD = f"set -a && . {CONFIG_ENV} && set +a && uvx --from mcp-xerxes-tg xerxes-tg"


# ---------------------------------------------------------------------------
# Animations
# ---------------------------------------------------------------------------

SETUP_BANNER = r"""
  ╔═══════════════════════════════════════╗
  ║        xerxes-tg  ·  setup            ║
  ╚═══════════════════════════════════════╝
"""

COLORS_CYAN = ["00d4ff", "00b4d8", "0096c7", "0077b6", "023e8a"]
COLORS_GREEN = ["00ff87", "00e676", "00c853", "009624"]
COLORS_GOLD = ["ffd700", "ffb300", "ff8f00", "ff6f00"]
COLORS_PINK = ["ff6b9d", "c084fc", "818cf8", "6366f1"]

STEP_ART = {
    1: "  🔑",
    2: "  📱",
    3: "  🛡️ ",
    4: "  🤖",
}


def _play_effect(text: str, effect_name: str = "decrypt", colors: list[str] | None = None) -> None:
    """Play a TTE effect on the given text."""
    try:
        if effect_name == "decrypt":
            from terminaltexteffects.effects.effect_decrypt import Decrypt, DecryptConfig
            from terminaltexteffects.utils.graphics import Color

            cfg = DecryptConfig(
                typing_speed=4,
                ciphertext_colors=tuple(Color(c) for c in (colors or COLORS_GREEN)),
                final_gradient_stops=tuple(Color(c) for c in (colors or COLORS_CYAN)),
                final_gradient_steps=8,
            )
            effect = Decrypt(text, effect_config=cfg)
        elif effect_name == "slide":
            from terminaltexteffects.effects.effect_slide import Slide, SlideConfig
            from terminaltexteffects.engine.terminal import TerminalConfig
            from terminaltexteffects.utils.graphics import Color

            cfg = SlideConfig(
                final_gradient_stops=tuple(Color(c) for c in (colors or COLORS_CYAN)),
                final_gradient_steps=8,
            )
            tcfg = TerminalConfig(frame_rate=120)
            effect = Slide(text, effect_config=cfg, terminal_config=tcfg)
        elif effect_name == "sweep":
            from terminaltexteffects.effects.effect_sweep import Sweep, SweepConfig
            from terminaltexteffects.engine.terminal import TerminalConfig
            from terminaltexteffects.utils.graphics import Color

            cfg = SweepConfig(
                final_gradient_stops=tuple(Color(c) for c in (colors or COLORS_CYAN)),
                final_gradient_steps=6,
            )
            tcfg = TerminalConfig(frame_rate=120)
            effect = Sweep(text, effect_config=cfg, terminal_config=tcfg)
        elif effect_name == "wipe":
            from terminaltexteffects.effects.effect_wipe import Wipe, WipeConfig
            from terminaltexteffects.engine.terminal import TerminalConfig
            from terminaltexteffects.utils.graphics import Color

            cfg = WipeConfig(
                final_gradient_stops=tuple(Color(c) for c in (colors or COLORS_CYAN)),
                final_gradient_steps=6,
            )
            tcfg = TerminalConfig(frame_rate=120)
            effect = Wipe(text, effect_config=cfg, terminal_config=tcfg)
        else:
            print(text)
            return

        with effect.terminal_output() as terminal:
            for frame in effect:
                terminal.print(frame)
    except Exception:
        # Fallback if TTE fails (e.g. weird terminal)
        print(text)


def _step_header(step: int, total: int, title: str) -> None:
    """Render animated step header."""
    art = STEP_ART.get(step, "  ▸")
    bar_filled = "━" * step
    bar_empty = "╌" * (total - step)
    progress = f"[bold cyan]{bar_filled}[/bold cyan][dim]{bar_empty}[/dim]"

    console.print()
    console.rule(style="dim cyan")
    console.print(f"  {progress}  [dim]step {step}/{total}[/dim]")
    console.print(f"{art}  [bold bright_cyan]{title}[/bold bright_cyan]")
    console.print()


def _success_box(text: str) -> None:
    """Render a success panel with gradient style."""
    console.print()
    console.print(
        Panel(
            Align.center(Text(text, style="bold bright_green")),
            border_style="bright_green",
            padding=(0, 2),
        )
    )


def _typewriter(text: str, delay: float = 0.02) -> None:
    """Print text with typewriter effect."""
    for char in text:
        sys.stdout.write(char)
        sys.stdout.flush()
        time.sleep(delay)
    sys.stdout.write("\n")
    sys.stdout.flush()


# ---------------------------------------------------------------------------
# Agent registry
# ---------------------------------------------------------------------------

class _Agent:
    name: str
    display: str
    icon: str = "⬡"

    def global_path(self) -> Path:
        raise NotImplementedError

    def project_path(self, project_dir: Path) -> Path:
        raise NotImplementedError

    def write_config(self, path: Path) -> None:
        raise NotImplementedError

    def _ensure_parent(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)


class _JsonAgent(_Agent):
    servers_key: str = "mcpServers"

    def _server_entry(self) -> dict:
        return {"command": "bash", "args": ["-c", _LAUNCH_CMD]}

    def write_config(self, path: Path) -> None:
        self._ensure_parent(path)
        data: dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, ValueError):
                data = {}
        servers = data.setdefault(self.servers_key, {})
        servers["mcp-telegram"] = self._server_entry()
        path.write_text(json.dumps(data, indent=4) + "\n")


class ClaudeCode(_JsonAgent):
    name = "claude-code"
    display = "Claude Code"
    icon = "◈"
    def global_path(self) -> Path:
        return Path.home() / ".claude" / ".claude.json"
    def project_path(self, project_dir: Path) -> Path:
        return project_dir / ".claude" / ".claude.json"


class Cursor(_JsonAgent):
    name = "cursor"
    display = "Cursor"
    icon = "⌁"
    def global_path(self) -> Path:
        return Path.home() / ".cursor" / "mcp.json"
    def project_path(self, project_dir: Path) -> Path:
        return project_dir / ".cursor" / "mcp.json"


class Windsurf(_JsonAgent):
    name = "windsurf"
    display = "Windsurf"
    icon = "◉"
    def global_path(self) -> Path:
        return Path.home() / ".codeium" / "windsurf" / "mcp_config.json"
    def project_path(self, project_dir: Path) -> Path:
        return project_dir / ".windsurf" / "mcp.json"


class VSCode(_JsonAgent):
    name = "vscode"
    display = "VS Code (Copilot)"
    icon = "⬢"
    servers_key = "servers"
    def _server_entry(self) -> dict:
        return {"type": "stdio", "command": "bash", "args": ["-c", _LAUNCH_CMD]}
    def global_path(self) -> Path:
        if platform.system() == "Darwin":
            return Path.home() / "Library/Application Support/Code/User/settings.json"
        return Path.home() / ".config/Code/User/settings.json"
    def project_path(self, project_dir: Path) -> Path:
        return project_dir / ".vscode" / "mcp.json"


class Zed(_Agent):
    name = "zed"
    display = "Zed"
    icon = "⚡"
    def global_path(self) -> Path:
        if platform.system() == "Darwin":
            return Path.home() / "Library/Application Support/Zed/settings.json"
        return Path.home() / ".config/zed/settings.json"
    def project_path(self, project_dir: Path) -> Path:
        return project_dir / ".zed" / "settings.json"
    def write_config(self, path: Path) -> None:
        self._ensure_parent(path)
        data: dict = {}
        if path.exists():
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, ValueError):
                data = {}
        servers = data.setdefault("context_servers", {})
        servers["mcp-telegram"] = {
            "source": "custom",
            "command": "bash",
            "args": ["-c", _LAUNCH_CMD],
        }
        path.write_text(json.dumps(data, indent=4) + "\n")


class Amp(_JsonAgent):
    name = "amp"
    display = "Amp (Sourcegraph)"
    icon = "△"
    def global_path(self) -> Path:
        return Path.home() / ".amp" / "settings.json"
    def project_path(self, project_dir: Path) -> Path:
        return project_dir / ".amp" / "settings.json"


class CodexCLI(_Agent):
    name = "codex"
    display = "Codex CLI (OpenAI)"
    icon = "◆"
    def global_path(self) -> Path:
        return Path.home() / ".codex" / "config.toml"
    def project_path(self, project_dir: Path) -> Path:
        return project_dir / ".codex" / "config.toml"
    def write_config(self, path: Path) -> None:
        self._ensure_parent(path)
        lines = path.read_text().splitlines() if path.exists() else []

        # Remove existing mcp-telegram block (header + all following key=value lines until next [header])
        cleaned: list[str] = []
        skip = False
        for line in lines:
            stripped = line.strip()
            if stripped in ('[mcp_servers."mcp-telegram"]', "[mcp_servers.mcp-telegram]"):
                skip = True
                continue
            if skip:
                if stripped.startswith("["):
                    skip = False  # new section, stop skipping
                else:
                    continue
            cleaned.append(line)

        # Remove trailing blank lines
        while cleaned and not cleaned[-1].strip():
            cleaned.pop()

        # Append new block
        cleaned.append("")
        cleaned.append('[mcp_servers."mcp-telegram"]')
        cleaned.append('command = "bash"')
        cleaned.append(f'args = ["-c", "{_LAUNCH_CMD}"]')
        cleaned.append("")

        path.write_text("\n".join(cleaned))


class GeminiCLI(_JsonAgent):
    name = "gemini"
    display = "Gemini CLI"
    icon = "✦"
    def global_path(self) -> Path:
        return Path.home() / ".gemini" / "settings.json"
    def project_path(self, project_dir: Path) -> Path:
        return project_dir / ".gemini" / "settings.json"


class OpenCode(_JsonAgent):
    name = "opencode"
    display = "OpenCode"
    icon = "⬟"
    servers_key = "mcp"
    def global_path(self) -> Path:
        return Path.home() / ".config" / "opencode" / "opencode.json"
    def project_path(self, project_dir: Path) -> Path:
        return project_dir / "opencode.json"


class RooCode(_JsonAgent):
    name = "roo-code"
    display = "Roo Code / Cline"
    icon = "◎"
    def global_path(self) -> Path:
        if platform.system() == "Darwin":
            base = Path.home() / "Library/Application Support/Code/User/globalStorage"
        else:
            base = Path.home() / ".config/Code/User/globalStorage"
        return base / "rooveterinaryinc.roo-cline" / "settings" / "mcp_settings.json"
    def project_path(self, project_dir: Path) -> Path:
        return project_dir / ".roo" / "mcp.json"


ALL_AGENTS: list[_Agent] = [
    ClaudeCode(), CodexCLI(), GeminiCLI(), Cursor(), VSCode(),
    Windsurf(), Zed(), Amp(), OpenCode(), RooCode(),
]


# ---------------------------------------------------------------------------
# Config file helpers
# ---------------------------------------------------------------------------

def _load_config() -> dict[str, str]:
    cfg: dict[str, str] = {}
    if not CONFIG_ENV.exists():
        return cfg
    for line in CONFIG_ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            k, _, v = line.partition("=")
            cfg[k.strip()] = v.strip()
    return cfg


def _save_config(cfg: dict[str, str]) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    lines: list[str] = []
    for k, v in cfg.items():
        # Quote values that contain spaces or special shell characters
        if v and (" " in v or "'" in v or '"' in v or "#" in v or ";" in v):
            # Use single quotes, escaping any existing single quotes
            escaped = v.replace("'", "'\\''")
            lines.append(f"{k}='{escaped}'")
        else:
            lines.append(f"{k}={v}")
    CONFIG_ENV.write_text("\n".join(lines) + "\n")


# ---------------------------------------------------------------------------
# Interactive helpers
# ---------------------------------------------------------------------------

def _input(prompt: str, default: str = "") -> str:
    if default:
        console.print(f"  [dim]{prompt}[/dim] [bright_cyan][{default}][/bright_cyan] ", end="")
    else:
        console.print(f"  [dim]{prompt}[/dim] ", end="")
    value = input("").strip()
    return value or default


PAGE_SIZE = 20

# ---------------------------------------------------------------------------
# Raw keyboard input
# ---------------------------------------------------------------------------

def _readkey() -> str:
    """Read a single keypress. Returns 'up','down','left','right','space','enter','w','/','a','esc', or the char."""
    import tty
    import termios

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x1b":
            ch2 = sys.stdin.read(1)
            if ch2 == "[":
                ch3 = sys.stdin.read(1)
                return {"A": "up", "B": "down", "C": "right", "D": "left"}.get(ch3, "")
            return "esc"
        if ch == " ":
            return "space"
        if ch in ("\r", "\n"):
            return "enter"
        if ch == "\x03":
            raise KeyboardInterrupt
        return ch
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


# ---------------------------------------------------------------------------
# Interactive multi-select (keyboard driven)
# ---------------------------------------------------------------------------

_CSI = "\033["
_HIDE_CURSOR = f"{_CSI}?25l"
_SHOW_CURSOR = f"{_CSI}?25h"
_SAVE_CURSOR = f"{_CSI}s"
_RESTORE_CURSOR = f"{_CSI}u"
_ERASE_BELOW = f"{_CSI}J"


def _interactive_select(
    items: list[tuple[str, str]],  # (display_name, extra_info)
    noun: str,
    preselected: set[int] | None = None,  # indices (0-based)
    pre_write: set[int] | None = None,  # indices with write access
    allow_write: bool = False,  # show 'w' to toggle write
    page_size: int = PAGE_SIZE,
) -> tuple[set[int], set[int] | None]:
    """
    Arrow-key driven multi-select.
    Returns (selected_indices, write_indices) — write_indices is None if allow_write=False.
    selected = all chats with ANY access (read or write).
    write_indices = subset of selected that also have write.
    Special returns: selected={-1} means 'all/unrestricted'.
    """
    selected: set[int] = set(preselected or set())
    write_set: set[int] = set(pre_write or set())
    cursor = 0
    page = 0
    search_query = ""
    search_mode = False
    total = len(items)

    def _view() -> list[int]:
        if not search_query:
            return list(range(total))
        q = search_query.lower()
        return [i for i in range(total) if q in items[i][0].lower()]

    last_lines = [0]

    def _render(view_idx: list[int], extra_line: str = "") -> None:
        """Move up over previous render, erase, and redraw."""
        if last_lines[0] > 0:
            sys.stdout.write(f"{_CSI}{last_lines[0]}A{_ERASE_BELOW}")
        else:
            sys.stdout.write(_ERASE_BELOW)

        tp = max(1, (len(view_idx) + page_size - 1) // page_size)
        p = min(page, tp - 1)
        start = p * page_size
        page_items = view_idx[start : start + page_size]

        out: list[str] = []

        # Nav bar
        nav = f"  \033[2m◂\033[0m page {p + 1}/{tp} \033[2m▸\033[0m"
        if allow_write:
            r_only = len(selected - write_set)
            rw = len(selected & write_set)
            w_only = len(write_set - selected)
            total_sel = r_only + rw + w_only
            if total_sel:
                parts = []
                if r_only:
                    parts.append(f"\033[32m{r_only} read\033[0m")
                if rw:
                    parts.append(f"\033[38;2;0;200;255m{rw} read+write\033[0m")
                if w_only:
                    parts.append(f"\033[38;2;0;200;255m{w_only} write\033[0m")
                nav += "  │  " + "  ".join(parts)
            else:
                nav += f"  │  \033[2m0 selected\033[0m"
        elif selected:
            nav += f"  │  \033[32m{len(selected)} selected\033[0m"
        else:
            nav += f"  │  \033[2m0 selected\033[0m"
        nav += f"  │  \033[2m{len(view_idx)} total\033[0m"
        if search_query:
            nav += f"  │  \033[36m/{search_query}\033[0m"
        out.append(nav)
        out.append("")

        for vi_pos, idx in enumerate(page_items):
            global_pos = start + vi_pos
            is_cursor = global_pos == cursor
            name, extra = items[idx]

            has_read = idx in selected
            has_write = allow_write and idx in write_set
            if has_read and has_write:
                mark = "\033[1;32m●\033[0m\033[1;38;2;0;200;255m◆\033[0m"
                name_fmt = f"\033[38;2;0;200;255m{name}\033[0m"
            elif has_write:
                mark = " \033[1;38;2;0;200;255m◆\033[0m"
                name_fmt = f"\033[38;2;0;200;255m{name}\033[0m"
            elif has_read:
                mark = "\033[1;32m●\033[0m "
                name_fmt = f"\033[32m{name}\033[0m"
            else:
                mark = "\033[2m· \033[0m"
                name_fmt = f"\033[2m{name}\033[0m" if not is_cursor else name

            if is_cursor:
                pointer = "\033[1;36m▸\033[0m"
                # highlight row background
                bg = "\033[48;2;20;30;50m" if is_cursor else ""
                bg_end = "\033[0m" if is_cursor else ""
            else:
                pointer = " "
                bg = ""
                bg_end = ""

            row = f"{bg}  {pointer} {mark}  {name_fmt}"
            if extra:
                row += f"  \033[2m{extra}\033[0m"
            row += bg_end
            out.append(row)

        out.append("")

        # Help line
        help_parts = ["\033[36m↑↓\033[0;2m move"]
        help_parts.append("\033[0;36mspace\033[0;2m select")
        if allow_write:
            help_parts.append("\033[0;36mw\033[0;2m write")
        help_parts.append("\033[0;36m←→\033[0;2m page")
        help_parts.append("\033[0;36m/\033[0;2m search")
        help_parts.append("\033[0;36ma\033[0;2m all")
        help_parts.append("\033[0;36m⏎\033[0;2m done\033[0m")
        out.append("  " + "  ".join(help_parts))

        if extra_line:
            out.append(extra_line)

        content = "\n".join(out)
        sys.stdout.write(content)
        sys.stdout.flush()
        last_lines[0] = len(out)

    # Hide cursor during selection
    sys.stdout.write(_HIDE_CURSOR)
    sys.stdout.flush()

    try:
        while True:
            view_idx = _view()
            tp = max(1, (len(view_idx) + page_size - 1) // page_size)

            if search_mode:
                _render(view_idx, f"  \033[36m/\033[0m{search_query}\033[5m_\033[0m")
                key = _readkey()
                if key == "enter" or key == "esc":
                    search_mode = False
                elif key == "space":
                    search_query += " "
                elif len(key) == 1 and key.isprintable():
                    search_query += key
                elif key in ("\x7f", "\x08"):  # backspace
                    search_query = search_query[:-1]
                    if not search_query:
                        search_mode = False
                continue

            _render(view_idx)
            key = _readkey()

            if key == "up":
                if cursor > 0:
                    cursor -= 1
                    if cursor < page * page_size:
                        page = max(0, page - 1)
            elif key == "down":
                if cursor < len(view_idx) - 1:
                    cursor += 1
                    if cursor >= (page + 1) * page_size:
                        page = min(tp - 1, page + 1)
            elif key == "left":
                page = max(0, page - 1)
                cursor = page * page_size
            elif key == "right":
                page = min(tp - 1, page + 1)
                cursor = page * page_size
            elif key == "space":
                # Toggle read independently
                if view_idx:
                    idx = view_idx[cursor]
                    if idx in selected:
                        selected.discard(idx)
                    else:
                        selected.add(idx)
            elif key == "w" and allow_write:
                # Toggle write independently
                if view_idx:
                    idx = view_idx[cursor]
                    if idx in write_set:
                        write_set.discard(idx)
                    else:
                        write_set.add(idx)
            elif key == "a":
                return ({-1}, None)
            elif key == "/":
                search_mode = True
                search_query = ""
                cursor = 0
                page = 0
            elif key == "enter":
                break
            elif key == "esc":
                if search_query:
                    search_query = ""
                    cursor = 0
                    page = 0
    finally:
        # Erase the selector and show cursor
        if last_lines[0] > 0:
            sys.stdout.write(f"{_CSI}{last_lines[0]}A{_ERASE_BELOW}")
        sys.stdout.write(_SHOW_CURSOR)
        sys.stdout.flush()

    return (selected, write_set if allow_write else None)


# ---------------------------------------------------------------------------
# Chat selector (wraps _interactive_select)
# ---------------------------------------------------------------------------

def _select_chats(
    dialogs: list[tuple[int, str]],
    prev_read: list[int],
    prev_write: list[int],
) -> tuple[list[int] | None, list[int] | None]:
    """
    Select read/write chats in one pass.
    space = toggle read, w = toggle write (write implies read).
    Returns (read_ids, write_ids) — None means unrestricted.
    """
    items = [(name, str(cid)) for cid, name in dialogs]
    pre_read_set = set()
    pre_write_set = set()
    for i, (cid, _) in enumerate(dialogs):
        if cid in prev_read or cid in prev_write:
            pre_read_set.add(i)
        if cid in prev_write:
            pre_write_set.add(i)

    selected, write_idx = _interactive_select(
        items,
        noun="chats",
        preselected=pre_read_set,
        pre_write=pre_write_set,
        allow_write=True,
    )

    if -1 in selected:
        return (None, None)

    w = write_idx or set()
    # READ_CHATS = chats with read access (● or ●◆)
    # WRITE_CHATS = chats with write access (◆ or ●◆)
    read_ids = [dialogs[i][0] for i in sorted(selected)]
    write_ids = [dialogs[i][0] for i in sorted(w)]
    return (read_ids, write_ids)


# ---------------------------------------------------------------------------
# Agent selector (wraps _interactive_select)
# ---------------------------------------------------------------------------

def _select_agents() -> list[_Agent]:
    items = [(a.display, str(a.global_path())) for a in ALL_AGENTS]
    selected, _ = _interactive_select(items, noun="agents")
    if -1 in selected:
        return list(ALL_AGENTS)
    return [ALL_AGENTS[i] for i in sorted(selected)]


def _select_project_dirs() -> list[Path]:
    console.print()
    console.print("  [dim]Enter absolute paths separated by commas, or empty for global only.[/dim]")
    raw = input("\n  project dirs ▸ ").strip()
    if not raw:
        return []
    dirs: list[Path] = []
    for part in raw.split(","):
        p = Path(part.strip()).expanduser().resolve()
        if p.is_dir():
            dirs.append(p)
            console.print(f"  [bright_green]  + {p}[/bright_green]")
        else:
            console.print(f"  [yellow]  ✗ {part.strip()} (not found)[/yellow]")
    return dirs


async def _fetch_dialogs(client: TelegramClient) -> list[tuple[int, str]]:
    dialogs: list[tuple[int, str]] = []
    async for d in client.iter_dialogs():
        dialogs.append((d.id, d.name or f"(unnamed {d.id})"))
    return dialogs


def _parse_aliases_str(raw: str) -> dict[str, int]:
    aliases: dict[str, int] = {}
    if not raw or not raw.strip():
        return aliases
    for pair in raw.split(","):
        pair = pair.strip()
        if ":" in pair:
            name, _, cid = pair.partition(":")
            name = name.strip().lower()
            if name and cid.strip().lstrip("-").isdigit():
                aliases[name] = int(cid.strip())
    return aliases


def _parse_ids(raw: str) -> list[int]:
    if not raw or not raw.strip():
        return []
    return [int(x.strip()) for x in raw.split(",") if x.strip()]


def _parse_listener_modes(raw: str) -> dict[int, str]:
    mapping: dict[int, str] = {}
    if not raw or not raw.strip():
        return mapping
    for pair in raw.split(","):
        item = pair.strip()
        if not item or "=" not in item:
            continue
        cid, _, mode = item.partition("=")
        cid = cid.strip()
        mode = mode.strip().lower()
        if cid.lstrip("-").isdigit() and mode:
            mapping[int(cid)] = mode
    return mapping


def _parse_bool(raw: str, default: bool = False) -> bool:
    if not raw:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _select_listener_targets(
    dialogs: list[tuple[int, str]],
    previous_ids: set[int],
) -> list[int]:
    items = [(name, str(cid)) for cid, name in dialogs]
    preselected = {i for i, (cid, _) in enumerate(dialogs) if cid in previous_ids}
    selected, _ = _interactive_select(items, noun="listener chats", preselected=preselected)
    if -1 in selected:
        return [cid for cid, _ in dialogs]
    return [dialogs[i][0] for i in sorted(selected)]


def _prompt_listener_mode(name: str, can_write: bool, default: str) -> str:
    allowed = ["read", "skip"] if not can_write else ["read", "ask", "auto", "decide", "skip"]
    fallback = default if default in allowed else ("read" if not can_write else "ask")
    while True:
        value = _input(f"Mode for {name} ({'/'.join(allowed)})", fallback).strip().lower()
        if value in allowed:
            return value
        console.print(f"  [yellow]Choose one of: {', '.join(allowed)}[/yellow]")


# ---------------------------------------------------------------------------
# Main wizard
# ---------------------------------------------------------------------------

async def _run_setup() -> None:  # noqa: C901
    # --- Animated banner ---
    _play_effect(SETUP_BANNER.strip(), "decrypt", COLORS_CYAN)
    time.sleep(0.2)

    existing = _load_config()
    total_steps = 5

    # ── Step 1: API credentials ──────────────────────────────────────────
    _step_header(1, total_steps, "Telegram API credentials")
    console.print("  [dim]Get yours at[/dim] [link=https://my.telegram.org/apps]https://my.telegram.org/apps[/link]")
    console.print()

    api_id = _input("API ID", existing.get("TELEGRAM_API_ID", ""))
    api_hash = _input("API Hash", existing.get("TELEGRAM_API_HASH", ""))

    if not api_id or not api_hash:
        console.print("\n  [bold red]API ID and Hash are required. Aborting.[/bold red]")
        sys.exit(1)

    console.print("  [bright_green]✓[/bright_green] credentials set")

    # ── Step 2: Login ────────────────────────────────────────────────────
    _step_header(2, total_steps, "Telegram login")

    state_home = xdg_state_home() / "mcp-telegram"
    state_home.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(state_home / "mcp_telegram_session", api_id, api_hash)
    await client.connect()

    if await client.is_user_authorized():
        me = await client.get_me()
        name = me.first_name if isinstance(me, User) else "User"
        console.print(f"  [bright_green]✓[/bright_green] already logged in as [bold]{name}[/bold]")
    else:
        phone = _input("Phone number (with country code)")
        with console.status("  [cyan]sending code...[/cyan]", spinner="dots"):
            result = await client.send_code_request(phone)
        code = _input("Login code from Telegram")
        try:
            with console.status("  [cyan]verifying...[/cyan]", spinner="dots"):
                await client.sign_in(phone=phone, code=code, phone_code_hash=result.phone_code_hash)
        except SessionPasswordNeededError:
            password = getpass("  2FA password: ")
            with console.status("  [cyan]verifying 2FA...[/cyan]", spinner="dots"):
                await client.sign_in(password=password)

        me = await client.get_me()
        if isinstance(me, User):
            console.print(f"  [bright_green]✓[/bright_green] logged in as [bold]{me.first_name}[/bold] (@{me.username})")
        else:
            console.print("  [bright_green]✓[/bright_green] logged in")

    # ── Step 3: Chat ACL ─────────────────────────────────────────────────
    _step_header(3, total_steps, "Chat access control")
    console.print("  [dim]Choose which chats the MCP server can read/write.[/dim]")

    with console.status("  [cyan]fetching chats...[/cyan]", spinner="dots"):
        dialogs = await _fetch_dialogs(client)
    console.print(f"  [bright_green]✓[/bright_green] found [bold]{len(dialogs)}[/bold] chats")

    await client.disconnect()

    prev_read = _parse_ids(existing.get("TELEGRAM_READ_CHATS", ""))
    prev_write = _parse_ids(existing.get("TELEGRAM_WRITE_CHATS", ""))

    console.print()
    console.print("  [bright_cyan]space[/bright_cyan] = toggle read    [bright_cyan]w[/bright_cyan] = toggle write    [bright_cyan]a[/bright_cyan] = all    [bright_cyan]enter[/bright_cyan] = done")
    console.print("  [green]●[/green] read    [green]●[/green][bright_cyan]◆[/bright_cyan] read+write    [bright_cyan]◆[/bright_cyan] write    [dim]· none[/dim]")

    read_ids, write_ids = _select_chats(dialogs, prev_read, prev_write)

    # Alias naming — for all selected chats (read or write)
    all_selected_ids = set(read_ids or []) | set(write_ids or [])
    prev_aliases = _parse_aliases_str(existing.get("TELEGRAM_ALIASES", ""))

    if all_selected_ids:
        console.print()
        console.print("  [bold bright_cyan]Name your chats[/bold bright_cyan] [dim](aliases let agents use names instead of IDs)[/dim]")
        console.print("  [dim]Press enter to skip, or type a short name for each chat.[/dim]")
        console.print()

        aliases: dict[str, int] = {}
        for cid in sorted(all_selected_ids):
            tg_name = next((n for c, n in dialogs if c == cid), str(cid))
            prev_alias = next((a for a, aid in prev_aliases.items() if aid == cid), "")
            alias = _input(f"  {tg_name}", prev_alias)
            if alias:
                aliases[alias.lower().replace(" ", "-")] = cid
        alias_val = ",".join(f"{name}:{cid}" for name, cid in aliases.items())
    else:
        alias_val = ""

    # ── Step 4: Listener automation ─────────────────────────────────────
    _step_header(4, total_steps, "Listener automation")
    console.print("  [dim]Configure optional chat listener and auto-response modes.[/dim]")

    previous_listener_modes = _parse_listener_modes(existing.get("TELEGRAM_LISTENER_CHATS", ""))
    listener_enabled = _parse_bool(existing.get("TELEGRAM_LISTENER_ENABLED", ""), False)
    enable_listener = _input("Enable listener? (y/n)", "y" if listener_enabled else "n").lower() == "y"

    listener_map: dict[int, str] = {}
    listener_approval_chat = existing.get("TELEGRAM_LISTENER_APPROVAL_CHAT", "")
    listener_agent_cmd = ""
    listener_system_prompt = existing.get("TELEGRAM_LISTENER_SYSTEM_PROMPT", "")
    claude_path = existing.get("TELEGRAM_LISTENER_CLAUDE_PATH", "claude")

    if enable_listener:
        if read_ids is None and write_ids is None:
            listener_candidates = dialogs
            writable_ids = {cid for cid, _ in dialogs}
        else:
            accessible_ids = set(read_ids or []) | set(write_ids or [])
            writable_ids = set(write_ids or [])
            listener_candidates = [(cid, name) for cid, name in dialogs if cid in accessible_ids]

        console.print()
        console.print("  [bright_cyan]Select listened chats[/bright_cyan]")
        selected_listener_ids = _select_listener_targets(listener_candidates, set(previous_listener_modes))

        for cid in selected_listener_ids:
            chat_name = next((name for did, name in listener_candidates if did == cid), str(cid))
            default_mode = previous_listener_modes.get(cid, "read")
            chosen_mode = _prompt_listener_mode(chat_name, cid in writable_ids, default_mode)
            if chosen_mode != "skip":
                listener_map[cid] = chosen_mode

        if any(mode in {"ask", "decide"} for mode in listener_map.values()):
            console.print()
            console.print("  [dim]When a chat is in ask/decide mode, the listener drafts a reply and[/dim]")
            console.print("  [dim]sends it to YOU for approval before posting. Where should approvals go?[/dim]")
            console.print("  [dim]Use an alias (e.g. saved-messages, xerxes) or a numeric chat ID.[/dim]")
            default_approval = listener_approval_chat or "saved-messages"
            listener_approval_chat = _input("Send approvals to", default_approval)

        if any(mode in {"ask", "auto", "decide"} for mode in listener_map.values()):
            console.print()
            console.print("  [dim]The listener uses an external agent CLI for auto-responses.[/dim]")
            claude_path = existing.get("TELEGRAM_LISTENER_CLAUDE_PATH", "claude")
            claude_path = _input("Agent CLI path", claude_path)
            listener_agent_cmd = ""  # retained for compatibility
            listener_system_prompt = _input("Custom instructions (optional, Enter to use default)", listener_system_prompt)
    else:
        listener_map = {}
        listener_approval_chat = ""
        listener_agent_cmd = ""
        listener_system_prompt = ""

    # Save config.env
    read_val = ",".join(str(i) for i in read_ids) if read_ids is not None else ""
    write_val = ",".join(str(i) for i in write_ids) if write_ids is not None else ""
    listener_val = ",".join(f"{cid}={mode}" for cid, mode in sorted(listener_map.items()))

    cfg = {
        "TELEGRAM_API_ID": api_id,
        "TELEGRAM_API_HASH": api_hash,
        "TELEGRAM_READ_CHATS": read_val,
        "TELEGRAM_WRITE_CHATS": write_val,
        "TELEGRAM_ALIASES": alias_val,
        "TELEGRAM_LISTENER_ENABLED": "true" if enable_listener else "false",
        "TELEGRAM_LISTENER_CHATS": listener_val,
        "TELEGRAM_LISTENER_APPROVAL_CHAT": listener_approval_chat,
        "TELEGRAM_LISTENER_CLAUDE_PATH": claude_path,
        "TELEGRAM_LISTENER_SYSTEM_PROMPT": listener_system_prompt,
    }
    _save_config(cfg)

    console.print()
    console.print(f"  [bright_green]✓[/bright_green] config saved to [dim]{CONFIG_ENV}[/dim]")

    # ACL summary
    if read_ids is None and write_ids is None:
        console.print("  [yellow]ACL: unrestricted[/yellow]")
    else:
        r_count = len(read_ids) if read_ids is not None else "all"
        w_count = len(write_ids) if write_ids is not None else "all"
        console.print(f"  [cyan]read:[/cyan] {r_count}  [cyan]write:[/cyan] {w_count}")

    if enable_listener:
        console.print(f"  [cyan]listener chats:[/cyan] {len(listener_map)}")
        modes_summary = ", ".join(f"{m}={sum(1 for v in listener_map.values() if v == m)}" for m in sorted(set(listener_map.values())))
        console.print(f"  [cyan]modes:[/cyan] {modes_summary}")
        if any(mode in {'ask', 'decide'} for mode in listener_map.values()):
            console.print(f"  [cyan]approval chat:[/cyan] {listener_approval_chat}")
        console.print(f"  [cyan]agent cli:[/cyan] {claude_path}")
        console.print("  [bright_green]listener auto-starts with the MCP server[/bright_green]")

    # ── Step 5: Select coding agents + scope ────────────────────────────
    _step_header(5, total_steps, "Coding agent integration")
    console.print("  [dim]Select which agents should get mcp-telegram access.[/dim]")

    agents = _select_agents()

    if not agents:
        console.print()
        console.print("  [yellow]No agents selected. Run setup again to add later.[/yellow]")
        _finish_animation()
        return

    console.print()
    install_global = _input("Install globally? (y/n)", "y").lower() == "y"

    project_dirs: list[Path] = []
    if not install_global:
        console.print()
        console.print("  [bold bright_cyan]Project directories[/bold bright_cyan] [dim](adds project-scoped config)[/dim]")
        project_dirs = _select_project_dirs()

    # ── Write configs ────────────────────────────────────────────────────
    console.print()
    results: list[tuple[str, str, bool]] = []  # (agent, path, success)

    with console.status("  [cyan]writing configs...[/cyan]", spinner="dots"):
        for agent in agents:
            if install_global:
                path = agent.global_path()
                try:
                    agent.write_config(path)
                    results.append((agent.display, str(path), True))
                except Exception as e:
                    results.append((agent.display, str(e), False))

            for proj_dir in project_dirs:
                path = agent.project_path(proj_dir)
                try:
                    agent.write_config(path)
                    results.append((agent.display, str(path), True))
                except Exception as e:
                    results.append((agent.display, str(e), False))

    # Results table
    table = Table(
        show_header=True,
        header_style="bold bright_cyan",
        border_style="dim cyan",
        padding=(0, 1),
        show_edge=False,
    )
    table.add_column("", width=2)
    table.add_column("Agent", min_width=20)
    table.add_column("Path", style="dim")

    for agent_name, path_str, ok in results:
        icon = "[bright_green]✓[/bright_green]" if ok else "[red]✗[/red]"
        table.add_row(icon, agent_name, path_str)
    console.print(table)

    # ── Finish ───────────────────────────────────────────────────────────
    _finish_animation()


def _finish_animation() -> None:
    """Play the completion animation."""
    console.print()

    done_text = """
  ┌──────────────────────────────────────┐
  │                                      │
  │     ✓  setup complete                │
  │                                      │
  │     restart your coding agents       │
  │     to pick up changes               │
  │                                      │
  └──────────────────────────────────────┘
"""
    _play_effect(done_text.strip(), "wipe", COLORS_GREEN)

    console.print()
    console.print(f"  [dim]config:[/dim]  {CONFIG_ENV}")
    console.print()


def run_setup() -> None:
    asyncio.run(_run_setup())
