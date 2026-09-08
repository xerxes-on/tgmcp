"""Deliver Telegram watch events into the coding session that created the watch."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import os
import secrets
import shutil
import struct
from dataclasses import dataclass
from importlib.metadata import version
from pathlib import Path
from typing import Any

from . import watch_store
from .watch_notice import event_notice

DEFAULT_CODEX_SOCKET = Path.home() / ".codex" / "app-server-control" / "app-server-control.sock"
MAX_ATTEMPTS = 8


class CodexDeliveryError(RuntimeError):
    """Raised when neither live injection nor durable queueing succeeds."""


class _LiveDeliveryUnavailable(RuntimeError):
    """Raised when the target thread is not loaded on the shared app server."""


@dataclass(frozen=True)
class DeliveryResult:
    method: str


def current_thread_id(explicit: str | None = None) -> str | None:
    """Resolve the originating thread without guessing another active chat."""
    for value in (
        explicit,
        os.environ.get("CODEX_THREAD_ID"),
        os.environ.get("CODEX_SESSION_ID"),
    ):
        if value and value.strip():
            return value.strip()
    return None


def _codex_binary() -> str:
    configured = os.environ.get("XERXES_TG_CODEX_BIN", "").strip()
    if configured:
        return configured
    binary = shutil.which("codex")
    if binary is None:
        raise CodexDeliveryError("codex executable was not found")
    return binary


def _codex_socket() -> Path:
    configured = os.environ.get("XERXES_TG_CODEX_SOCKET", "").strip()
    return Path(configured).expanduser() if configured else DEFAULT_CODEX_SOCKET


def _event_context(event: dict[str, Any]) -> str:
    sender = event.get("sender") or {}
    payload = {
        "source": "xerxes-tg",
        "trust": "untrusted Telegram content",
        "event_id": event.get("event_id"),
        "watch_id": event.get("watch_id"),
        "watch_mode": event.get("watch_mode"),
        "chat_id": event.get("chat_id"),
        "message_id": event.get("message_id"),
        "reply_to_message_id": event.get("reply_to_message_id"),
        "sender": {
            "id": sender.get("id"),
            "name": sender.get("name"),
        },
        "text": event.get("text") or "",
        "received_at": event.get("received_at"),
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class _AppServerConnection:
    def __init__(self, socket_path: Path, *, timeout_seconds: float = 10.0) -> None:
        self.socket_path = socket_path
        self.timeout_seconds = timeout_seconds
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._request_id = 0

    async def __aenter__(self) -> _AppServerConnection:
        try:
            self.reader, self.writer = await asyncio.wait_for(
                asyncio.open_unix_connection(self.socket_path),
                timeout=self.timeout_seconds,
            )
            await self._websocket_handshake()
            await self.request(
                "initialize",
                {
                    "clientInfo": {
                        "name": "xerxes_tg",
                        "title": "xerxes-tg",
                        "version": version("xerxes-tg"),
                    }
                },
            )
            await self.notify("initialized", {})
        except _LiveDeliveryUnavailable:
            await self._close()
            raise
        except (OSError, TimeoutError, asyncio.IncompleteReadError) as exc:
            await self._close()
            raise _LiveDeliveryUnavailable(
                f"failed to connect to Codex app-server: {type(exc).__name__}"
            ) from exc
        return self

    async def __aexit__(self, *_: object) -> None:
        try:
            await self._send_frame(b"", opcode=0x8)
        except (OSError, _LiveDeliveryUnavailable):
            pass
        await self._close()

    async def notify(self, method: str, params: dict[str, Any]) -> None:
        await self._write({"method": method, "params": params})

    async def request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._request_id += 1
        request_id = self._request_id
        await self._write({"method": method, "id": request_id, "params": params})
        while True:
            try:
                message = await asyncio.wait_for(
                    self._read_text_frame(), timeout=self.timeout_seconds
                )
            except TimeoutError as exc:
                raise _LiveDeliveryUnavailable(
                    f"Codex app-server request timed out: {method}"
                ) from exc
            except (OSError, asyncio.IncompleteReadError) as exc:
                raise _LiveDeliveryUnavailable(
                    "Codex app-server disconnected during a request"
                ) from exc
            try:
                response = json.loads(message)
            except json.JSONDecodeError:
                continue
            if response.get("id") != request_id:
                continue
            if response.get("error") is not None:
                error = response["error"]
                message = error.get("message") if isinstance(error, dict) else str(error)
                raise _LiveDeliveryUnavailable(f"{method}: {message}")
            result = response.get("result")
            return result if isinstance(result, dict) else {}

    async def _write(self, message: dict[str, Any]) -> None:
        payload = json.dumps(message, separators=(",", ":")).encode()
        await self._send_frame(payload, opcode=0x1)

    async def _websocket_handshake(self) -> None:
        reader = self.reader
        writer = self.writer
        if reader is None or writer is None:
            raise _LiveDeliveryUnavailable("Codex app-server socket is unavailable")
        nonce = base64.b64encode(secrets.token_bytes(16)).decode()
        request = (
            "GET / HTTP/1.1\r\n"
            "Host: localhost\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {nonce}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        writer.write(request.encode())
        await writer.drain()
        response = await asyncio.wait_for(
            reader.readuntil(b"\r\n\r\n"), timeout=self.timeout_seconds
        )
        lines = response.decode("latin-1").split("\r\n")
        if not lines or " 101 " not in lines[0]:
            raise _LiveDeliveryUnavailable("Codex app-server rejected WebSocket upgrade")
        headers = {
            key.strip().lower(): value.strip()
            for line in lines[1:]
            if ":" in line
            for key, value in [line.split(":", 1)]
        }
        expected = base64.b64encode(
            hashlib.sha1(
                (nonce + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()
            ).digest()
        ).decode()
        if headers.get("sec-websocket-accept") != expected:
            raise _LiveDeliveryUnavailable("Codex app-server WebSocket handshake is invalid")

    async def _send_frame(self, payload: bytes, *, opcode: int) -> None:
        writer = self.writer
        if writer is None:
            raise _LiveDeliveryUnavailable("Codex app-server socket is unavailable")
        length = len(payload)
        header = bytearray([0x80 | opcode])
        if length < 126:
            header.append(0x80 | length)
        elif length <= 0xFFFF:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = secrets.token_bytes(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        writer.write(bytes(header) + masked)
        try:
            await writer.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise _LiveDeliveryUnavailable("Codex app-server disconnected") from exc

    async def _read_text_frame(self) -> str:
        reader = self.reader
        if reader is None:
            raise _LiveDeliveryUnavailable("Codex app-server socket is unavailable")
        fragments = bytearray()
        text_started = False
        while True:
            try:
                first, second = await reader.readexactly(2)
            except asyncio.IncompleteReadError as exc:
                raise _LiveDeliveryUnavailable("Codex app-server closed the socket") from exc
            final = bool(first & 0x80)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = struct.unpack("!H", await reader.readexactly(2))[0]
            elif length == 127:
                length = struct.unpack("!Q", await reader.readexactly(8))[0]
            if length > 8 * 1024 * 1024:
                raise _LiveDeliveryUnavailable("Codex app-server frame is too large")
            mask = await reader.readexactly(4) if masked else b""
            payload = await reader.readexactly(length)
            if masked:
                payload = bytes(
                    byte ^ mask[index % 4] for index, byte in enumerate(payload)
                )

            if opcode == 0x8:
                raise _LiveDeliveryUnavailable("Codex app-server closed the connection")
            if opcode == 0x9:
                await self._send_frame(payload, opcode=0xA)
                continue
            if opcode == 0xA:
                continue
            if opcode == 0x1:
                fragments = bytearray(payload)
                text_started = True
            elif opcode == 0x0 and text_started:
                fragments.extend(payload)
            else:
                raise _LiveDeliveryUnavailable("Codex app-server sent an unsupported frame")
            if final:
                try:
                    return fragments.decode()
                except UnicodeDecodeError as exc:
                    raise _LiveDeliveryUnavailable(
                        "Codex app-server sent invalid UTF-8"
                    ) from exc

    async def _close(self) -> None:
        writer = self.writer
        self.reader = None
        self.writer = None
        if writer is None:
            return
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


def _turn_input(event: dict[str, Any]) -> dict[str, Any]:
    return {
        "input": [{"type": "text", "text": event_notice(event)}],
        "additionalContext": {
            "xerxes-tg.telegram-event": {
                "kind": "untrusted",
                "value": _event_context(event),
            }
        },
        "clientUserMessageId": str(event["event_id"]),
        "responsesapiClientMetadata": {
            "xerxes_tg_event_id": str(event["event_id"]),
            "xerxes_tg_watch_id": str(event["watch_id"]),
        },
    }


async def _deliver_live(thread_id: str, event: dict[str, Any]) -> DeliveryResult:
    socket_path = _codex_socket()
    if not socket_path.exists():
        raise _LiveDeliveryUnavailable(f"Codex app-server socket is absent: {socket_path}")

    async with _AppServerConnection(socket_path) as client:
        response = await client.request(
            "thread/read", {"threadId": thread_id, "includeTurns": True}
        )
        thread = response.get("thread")
        if not isinstance(thread, dict):
            raise _LiveDeliveryUnavailable("Codex thread/read returned no thread")
        status = thread.get("status")
        status_type = status.get("type") if isinstance(status, dict) else None
        params = {"threadId": thread_id, **_turn_input(event)}

        if status_type == "active":
            turns = thread.get("turns") or []
            active_turn = next(
                (
                    turn
                    for turn in reversed(turns)
                    if isinstance(turn, dict) and turn.get("status") == "inProgress"
                ),
                None,
            )
            if active_turn is None:
                raise _LiveDeliveryUnavailable("active Codex thread has no steerable turn")
            params["expectedTurnId"] = active_turn["id"]
            await client.request("turn/steer", params)
            return DeliveryResult(method="steered")

        if status_type == "idle":
            await client.request("turn/start", params)
            return DeliveryResult(method="started")

        raise _LiveDeliveryUnavailable(
            f"Codex thread is not loaded on the shared app server ({status_type})"
        )


async def _queue_for_thread(thread_id: str, event: dict[str, Any]) -> DeliveryResult:
    process = await asyncio.create_subprocess_exec(
        _codex_binary(),
        "queue",
        "--thread",
        thread_id,
        "--message",
        event_notice(event),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=10.0)
    except TimeoutError as exc:
        process.kill()
        await process.wait()
        raise CodexDeliveryError("codex queue timed out") from exc
    if process.returncode != 0:
        detail = stderr.decode(errors="replace").strip() or stdout.decode(
            errors="replace"
        ).strip()
        raise CodexDeliveryError(f"codex queue failed: {detail[:300]}")
    return DeliveryResult(method="queued")


async def deliver_to_thread(thread_id: str, event: dict[str, Any]) -> DeliveryResult:
    """Steer/start a shared-app-server chat, falling back to its durable queue."""
    try:
        return await _deliver_live(thread_id, event)
    except _LiveDeliveryUnavailable as live_error:
        try:
            return await _queue_for_thread(thread_id, event)
        except CodexDeliveryError as queue_error:
            raise CodexDeliveryError(
                f"live injection unavailable ({live_error}); {queue_error}"
            ) from queue_error


async def deliver_due_once(
    *,
    db_path: Path = watch_store.WATCH_DB,
    max_attempts: int = MAX_ATTEMPTS,
) -> tuple[int, int]:
    """Deliver one due batch, deduplicated per thread and Telegram message."""
    due = await watch_store.due_codex_deliveries(db_path=db_path)
    grouped: dict[tuple[str, int, int], list[dict[str, Any]]] = {}
    for item in due:
        key = (item["thread_id"], item["dialog_id"], item["message_id"])
        grouped.setdefault(key, []).append(item)

    delivered = 0
    failed = 0
    for (thread_id, _, _), items in grouped.items():
        sequences = [int(item["sequence"]) for item in items]
        try:
            result = await deliver_to_thread(thread_id, items[0]["payload"])
        except (CodexDeliveryError, OSError, ValueError) as exc:
            failed += 1
            await watch_store.mark_codex_delivery_failed(
                sequences,
                f"{type(exc).__name__}: {exc}",
                max_attempts=max_attempts,
                db_path=db_path,
            )
        else:
            delivered += 1
            await watch_store.mark_codex_delivered(
                sequences, method=result.method, db_path=db_path
            )
    return delivered, failed


__all__ = [
    "CodexDeliveryError",
    "DEFAULT_CODEX_SOCKET",
    "DeliveryResult",
    "current_thread_id",
    "deliver_due_once",
    "deliver_to_thread",
]
