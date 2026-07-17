from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, time, timezone
from pathlib import Path
from typing import Iterator

from .base import (
    BrokerFill,
    BrokerOrderRequest,
    BrokerOrderSnapshot,
    OrderSide,
    OrderStatus,
)
from .lifecycle import (
    build_lifecycle_report,
    lifecycle_error_report,
    recover_order_states,
    validate_broker_fill,
    validate_order_snapshot,
)


ORDER_EVENT_FIELDS = [
    "client_order_id",
    "broker_order_id",
    "symbol",
    "side",
    "quantity",
    "filled_quantity",
    "limit_price",
    "average_fill_price",
    "status",
    "updated_at",
    "message",
    "event_id",
]

FILL_FIELDS = [
    "fill_id",
    "broker_order_id",
    "client_order_id",
    "symbol",
    "side",
    "quantity",
    "price",
    "commission",
    "tax",
    "filled_at",
]

INTEGER_LEDGER_FIELDS = frozenset({"quantity", "filled_quantity"})
FLOAT_LEDGER_FIELDS = frozenset(
    {"limit_price", "average_fill_price", "price", "commission", "tax"}
)


def append_order_events(path: Path, orders: list[BrokerOrderSnapshot]) -> None:
    if not orders:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_lock(path):
        existing_rows = _read_rows(path, "event_id")
        existing_events = _order_events_from_rows(existing_rows)
        recover_order_states(existing_events + orders)
        pending_orders = [order for order in orders if order not in existing_events]
        if pending_orders:
            _append_rows(
                path,
                [_order_event_payload(order) for order in pending_orders],
                "event_id",
                existing_rows,
            )


def reserve_order_intents(
    path: Path,
    orders: list[BrokerOrderRequest],
    on_date: date,
    max_daily_notional: float,
    max_daily_orders: int | None = None,
) -> float:
    """Persist order IDs before broker I/O and enforce the daily limit atomically."""
    if not math.isfinite(max_daily_notional) or max_daily_notional <= 0:
        raise ValueError("max_daily_notional must be finite and positive")
    if max_daily_orders is not None and (
        isinstance(max_daily_orders, bool)
        or not isinstance(max_daily_orders, int)
        or max_daily_orders < 1
    ):
        raise ValueError("max_daily_orders must be a positive integer")
    timestamp = datetime.combine(on_date, time.min, tzinfo=timezone.utc)
    snapshots = [
        BrokerOrderSnapshot(
            client_order_id=order.client_order_id,
            broker_order_id="",
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            filled_quantity=0,
            limit_price=order.limit_price,
            average_fill_price=None,
            status=OrderStatus.PENDING_SUBMIT,
            updated_at=timestamp,
            message="Local submission intent reserved before broker I/O",
        )
        for order in orders
    ]
    rows = [_order_event_payload(snapshot) for snapshot in snapshots]
    batch_notional = sum(order.quantity * order.limit_price for order in orders)

    path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_lock(path):
        existing_rows = _read_rows(path, "event_id")
        existing_client_ids = {
            str(row.get("client_order_id", "")) for row in existing_rows
        }
        duplicates = sorted(
            order.client_order_id
            for order in orders
            if order.client_order_id in existing_client_ids
        )
        if duplicates:
            raise RuntimeError(
                "client_order_id already exists in the broker ledger: "
                + ", ".join(duplicates)
            )
        recover_order_states(_order_events_from_rows(existing_rows) + snapshots)
        submitted = _submitted_order_notional(existing_rows, on_date)
        if submitted + batch_notional > max_daily_notional:
            raise ValueError("Orders exceed configured daily notional limit")
        if max_daily_orders is not None:
            submitted_count = sum(
                updated_at.date() == on_date
                for updated_at, _ in _first_order_events(existing_rows).values()
            )
            if submitted_count + len(orders) > max_daily_orders:
                raise ValueError("Orders exceed configured daily order count")
        _append_rows(path, rows, "event_id", existing_rows)
    return submitted + batch_notional


def append_fills(path: Path, fills: list[BrokerFill]) -> None:
    if not fills:
        return
    rows = [_fill_payload(fill) for fill in fills]
    path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_lock(path):
        existing_rows = _read_rows(path, "fill_id")
        _fills_from_rows(existing_rows)
        _append_rows(path, rows, "fill_id", existing_rows)


