"""Auditable, owner-scoped webhook delivery for monitoring notifications.

Webhook delivery is deliberately downstream of the immutable local inbox. A
remote failure can never roll back an alert, scan, or notification record.
Secrets are read from the process environment and are never persisted.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import hmac
from ipaddress import ip_address
import json
import os
from pathlib import Path
import re
import socket
import ssl
import time
from typing import Any, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request

from .json_utils import load_unique_json


SCHEMA_VERSION = 1
MAX_OUTBOX_RECORDS = 7_000
MAX_ATTEMPT_RECORDS = 35_000
MAX_RECORD_BYTES = 512 * 1024
MAX_RESPONSE_BYTES = 64 * 1024
DELIVERY_ID = re.compile(r"webhook_[0-9a-f]{32}\Z")
ATTEMPT_ID = re.compile(r"webhook_attempt_[0-9a-f]{32}_[0-9]{3}\Z")
FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
PROFILE_ID = FINGERPRINT
NOTIFICATION_ID = re.compile(r"notification_[0-9a-f]{32}\Z")

_OUTBOX_FIELDS = frozenset(
    {
        "schema_version",
        "delivery_id",
        "profile_id",
        "notification_id",
        "notification_fingerprint",
        "created_at",
        "event",
        "target_origin",
        "target_fingerprint",
        "payload",
        "payload_sha256",
        "payload_bytes",
        "authority",
        "fingerprint",
    }
)
_ATTEMPT_FIELDS = frozenset(
    {
        "schema_version",
        "attempt_id",
        "delivery_id",
        "profile_id",
        "notification_id",
        "created_at",
        "sequence",
        "status",
        "http_status",
        "response_sha256",
        "response_bytes",
        "error_code",
        "error_message",
        "duration_ms",
        "fingerprint",
    }
)
_AUTHORITY = {
    "research_only": True,
    "execution_authorized": False,
    "strategy_changed": False,
    "paper_account_changed": False,
    "broker_permissions_changed": False,
}
_NOTIFICATION_PAYLOAD_FIELDS = (
    "notification_id",
    "created_at",
    "source_type",
    "source_id",
    "source_fingerprint",
    "evidence_fingerprint",
    "severity",
    "title",
    "message",
    "symbol",
    "data_date",
    "fingerprint",
)


@dataclass(frozen=True)
class WebhookSettings:
    url: str | None
    secret: bytes | None
    target_origin: str | None
    target_fingerprint: str | None
    timeout_seconds: float
    max_attempts: int
    retry_base_seconds: float
    batch_size: int
    configuration_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(
            self.url
            and self.secret
            and self.target_origin
            and self.target_fingerprint
            and not self.configuration_error
        )


class _RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args: Any, **_kwargs: Any) -> None:
        return None


_OPENER = urllib.request.build_opener(_RejectRedirects())


def load_webhook_settings(
    environ: Mapping[str, str] | None = None,
) -> WebhookSettings:
    """Load bounded non-secret status and secret delivery settings."""

    values = os.environ if environ is None else environ
    raw_url = str(values.get("AI_TRADE_WEBHOOK_URL", "")).strip()
    raw_secret = str(values.get("AI_TRADE_WEBHOOK_SECRET", ""))
    timeout = _environment_float(
        values, "AI_TRADE_WEBHOOK_TIMEOUT_SECONDS", 5.0, 1.0, 30.0
    )
    attempts = _environment_int(
        values, "AI_TRADE_WEBHOOK_MAX_ATTEMPTS", 3, 1, 5
    )
    retry_base = _environment_float(
        values, "AI_TRADE_WEBHOOK_RETRY_BASE_SECONDS", 0.5, 0.0, 10.0
    )
    batch_size = _environment_int(
        values, "AI_TRADE_WEBHOOK_BATCH_SIZE", 50, 1, 200
    )
    numeric_error = next(
        (
            item
            for item in (timeout, attempts, retry_base, batch_size)
            if isinstance(item, str)
        ),
        None,
    )
    if numeric_error:
        return _settings_error(str(numeric_error))
    if not raw_url and not raw_secret:
        return WebhookSettings(
            None,
            None,
            None,
            None,
            float(timeout),
            int(attempts),
            float(retry_base),
            int(batch_size),
        )
    if not raw_url or not raw_secret:
        return _settings_error(
            "AI_TRADE_WEBHOOK_URL and AI_TRADE_WEBHOOK_SECRET must be set together"
        )
    secret = raw_secret.encode("utf-8")
    if not 16 <= len(secret) <= 1_024:
        return _settings_error(
            "AI_TRADE_WEBHOOK_SECRET must contain 16 to 1024 UTF-8 bytes"
        )
    try:
        origin = _validate_url_syntax(raw_url)
    except ValueError as exc:
        return _settings_error(str(exc))
    return WebhookSettings(
        raw_url,
        secret,
        origin,
        sha256(raw_url.encode("utf-8")).hexdigest(),
        float(timeout),
        int(attempts),
        float(retry_base),
        int(batch_size),
    )


def deliver_webhook_notifications(
    profile_directory: str | Path,
    profile_id: str,
    notifications: Sequence[Mapping[str, Any]],
    *,
    settings: WebhookSettings | None = None,
) -> dict[str, Any]:
    """Deliver unread notifications without changing their local state."""

    selected = settings or load_webhook_settings()
    directory = Path(profile_directory).resolve()
    if not selected.enabled:
        return webhook_delivery_status(
            directory, profile_id, notifications, settings=selected
        )
    candidates = [
        dict(item)
        for item in notifications
        if item.get("status") == "unread"
    ][: selected.batch_size]
    try:
        from .monitoring import _file_lock

        with _file_lock(directory / ".webhook.lock"):
            for notification in candidates:
                _deliver_one(directory, profile_id, notification, selected)
    except Exception as exc:
        status = webhook_delivery_status(
            directory, profile_id, notifications, settings=selected
        )
        status["status"] = "failed"
        status["last_error"] = _safe_error(exc)
        return status
    return webhook_delivery_status(
        directory, profile_id, notifications, settings=selected
    )


def webhook_delivery_status(
    profile_directory: str | Path,
    profile_id: str,
    notifications: Sequence[Mapping[str, Any]],
    *,
    settings: WebhookSettings | None = None,
) -> dict[str, Any]:
    """Project non-secret delivery state from immutable local records."""

    selected = settings or load_webhook_settings()
    base = {
        "mode": "local_inbox+webhook" if selected.enabled else "local_inbox",
        "external_delivery_configured": selected.enabled,
        "configuration_status": (
            "invalid"
            if selected.configuration_error
            else "configured" if selected.enabled else "disabled"
        ),
        "endpoint_origin": selected.target_origin,
        "endpoint_fingerprint": selected.target_fingerprint,
        "status": (
            "configuration_error"
            if selected.configuration_error
            else "idle" if selected.enabled else "disabled"
        ),
        "pending_count": 0,
        "succeeded_count": 0,
        "failed_count": 0,
        "attempt_count": 0,
        "last_delivery_at": None,
        "last_error": selected.configuration_error,
        "authority": dict(_AUTHORITY),
    }
    if not PROFILE_ID.fullmatch(profile_id):
        base["status"] = "invalid_evidence"
        base["last_error"] = "monitoring profile id is invalid"
        return base
    directory = Path(profile_directory).resolve()
    try:
        by_notification = {
            str(item.get("notification_id")): dict(item)
            for item in notifications
            if NOTIFICATION_ID.fullmatch(str(item.get("notification_id", "")))
        }
        outboxes, attempts = verify_webhook_records(
            directory, profile_id, by_notification
        )
    except (OSError, RuntimeError, ValueError) as exc:
        base["status"] = "invalid_evidence"
        base["last_error"] = _safe_error(exc)
        return base
    target_outboxes = [
        item
        for item in outboxes
        if selected.target_fingerprint is not None
        and item["target_fingerprint"] == selected.target_fingerprint
    ]
    target_ids = {item["delivery_id"] for item in target_outboxes}
    target_attempts = [
        item for item in attempts if item["delivery_id"] in target_ids
    ]
    by_delivery: dict[str, list[dict[str, Any]]] = {}
    for attempt in target_attempts:
        by_delivery.setdefault(attempt["delivery_id"], []).append(attempt)
    succeeded = {
        delivery_id
        for delivery_id, items in by_delivery.items()
        if any(item["status"] == "succeeded" for item in items)
    }
    failed = {
        delivery_id
        for delivery_id, items in by_delivery.items()
        if items and delivery_id not in succeeded
    }
    eligible_ids = {
        str(item.get("notification_id"))
        for item in notifications
        if item.get("status") == "unread"
    }
    outbox_notification_ids = {
        item["notification_id"] for item in target_outboxes
    }
    base.update(
        {
            "pending_count": len(eligible_ids - outbox_notification_ids),
            "succeeded_count": len(succeeded),
            "failed_count": len(failed),
            "attempt_count": len(target_attempts),
            "last_delivery_at": max(
                (item["created_at"] for item in target_attempts), default=None
            ),
        }
    )
    if selected.enabled:
        if failed:
            base["status"] = "partial" if succeeded else "failed"
        elif succeeded:
            base["status"] = "succeeded"
    last_failed = max(
        (item for item in target_attempts if item["status"] != "succeeded"),
        key=lambda item: item["created_at"],
        default=None,
    )
    if last_failed:
        base["last_error"] = last_failed.get("error_message")
    return base


def verify_webhook_records(
    profile_directory: str | Path,
    profile_id: str,
    notifications: Mapping[str, Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Validate delivery evidence and its binding to local notifications."""

    if not PROFILE_ID.fullmatch(profile_id):
        raise ValueError("webhook profile id is invalid")
    directory = Path(profile_directory).resolve()
    outboxes = _read_directory(
        directory / "webhook_outbox",
        _OUTBOX_FIELDS,
        MAX_OUTBOX_RECORDS,
        DELIVERY_ID,
    )
    attempts = _read_directory(
        directory / "webhook_attempts",
        _ATTEMPT_FIELDS,
        MAX_ATTEMPT_RECORDS,
        ATTEMPT_ID,
    )
    by_delivery: dict[str, dict[str, Any]] = {}
    for record in outboxes:
        _validate_outbox(record, profile_id)
        notification = notifications.get(record["notification_id"])
        if notification is None or (
            notification.get("fingerprint")
            != record["notification_fingerprint"]
        ):
            raise RuntimeError(
                "Webhook outbox notification binding is invalid"
            )
        if record["delivery_id"] in by_delivery:
            raise RuntimeError("Webhook outbox delivery id is duplicated")
        by_delivery[record["delivery_id"]] = record
    by_attempt: dict[str, list[dict[str, Any]]] = {}
    for record in attempts:
        _validate_attempt(record, profile_id)
        outbox = by_delivery.get(record["delivery_id"])
        if outbox is None or (
            outbox["notification_id"] != record["notification_id"]
        ):
            raise RuntimeError("Webhook attempt outbox binding is invalid")
        by_attempt.setdefault(record["delivery_id"], []).append(record)
    for items in by_attempt.values():
        ordered = sorted(items, key=lambda item: item["sequence"])
        if [item["sequence"] for item in ordered] != list(
            range(1, len(ordered) + 1)
        ):
            raise RuntimeError("Webhook attempt sequence is not contiguous")
        success = [item for item in ordered if item["status"] == "succeeded"]
        if len(success) > 1 or (success and success[0] is not ordered[-1]):
            raise RuntimeError("Webhook success must terminate its attempt chain")
    return outboxes, attempts


