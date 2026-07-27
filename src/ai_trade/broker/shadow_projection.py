from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import json
import re
from typing import Any, Mapping, Sequence

from .shadow_ledger import ShadowEventLedger
from .shadow_schema import decimal_text, json_fingerprint, validate_shadow_event


PROJECTION_SAFETY = {
    "shadow_only": True,
    "qualifying_broker_sandbox_evidence": False,
    "execution_enabled": False,
}
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_SYMBOL = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")
_PROJECTION_FIELDS = frozenset(
    {
        "schema_version",
        "ledger_id",
        "events",
        "last_sequence",
        "last_record_fingerprint",
        "initialized",
        "currency",
        "cash",
        "positions",
        "realized_pnl",
        "fees",
        "equity",
        "missing_marks",
        "latest_account_snapshot",
        "safety",
        "projection_fingerprint",
    }
)
_POSITION_FIELDS = frozenset(
    {"quantity", "average_cost", "mark", "market_value"}
)
_ACCOUNT_SNAPSHOT_FIELDS = frozenset(
    {"sequence", "cash", "positions", "observed_at", "record_fingerprint"}
)


def project_shadow_account(ledger: ShadowEventLedger) -> dict[str, Any]:
    return project_shadow_events(ledger.ledger_id, ledger.events())