def append_broker_observation(
    orders_path: Path,
    fills_path: Path,
    orders: list[BrokerOrderSnapshot],
    fills: list[BrokerFill],
) -> dict[str, object]:
    """Validate and append one restart-safe broker polling observation."""
    if orders_path.resolve() == fills_path.resolve():
        raise ValueError("Broker order and fill ledgers must use different paths")
    fill_rows = [_fill_payload(fill) for fill in fills]
    orders_path.parent.mkdir(parents=True, exist_ok=True)
    fills_path.parent.mkdir(parents=True, exist_ok=True)

    paths = sorted((orders_path, fills_path), key=lambda value: str(value.resolve()))
    with ExitStack() as stack:
        for path in paths:
            stack.enter_context(ledger_lock(path))
        existing_order_rows = _read_rows(orders_path, "event_id")
        existing_fill_rows = _read_rows(fills_path, "fill_id")
        existing_events = _order_events_from_rows(existing_order_rows)
        pending_orders = [order for order in orders if order not in existing_events]
        order_rows = [_order_event_payload(order) for order in pending_orders]
        prospective_order_rows = _merged_rows(
            existing_order_rows, order_rows, "event_id"
        )
        prospective_fill_rows = _merged_rows(
            existing_fill_rows, fill_rows, "fill_id"
        )
        report = build_lifecycle_report(
            _order_events_from_rows(prospective_order_rows),
            _fills_from_rows(prospective_fill_rows),
        )
        errors = report["integrity_errors"]
        if errors:
            first = errors[0]
            raise RuntimeError(
                "Broker observation does not reconcile with the persisted lifecycle: "
                + str(first["message"])
            )
        if order_rows:
            _append_rows(orders_path, order_rows, "event_id", existing_order_rows)
        if fill_rows:
            _append_rows(fills_path, fill_rows, "fill_id", existing_fill_rows)
    return report


def read_order_events(path: Path) -> list[BrokerOrderSnapshot]:
    if not path.exists():
        return []
    with ledger_lock(path):
        return _order_events_from_rows(_read_rows(path, "event_id"))


def read_fills(path: Path) -> list[BrokerFill]:
    if not path.exists():
        return []
    with ledger_lock(path):
        return _fills_from_rows(_read_rows(path, "fill_id"))


def recover_order_lifecycle(
    orders_path: Path,
    fills_path: Path,
) -> dict[str, object]:
    try:
        events = read_order_events(orders_path)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return lifecycle_error_report(
            "order_ledger_invalid", f"Broker order ledger is invalid: {exc}"
        )
    try:
        fills = read_fills(fills_path)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return lifecycle_error_report(
            "fill_ledger_invalid", f"Broker fill ledger is invalid: {exc}"
        )
    try:
        return build_lifecycle_report(events, fills)
    except (RuntimeError, TypeError, ValueError) as exc:
        return lifecycle_error_report(
            "lifecycle_invalid", f"Broker order lifecycle is invalid: {exc}"
        )


def submitted_order_notional(path: Path, on_date: date) -> float:
    """Return gross notional first submitted on a date, counted once per client order."""
    if not path.exists():
        return 0.0
    with ledger_lock(path):
        rows = _read_rows(path, "event_id")
        recover_order_states(_order_events_from_rows(rows))
        return _submitted_order_notional(rows, on_date)


def submitted_order_count(path: Path, on_date: date) -> int:
    """Return client orders first reserved on a date, counted once per order."""
    if not path.exists():
        return 0
    with ledger_lock(path):
        rows = _read_rows(path, "event_id")
        recover_order_states(_order_events_from_rows(rows))
        return sum(
            updated_at.date() == on_date
            for updated_at, _ in _first_order_events(rows).values()
        )


def _submitted_order_notional(
    rows: list[dict[str, str]], on_date: date
) -> float:
    return sum(
        notional
        for updated_at, notional in _first_order_events(rows).values()
        if updated_at.date() == on_date
    )