def _deliver_one(
    directory: Path,
    profile_id: str,
    notification: Mapping[str, Any],
    settings: WebhookSettings,
) -> None:
    if not settings.enabled or settings.url is None or settings.secret is None:
        return
    outbox = _ensure_outbox(directory, profile_id, notification, settings)
    attempts = _attempts_for(directory, outbox["delivery_id"], profile_id)
    if any(item["status"] == "succeeded" for item in attempts):
        return
    if attempts and attempts[-1]["status"] == "permanent_failure":
        return
    sequence = len(attempts) + 1
    while sequence <= settings.max_attempts:
        started = time.monotonic()
        try:
            _validate_resolved_endpoint(settings.url)
            http_status, response = _post_webhook(
                settings,
                outbox["delivery_id"],
                outbox["payload"],
            )
            retryable = http_status in {408, 425, 429} or http_status >= 500
            succeeded = 200 <= http_status < 300
            status = (
                "succeeded"
                if succeeded
                else "retryable_failure" if retryable else "permanent_failure"
            )
            error_code = None if succeeded else f"http_{http_status}"
            error_message = (
                None
                if succeeded
                else f"Webhook returned HTTP {http_status}"
            )
        except Exception as exc:
            http_status = getattr(exc, "code", None)
            response = _http_error_body(exc)
            retryable = _retryable_error(exc)
            status = "retryable_failure" if retryable else "permanent_failure"
            error_code = _error_code(exc)
            error_message = _safe_error(exc)
        attempt = _attempt_record(
            outbox,
            sequence=sequence,
            status=status,
            http_status=http_status if isinstance(http_status, int) else None,
            response=response,
            error_code=error_code,
            error_message=error_message,
            duration_ms=max(0, round((time.monotonic() - started) * 1_000)),
        )
        _write_record(
            directory / "webhook_attempts" / f"{attempt['attempt_id']}.json",
            attempt,
            _ATTEMPT_FIELDS,
        )
        if status == "succeeded" or not retryable:
            return
        if sequence < settings.max_attempts and settings.retry_base_seconds:
            time.sleep(
                min(10.0, settings.retry_base_seconds * (2 ** (sequence - 1)))
            )
        sequence += 1


