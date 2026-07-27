from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import json
import re
from typing import Any, Mapping


SHADOW_EVENT_SCHEMA_VERSION = 1
SHADOW_EVENT_ID = re.compile(r"se_[0-9a-f]{32}\Z")
FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
SYMBOL = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
EVENT_TYPES = frozenset(
    {
        "opening_balance",
        "cash_deposit",
        "cash_withdrawal",
        "fill",
        "fee_adjustment",
        "position_adjustment",
        "corporate_action",
        "mark",
        "account_snapshot",
    }
)
CHINA_TIMEZONE = timezone(timedelta(hours=8))
MAX_DECIMAL_ABSOLUTE = Decimal("1000000000000000000")
MAX_DECIMAL_INPUT_CHARACTERS = 128

_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "ledger_id",
        "event_id",
        "sequence",
        "event_type",
        "occurred_at",
        "observed_at",
        "trading_session",
        "source",
        "external_id",
        "payload",
        "previous_record_fingerprint",
        "event_fingerprint",
        "record_fingerprint",
    }
)
_PAYLOAD_FIELDS = {
    "opening_balance": frozenset({"cash", "currency"}),
    "cash_deposit": frozenset({"amount", "currency"}),
    "cash_withdrawal": frozenset({"amount", "currency"}),
    "fill": frozenset(
        {
            "symbol",
            "side",
            "quantity",
            "price",
            "commission",
            "stamp_duty",
            "transfer_fee",
            "order_id",
            "portfolio_plan_id",
            "prediction_snapshot_id",
            "model_artifact_id",
        }
    ),
    "fee_adjustment": frozenset({"cash_delta", "reason"}),
    "position_adjustment": frozenset(
        {"symbol", "quantity_delta", "cash_delta", "reason"}
    ),
    "corporate_action": frozenset(
        {"symbol", "quantity_multiplier", "cash_per_share", "reason"}
    ),
    "mark": frozenset({"symbol", "price"}),
    "account_snapshot": frozenset({"cash", "positions"}),
}


def finalize_shadow_event(
    draft: Mapping[str, Any],
    *,
    sequence: int,
    previous_record_fingerprint: str | None,
) -> dict[str, Any]:
    record = _clone(draft)
    forbidden = {
        "event_id",
        "sequence",
        "previous_record_fingerprint",
        "event_fingerprint",
        "record_fingerprint",
    }
    if not isinstance(record, dict) or forbidden & set(record):
        raise ValueError("Shadow event chain fields are assigned by the ledger")
    record.update(
        {
            "event_id": None,
            "sequence": sequence,
            "previous_record_fingerprint": previous_record_fingerprint,
            "event_fingerprint": None,
            "record_fingerprint": None,
        }
    )
    event_fingerprint = shadow_event_fingerprint(record)
    record["event_id"] = "se_" + event_fingerprint[:32]
    record["event_fingerprint"] = event_fingerprint
    record["record_fingerprint"] = shadow_record_fingerprint(record)
    validate_shadow_event(record)
    return record