def _first_order_events(
    rows: list[dict[str, str]],
) -> dict[str, tuple[datetime, float]]:
    first_events: dict[str, tuple[datetime, float]] = {}
    for row in rows:
        try:
            updated_at = datetime.fromisoformat(str(row["updated_at"]))
            quantity = int(row["quantity"])
            limit_price = float(row["limit_price"])
            notional = quantity * limit_price
            if quantity <= 0 or not math.isfinite(limit_price) or limit_price <= 0:
                raise ValueError("quantity and limit_price must be positive")
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid broker order ledger row: {exc}") from exc
        order_id = str(row["client_order_id"])
        if not order_id:
            raise RuntimeError("Invalid broker order ledger row: empty client_order_id")
        current = first_events.get(order_id)
        if current is None or updated_at < current[0]:
            first_events[order_id] = (updated_at, notional)
    return first_events


def _merged_rows(
    existing_rows: list[dict[str, str]],
    new_rows: list[dict[str, object]],
    key: str,
) -> list[dict[str, object]]:
    if not new_rows:
        return [dict(row) for row in existing_rows]
    fieldnames = list(new_rows[0])
    if any(list(row) != fieldnames for row in new_rows):
        raise RuntimeError("Broker ledger rows have inconsistent schemas")
    result: list[dict[str, object]] = [dict(row) for row in existing_rows]
    indexed = {
        str(row.get(key, "")): _normalized_row(row, fieldnames)
        for row in existing_rows
    }
    for row in new_rows:
        row_key = str(row[key])
        normalized = _normalized_row(row, fieldnames)
        previous = indexed.get(row_key)
        if previous is not None and previous != normalized:
            raise RuntimeError(f"Conflicting broker ledger row for {key}={row_key}")
        if previous is None:
            result.append(dict(row))
            indexed[row_key] = normalized
    return result


def _append_rows(
    path: Path,
    rows: list[dict[str, object]],
    key: str,
    existing_rows: list[dict[str, str]],
) -> None:
    fieldnames = list(rows[0])
    if any(list(row) != fieldnames for row in rows):
        raise RuntimeError("Broker ledger rows have inconsistent schemas")
    existing = {str(row[key]): _normalized_row(row, fieldnames) for row in existing_rows}
    pending: dict[str, tuple[str, ...]] = {}
    for row in rows:
        row_key = str(row[key])
        normalized = _normalized_row(row, fieldnames)
        prior = pending.get(row_key, existing.get(row_key))
        if prior is not None and prior != normalized:
            raise RuntimeError(f"Conflicting broker ledger row for {key}={row_key}")
        if prior is None:
            pending[row_key] = normalized
    if not pending:
        return

    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        if not exists:
            writer.writeheader()
        for row in rows:
            row_key = str(row[key])
            if row_key in pending:
                writer.writerow(row)
                pending.pop(row_key)
        handle.flush()
        os.fsync(handle.fileno())


def _read_rows(path: Path, key: str) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or key not in reader.fieldnames:
            raise RuntimeError(f"Invalid broker ledger schema: {path}")
        rows = list(reader)
    seen: dict[str, tuple[tuple[str, str], ...]] = {}
    for row in rows:
        row_key = str(row.get(key, ""))
        if not row_key:
            raise RuntimeError(f"Invalid broker ledger row: empty {key}")
        normalized = tuple((name, str(value)) for name, value in row.items())
        prior = seen.get(row_key)
        if prior is not None:
            if prior != normalized:
                raise RuntimeError(
                    f"Conflicting broker ledger rows for {key}={row_key}"
                )
            raise RuntimeError(f"Duplicate broker ledger row for {key}={row_key}")
        seen[row_key] = normalized
    return rows


def _normalized_row(
    row: dict[str, object], fieldnames: list[str]
) -> tuple[str, ...]:
    values = []
    for name in fieldnames:
        value = row.get(name)
        if value is None or value == "":
            values.append("")
        elif name in INTEGER_LEDGER_FIELDS:
            try:
                values.append(str(int(value)))
            except (TypeError, ValueError):
                values.append(str(value))
        elif name in FLOAT_LEDGER_FIELDS:
            try:
                numeric = float(value)
                values.append(str(0.0 if numeric == 0 else numeric))
            except (TypeError, ValueError):
                values.append(str(value))
        else:
            values.append(str(value))
    return tuple(values)