def _ensure_outbox(
    directory: Path,
    profile_id: str,
    notification: Mapping[str, Any],
    settings: WebhookSettings,
) -> dict[str, Any]:
    notification_id = str(notification.get("notification_id", ""))
    notification_fingerprint = str(notification.get("fingerprint", ""))
    if not NOTIFICATION_ID.fullmatch(notification_id) or not FINGERPRINT.fullmatch(
        notification_fingerprint
    ):
        raise ValueError("notification identity is invalid for webhook delivery")
    if settings.target_fingerprint is None or settings.target_origin is None:
        raise ValueError("webhook target is unavailable")
    delivery_id = "webhook_" + _hash_json(
        {
            "profile_id": profile_id,
            "notification_id": notification_id,
            "notification_fingerprint": notification_fingerprint,
            "target_fingerprint": settings.target_fingerprint,
        }
    )[:32]
    path = directory / "webhook_outbox" / f"{delivery_id}.json"
    if path.exists():
        record = _read_record(path, _OUTBOX_FIELDS)
        _validate_outbox(record, profile_id)
        return record
    public_notification = {
        key: notification.get(key) for key in _NOTIFICATION_PAYLOAD_FIELDS
    }
    payload = {
        "schema_version": SCHEMA_VERSION,
        "event": "monitoring.notification",
        "delivery_id": delivery_id,
        "notification": public_notification,
        "authority": dict(_AUTHORITY),
    }
    payload_bytes = _json_bytes(payload)
    record = {
        "schema_version": SCHEMA_VERSION,
        "delivery_id": delivery_id,
        "profile_id": profile_id,
        "notification_id": notification_id,
        "notification_fingerprint": notification_fingerprint,
        "created_at": _now(),
        "event": "monitoring.notification",
        "target_origin": settings.target_origin,
        "target_fingerprint": settings.target_fingerprint,
        "payload": payload,
        "payload_sha256": sha256(payload_bytes).hexdigest(),
        "payload_bytes": len(payload_bytes),
        "authority": dict(_AUTHORITY),
    }
    record["fingerprint"] = _record_fingerprint(record)
    _validate_outbox(record, profile_id)
    try:
        _write_record(path, record, _OUTBOX_FIELDS)
    except FileExistsError:
        record = _read_record(path, _OUTBOX_FIELDS)
        _validate_outbox(record, profile_id)
    return record


