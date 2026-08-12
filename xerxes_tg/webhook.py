"""Signed webhook delivery for temporary Telegram watch events."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse

from . import watch_store
from .telegram import CONFIG_ENV


def _config_env_value(name: str) -> str:
    if not CONFIG_ENV.exists():
        return ""
    for raw_line in CONFIG_ENV.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == name:
            return value.strip().strip("'\"")
    return ""


def _setting(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or _config_env_value(name) or default


@dataclass(frozen=True)
class WebhookConfig:
    url: str = ""
    secret: str = ""
    timeout_seconds: float = 10.0
    max_attempts: int = 8

    @classmethod
    def load(cls) -> WebhookConfig:
        timeout_raw = _setting("XERXES_TG_WEBHOOK_TIMEOUT_SECONDS", "10")
        attempts_raw = _setting("XERXES_TG_WEBHOOK_MAX_ATTEMPTS", "8")
        try:
            timeout = max(1.0, float(timeout_raw))
        except ValueError:
            timeout = 10.0
        try:
            attempts = max(1, int(attempts_raw))
        except ValueError:
            attempts = 8
        return cls(
            url=_setting("XERXES_TG_WEBHOOK_URL"),
            secret=_setting("XERXES_TG_WEBHOOK_SECRET"),
            timeout_seconds=timeout,
            max_attempts=attempts,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.url and self.secret)

    def validate(self) -> None:
        if not self.url:
            raise ValueError("XERXES_TG_WEBHOOK_URL is not configured")
        if not self.secret:
            raise ValueError("XERXES_TG_WEBHOOK_SECRET is not configured")
        parsed = urlparse(self.url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("XERXES_TG_WEBHOOK_URL must be an absolute http(s) URL")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("XERXES_TG_WEBHOOK_URL must use HTTPS unless it targets localhost")
        if parsed.username or parsed.password:
            raise ValueError("XERXES_TG_WEBHOOK_URL must not contain embedded credentials")

    @property
    def display_url(self) -> str:
        """Return a status-safe URL with query and fragment values removed."""
        if not self.url:
            return ""
        parsed = urlparse(self.url)
        query = "…" if parsed.query else ""
        fragment = "…" if parsed.fragment else ""
        return urlunparse((parsed.scheme, parsed.netloc, parsed.path, "", query, fragment))


def signed_headers(*, secret: str, body: bytes, event_id: str, timestamp: int) -> dict[str, str]:
    signed = str(timestamp).encode() + b"." + body
    digest = hmac.new(secret.encode(), signed, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "User-Agent": "xerxes-tg-webhook/1",
        "X-Xerxes-TG-Event-Id": event_id,
        "X-Xerxes-TG-Timestamp": str(timestamp),
        "X-Xerxes-TG-Signature": f"v1={digest}",
    }


def verify_signature(
    *,
    secret: str,
    body: bytes,
    timestamp: str,
    signature: str,
    now: int | None = None,
    tolerance_seconds: int = 300,
) -> bool:
    """Helper for webhook receivers and tests."""
    try:
        ts = int(timestamp)
    except ValueError:
        return False
    current = now if now is not None else int(time.time())
    if tolerance_seconds >= 0 and abs(current - ts) > tolerance_seconds:
        return False
    expected = signed_headers(secret=secret, body=body, event_id="", timestamp=ts)[
        "X-Xerxes-TG-Signature"
    ]
    return hmac.compare_digest(expected, signature)


def _post(config: WebhookConfig, event_id: str, payload: dict) -> None:
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
    timestamp = int(time.time())
    request = urllib.request.Request(
        config.url,
        data=body,
        headers=signed_headers(
            secret=config.secret,
            body=body,
            event_id=event_id,
            timestamp=timestamp,
        ),
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=config.timeout_seconds) as response:
            status = response.status
            response.read(1024)
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"webhook returned HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"webhook connection failed: {exc.reason}") from exc
    if not 200 <= status < 300:
        raise RuntimeError(f"webhook returned HTTP {status}")


async def deliver_due_once(
    config: WebhookConfig,
    *,
    db_path=watch_store.WATCH_DB,
) -> tuple[int, int]:
    """Deliver one due batch. Returns ``(delivered, failed)``."""
    config.validate()
    due = await watch_store.due_deliveries(db_path=db_path)
    delivered = 0
    failed = 0
    for item in due:
        try:
            await asyncio.to_thread(_post, config, item["event_id"], item["payload"])
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            failed += 1
            await watch_store.mark_delivery_failed(
                item["sequence"],
                f"{type(exc).__name__}: {exc}",
                max_attempts=config.max_attempts,
                db_path=db_path,
            )
        else:
            delivered += 1
            await watch_store.mark_delivered(item["sequence"], db_path=db_path)
    return delivered, failed


__all__ = [
    "WebhookConfig",
    "deliver_due_once",
    "signed_headers",
    "verify_signature",
]