def _order_event_payload(order: BrokerOrderSnapshot) -> dict[str, object]:
    validate_order_snapshot(order)
    payload: dict[str, object] = {
        "client_order_id": order.client_order_id,
        "broker_order_id": order.broker_order_id,
        "symbol": order.symbol,
        "side": order.side.value,
        "quantity": order.quantity,
        "filled_quantity": order.filled_quantity,
        "limit_price": float(order.limit_price),
        "average_fill_price": (
            float(order.average_fill_price)
            if order.average_fill_price is not None
            else None
        ),
        "status": order.status.value,
        "updated_at": order.updated_at.isoformat(),
        "message": order.message,
    }
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    payload["event_id"] = "v2_" + hashlib.sha256(raw.encode("ascii")).hexdigest()[:24]
    return payload


def _fill_payload(fill: BrokerFill) -> dict[str, object]:
    validate_broker_fill(fill)
    return {
        "fill_id": fill.fill_id,
        "broker_order_id": fill.broker_order_id,
        "client_order_id": fill.client_order_id,
        "symbol": fill.symbol,
        "side": fill.side.value,
        "quantity": fill.quantity,
        "price": float(fill.price),
        "commission": float(fill.commission),
        "tax": float(fill.tax),
        "filled_at": fill.filled_at.isoformat(),
    }


def _order_events_from_rows(
    rows: list[dict[str, object]],
) -> list[BrokerOrderSnapshot]:
    events = []
    for row in rows:
        if list(row) != ORDER_EVENT_FIELDS:
            raise RuntimeError("Invalid broker order ledger schema")
        try:
            event = BrokerOrderSnapshot(
                client_order_id=str(row["client_order_id"]),
                broker_order_id=str(row["broker_order_id"]),
                symbol=str(row["symbol"]),
                side=OrderSide(str(row["side"])),
                quantity=int(row["quantity"]),
                filled_quantity=int(row["filled_quantity"]),
                limit_price=float(row["limit_price"]),
                average_fill_price=(
                    float(row["average_fill_price"])
                    if row["average_fill_price"]
                    else None
                ),
                status=OrderStatus(str(row["status"])),
                updated_at=datetime.fromisoformat(str(row["updated_at"])),
                message=str(row["message"]),
            )
            expected_event_id = str(_order_event_payload(event)["event_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid broker order ledger row: {exc}") from exc
        actual_event_id = str(row["event_id"])
        if actual_event_id.startswith("v2_"):
            valid_event_id = actual_event_id == expected_event_id
        else:
            valid_event_id = actual_event_id == _legacy_order_event_id(event)
        if not valid_event_id:
            raise RuntimeError(
                f"Broker order ledger row failed content validation: {event.client_order_id}"
            )
        events.append(event)
    return events


def _fills_from_rows(rows: list[dict[str, object]]) -> list[BrokerFill]:
    fills = []
    for row in rows:
        if list(row) != FILL_FIELDS:
            raise RuntimeError("Invalid broker fill ledger schema")
        try:
            fill = BrokerFill(
                fill_id=str(row["fill_id"]),
                broker_order_id=str(row["broker_order_id"]),
                client_order_id=str(row["client_order_id"]),
                symbol=str(row["symbol"]),
                side=OrderSide(str(row["side"])),
                quantity=int(row["quantity"]),
                price=float(row["price"]),
                commission=float(row["commission"]),
                tax=float(row["tax"]),
                filled_at=datetime.fromisoformat(str(row["filled_at"])),
            )
            _fill_payload(fill)
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid broker fill ledger row: {exc}") from exc
        fills.append(fill)
    return fills


def _legacy_order_event_id(order: BrokerOrderSnapshot) -> str:
    raw = "|".join(
        [
            order.client_order_id,
            order.broker_order_id,
            order.status.value,
            order.updated_at.isoformat(),
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


@contextmanager
def ledger_lock(path: Path) -> Iterator[None]:
    """Fail closed when another process is changing a broker ledger."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError(f"Broker ledger is locked: {lock_path}") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