def project_shadow_events(
    ledger_id: str, events: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    cash = Decimal("0")
    positions: dict[str, Decimal] = {}
    average_cost: dict[str, Decimal] = {}
    marks: dict[str, Decimal] = {}
    realized_pnl = Decimal("0")
    fees = Decimal("0")
    currency: str | None = None
    initialized = False
    previous: str | None = None
    latest_snapshot: dict[str, Any] | None = None
    last_record: str | None = None

    for expected_sequence, raw_event in enumerate(events, start=1):
        event = _without_reused(raw_event)
        validate_shadow_event(event)
        if (
            event["ledger_id"] != ledger_id
            or event["sequence"] != expected_sequence
            or event["previous_record_fingerprint"] != previous
        ):
            raise RuntimeError("Shadow projection event chain is inconsistent")
        event_type = str(event["event_type"])
        payload = event["payload"]
        if not isinstance(payload, Mapping):
            raise RuntimeError("Shadow projection payload is invalid")
        if event_type == "opening_balance":
            if initialized or expected_sequence != 1:
                raise RuntimeError("Shadow account opening balance must be first and unique")
            cash = Decimal(str(payload["cash"]))
            currency = str(payload["currency"])
            initialized = True
        else:
            if not initialized:
                raise RuntimeError("Shadow account has events before its opening balance")
            if event_type in {"cash_deposit", "cash_withdrawal"}:
                if payload["currency"] != currency:
                    raise RuntimeError("Shadow account currency changed")
                amount = Decimal(str(payload["amount"]))
                cash += amount if event_type == "cash_deposit" else -amount
            elif event_type == "fill":
                symbol = str(payload["symbol"])
                quantity = Decimal(int(payload["quantity"]))
                price = Decimal(str(payload["price"]))
                event_fees = sum(
                    Decimal(str(payload[field]))
                    for field in ("commission", "stamp_duty", "transfer_fee")
                )
                fees += event_fees
                current_quantity = positions.get(symbol, Decimal("0"))
                current_average = average_cost.get(symbol, Decimal("0"))
                notional = quantity * price
                if payload["side"] == "BUY":
                    new_quantity = current_quantity + quantity
                    total_basis = current_quantity * current_average + notional + event_fees
                    average_cost[symbol] = total_basis / new_quantity
                    positions[symbol] = new_quantity
                    cash -= notional + event_fees
                else:
                    if quantity > current_quantity:
                        raise RuntimeError("Shadow sell exceeds the reconstructed position")
                    proceeds = notional - event_fees
                    realized_pnl += proceeds - quantity * current_average
                    cash += proceeds
                    remaining = current_quantity - quantity
                    if remaining == 0:
                        positions.pop(symbol, None)
                        average_cost.pop(symbol, None)
                    else:
                        positions[symbol] = remaining
            elif event_type == "fee_adjustment":
                delta = Decimal(str(payload["cash_delta"]))
                cash += delta
                if delta < 0:
                    fees += -delta
            elif event_type == "position_adjustment":
                symbol = str(payload["symbol"])
                delta = Decimal(str(payload["quantity_delta"]))
                cash_delta = Decimal(str(payload["cash_delta"]))
                current_quantity = positions.get(symbol, Decimal("0"))
                current_average = average_cost.get(symbol, Decimal("0"))
                new_quantity = current_quantity + delta
                if new_quantity < 0:
                    raise RuntimeError("Shadow position adjustment creates a short position")
                if delta > 0:
                    added_basis = max(Decimal("0"), -cash_delta)
                    average_cost[symbol] = (
                        current_quantity * current_average + added_basis
                    ) / new_quantity
                if new_quantity == 0:
                    positions.pop(symbol, None)
                    average_cost.pop(symbol, None)
                else:
                    positions[symbol] = new_quantity
                cash += cash_delta
            elif event_type == "corporate_action":
                symbol = str(payload["symbol"])
                quantity = positions.get(symbol, Decimal("0"))
                multiplier = Decimal(str(payload["quantity_multiplier"]))
                cash_per_share = Decimal(str(payload["cash_per_share"]))
                cash += quantity * cash_per_share
                if quantity > 0:
                    positions[symbol] = quantity * multiplier
                    average_cost[symbol] = average_cost.get(symbol, Decimal("0")) / multiplier
            elif event_type == "mark":
                marks[str(payload["symbol"])] = Decimal(str(payload["price"]))
            elif event_type == "account_snapshot":
                latest_snapshot = {
                    "sequence": expected_sequence,
                    "cash": str(payload["cash"]),
                    "positions": dict(payload["positions"]),
                    "observed_at": event["observed_at"],
                    "record_fingerprint": event["record_fingerprint"],
                }
        if cash < 0:
            raise RuntimeError("Shadow event chain creates a negative cash balance")
        previous = str(event["record_fingerprint"])
        last_record = previous

    missing_marks = sorted(symbol for symbol in positions if symbol not in marks)
    equity: Decimal | None = None
    if initialized and not missing_marks:
        equity = cash + sum(
            quantity * marks[symbol] for symbol, quantity in positions.items()
        )
    position_rows = {}
    for symbol in sorted(positions):
        mark = marks.get(symbol)
        position_rows[symbol] = {
            "quantity": decimal_text(positions[symbol]),
            "average_cost": decimal_text(average_cost.get(symbol, Decimal("0"))),
            "mark": decimal_text(mark) if mark is not None else None,
            "market_value": (
                decimal_text(positions[symbol] * mark) if mark is not None else None
            ),
        }
    result = {
        "schema_version": 1,
        "ledger_id": ledger_id,
        "events": len(events),
        "last_sequence": len(events),
        "last_record_fingerprint": last_record,
        "initialized": initialized,
        "currency": currency,
        "cash": decimal_text(cash),
        "positions": position_rows,
        "realized_pnl": decimal_text(realized_pnl),
        "fees": decimal_text(fees),
        "equity": decimal_text(equity) if equity is not None else None,
        "missing_marks": missing_marks,
        "latest_account_snapshot": latest_snapshot,
        "safety": dict(PROJECTION_SAFETY),
    }
    result["projection_fingerprint"] = json_fingerprint(result)
    return result


def validate_shadow_projection(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != _PROJECTION_FIELDS:
        raise ValueError("Shadow projection fields are invalid")
    if value.get("schema_version") != 1:
        raise ValueError("Shadow projection schema version is invalid")
    _fingerprint(value.get("ledger_id"), "ledger id")
    events = value.get("events")
    last_sequence = value.get("last_sequence")
    if (
        type(events) is not int
        or not 0 <= events <= 100_000
        or last_sequence != events
    ):
        raise ValueError("Shadow projection sequence is invalid")
    last_record = value.get("last_record_fingerprint")
    if events == 0:
        if last_record is not None:
            raise ValueError("Empty shadow projection has a record fingerprint")
    else:
        _fingerprint(last_record, "last record fingerprint")
    initialized = value.get("initialized")
    currency = value.get("currency")
    if type(initialized) is not bool:
        raise ValueError("Shadow projection initialized flag is invalid")
    if initialized != (events > 0):
        raise ValueError("Shadow projection initialization state is inconsistent")
    if initialized:
        if not isinstance(currency, str) or not re.fullmatch(r"[A-Z]{3}", currency):
            raise ValueError("Shadow projection currency is invalid")
    elif currency is not None:
        raise ValueError("Empty shadow projection currency is invalid")
    cash = _canonical_decimal(value.get("cash"), "cash", nonnegative=True)
    positions = value.get("positions")
    if not isinstance(positions, Mapping) or list(positions) != sorted(positions):
        raise ValueError("Shadow projection positions are invalid")
    missing_marks: list[str] = []
    position_total = Decimal("0")
    for symbol, raw_position in positions.items():
        _symbol(symbol)
        if not isinstance(raw_position, Mapping) or set(raw_position) != _POSITION_FIELDS:
            raise ValueError("Shadow projection position fields are invalid")
        quantity = _canonical_decimal(
            raw_position.get("quantity"), "position quantity", positive=True
        )
        _canonical_decimal(
            raw_position.get("average_cost"),
            "position average cost",
            nonnegative=True,
        )
        mark_value = raw_position.get("mark")
        market_value = raw_position.get("market_value")
        if mark_value is None:
            if market_value is not None:
                raise ValueError("Unmarked shadow position has a market value")
            missing_marks.append(symbol)
        else:
            mark = _canonical_decimal(mark_value, "position mark", positive=True)
            recorded_market_value = _canonical_decimal(
                market_value, "position market value", nonnegative=True
            )
            if recorded_market_value != quantity * mark:
                raise ValueError("Shadow projection market value is inconsistent")
            position_total += recorded_market_value
    _canonical_decimal(value.get("realized_pnl"), "realized pnl")
    _canonical_decimal(value.get("fees"), "fees", nonnegative=True)
    equity_value = value.get("equity")
    if not initialized:
        if equity_value is not None or positions or cash != 0:
            raise ValueError("Empty shadow projection balances are invalid")
    elif missing_marks:
        if equity_value is not None:
            raise ValueError("Shadow projection with missing marks has equity")
    else:
        equity = _canonical_decimal(equity_value, "equity", nonnegative=True)
        if equity != cash + position_total:
            raise ValueError("Shadow projection equity is inconsistent")
    recorded_missing = value.get("missing_marks")
    if recorded_missing != missing_marks:
        raise ValueError("Shadow projection missing marks are inconsistent")
    _validate_account_snapshot(value.get("latest_account_snapshot"), events)
    if value.get("safety") != PROJECTION_SAFETY:
        raise ValueError("Shadow projection safety boundary is invalid")
    projection_fingerprint = _fingerprint(
        value.get("projection_fingerprint"), "projection fingerprint"
    )
    body = dict(value)
    body.pop("projection_fingerprint")
    if projection_fingerprint != json_fingerprint(body):
        raise ValueError("Shadow projection fingerprint is inconsistent")


def _validate_account_snapshot(value: object, events: int) -> None:
    if value is None:
        return
    if not isinstance(value, Mapping) or set(value) != _ACCOUNT_SNAPSHOT_FIELDS:
        raise ValueError("Shadow projection account snapshot fields are invalid")
    sequence = value.get("sequence")
    if type(sequence) is not int or not 1 <= sequence <= events:
        raise ValueError("Shadow projection account snapshot sequence is invalid")
    _canonical_decimal(value.get("cash"), "account snapshot cash", nonnegative=True)
    positions = value.get("positions")
    if not isinstance(positions, Mapping) or list(positions) != sorted(positions):
        raise ValueError("Shadow projection account snapshot positions are invalid")
    for symbol, quantity in positions.items():
        _symbol(symbol)
        _canonical_decimal(
            quantity, "account snapshot position", positive=True
        )
    observed_at = value.get("observed_at")
    if not isinstance(observed_at, str):
        raise ValueError("Shadow projection account snapshot timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            "Shadow projection account snapshot timestamp is invalid"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Shadow projection account snapshot timestamp lacks timezone")
    _fingerprint(value.get("record_fingerprint"), "account snapshot fingerprint")


def _canonical_decimal(
    value: object,
    label: str,
    *,
    nonnegative: bool = False,
    positive: bool = False,
) -> Decimal:
    if not isinstance(value, str):
        raise ValueError(f"Shadow projection {label} must be a decimal string")
    canonical = decimal_text(value, nonnegative=nonnegative, positive=positive)
    if canonical != value:
        raise ValueError(f"Shadow projection {label} is not canonical")
    return Decimal(canonical)


def _fingerprint(value: object, label: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise ValueError(f"Shadow projection {label} is invalid")
    return value


def _symbol(value: object) -> str:
    if not isinstance(value, str) or _SYMBOL.fullmatch(value) is None:
        raise ValueError("Shadow projection symbol is invalid")
    return value


def _without_reused(value: Mapping[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))
    if not isinstance(result, dict):
        raise RuntimeError("Shadow event must be an object")
    result.pop("reused", None)
    return result


__all__ = [
    "PROJECTION_SAFETY",
    "project_shadow_account",
    "project_shadow_events",
    "validate_shadow_projection",
]