def _attempts_for(
    directory: Path, delivery_id: str, profile_id: str
) -> list[dict[str, Any]]:
    attempts = _read_directory(
        directory / "webhook_attempts",
        _ATTEMPT_FIELDS,
        MAX_ATTEMPT_RECORDS,
        ATTEMPT_ID,
    )
    selected = [item for item in attempts if item["delivery_id"] == delivery_id]
    for item in selected:
        _validate_attempt(item, profile_id)
    return sorted(selected, key=lambda item: item["sequence"])


def _attempt_record(
    outbox: Mapping[str, Any],
    *,
    sequence: int,
    status: str,
    http_status: int | None,
    response: bytes,
    error_code: str | None,
    error_message: str | None,
    duration_ms: int,
) -> dict[str, Any]:
    attempt_id = (
        "webhook_attempt_"
        + sha256(str(outbox["delivery_id"]).encode("ascii")).hexdigest()[:32]
        + f"_{sequence:03d}"
    )
    record = {
        "schema_version": SCHEMA_VERSION,
        "attempt_id": attempt_id,
        "delivery_id": outbox["delivery_id"],
        "profile_id": outbox["profile_id"],
        "notification_id": outbox["notification_id"],
        "created_at": _now(),
        "sequence": sequence,
        "status": status,
        "http_status": http_status,
        "response_sha256": sha256(response).hexdigest() if response else None,
        "response_bytes": len(response),
        "error_code": error_code,
        "error_message": error_message,
        "duration_ms": duration_ms,
    }
    record["fingerprint"] = _record_fingerprint(record)
    _validate_attempt(record, str(outbox["profile_id"]))
    return record


