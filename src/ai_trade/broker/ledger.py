from __future__ import annotations

import csv
import hashlib
import math
import os
from contextlib import contextmanager
from dataclasses import asdict
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


def append_order_events(path: Path, orders: list[BrokerOrderSnapshot]) -> None:
    _append_unique(path, [_order_event_payload(order) for order in orders], "event_id")


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
    rows = []
    for fill in fills:
        if (
            not fill.fill_id
            or not fill.broker_order_id
            or not fill.client_order_id
            or not fill.symbol
            or not isinstance(fill.side, OrderSide)
            or isinstance(fill.quantity, bool)
            or not isinstance(fill.quantity, int)
            or fill.quantity <= 0
            or not math.isfinite(fill.price)
            or fill.price <= 0
            or not math.isfinite(fill.commission)
            or fill.commission < 0
            or not math.isfinite(fill.tax)
            or fill.tax < 0
            or fill.filled_at.tzinfo is None
        ):
            raise ValueError("Broker fill contains invalid or incomplete values")
        payload = asdict(fill)
        payload["side"] = fill.side.value
        payload["filled_at"] = fill.filled_at.isoformat()
        rows.append(payload)
    _append_unique(path, rows, "fill_id")


def submitted_order_notional(path: Path, on_date: date) -> float:
    """Return gross notional first submitted on a date, counted once per client order."""
    if not path.exists():
        return 0.0
    return _submitted_order_notional(_read_rows(path, "event_id"), on_date)


def submitted_order_count(path: Path, on_date: date) -> int:
    """Return client orders first reserved on a date, counted once per order."""
    if not path.exists():
        return 0
    return sum(
        updated_at.date() == on_date
        for updated_at, _ in _first_order_events(
            _read_rows(path, "event_id")
        ).values()
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


def _append_unique(path: Path, rows: list[dict[str, object]], key: str) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_lock(path):
        existing_rows = _read_rows(path, key)
        _append_rows(path, rows, key, existing_rows)


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
        if prior is not None and prior != normalized:
            raise RuntimeError(f"Conflicting broker ledger rows for {key}={row_key}")
        seen[row_key] = normalized
    return rows


def _normalized_row(
    row: dict[str, object], fieldnames: list[str]
) -> tuple[str, ...]:
    return tuple("" if row.get(name) is None else str(row.get(name)) for name in fieldnames)


def _order_event_payload(order: BrokerOrderSnapshot) -> dict[str, object]:
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
        or not math.isfinite(order.limit_price)
        or order.limit_price <= 0
        or (
            order.average_fill_price is not None
            and (
                not math.isfinite(order.average_fill_price)
                or order.average_fill_price <= 0
            )
        )
        or order.updated_at.tzinfo is None
        or (
            order.status != OrderStatus.PENDING_SUBMIT
            and not order.broker_order_id
        )
    ):
        raise ValueError("Broker order event contains invalid or incomplete values")
    payload = asdict(order)
    payload["side"] = order.side.value
    payload["status"] = order.status.value
    payload["updated_at"] = order.updated_at.isoformat()
    raw = "|".join(
        [
            order.client_order_id,
            order.broker_order_id,
            order.status.value,
            payload["updated_at"],
        ]
    )
    payload["event_id"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
    return payload


@contextmanager
def ledger_lock(path: Path) -> Iterator[None]:
    """Fail closed when another process is changing a broker ledger."""
    lock_path = path.with_suffix(path.suffix + ".lock")
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError(f"Broker ledger is locked: {lock_path}") from exc
    try:
        os.write(descriptor, str(os.getpid()).encode("ascii"))
        yield
    finally:
        os.close(descriptor)
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass
