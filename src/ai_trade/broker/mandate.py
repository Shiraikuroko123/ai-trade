from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .base import BrokerOrderRequest, OrderSide
from .ledger import ledger_lock


AUTHORIZATION_SCHEMA_VERSION = 2
MANDATE_SCHEMA_VERSION = 1
BATCH_APPROVAL_SCHEMA_VERSION = 1
MAX_BATCH_APPROVAL_LIFETIME = timedelta(minutes=15)

_SYMBOL = re.compile(r"[A-Za-z0-9._-]{1,64}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_APPROVAL_ID = re.compile(r"approval_[0-9a-f]{32}\Z")
_AUTHORIZATION_FIELDS = {
    "schema_version",
    "approved",
    "approved_by",
    "approved_at",
    "adapter",
    "account_id",
    "config_fingerprint",
    "expires_at",
    "mandate",
}
_MANDATE_FIELDS = {
    "schema_version",
    "allowed_symbols",
    "allowed_sides",
    "max_order_notional",
    "max_daily_notional",
    "max_orders_per_day",
    "require_batch_approval",
}
_BATCH_APPROVAL_FIELDS = {
    "schema_version",
    "approval_id",
    "approved",
    "approved_by",
    "approved_at",
    "expires_at",
    "adapter",
    "account_id",
    "config_fingerprint",
    "batch_fingerprint",
}


@dataclass(frozen=True)
class BrokerMandate:
    allowed_symbols: frozenset[str]
    allowed_sides: frozenset[OrderSide]
    max_order_notional: float
    max_daily_notional: float
    max_orders_per_day: int
    require_batch_approval: bool = True

    def public_dict(self) -> dict[str, object]:
        return {
            "schema_version": MANDATE_SCHEMA_VERSION,
            "allowed_symbols": sorted(self.allowed_symbols),
            "allowed_sides": sorted(value.value for value in self.allowed_sides),
            "max_order_notional": self.max_order_notional,
            "max_daily_notional": self.max_daily_notional,
            "max_orders_per_day": self.max_orders_per_day,
            "require_batch_approval": self.require_batch_approval,
        }

    def enforce(
        self,
        orders: Sequence[BrokerOrderRequest],
        *,
        submitted_orders: int,
        submitted_notional: float,
    ) -> dict[str, float | int]:
        if not orders:
            raise ValueError("A broker mandate cannot approve an empty order batch")
        if submitted_orders < 0:
            raise ValueError("submitted_orders must be non-negative")
        if not math.isfinite(submitted_notional) or submitted_notional < 0:
            raise ValueError("submitted_notional must be finite and non-negative")
        if submitted_orders + len(orders) > self.max_orders_per_day:
            raise PermissionError("Order batch exceeds the mandate daily order count")

        batch_notional = 0.0
        for order in orders:
            if order.symbol not in self.allowed_symbols:
                raise PermissionError(
                    f"Order symbol is outside the approved mandate: {order.symbol}"
                )
            if order.side not in self.allowed_sides:
                raise PermissionError(
                    f"Order side is outside the approved mandate: {order.side.value}"
                )
            notional = order.quantity * order.limit_price
            if not math.isfinite(notional) or notional <= 0:
                raise ValueError("Order mandate validation requires positive notional")
            if notional > self.max_order_notional:
                raise PermissionError(
                    f"Order notional for {order.symbol} exceeds the approved mandate"
                )
            batch_notional += notional
        if submitted_notional + batch_notional > self.max_daily_notional:
            raise PermissionError("Order batch exceeds the mandate daily notional")
        return {
            "batch_notional": batch_notional,
            "daily_notional_after_batch": submitted_notional + batch_notional,
            "daily_orders_after_batch": submitted_orders + len(orders),
        }


def authorization_mandate_status(
    authorization: Mapping[str, Any] | None,
    *,
    configured_max_order_notional: float,
    configured_max_daily_notional: float,
) -> tuple[BrokerMandate | None, str]:
    try:
        mandate = _parse_authorization_mandate(
            authorization,
            configured_max_order_notional=configured_max_order_notional,
            configured_max_daily_notional=configured_max_daily_notional,
        )
    except (TypeError, ValueError) as exc:
        return None, str(exc)
    return mandate, "mandate is explicit, bounded, and requires batch approval"


def parse_mandate(
    value: Mapping[str, Any],
    *,
    configured_max_order_notional: float,
    configured_max_daily_notional: float,
) -> BrokerMandate:
    return _parse_mandate(
        value,
        configured_max_order_notional=configured_max_order_notional,
        configured_max_daily_notional=configured_max_daily_notional,
    )


def order_batch_fingerprint(
    orders: Sequence[BrokerOrderRequest],
    *,
    on_date: date,
    adapter: str,
    account_id: str,
    config_fingerprint: str,
) -> str:
    if not orders:
        raise ValueError("Cannot fingerprint an empty order batch")
    payload = {
        "schema_version": 1,
        "date": on_date.isoformat(),
        "adapter": _required_text(adapter, "adapter", 128),
        "account_id": _required_text(account_id, "account_id", 128),
        "config_fingerprint": _required_fingerprint(config_fingerprint),
        "orders": [
            {
                "client_order_id": _required_text(
                    order.client_order_id, "client_order_id", 200
                ),
                "symbol": order.symbol,
                "side": order.side.value,
                "quantity": order.quantity,
                "limit_price": _canonical_decimal(order.limit_price),
                "time_in_force": order.time_in_force,
            }
            for order in orders
        ],
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def create_batch_approval(
    *,
    approved_by: str,
    adapter: str,
    account_id: str,
    config_fingerprint: str,
    batch_fingerprint: str,
    now: datetime | None = None,
    lifetime: timedelta = timedelta(minutes=10),
) -> dict[str, object]:
    created_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if lifetime <= timedelta(0) or lifetime > MAX_BATCH_APPROVAL_LIFETIME:
        raise ValueError("Batch approval lifetime must be between 0 and 15 minutes")
    return {
        "schema_version": BATCH_APPROVAL_SCHEMA_VERSION,
        "approval_id": f"approval_{uuid4().hex}",
        "approved": True,
        "approved_by": _required_text(approved_by, "approved_by", 200),
        "approved_at": created_at.isoformat(),
        "expires_at": (created_at + lifetime).isoformat(),
        "adapter": _required_text(adapter, "adapter", 128),
        "account_id": _required_text(account_id, "account_id", 128),
        "config_fingerprint": _required_fingerprint(config_fingerprint),
        "batch_fingerprint": _required_fingerprint(batch_fingerprint),
    }


def write_batch_approval(path: Path, approval: Mapping[str, Any]) -> Path:
    _validate_batch_approval_shape(approval)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                dict(approval),
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if path.exists():
            raise FileExistsError("A pending broker batch approval already exists")
        _move_without_replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def consume_batch_approval(
    path: Path,
    *,
    adapter: str,
    account_id: str,
    config_fingerprint: str,
    batch_fingerprint: str,
    now: datetime | None = None,
) -> dict[str, object]:
    if not path.exists():
        raise PermissionError("A one-time broker batch approval file is required")
    with ledger_lock(path):
        try:
            if path.stat().st_size > 64 * 1024:
                raise ValueError("Batch approval file exceeds 64 KiB")
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PermissionError("Batch approval file is invalid") from exc
        _validate_batch_approval(
            value,
            adapter=adapter,
            account_id=account_id,
            config_fingerprint=config_fingerprint,
            batch_fingerprint=batch_fingerprint,
            now=now or datetime.now(timezone.utc),
        )
        approval_id = str(value["approval_id"])
        consumed_path = path.with_name(
            f"{path.stem}.{approval_id}.consumed{path.suffix}"
        )
        if consumed_path.exists():
            raise RuntimeError("Batch approval audit record already exists")
        os.replace(path, consumed_path)
        _fsync_directory(path.parent)
    return {
        "approval_id": approval_id,
        "approved_by": str(value["approved_by"]),
        "approved_at": str(value["approved_at"]),
        "expires_at": str(value["expires_at"]),
        "batch_fingerprint": str(value["batch_fingerprint"]),
        "audit_file": str(consumed_path),
    }


def _parse_authorization_mandate(
    authorization: Mapping[str, Any] | None,
    *,
    configured_max_order_notional: float,
    configured_max_daily_notional: float,
) -> BrokerMandate:
    if not isinstance(authorization, Mapping):
        raise ValueError("authorization file is missing or invalid")
    if set(authorization) != _AUTHORIZATION_FIELDS:
        raise ValueError("authorization schema fields are invalid")
    if authorization.get("schema_version") != AUTHORIZATION_SCHEMA_VERSION:
        raise ValueError("authorization schema version is unsupported")
    _required_text(authorization.get("approved_by"), "approved_by", 200)
    _parse_datetime(authorization.get("approved_at"), "approved_at")
    raw = authorization.get("mandate")
    return _parse_mandate(
        raw,
        configured_max_order_notional=configured_max_order_notional,
        configured_max_daily_notional=configured_max_daily_notional,
    )


def _parse_mandate(
    raw: Any,
    *,
    configured_max_order_notional: float,
    configured_max_daily_notional: float,
) -> BrokerMandate:
    if not isinstance(raw, Mapping) or set(raw) != _MANDATE_FIELDS:
        raise ValueError("authorization mandate fields are invalid")
    if raw.get("schema_version") != MANDATE_SCHEMA_VERSION:
        raise ValueError("authorization mandate schema version is unsupported")

    raw_symbols = raw.get("allowed_symbols")
    if not isinstance(raw_symbols, list) or not 1 <= len(raw_symbols) <= 1000:
        raise ValueError("mandate allowed_symbols must contain 1 to 1000 symbols")
    symbols = []
    for value in raw_symbols:
        if not isinstance(value, str) or not _SYMBOL.fullmatch(value):
            raise ValueError("mandate contains an invalid symbol")
        symbols.append(value)
    if len(set(symbols)) != len(symbols):
        raise ValueError("mandate allowed_symbols must be unique")

    raw_sides = raw.get("allowed_sides")
    if not isinstance(raw_sides, list) or not raw_sides:
        raise ValueError("mandate allowed_sides must be a non-empty list")
    try:
        sides = [OrderSide(value) for value in raw_sides]
    except (TypeError, ValueError) as exc:
        raise ValueError("mandate contains an invalid order side") from exc
    if len(set(sides)) != len(sides):
        raise ValueError("mandate allowed_sides must be unique")

    max_order = _positive_float(raw.get("max_order_notional"), "max_order_notional")
    max_daily = _positive_float(raw.get("max_daily_notional"), "max_daily_notional")
    if max_daily < max_order:
        raise ValueError("mandate daily notional must cover one maximum order")
    if max_order > configured_max_order_notional:
        raise ValueError("mandate order notional exceeds the configured hard limit")
    if max_daily > configured_max_daily_notional:
        raise ValueError("mandate daily notional exceeds the configured hard limit")
    raw_count = raw.get("max_orders_per_day")
    if (
        isinstance(raw_count, bool)
        or not isinstance(raw_count, int)
        or not 1 <= raw_count <= 100
    ):
        raise ValueError("mandate max_orders_per_day must be between 1 and 100")
    if raw.get("require_batch_approval") is not True:
        raise ValueError("live mandates must require one-time batch approval")
    return BrokerMandate(
        allowed_symbols=frozenset(symbols),
        allowed_sides=frozenset(sides),
        max_order_notional=max_order,
        max_daily_notional=max_daily,
        max_orders_per_day=raw_count,
    )


def _validate_batch_approval(
    value: Any,
    *,
    adapter: str,
    account_id: str,
    config_fingerprint: str,
    batch_fingerprint: str,
    now: datetime,
) -> None:
    _validate_batch_approval_shape(value)
    if value["adapter"] != adapter or value["account_id"] != account_id:
        raise PermissionError("Batch approval broker identity does not match")
    if value["config_fingerprint"] != config_fingerprint:
        raise PermissionError("Batch approval configuration fingerprint is stale")
    if value["batch_fingerprint"] != batch_fingerprint:
        raise PermissionError("Batch approval does not match the exact order batch")
    approved_at = _parse_datetime(value["approved_at"], "approved_at")
    expires_at = _parse_datetime(value["expires_at"], "expires_at")
    current = now.astimezone(timezone.utc)
    if approved_at > current + timedelta(minutes=1):
        raise PermissionError("Batch approval timestamp is in the future")
    if expires_at <= current:
        raise PermissionError("Batch approval has expired")
    if expires_at <= approved_at or expires_at - approved_at > MAX_BATCH_APPROVAL_LIFETIME:
        raise PermissionError("Batch approval lifetime exceeds 15 minutes")


def _validate_batch_approval_shape(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != _BATCH_APPROVAL_FIELDS:
        raise ValueError("Batch approval schema fields are invalid")
    if value.get("schema_version") != BATCH_APPROVAL_SCHEMA_VERSION:
        raise ValueError("Batch approval schema version is unsupported")
    if value.get("approved") is not True:
        raise ValueError("Batch approval is not approved")
    if not isinstance(value.get("approval_id"), str) or not _APPROVAL_ID.fullmatch(
        value["approval_id"]
    ):
        raise ValueError("Batch approval id is invalid")
    _required_text(value.get("approved_by"), "approved_by", 200)
    _required_text(value.get("adapter"), "adapter", 128)
    _required_text(value.get("account_id"), "account_id", 128)
    _required_fingerprint(value.get("config_fingerprint"))
    _required_fingerprint(value.get("batch_fingerprint"))
    _parse_datetime(value.get("approved_at"), "approved_at")
    _parse_datetime(value.get("expires_at"), "expires_at")


def _required_text(value: Any, name: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{name} is invalid")
    return value


def _required_fingerprint(value: Any) -> str:
    if not isinstance(value, str) or not _FINGERPRINT.fullmatch(value):
        raise ValueError("fingerprint must be 64 lowercase hexadecimal characters")
    return value


def _positive_float(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"mandate {name} must be finite and positive")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"mandate {name} must be finite and positive") from exc
    if not math.isfinite(result) or result <= 0:
        raise ValueError(f"mandate {name} must be finite and positive")
    return result


def _parse_datetime(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be a timezone-aware timestamp")
    raw = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a timezone-aware timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{name} must be a timezone-aware timestamp")
    return parsed.astimezone(timezone.utc)


def _canonical_decimal(value: Any) -> str:
    try:
        parsed = Decimal(str(value))
    except Exception as exc:
        raise ValueError("limit_price is invalid") from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ValueError("limit_price is invalid")
    return format(parsed.normalize(), "f")


def _move_without_replace(source: Path, target: Path) -> None:
    if os.name == "nt":
        os.rename(source, target)
        return
    os.link(source, target)
    source.unlink()


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
