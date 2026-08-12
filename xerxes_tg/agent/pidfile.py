from __future__ import annotations

import os
import signal

from .config import AGENT_PID


def write_pidfile() -> None:
    AGENT_PID.parent.mkdir(parents=True, exist_ok=True)
    AGENT_PID.write_text(str(os.getpid()))


def read_pidfile() -> int | None:
    if not AGENT_PID.exists():
        return None
    try:
        return int(AGENT_PID.read_text().strip())
    except (ValueError, OSError):
        return None


def remove_pidfile() -> None:
    try:
        AGENT_PID.unlink(missing_ok=True)
    except OSError:
        pass


def is_running() -> bool:
    pid = read_pidfile()
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False