def _post_webhook(
    settings: WebhookSettings,
    delivery_id: str,
    payload: Mapping[str, Any],
) -> tuple[int, bytes]:
    if settings.url is None or settings.secret is None:
        raise ValueError("webhook settings are disabled")
    body = _json_bytes(payload)
    timestamp = str(int(time.time()))
    signature = hmac.new(
        settings.secret,
        timestamp.encode("ascii") + b"." + body,
        sha256,
    ).hexdigest()
    request = urllib.request.Request(
        settings.url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "Idempotency-Key": delivery_id,
            "User-Agent": "AI-Trade-Webhook/1",
            "X-AI-Trade-Delivery-Id": delivery_id,
            "X-AI-Trade-Event": "monitoring.notification",
            "X-AI-Trade-Signature": f"sha256={signature}",
            "X-AI-Trade-Timestamp": timestamp,
        },
    )
    with _open_request(request, settings.timeout_seconds) as response:
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RuntimeError("webhook response exceeds the 64 KiB limit")
        status = int(getattr(response, "status", response.getcode()))
    return status, raw


def _open_request(
    request: urllib.request.Request, timeout_seconds: float
) -> Any:
    return _OPENER.open(request, timeout=timeout_seconds)


def _validate_url_syntax(url: str) -> str:
    if len(url) > 2_048 or any(ord(char) < 32 for char in url):
        raise ValueError("AI_TRADE_WEBHOOK_URL is invalid")
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("AI_TRADE_WEBHOOK_URL must be an absolute HTTP(S) URL")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(
            "AI_TRADE_WEBHOOK_URL must not contain credentials, a query, or a fragment"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError("AI_TRADE_WEBHOOK_URL port is invalid") from exc
    host = parsed.hostname.rstrip(".").lower()
    loopback_host = _is_loopback_host(host)
    if parsed.scheme == "http" and not loopback_host:
        raise ValueError("external webhook endpoints must use HTTPS")
    if parsed.scheme == "http" and parsed.path not in {"", "/"}:
        # Loopback integration tests may use a path, but external HTTP remains
        # forbidden. This branch is intentionally a no-op after host validation.
        pass
    default_port = 443 if parsed.scheme == "https" else 80
    rendered_port = "" if port in {None, default_port} else f":{port}"
    host_text = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme}://{host_text}{rendered_port}"


def _validate_resolved_endpoint(url: str) -> None:
    parsed = urllib.parse.urlsplit(url)
    host = str(parsed.hostname or "").rstrip(".").lower()
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        addresses = {
            ip_address(item[4][0])
            for item in socket.getaddrinfo(
                host, port, type=socket.SOCK_STREAM
            )
        }
    except (OSError, ValueError) as exc:
        raise OSError("webhook hostname could not be resolved") from exc
    if not addresses:
        raise OSError("webhook hostname did not resolve to an address")
    if _is_loopback_host(host):
        if not all(item.is_loopback for item in addresses):
            raise ValueError("loopback webhook host resolved outside loopback")
        return
    if not all(item.is_global for item in addresses):
        raise ValueError("webhook endpoint resolved to a non-public address")


