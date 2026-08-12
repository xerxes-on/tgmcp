"""JSONL audit log for every MCP tool call.

Keeps the last 48 hours of activity at
``~/.local/state/xerxes-tg/audit.jsonl``. Compaction runs lazily on write.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from xdg_base_dirs import xdg_state_home  # type: ignore[import-error]

logger = logging.getLogger(__name__)

AUDIT_PATH = xdg_state_home() / "xerxes-tg" / "audit.jsonl"
_RETENTION = timedelta(hours=48)
_COMPACT_INTERVAL = timedelta(hours=1)

_REDACT_KEYS = {"api_id", "api_hash", "api_key", "token", "password"}
_PREVIEW_CHARS = 200


def _redact(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            k: ("<redacted>" if k.lower() in _REDACT_KEYS else _redact(v))
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [_redact(v) for v in value]
    if isinstance(value, str) and len(value) > _PREVIEW_CHARS:
        return value[:_PREVIEW_CHARS] + "…"
    return value


def _preview(result: Any) -> str:
    if result is None:
        return ""
    if isinstance(result, str):
        text = result
    else:
        try:
            text = str(result)
        except Exception:
            return ""
    return text[:_PREVIEW_CHARS]


def _compact_if_stale() -> None:
    """Drop lines older than 48h. No-op if the log hasn't aged past the interval."""
    try:
        st = AUDIT_PATH.stat()
    except FileNotFoundError:
        return
    age = time.time() - st.st_mtime
    if age < _COMPACT_INTERVAL.total_seconds():
        return
    cutoff = (datetime.now(UTC) - _RETENTION).isoformat()
    try:
        kept: list[str] = []
        with AUDIT_PATH.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("ts", "") >= cutoff:
                    kept.append(line)
        tmp = AUDIT_PATH.with_suffix(".tmp")
        tmp.write_text("\n".join(kept) + ("\n" if kept else ""), encoding="utf-8")
        os.replace(tmp, AUDIT_PATH)
    except Exception:
        logger.exception("audit log compaction failed")


def write(
    tool: str,
    args: dict[str, Any],
    *,
    ok: bool,
    result_preview: str = "",
    error: str = "",
) -> None:
    """Append one audit record. Swallows all IO errors — never breaks a tool call."""
    AUDIT_PATH.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "ts": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "tool": tool,
        "ok": ok,
        "args": _redact(args),
    }
    if result_preview:
        record["result_preview"] = result_preview
    if error:
        record["error"] = error
    try:
        with AUDIT_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, default=str) + "\n")
    except Exception:
        logger.exception("audit log write failed")
    _compact_if_stale()


def audit_tool(func: Callable) -> Callable:
    """Decorator for tool_runner functions. Captures the tool name, args, and
    a result preview, logs success or failure."""

    @functools.wraps(func)
    async def wrapper(args):  # noqa: ANN001
        tool_name = type(args).__name__
        try:
            arg_dict = args.model_dump() if hasattr(args, "model_dump") else dict(args.__dict__)
        except Exception:
            arg_dict = {}
        try:
            result = await func(args)
        except Exception as exc:
            write(tool_name, arg_dict, ok=False, error=f"{type(exc).__name__}: {exc}")
            raise
        preview_text = ""
        try:
            if isinstance(result, list) and result:
                first = result[0]
                preview_text = _preview(getattr(first, "text", first))
        except Exception:
            preview_text = ""
        write(tool_name, arg_dict, ok=True, result_preview=preview_text)
        return result

    return wrapper


def tail(*, limit: int = 50, tool: str | None = None, since_hours: float | None = None) -> list[dict]:
    """Return the last ``limit`` audit records (newest last), optionally filtered."""
    if not AUDIT_PATH.exists():
        return []
    cutoff_iso = (
        (datetime.now(UTC) - timedelta(hours=since_hours)).isoformat() if since_hours else None
    )
    records: list[dict] = []
    with AUDIT_PATH.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if tool and rec.get("tool") != tool:
                continue
            if cutoff_iso and rec.get("ts", "") < cutoff_iso:
                continue
            records.append(rec)
    return records[-limit:]


__all__ = ["audit_tool", "write", "tail", "AUDIT_PATH"]


# Keep asyncio import reachable for type checkers.
_ = asyncio
