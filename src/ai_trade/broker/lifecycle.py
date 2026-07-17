from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from .base import BrokerFill, BrokerOrderSnapshot, OrderSide, OrderStatus


TERMINAL_ORDER_STATUSES = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.CANCELLED,
        OrderStatus.REJECTED,
        OrderStatus.EXPIRED,
    }
)

_ALLOWED_TRANSITIONS = {
    OrderStatus.PENDING_SUBMIT: frozenset(OrderStatus),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.CANCEL_PENDING: frozenset(
        {
            OrderStatus.CANCEL_PENDING,
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.FILLED: frozenset({OrderStatus.FILLED}),
    OrderStatus.CANCELLED: frozenset({OrderStatus.CANCELLED}),
    OrderStatus.REJECTED: frozenset({OrderStatus.REJECTED}),
    OrderStatus.EXPIRED: frozenset({OrderStatus.EXPIRED}),
}


@dataclass(frozen=True)
class RecoveredOrder:
    current: BrokerOrderSnapshot
    first_observed_at: datetime
    event_count: int
    history_complete: bool
    out_of_order_events: int
    cancel_race_observed: bool


def validate_order_snapshot(order: BrokerOrderSnapshot) -> None:
    if not isinstance(order, BrokerOrderSnapshot):
        raise ValueError("Broker order event must be a BrokerOrderSnapshot")
    if (
        not order.client_order_id
        or not order.symbol
        or not isinstance(order.side, OrderSide)
        or not isinstance(order.status, OrderStatus)
        or isinstance(order.quantity, bool)
        or not isinstance(order.quantity, int)
        or order.quantity <= 0
        or isinstance(order.filled_quantity, bool)
        or not isinstance(order.filled_quantity, int)
        or not 0 <= order.filled_quantity <= order.quantity
        or not _positive_finite(order.limit_price)
        or (
            order.average_fill_price is not None
            and not _positive_finite(order.average_fill_price)
        )
        or not isinstance(order.updated_at, datetime)
        or order.updated_at.tzinfo is None
        or order.updated_at.utcoffset() is None
        or not isinstance(order.message, str)
        or (
            order.status != OrderStatus.PENDING_SUBMIT
            and not order.broker_order_id
        )
    ):
        raise ValueError("Broker order event contains invalid or incomplete values")

    filled = order.filled_quantity
    average = order.average_fill_price
    if (filled == 0) != (average is None):
        raise ValueError(
            "Broker order average_fill_price must be present exactly when quantity is filled"
        )
    if order.status in {
        OrderStatus.PENDING_SUBMIT,
        OrderStatus.SUBMITTED,
        OrderStatus.REJECTED,
    } and filled != 0:
        raise ValueError(f"Order status {order.status.value} cannot contain a fill")
    if order.status == OrderStatus.PARTIALLY_FILLED and not 0 < filled < order.quantity:
        raise ValueError("PARTIALLY_FILLED requires a quantity between zero and the order size")
    if order.status == OrderStatus.FILLED and filled != order.quantity:
        raise ValueError("FILLED requires the entire order quantity")
    if order.status in {
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    } and filled >= order.quantity:
        raise ValueError(f"Order status {order.status.value} cannot contain a full fill")


def validate_broker_fill(fill: BrokerFill) -> None:
    if not isinstance(fill, BrokerFill):
        raise ValueError("Broker fill must be a BrokerFill")
    if (
        not fill.fill_id
        or not fill.broker_order_id
        or not fill.client_order_id
        or not fill.symbol
        or not isinstance(fill.side, OrderSide)
        or isinstance(fill.quantity, bool)
        or not isinstance(fill.quantity, int)
        or fill.quantity <= 0
        or not _positive_finite(fill.price)
        or not _nonnegative_finite(fill.commission)
        or not _nonnegative_finite(fill.tax)
        or not isinstance(fill.filled_at, datetime)
        or fill.filled_at.tzinfo is None
        or fill.filled_at.utcoffset() is None
    ):
        raise ValueError("Broker fill contains invalid or incomplete values")


def recover_order_states(
    events: list[BrokerOrderSnapshot],
) -> dict[str, RecoveredOrder]:
    indexed: list[tuple[int, BrokerOrderSnapshot]] = []
    broker_order_owners: dict[str, str] = {}
    for index, event in enumerate(events):
        validate_order_snapshot(event)
        indexed.append((index, event))
        if event.broker_order_id:
            owner = broker_order_owners.setdefault(
                event.broker_order_id, event.client_order_id
            )
            if owner != event.client_order_id:
                raise RuntimeError(
                    "Broker order ID is linked to multiple client order IDs: "
                    + event.broker_order_id
                )

    grouped: dict[str, list[tuple[int, BrokerOrderSnapshot]]] = {}
    for item in indexed:
        grouped.setdefault(item[1].client_order_id, []).append(item)

    recovered: dict[str, RecoveredOrder] = {}
    for client_order_id, observed in grouped.items():
        out_of_order = _out_of_order_count(observed)
        history = sorted(observed, key=lambda item: (item[1].updated_at, item[0]))
        baseline = history[0][1]
        assigned_broker_order_id = ""
        cancel_race = False
        previous: BrokerOrderSnapshot | None = None
        for _, current in history:
            _validate_identity(baseline, current)
            if current.broker_order_id:
                if (
                    assigned_broker_order_id
                    and current.broker_order_id != assigned_broker_order_id
                ):
                    raise RuntimeError(
                        f"Broker order ID changed for client order {client_order_id}"
                    )
                assigned_broker_order_id = current.broker_order_id
            elif assigned_broker_order_id:
                raise RuntimeError(
                    f"Broker order ID disappeared for client order {client_order_id}"
                )
            if previous is not None:
                _validate_transition(previous, current)
                if (
                    previous.status == OrderStatus.CANCEL_PENDING
                    and (
                        current.filled_quantity > previous.filled_quantity
                        or current.status
                        in {OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED}
                    )
                ):
                    cancel_race = True
            previous = current

        first = history[0][1]
        current = history[-1][1]
        recovered[client_order_id] = RecoveredOrder(
            current=current,
            first_observed_at=first.updated_at,
            event_count=len(history),
            history_complete=first.status
            in {OrderStatus.PENDING_SUBMIT, OrderStatus.SUBMITTED},
            out_of_order_events=out_of_order,
            cancel_race_observed=cancel_race,
        )
    return recovered


def build_lifecycle_report(
    events: list[BrokerOrderSnapshot],
    fills: list[BrokerFill],
) -> dict[str, object]:
    recovered = recover_order_states(events)
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    unique_fills: dict[str, BrokerFill] = {}
    for fill in fills:
        validate_broker_fill(fill)
        previous = unique_fills.get(fill.fill_id)
        if previous is not None:
            errors.append(
                _issue(
                    "duplicate_fill_id",
                    fill.client_order_id,
                    f"Duplicate fill ID is present in the persisted ledger: {fill.fill_id}",
                )
            )
            if previous != fill:
                errors.append(
                    _issue(
                        "conflicting_fill_id",
                        fill.client_order_id,
                        f"Fill ID has conflicting persisted values: {fill.fill_id}",
                    )
                )
            continue
        unique_fills[fill.fill_id] = fill

    fills_by_order: dict[str, list[BrokerFill]] = {}
    for fill in unique_fills.values():
        state = recovered.get(fill.client_order_id)
        if state is None:
            errors.append(
                _issue(
                    "orphan_fill",
                    fill.client_order_id,
                    f"Fill {fill.fill_id} has no persisted order lifecycle",
                )
            )
            continue
        current = state.current
        identity_mismatches = []
        if fill.broker_order_id != current.broker_order_id:
            identity_mismatches.append("broker_order_id")
        if fill.symbol != current.symbol:
            identity_mismatches.append("symbol")
        if fill.side != current.side:
            identity_mismatches.append("side")
        if identity_mismatches:
            errors.append(
                _issue(
                    "fill_identity_mismatch",
                    fill.client_order_id,
                    f"Fill {fill.fill_id} disagrees with its order on: "
                    + ", ".join(identity_mismatches),
                )
            )
        fills_by_order.setdefault(fill.client_order_id, []).append(fill)

    rows: list[dict[str, object]] = []
    for client_order_id, state in recovered.items():
        current = state.current
        order_fills = fills_by_order.get(client_order_id, [])
        fill_quantity = sum(value.quantity for value in order_fills)
        fill_notional = math.fsum(value.quantity * value.price for value in order_fills)
        fill_average = fill_notional / fill_quantity if fill_quantity else None
        if fill_quantity != current.filled_quantity:
            errors.append(
                _issue(
                    "fill_quantity_mismatch",
                    client_order_id,
                    "Persisted fills total "
                    f"{fill_quantity}, but the latest order snapshot reports "
                    f"{current.filled_quantity}",
                )
            )
        if (
            fill_average is not None
            and current.average_fill_price is not None
            and not math.isclose(
                fill_average,
                current.average_fill_price,
                rel_tol=1e-6,
                abs_tol=1e-6,
            )
        ):
            errors.append(
                _issue(
                    "average_fill_price_mismatch",
                    client_order_id,
                    "Persisted fill prices do not reproduce the latest average fill price",
                )
            )
        if not state.history_complete:
            warnings.append(
                _issue(
                    "history_started_mid_lifecycle",
                    client_order_id,
                    "The first persisted event starts after submission; earlier history is unavailable",
                )
            )
        if state.out_of_order_events:
            warnings.append(
                _issue(
                    "out_of_order_events_recovered",
                    client_order_id,
                    f"Recovered {state.out_of_order_events} late event(s) by broker timestamp",
                )
            )
        rows.append(
            {
                "client_order_id": current.client_order_id,
                "broker_order_id": current.broker_order_id,
                "symbol": current.symbol,
                "side": current.side.value,
                "quantity": current.quantity,
                "filled_quantity": current.filled_quantity,
                "remaining_quantity": current.quantity - current.filled_quantity,
                "limit_price": current.limit_price,
                "average_fill_price": current.average_fill_price,
                "status": current.status.value,
                "updated_at": current.updated_at.isoformat(),
                "message": current.message,
                "event_count": state.event_count,
                "fill_count": len(order_fills),
                "fill_quantity": fill_quantity,
                "fill_notional": fill_notional,
                "first_observed_at": state.first_observed_at.isoformat(),
                "history_complete": state.history_complete,
                "out_of_order_events": state.out_of_order_events,
                "cancel_race_observed": state.cancel_race_observed,
                "terminal": current.status in TERMINAL_ORDER_STATUSES,
                "integrity_ok": not any(
                    value["client_order_id"] == client_order_id for value in errors
                ),
            }
        )

    rows.sort(
        key=lambda value: (
            str(value["updated_at"]),
            str(value["client_order_id"]),
        ),
        reverse=True,
    )
    open_count = sum(not bool(value["terminal"]) for value in rows)
    if errors:
        status = "INTEGRITY_ERROR"
    elif warnings:
        status = "RECOVERED"
    elif rows or unique_fills:
        status = "VERIFIED"
    else:
        status = "EMPTY"
    return {
        "schema_version": 1,
        "status": status,
        "order_count": len(rows),
        "open_order_count": open_count,
        "terminal_order_count": len(rows) - open_count,
        "partial_order_count": sum(
            value["status"] == OrderStatus.PARTIALLY_FILLED.value for value in rows
        ),
        "cancel_pending_count": sum(
            value["status"] == OrderStatus.CANCEL_PENDING.value for value in rows
        ),
        "fill_count": len(unique_fills),
        "orders": rows,
        "integrity_errors": errors,
        "recovery_warnings": warnings,
        "qualifying_evidence": False,
        "execution_enabled": False,
    }


def lifecycle_error_report(code: str, message: str) -> dict[str, object]:
    return {
        "schema_version": 1,
        "status": "INTEGRITY_ERROR",
        "order_count": 0,
        "open_order_count": 0,
        "terminal_order_count": 0,
        "partial_order_count": 0,
        "cancel_pending_count": 0,
        "fill_count": 0,
        "orders": [],
        "integrity_errors": [_issue(code, "", message)],
        "recovery_warnings": [],
        "qualifying_evidence": False,
        "execution_enabled": False,
    }


def _validate_identity(
    baseline: BrokerOrderSnapshot, current: BrokerOrderSnapshot
) -> None:
    if (
        current.symbol != baseline.symbol
        or current.side != baseline.side
        or current.quantity != baseline.quantity
        or not math.isclose(
            current.limit_price,
            baseline.limit_price,
            rel_tol=0.0,
            abs_tol=1e-12,
        )
    ):
        raise RuntimeError(
            f"Immutable order fields changed for client order {baseline.client_order_id}"
        )


def _validate_transition(
    previous: BrokerOrderSnapshot, current: BrokerOrderSnapshot
) -> None:
    if current.status not in _ALLOWED_TRANSITIONS[previous.status]:
        raise RuntimeError(
            f"Illegal order transition for {current.client_order_id}: "
            f"{previous.status.value} -> {current.status.value}"
        )
    if current.filled_quantity < previous.filled_quantity:
        raise RuntimeError(
            f"Filled quantity moved backwards for client order {current.client_order_id}"
        )


def _out_of_order_count(
    observed: list[tuple[int, BrokerOrderSnapshot]],
) -> int:
    latest: datetime | None = None
    count = 0
    for _, event in observed:
        if latest is not None and event.updated_at < latest:
            count += 1
        if latest is None or event.updated_at > latest:
            latest = event.updated_at
    return count


def _positive_finite(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) > 0
    )


def _nonnegative_finite(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
        and float(value) >= 0
    )


def _issue(code: str, client_order_id: str, message: str) -> dict[str, str]:
    return {
        "code": code,
        "client_order_id": client_order_id,
        "message": message,
    }