def _is_loopback_host(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ip_address(host).is_loopback
    except ValueError:
        return False


def _read_directory(
    directory: Path,
    fields: frozenset[str],
    maximum: int,
    name_pattern: re.Pattern[str],
) -> list[dict[str, Any]]:
    if directory.is_symlink():
        raise RuntimeError("Webhook evidence directory must not be a symbolic link")
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise RuntimeError("Webhook evidence path must be a directory")
    paths = list(directory.iterdir())
    if len(paths) > maximum:
        raise RuntimeError("Webhook evidence capacity is exceeded")
    records = []
    for path in paths:
        if path.is_symlink() or not path.is_file() or path.suffix != ".json":
            raise RuntimeError("Unexpected webhook evidence entry")
        if not name_pattern.fullmatch(path.stem):
            raise RuntimeError("Webhook evidence filename is invalid")
        records.append(_read_record(path, fields))
    return records


def _read_record(path: Path, fields: frozenset[str]) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Webhook evidence must be a regular file")
    value = load_unique_json(path, max_bytes=MAX_RECORD_BYTES)
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimeError("Webhook evidence schema is invalid")
    if value.get("fingerprint") != _record_fingerprint(value):
        raise RuntimeError("Webhook evidence fingerprint does not match")
    return value


def _write_record(
    path: Path, value: Mapping[str, Any], fields: frozenset[str]
) -> None:
    from .monitoring import _atomic_create_json

    _atomic_create_json(path, value, fields, MAX_RECORD_BYTES)


def _validate_outbox(value: Mapping[str, Any], profile_id: str) -> None:
    if set(value) != _OUTBOX_FIELDS or value.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("Webhook outbox schema is invalid")
    if not DELIVERY_ID.fullmatch(str(value.get("delivery_id", ""))):
        raise RuntimeError("Webhook delivery id is invalid")
    if value.get("profile_id") != profile_id:
        raise RuntimeError("Webhook outbox profile binding is invalid")
    if not NOTIFICATION_ID.fullmatch(str(value.get("notification_id", ""))):
        raise RuntimeError("Webhook notification id is invalid")
    for field in (
        "notification_fingerprint",
        "target_fingerprint",
        "payload_sha256",
        "fingerprint",
    ):
        if not FINGERPRINT.fullmatch(str(value.get(field, ""))):
            raise RuntimeError(f"Webhook outbox {field} is invalid")
    _valid_timestamp(value.get("created_at"))
    if value.get("event") != "monitoring.notification":
        raise RuntimeError("Webhook event is invalid")
    origin = str(value.get("target_origin", ""))
    if _validate_url_syntax(origin) != origin:
        raise RuntimeError("Webhook target origin is not canonical")
    payload = value.get("payload")
    if not isinstance(payload, dict) or payload.get("delivery_id") != value.get(
        "delivery_id"
    ):
        raise RuntimeError("Webhook payload delivery binding is invalid")
    encoded = _json_bytes(payload)
    if len(encoded) != value.get("payload_bytes") or sha256(encoded).hexdigest() != value.get(
        "payload_sha256"
    ):
        raise RuntimeError("Webhook payload fingerprint is invalid")
    if value.get("authority") != _AUTHORITY or payload.get("authority") != _AUTHORITY:
        raise RuntimeError("Webhook authority boundary is invalid")
    if value.get("fingerprint") != _record_fingerprint(value):
        raise RuntimeError("Webhook outbox fingerprint does not match")


def _validate_attempt(value: Mapping[str, Any], profile_id: str) -> None:
    if set(value) != _ATTEMPT_FIELDS or value.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError("Webhook attempt schema is invalid")
    if not ATTEMPT_ID.fullmatch(str(value.get("attempt_id", ""))):
        raise RuntimeError("Webhook attempt id is invalid")
    if not DELIVERY_ID.fullmatch(str(value.get("delivery_id", ""))):
        raise RuntimeError("Webhook attempt delivery id is invalid")
    if value.get("profile_id") != profile_id:
        raise RuntimeError("Webhook attempt profile binding is invalid")
    if not NOTIFICATION_ID.fullmatch(str(value.get("notification_id", ""))):
        raise RuntimeError("Webhook attempt notification id is invalid")
    _valid_timestamp(value.get("created_at"))
    sequence = value.get("sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or not 1 <= sequence <= 999:
        raise RuntimeError("Webhook attempt sequence is invalid")
    if value.get("status") not in {
        "succeeded",
        "retryable_failure",
        "permanent_failure",
    }:
        raise RuntimeError("Webhook attempt status is invalid")
    http_status = value.get("http_status")
    if http_status is not None and (
        isinstance(http_status, bool)
        or not isinstance(http_status, int)
        or not 100 <= http_status <= 599
    ):
        raise RuntimeError("Webhook HTTP status is invalid")
    response_sha = value.get("response_sha256")
    response_bytes = value.get("response_bytes")
    if (
        isinstance(response_bytes, bool)
        or not isinstance(response_bytes, int)
        or not 0 <= response_bytes <= MAX_RESPONSE_BYTES
    ):
        raise RuntimeError("Webhook response size is invalid")
    if response_bytes and not FINGERPRINT.fullmatch(str(response_sha or "")):
        raise RuntimeError("Webhook response fingerprint is invalid")
    if not response_bytes and response_sha is not None:
        raise RuntimeError("Empty webhook response must not have a fingerprint")
    if value.get("status") == "succeeded":
        if not isinstance(http_status, int) or not 200 <= http_status < 300:
            raise RuntimeError("Webhook success HTTP status is invalid")
        if value.get("error_code") is not None or value.get("error_message") is not None:
            raise RuntimeError("Webhook success must not contain an error")
    else:
        if not _bounded_optional_text(value.get("error_code"), 100):
            raise RuntimeError("Webhook failure code is invalid")
        if not _bounded_optional_text(value.get("error_message"), 500):
            raise RuntimeError("Webhook failure message is invalid")
    duration = value.get("duration_ms")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
        raise RuntimeError("Webhook duration is invalid")
    if value.get("fingerprint") != _record_fingerprint(value):
        raise RuntimeError("Webhook attempt fingerprint does not match")


def _environment_int(
    values: Mapping[str, str], name: str, default: int, minimum: int, maximum: int
) -> int | str:
    raw = str(values.get(name, default)).strip()
    try:
        parsed = int(raw)
    except ValueError:
        return f"{name} must be an integer"
    if not minimum <= parsed <= maximum:
        return f"{name} must be between {minimum} and {maximum}"
    return parsed


def _environment_float(
    values: Mapping[str, str],
    name: str,
    default: float,
    minimum: float,
    maximum: float,
) -> float | str:
    raw = str(values.get(name, default)).strip()
    try:
        parsed = float(raw)
    except ValueError:
        return f"{name} must be numeric"
    if not minimum <= parsed <= maximum:
        return f"{name} must be between {minimum:g} and {maximum:g}"
    return parsed


def _settings_error(message: str) -> WebhookSettings:
    return WebhookSettings(None, None, None, None, 5.0, 3, 0.5, 50, message)


def _record_fingerprint(value: Mapping[str, Any]) -> str:
    return _hash_json({key: item for key, item in value.items() if key != "fingerprint"})


def _hash_json(value: Mapping[str, Any]) -> str:
    return sha256(_json_bytes(value)).hexdigest()


def _json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")


def _valid_timestamp(value: Any) -> str:
    if not isinstance(value, str):
        raise RuntimeError("Webhook timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError("Webhook timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise RuntimeError("Webhook timestamp must include a timezone")
    return value


def _bounded_optional_text(value: Any, maximum: int) -> bool:
    return isinstance(value, str) and 0 < len(value) <= maximum and not any(
        ord(char) < 32 for char in value
    )


def _http_error_body(exc: Exception) -> bytes:
    if not isinstance(exc, urllib.error.HTTPError):
        return b""
    try:
        raw = exc.read(MAX_RESPONSE_BYTES + 1)
    except OSError:
        return b""
    return raw[:MAX_RESPONSE_BYTES]


def _retryable_error(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code in {408, 425, 429} or exc.code >= 500
    return isinstance(
        exc,
        (
            TimeoutError,
            socket.timeout,
            ssl.SSLError,
            urllib.error.URLError,
            OSError,
        ),
    )


def _error_code(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"http_{exc.code}"
    if isinstance(exc, (TimeoutError, socket.timeout)):
        return "timeout"
    if isinstance(exc, ssl.SSLError):
        return "tls_error"
    if isinstance(exc, urllib.error.URLError):
        return "transport_error"
    if isinstance(exc, ValueError):
        return "endpoint_rejected"
    return "delivery_error"


def _safe_error(exc: Exception) -> str:
    text = str(exc).strip().replace("\r", " ").replace("\n", " ")
    return (text or type(exc).__name__)[:500]


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


__all__ = [
    "WebhookSettings",
    "deliver_webhook_notifications",
    "load_webhook_settings",
    "verify_webhook_records",
    "webhook_delivery_status",
]