def validate_shadow_event(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != _TOP_FIELDS:
        raise ValueError("Shadow event top-level fields are invalid")
    if value.get("schema_version") != SHADOW_EVENT_SCHEMA_VERSION:
        raise ValueError("Shadow event schema version is invalid")
    ledger_id = _fingerprint(value.get("ledger_id"), "ledger_id")
    event_id = _identifier(value.get("event_id"), SHADOW_EVENT_ID, "event_id")
    sequence = value.get("sequence")
    if type(sequence) is not int or sequence < 1:
        raise ValueError("Shadow event sequence is invalid")
    event_type = value.get("event_type")
    if event_type not in EVENT_TYPES:
        raise ValueError("Shadow event type is invalid")
    occurred = _timestamp(value.get("occurred_at"), "occurred_at")
    observed = _timestamp(value.get("observed_at"), "observed_at")
    if observed < occurred:
        raise ValueError("Shadow event was observed before it occurred")
    trading_session = _iso_date(value.get("trading_session"), "trading_session")
    if trading_session != occurred.astimezone(CHINA_TIMEZONE).date():
        raise ValueError(
            "Shadow event trading_session must match its China-local occurrence date"
        )
    _text(value.get("source"), "source", 120)
    _text(value.get("external_id"), "external_id", 200)
    previous = value.get("previous_record_fingerprint")
    if sequence == 1:
        if previous is not None:
            raise ValueError("First shadow event cannot have a previous record")
    else:
        _fingerprint(previous, "previous_record_fingerprint")
    payload = value.get("payload")
    expected_fields = _PAYLOAD_FIELDS[str(event_type)]
    if not isinstance(payload, Mapping) or set(payload) != expected_fields:
        raise ValueError("Shadow event payload fields are invalid")
    _validate_payload(str(event_type), payload)
    event_fingerprint = _fingerprint(value.get("event_fingerprint"), "event_fingerprint")
    if event_id != "se_" + event_fingerprint[:32]:
        raise ValueError("Shadow event id is inconsistent")
    if event_fingerprint != shadow_event_fingerprint(value):
        raise ValueError("Shadow event fingerprint does not match content")
    record_fingerprint = _fingerprint(value.get("record_fingerprint"), "record_fingerprint")
    if record_fingerprint != shadow_record_fingerprint(value):
        raise ValueError("Shadow record fingerprint does not match content")
    if ledger_id != value["ledger_id"]:
        raise ValueError("Shadow event ledger binding is invalid")


def canonical_payload(event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    if event_type not in EVENT_TYPES:
        raise ValueError("Unknown shadow event type")
    expected = _PAYLOAD_FIELDS[event_type]
    if not isinstance(payload, Mapping) or set(payload) != expected:
        raise ValueError("Shadow event payload fields are invalid")
    result = dict(payload)
    if event_type == "opening_balance":
        result["cash"] = decimal_text(result["cash"], nonnegative=True)
        result["currency"] = _currency(result["currency"])
    elif event_type in {"cash_deposit", "cash_withdrawal"}:
        result["amount"] = decimal_text(result["amount"], positive=True)
        result["currency"] = _currency(result["currency"])
    elif event_type == "fill":
        result["symbol"] = _identifier(result["symbol"], SYMBOL, "symbol")
        if result["side"] not in {"BUY", "SELL"}:
            raise ValueError("Shadow fill side is invalid")
        quantity = result["quantity"]
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("Shadow fill quantity must be a positive integer")
        for field in ("price",):
            result[field] = decimal_text(result[field], positive=True)
        for field in ("commission", "stamp_duty", "transfer_fee"):
            result[field] = decimal_text(result[field], nonnegative=True)
        result["order_id"] = _text(result["order_id"], "order_id", 200)
        for field, pattern in (
            ("portfolio_plan_id", r"pp_[0-9a-f]{32}"),
            ("prediction_snapshot_id", r"ps_[0-9a-f]{32}"),
            ("model_artifact_id", r"ma_[0-9a-f]{32}"),
        ):
            item = result[field]
            if item is not None and (
                not isinstance(item, str) or re.fullmatch(pattern, item) is None
            ):
                raise ValueError(f"Shadow fill {field} is invalid")
    elif event_type == "fee_adjustment":
        result["cash_delta"] = decimal_text(result["cash_delta"])
        result["reason"] = _text(result["reason"], "reason", 200)
    elif event_type == "position_adjustment":
        result["symbol"] = _identifier(result["symbol"], SYMBOL, "symbol")
        result["quantity_delta"] = decimal_text(result["quantity_delta"])
        if Decimal(result["quantity_delta"]) == 0:
            raise ValueError("Shadow position adjustment quantity cannot be zero")
        result["cash_delta"] = decimal_text(result["cash_delta"])
        result["reason"] = _text(result["reason"], "reason", 200)
    elif event_type == "corporate_action":
        result["symbol"] = _identifier(result["symbol"], SYMBOL, "symbol")
        result["quantity_multiplier"] = decimal_text(
            result["quantity_multiplier"], positive=True
        )
        result["cash_per_share"] = decimal_text(
            result["cash_per_share"], nonnegative=True
        )
        result["reason"] = _text(result["reason"], "reason", 200)
    elif event_type == "mark":
        result["symbol"] = _identifier(result["symbol"], SYMBOL, "symbol")
        result["price"] = decimal_text(result["price"], positive=True)
    elif event_type == "account_snapshot":
        result["cash"] = decimal_text(result["cash"], nonnegative=True)
        positions = result["positions"]
        if not isinstance(positions, Mapping):
            raise ValueError("Shadow account snapshot positions are invalid")
        result["positions"] = {
            _identifier(symbol, SYMBOL, "snapshot symbol"): decimal_text(
                quantity, nonnegative=True
            )
            for symbol, quantity in sorted(positions.items())
            if Decimal(decimal_text(quantity, nonnegative=True)) != 0
        }
    _validate_payload(event_type, result)
    return result


def shadow_event_fingerprint(value: Mapping[str, Any]) -> str:
    return json_fingerprint(
        {
            "schema_version": value.get("schema_version"),
            "ledger_id": value.get("ledger_id"),
            "event_type": value.get("event_type"),
            "occurred_at": value.get("occurred_at"),
            "trading_session": value.get("trading_session"),
            "source": value.get("source"),
            "external_id": value.get("external_id"),
            "payload": value.get("payload"),
        }
    )


def shadow_record_fingerprint(value: Mapping[str, Any]) -> str:
    body = _clone(value)
    body["record_fingerprint"] = None
    body.pop("reused", None)
    return json_fingerprint(body)


def ledger_id(account_reference: str) -> str:
    normalized = _text(account_reference, "account_reference", 256).casefold()
    return sha256(normalized.encode("utf-8")).hexdigest()


def decimal_text(
    value: object, *, positive: bool = False, nonnegative: bool = False
) -> str:
    if isinstance(value, bool):
        raise ValueError("Shadow decimal value is invalid")
    raw = str(value)
    if len(raw) > MAX_DECIMAL_INPUT_CHARACTERS:
        raise ValueError("Shadow decimal value is invalid")
    try:
        number = Decimal(raw)
    except (InvalidOperation, ValueError) as exc:
        raise ValueError("Shadow decimal value is invalid") from exc
    exponent = number.as_tuple().exponent
    if (
        not number.is_finite()
        or not isinstance(exponent, int)
        or exponent < -12
        or exponent > 18
        or abs(number) > MAX_DECIMAL_ABSOLUTE
    ):
        raise ValueError("Shadow decimal value is invalid")
    if positive and number <= 0:
        raise ValueError("Shadow decimal value must be positive")
    if nonnegative and number < 0:
        raise ValueError("Shadow decimal value must be non-negative")
    text = format(number, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"-0", ""} else text


def json_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _validate_payload(event_type: str, payload: Mapping[str, Any]) -> None:
    decimal_fields = {
        "cash",
        "amount",
        "price",
        "commission",
        "stamp_duty",
        "transfer_fee",
        "cash_delta",
        "quantity_delta",
        "quantity_multiplier",
        "cash_per_share",
    }
    for field, item in payload.items():
        if field in decimal_fields and (
            not isinstance(item, str) or decimal_text(item) != item
        ):
            raise ValueError(f"Shadow payload {field} is not canonical")
    if event_type == "opening_balance":
        decimal_text(payload["cash"], nonnegative=True)
        _currency(payload["currency"])
    elif event_type in {"cash_deposit", "cash_withdrawal"}:
        decimal_text(payload["amount"], positive=True)
        _currency(payload["currency"])
    elif event_type == "fill":
        _identifier(payload["symbol"], SYMBOL, "symbol")
        if payload["side"] not in {"BUY", "SELL"}:
            raise ValueError("Shadow fill side is invalid")
        if type(payload["quantity"]) is not int or payload["quantity"] <= 0:
            raise ValueError("Shadow fill quantity is invalid")
        decimal_text(payload["price"], positive=True)
        for field in ("commission", "stamp_duty", "transfer_fee"):
            decimal_text(payload[field], nonnegative=True)
        _text(payload["order_id"], "order_id", 200)
        for field, pattern in (
            ("portfolio_plan_id", r"pp_[0-9a-f]{32}"),
            ("prediction_snapshot_id", r"ps_[0-9a-f]{32}"),
            ("model_artifact_id", r"ma_[0-9a-f]{32}"),
        ):
            item = payload[field]
            if item is not None and (
                not isinstance(item, str) or re.fullmatch(pattern, item) is None
            ):
                raise ValueError(f"Shadow fill {field} is invalid")
    elif event_type == "fee_adjustment":
        decimal_text(payload["cash_delta"])
        _text(payload["reason"], "reason", 200)
    elif event_type == "position_adjustment":
        _identifier(payload["symbol"], SYMBOL, "symbol")
        if Decimal(str(payload["quantity_delta"])) == 0:
            raise ValueError("Shadow position adjustment cannot be zero")
        decimal_text(payload["cash_delta"])
        _text(payload["reason"], "reason", 200)
    elif event_type == "corporate_action":
        _identifier(payload["symbol"], SYMBOL, "symbol")
        decimal_text(payload["quantity_multiplier"], positive=True)
        decimal_text(payload["cash_per_share"], nonnegative=True)
        _text(payload["reason"], "reason", 200)
    elif event_type == "mark":
        _identifier(payload["symbol"], SYMBOL, "symbol")
        decimal_text(payload["price"], positive=True)
    elif event_type == "account_snapshot":
        decimal_text(payload["cash"], nonnegative=True)
        positions = payload.get("positions")
        if not isinstance(positions, Mapping):
            raise ValueError("Shadow snapshot positions are invalid")
        for symbol, quantity in positions.items():
            _identifier(symbol, SYMBOL, "snapshot symbol")
            if decimal_text(quantity, nonnegative=True) != quantity:
                raise ValueError("Shadow snapshot quantity is not canonical")


def _identifier(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"Shadow event {label} is invalid")
    return value


def _fingerprint(value: object, label: str) -> str:
    return _identifier(value, FINGERPRINT, label)


def _text(value: object, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"Shadow event {label} is invalid")
    return value


def _currency(value: object) -> str:
    if not isinstance(value, str) or re.fullmatch(r"[A-Z]{3}", value) is None:
        raise ValueError("Shadow event currency is invalid")
    return value


def _iso_date(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"Shadow event {label} is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Shadow event {label} is invalid") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"Shadow event {label} is invalid")
    return parsed


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Shadow event {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Shadow event {label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Shadow event {label} must include a timezone")
    return parsed


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


__all__ = [
    "EVENT_TYPES",
    "MAX_DECIMAL_ABSOLUTE",
    "MAX_DECIMAL_INPUT_CHARACTERS",
    "SHADOW_EVENT_ID",
    "SHADOW_EVENT_SCHEMA_VERSION",
    "canonical_payload",
    "decimal_text",
    "finalize_shadow_event",
    "ledger_id",
    "shadow_event_fingerprint",
    "shadow_record_fingerprint",
    "validate_shadow_event",
]
