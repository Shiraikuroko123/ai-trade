"""Audited email and Windows Toast delivery for monitoring notifications."""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import smtplib
import ssl
import subprocess
import sys
import time
from typing import Any, Mapping, Sequence

from .data.evidence_io import atomic_create_json, evidence_store_lock
from .json_utils import load_unique_json


SCHEMA_VERSION = 1
MAX_ATTEMPTS = 35_000
MAX_RECORD_BYTES = 128 * 1024
FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
PROFILE_ID = FINGERPRINT
NOTIFICATION_ID = re.compile(r"notification_[0-9a-f]{32}\Z")
ATTEMPT_FILE = re.compile(
    r"delivery_attempt_(?:email|desktop)_[0-9a-f]{32}_[0-9]{3}\.json\Z"
)
EMAIL_ADDRESS = re.compile(r"[^\s@]{1,128}@[^\s@]{1,190}\Z")

_FIELDS = frozenset(
    {
        "schema_version",
        "attempt_id",
        "channel",
        "profile_id",
        "notification_id",
        "notification_fingerprint",
        "target_fingerprint",
        "created_at",
        "sequence",
        "status",
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


@dataclass(frozen=True)
class EmailSettings:
    host: str | None
    port: int
    security: str
    username: str | None
    password: str | None
    sender: str | None
    recipient: str | None
    timeout_seconds: float
    max_attempts: int
    batch_size: int
    target_fingerprint: str | None
    configuration_error: str | None = None

    @property
    def enabled(self) -> bool:
        return bool(
            self.host
            and self.sender
            and self.recipient
            and self.target_fingerprint
            and not self.configuration_error
        )


@dataclass(frozen=True)
class DesktopSettings:
    requested: bool
    enabled: bool
    batch_size: int
    target_fingerprint: str | None
    configuration_error: str | None = None


def load_email_settings(environ: Mapping[str, str] | None = None) -> EmailSettings:
    values = os.environ if environ is None else environ
    host = str(values.get("AI_TRADE_EMAIL_SMTP_HOST", "")).strip()
    sender = str(values.get("AI_TRADE_EMAIL_FROM", "")).strip()
    recipient = str(values.get("AI_TRADE_EMAIL_TO", "")).strip()
    username = str(values.get("AI_TRADE_EMAIL_USERNAME", "")).strip()
    password = str(values.get("AI_TRADE_EMAIL_PASSWORD", ""))
    security = str(values.get("AI_TRADE_EMAIL_SECURITY", "starttls")).strip().lower()
    try:
        port = _integer(values, "AI_TRADE_EMAIL_SMTP_PORT", 587, 1, 65535)
        timeout = _number(values, "AI_TRADE_EMAIL_TIMEOUT_SECONDS", 10.0, 1.0, 60.0)
        attempts = _integer(values, "AI_TRADE_EMAIL_MAX_ATTEMPTS", 3, 1, 5)
        batch = _integer(values, "AI_TRADE_EMAIL_BATCH_SIZE", 20, 1, 100)
    except ValueError as exc:
        return _email_error(str(exc))
    supplied = any((host, sender, recipient, username, password))
    if not supplied:
        return EmailSettings(None, port, security, None, None, None, None, timeout, attempts, batch, None)
    if not host or len(host) > 253 or any(ord(char) < 33 for char in host):
        return _email_error("AI_TRADE_EMAIL_SMTP_HOST is invalid")
    if security not in {"starttls", "ssl"}:
        return _email_error("AI_TRADE_EMAIL_SECURITY must be starttls or ssl")
    if EMAIL_ADDRESS.fullmatch(sender) is None or EMAIL_ADDRESS.fullmatch(recipient) is None:
        return _email_error("AI_TRADE_EMAIL_FROM and AI_TRADE_EMAIL_TO must be valid addresses")
    if bool(username) != bool(password):
        return _email_error("AI_TRADE_EMAIL_USERNAME and AI_TRADE_EMAIL_PASSWORD must be set together")
    target = sha256(
        f"{host.lower()}:{port}:{security}:{recipient.lower()}".encode("utf-8")
    ).hexdigest()
    return EmailSettings(
        host,
        port,
        security,
        username or None,
        password or None,
        sender,
        recipient,
        timeout,
        attempts,
        batch,
        target,
    )


def load_desktop_settings(
    environ: Mapping[str, str] | None = None,
    *,
    platform: str | None = None,
) -> DesktopSettings:
    values = os.environ if environ is None else environ
    raw = str(values.get("AI_TRADE_DESKTOP_NOTIFICATIONS", "0")).strip().lower()
    if raw not in {"0", "1", "false", "true", "no", "yes", "off", "on"}:
        return DesktopSettings(False, False, 20, None, "AI_TRADE_DESKTOP_NOTIFICATIONS must be a boolean")
    requested = raw in {"1", "true", "yes", "on"}
    try:
        batch = _integer(values, "AI_TRADE_DESKTOP_BATCH_SIZE", 20, 1, 100)
    except ValueError as exc:
        return DesktopSettings(requested, False, 20, None, str(exc))
    selected_platform = sys.platform if platform is None else platform
    if requested and selected_platform != "win32":
        return DesktopSettings(requested, False, batch, None, "Desktop Toast delivery is supported only on Windows")
    fingerprint = sha256(b"windows-toast:AI Trade:v1").hexdigest() if requested else None
    return DesktopSettings(requested, requested, batch, fingerprint)


def configured_channel_delivery(
    *,
    email: EmailSettings | None = None,
    desktop: DesktopSettings | None = None,
) -> bool:
    selected_email = email or load_email_settings()
    selected_desktop = desktop or load_desktop_settings()
    return selected_email.enabled or selected_desktop.enabled


def external_delivery_configured() -> bool:
    from .notification_delivery import load_webhook_settings

    return load_webhook_settings().enabled or configured_channel_delivery()


def external_delivery_status(
    profile_directory: str | Path,
    profile_id: str,
    notifications: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    from .notification_delivery import webhook_delivery_status

    webhook = webhook_delivery_status(profile_directory, profile_id, notifications)
    channels = channel_delivery_status(profile_directory, profile_id, notifications)
    return _combined_status(webhook, channels)


def deliver_external_notifications(
    profile_directory: str | Path,
    profile_id: str,
    notifications: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    from .notification_delivery import (
        deliver_webhook_notifications,
        load_webhook_settings,
        webhook_delivery_status,
    )

    webhook_settings = load_webhook_settings()
    webhook = (
        deliver_webhook_notifications(
            profile_directory,
            profile_id,
            notifications,
            settings=webhook_settings,
        )
        if webhook_settings.enabled
        else webhook_delivery_status(
            profile_directory,
            profile_id,
            notifications,
            settings=webhook_settings,
        )
    )
    channels = deliver_channel_notifications(
        profile_directory, profile_id, notifications
    )
    return _combined_status(webhook, channels)


def deliver_channel_notifications(
    profile_directory: str | Path,
    profile_id: str,
    notifications: Sequence[Mapping[str, Any]],
    *,
    email: EmailSettings | None = None,
    desktop: DesktopSettings | None = None,
) -> dict[str, Any]:
    selected_email = email or load_email_settings()
    selected_desktop = desktop or load_desktop_settings()
    directory = Path(profile_directory).resolve()
    notification_map = {
        str(item.get("notification_id")): item for item in notifications
    }
    if selected_email.enabled:
        for notification in _eligible(notifications, selected_email.batch_size):
            _attempt_delivery(
                directory,
                profile_id,
                notification,
                notification_map=notification_map,
                channel="email",
                target_fingerprint=str(selected_email.target_fingerprint),
                maximum_attempts=selected_email.max_attempts,
                sender=lambda item: _send_email(selected_email, item),
            )
    if selected_desktop.enabled:
        for notification in _eligible(notifications, selected_desktop.batch_size):
            _attempt_delivery(
                directory,
                profile_id,
                notification,
                notification_map=notification_map,
                channel="desktop",
                target_fingerprint=str(selected_desktop.target_fingerprint),
                maximum_attempts=1,
                sender=_send_windows_toast,
            )
    return channel_delivery_status(
        directory,
        profile_id,
        notifications,
        email=selected_email,
        desktop=selected_desktop,
    )


def channel_delivery_status(
    profile_directory: str | Path,
    profile_id: str,
    notifications: Sequence[Mapping[str, Any]],
    *,
    email: EmailSettings | None = None,
    desktop: DesktopSettings | None = None,
) -> dict[str, Any]:
    selected_email = email or load_email_settings()
    selected_desktop = desktop or load_desktop_settings()
    notification_map = {
        str(item.get("notification_id")): item for item in notifications
    }
    try:
        attempts = verify_channel_records(
            profile_directory, profile_id, notification_map
        )
    except (OSError, RuntimeError, ValueError) as exc:
        return {
            "status": "invalid_evidence",
            "configured": selected_email.enabled or selected_desktop.enabled,
            "email": _empty_channel(selected_email.enabled, selected_email.configuration_error),
            "desktop": _empty_channel(selected_desktop.enabled, selected_desktop.configuration_error),
            "last_error": str(exc)[:500],
            "authority": dict(_AUTHORITY),
        }
    email_status = _channel_status(
        attempts,
        notifications,
        "email",
        selected_email.enabled,
        selected_email.target_fingerprint,
        selected_email.configuration_error,
    )
    desktop_status = _channel_status(
        attempts,
        notifications,
        "desktop",
        selected_desktop.enabled,
        selected_desktop.target_fingerprint,
        selected_desktop.configuration_error,
    )
    channels = (email_status, desktop_status)
    invalid = any(item["configuration_status"] == "invalid" for item in channels)
    failed = any(item["status"] == "failed" for item in channels)
    succeeded = any(item["status"] == "succeeded" for item in channels)
    return {
        "status": "configuration_error" if invalid else "failed" if failed else "succeeded" if succeeded else "disabled",
        "configured": selected_email.enabled or selected_desktop.enabled,
        "email": email_status,
        "desktop": desktop_status,
        "last_error": next((item["last_error"] for item in reversed(channels) if item["last_error"]), None),
        "authority": dict(_AUTHORITY),
    }


def verify_channel_records(
    profile_directory: str | Path,
    profile_id: str,
    notifications: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    if PROFILE_ID.fullmatch(profile_id) is None:
        raise ValueError("notification delivery profile id is invalid")
    directory = Path(profile_directory).resolve() / "delivery_attempts"
    if directory.is_symlink():
        raise RuntimeError("notification delivery evidence must not be symbolic")
    if not directory.exists():
        return []
    if not directory.is_dir():
        raise RuntimeError("notification delivery evidence path is invalid")
    paths = list(directory.iterdir())
    if len(paths) > MAX_ATTEMPTS:
        raise RuntimeError("notification delivery evidence capacity is exceeded")
    records: list[dict[str, Any]] = []
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    for path in paths:
        if path.is_symlink() or not path.is_file() or ATTEMPT_FILE.fullmatch(path.name) is None:
            raise RuntimeError("unexpected notification delivery evidence")
        value = load_unique_json(path, max_bytes=MAX_RECORD_BYTES)
        if not isinstance(value, dict) or set(value) != _FIELDS:
            raise RuntimeError("notification delivery evidence schema is invalid")
        if value.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError("notification delivery schema version is invalid")
        if value.get("attempt_id") != path.stem:
            raise RuntimeError("notification delivery attempt identity is invalid")
        if value.get("channel") not in {"email", "desktop"}:
            raise RuntimeError("notification delivery channel is invalid")
        if NOTIFICATION_ID.fullmatch(str(value.get("notification_id", ""))) is None:
            raise RuntimeError("notification delivery notification id is invalid")
        for field in (
            "notification_fingerprint",
            "target_fingerprint",
            "fingerprint",
        ):
            if FINGERPRINT.fullmatch(str(value.get(field, ""))) is None:
                raise RuntimeError(f"notification delivery {field} is invalid")
        sequence = value.get("sequence")
        duration = value.get("duration_ms")
        if isinstance(sequence, bool) or not isinstance(sequence, int) or not 1 <= sequence <= 999:
            raise RuntimeError("notification delivery sequence is invalid")
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
            raise RuntimeError("notification delivery duration is invalid")
        if value.get("status") not in {"succeeded", "failed"}:
            raise RuntimeError("notification delivery status is invalid")
        try:
            created = datetime.fromisoformat(str(value.get("created_at")))
        except ValueError as exc:
            raise RuntimeError("notification delivery timestamp is invalid") from exc
        if created.tzinfo is None:
            raise RuntimeError("notification delivery timestamp must include a timezone")
        for field in ("error_code", "error_message"):
            field_value = value.get(field)
            if field_value is not None and (
                not isinstance(field_value, str) or len(field_value) > 500
            ):
                raise RuntimeError(f"notification delivery {field} is invalid")
        if value.get("fingerprint") != _fingerprint(value):
            raise RuntimeError("notification delivery evidence fingerprint is invalid")
        if value.get("profile_id") != profile_id:
            raise RuntimeError("notification delivery profile binding is invalid")
        notification = notifications.get(str(value.get("notification_id")))
        if notification is None or notification.get("fingerprint") != value.get("notification_fingerprint"):
            raise RuntimeError("notification delivery source binding is invalid")
        key = (str(value["channel"]), str(value["notification_id"]), str(value["target_fingerprint"]))
        grouped.setdefault(key, []).append(value)
        records.append(value)
    for values in grouped.values():
        ordered = sorted(values, key=lambda item: int(item["sequence"]))
        if [item["sequence"] for item in ordered] != list(range(1, len(ordered) + 1)):
            raise RuntimeError("notification delivery attempt chain has a gap")
        if any(item["status"] == "succeeded" for item in ordered[:-1]):
            raise RuntimeError("notification delivery continued after success")
    return records


def _attempt_delivery(
    directory: Path,
    profile_id: str,
    notification: Mapping[str, Any],
    *,
    notification_map: Mapping[str, Mapping[str, Any]],
    channel: str,
    target_fingerprint: str,
    maximum_attempts: int,
    sender: Any,
) -> None:
    notification_id = str(notification.get("notification_id", ""))
    notification_fingerprint = str(notification.get("fingerprint", ""))
    if NOTIFICATION_ID.fullmatch(notification_id) is None or FINGERPRINT.fullmatch(notification_fingerprint) is None or FINGERPRINT.fullmatch(target_fingerprint) is None:
        raise ValueError("notification delivery identity is invalid")
    with evidence_store_lock(directory, "notification delivery"):
        existing = verify_channel_records(directory, profile_id, notification_map)
        selected = [
            item
            for item in existing
            if item["channel"] == channel
            and item["notification_id"] == notification_id
            and item["target_fingerprint"] == target_fingerprint
        ]
        if any(item["status"] == "succeeded" for item in selected) or len(
            selected
        ) >= maximum_attempts:
            return
        sequence = len(selected) + 1
        started = time.monotonic()
        try:
            sender(notification)
            status = "succeeded"
            error_code = None
            error_message = None
        except Exception as exc:
            status = "failed"
            error_code = type(exc).__name__[:80]
            error_message = _safe_error(exc)
        delivery_hash = sha256(
            (
                f"{channel}:{profile_id}:{notification_id}:"
                f"{notification_fingerprint}:{target_fingerprint}"
            ).encode("ascii")
        ).hexdigest()[:32]
        attempt_id = f"delivery_attempt_{channel}_{delivery_hash}_{sequence:03d}"
        record = {
            "schema_version": SCHEMA_VERSION,
            "attempt_id": attempt_id,
            "channel": channel,
            "profile_id": profile_id,
            "notification_id": notification_id,
            "notification_fingerprint": notification_fingerprint,
            "target_fingerprint": target_fingerprint,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "sequence": sequence,
            "status": status,
            "error_code": error_code,
            "error_message": error_message,
            "duration_ms": max(0, round((time.monotonic() - started) * 1000)),
        }
        record["fingerprint"] = _fingerprint(record)
        atomic_create_json(
            directory,
            directory / "delivery_attempts" / f"{attempt_id}.json",
            record,
            label="notification delivery",
            maximum_bytes=MAX_RECORD_BYTES,
        )


def _send_email(settings: EmailSettings, notification: Mapping[str, Any]) -> None:
    if not settings.enabled or settings.host is None or settings.sender is None or settings.recipient is None:
        raise RuntimeError("email delivery is disabled")
    message = EmailMessage()
    message["From"] = settings.sender
    message["To"] = settings.recipient
    message["Subject"] = f"[AI Trade] {str(notification.get('title') or '研究监控通知')[:160]}"
    message.set_content(
        "\n".join(
            [
                str(notification.get("message") or ""),
                f"证券: {notification.get('symbol') or '无'}",
                f"数据日期: {notification.get('data_date') or '未知'}",
                f"通知 ID: {notification.get('notification_id')}",
                "权限: research_only；本通知不会创建订单或改变策略。",
            ]
        )
    )
    context = ssl.create_default_context()
    if settings.security == "ssl":
        connection = smtplib.SMTP_SSL(
            settings.host,
            settings.port,
            timeout=settings.timeout_seconds,
            context=context,
        )
    else:
        connection = smtplib.SMTP(
            settings.host,
            settings.port,
            timeout=settings.timeout_seconds,
        )
    with connection as client:
        if settings.security == "starttls":
            client.ehlo()
            client.starttls(context=context)
            client.ehlo()
        if settings.username and settings.password:
            client.login(settings.username, settings.password)
        client.send_message(message)


def _send_windows_toast(notification: Mapping[str, Any]) -> None:
    title = str(notification.get("title") or "AI Trade 研究通知")[:160]
    message = str(notification.get("message") or "")[:500]
    title64 = base64.b64encode(title.encode("utf-8")).decode("ascii")
    message64 = base64.b64encode(message.encode("utf-8")).decode("ascii")
    script = f"""
$title = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{title64}'))
$message = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('{message64}'))
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
$template = [Windows.UI.Notifications.ToastTemplateType]::ToastText02
$xml = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent($template)
$nodes = $xml.GetElementsByTagName('text')
$nodes.Item(0).AppendChild($xml.CreateTextNode($title)) > $null
$nodes.Item(1).AppendChild($xml.CreateTextNode($message)) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('AI Trade').Show($toast)
"""
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        check=True,
        capture_output=True,
        timeout=15,
        creationflags=flags,
    )


def _eligible(notifications: Sequence[Mapping[str, Any]], limit: int) -> list[Mapping[str, Any]]:
    return [item for item in notifications if item.get("status") == "unread"][:limit]


def _channel_status(
    attempts: Sequence[Mapping[str, Any]],
    notifications: Sequence[Mapping[str, Any]],
    channel: str,
    enabled: bool,
    target_fingerprint: str | None,
    configuration_error: str | None,
) -> dict[str, Any]:
    if configuration_error:
        return _empty_channel(False, configuration_error)
    selected = [
        item for item in attempts if item["channel"] == channel and item["target_fingerprint"] == target_fingerprint
    ]
    succeeded_ids = {item["notification_id"] for item in selected if item["status"] == "succeeded"}
    failed = [item for item in selected if item["status"] == "failed" and item["notification_id"] not in succeeded_ids]
    unread = {str(item.get("notification_id")) for item in notifications if item.get("status") == "unread"}
    return {
        "configured": enabled,
        "configuration_status": "configured" if enabled else "disabled",
        "status": "failed" if failed else "succeeded" if succeeded_ids else "idle" if enabled else "disabled",
        "target_fingerprint": target_fingerprint,
        "pending_count": len(unread - succeeded_ids) if enabled else 0,
        "succeeded_count": len(succeeded_ids),
        "failed_count": len({item["notification_id"] for item in failed}),
        "attempt_count": len(selected),
        "last_delivery_at": max((str(item["created_at"]) for item in selected), default=None),
        "last_error": str(failed[-1]["error_message"]) if failed else None,
    }


def _empty_channel(configured: bool, error: str | None) -> dict[str, Any]:
    return {
        "configured": configured,
        "configuration_status": "invalid" if error else "configured" if configured else "disabled",
        "status": "configuration_error" if error else "idle" if configured else "disabled",
        "target_fingerprint": None,
        "pending_count": 0,
        "succeeded_count": 0,
        "failed_count": 0,
        "attempt_count": 0,
        "last_delivery_at": None,
        "last_error": error,
    }


def _combined_status(
    webhook: Mapping[str, Any], channels: Mapping[str, Any]
) -> dict[str, Any]:
    email = dict(channels.get("email", {}))
    desktop = dict(channels.get("desktop", {}))
    webhook_public = dict(webhook)
    configured_names = [
        name
        for name, item in (
            ("webhook", webhook_public),
            ("email", email),
            ("desktop", desktop),
        )
        if item.get("external_delivery_configured") is True
        or item.get("configured") is True
    ]
    channel_values = (webhook_public, email, desktop)
    configuration_error = any(
        item.get("configuration_status") == "invalid" for item in channel_values
    )
    statuses = {str(item.get("status")) for item in channel_values}
    if configuration_error:
        status = "configuration_error"
    elif "failed" in statuses or "invalid_evidence" in statuses:
        status = "failed"
    elif "partial" in statuses:
        status = "partial"
    elif "succeeded" in statuses:
        status = "succeeded"
    else:
        status = "idle" if configured_names else "disabled"
    return {
        "mode": "local_inbox" + "".join(f"+{name}" for name in configured_names),
        "external_delivery_configured": bool(configured_names),
        "configuration_status": "invalid" if configuration_error else "configured" if configured_names else "disabled",
        "endpoint_origin": webhook_public.get("endpoint_origin"),
        "endpoint_fingerprint": webhook_public.get("endpoint_fingerprint"),
        "status": status,
        "pending_count": sum(int(item.get("pending_count", 0) or 0) for item in channel_values),
        "succeeded_count": sum(int(item.get("succeeded_count", 0) or 0) for item in channel_values),
        "failed_count": sum(int(item.get("failed_count", 0) or 0) for item in channel_values),
        "attempt_count": sum(int(item.get("attempt_count", 0) or 0) for item in channel_values),
        "last_delivery_at": max((str(item.get("last_delivery_at")) for item in channel_values if item.get("last_delivery_at")), default=None),
        "last_error": next((str(item.get("last_error")) for item in reversed(channel_values) if item.get("last_error")), None),
        "channels": {"webhook": webhook_public, "email": email, "desktop": desktop},
        "authority": dict(_AUTHORITY),
    }


def _email_error(message: str) -> EmailSettings:
    return EmailSettings(None, 587, "starttls", None, None, None, None, 10.0, 3, 20, None, message)


def _integer(values: Mapping[str, str], name: str, default: int, minimum: int, maximum: int) -> int:
    raw = str(values.get(name, default))
    if not raw.isascii() or not raw.isdigit() or not minimum <= int(raw) <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return int(raw)


def _number(values: Mapping[str, str], name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(values.get(name, default))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} is invalid") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{name} must be between {minimum} and {maximum}")
    return value


def _fingerprint(value: Mapping[str, Any]) -> str:
    body = {key: item for key, item in value.items() if key != "fingerprint"}
    return sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _safe_error(exc: Exception) -> str:
    message = str(exc).replace("\r", " ").replace("\n", " ").strip()
    return (message or type(exc).__name__)[:500]
