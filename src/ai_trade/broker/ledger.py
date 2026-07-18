from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import threading
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Iterator
from uuid import uuid4

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
from .scope import (
    BrokerLedgerScope,
    ensure_scope_manifest,
    inspect_scope_manifest,
    preflight_scope_manifest,
    read_scope_manifest,
    require_scope_manifest,
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

LEGACY_FILL_FIELDS = [
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
FILL_FIELDS = [*LEGACY_FILL_FIELDS, "record_sha256"]

INTEGER_LEDGER_FIELDS = frozenset({"quantity", "filled_quantity"})
FLOAT_LEDGER_FIELDS = frozenset(
    {"limit_price", "average_fill_price", "price", "commission", "tax"}
)
_LEDGER_THREAD_LOCK = threading.RLock()
CHINA_STANDARD_TIME = timezone(timedelta(hours=8))
_APPROVAL_ID = re.compile(r"approval_[0-9a-f]{32}\Z")
_SHA256 = re.compile(r"[0-9a-f]{64}\Z")
_SCOPE_ID = re.compile(r"v1_[0-9a-f]{24}\Z")
_SCOPED_DIGEST = re.compile(r"(?:[0-9a-f]{24}|[0-9a-f]{64})\Z")
_LIVE_INTENT_PREFIX = "ai-trade-live-intent-v1:"
_SCOPED_RECORD_PREFIX = "s1:"


def initialize_broker_ledger_scope(
    scope_path: Path,
    orders_path: Path,
    fills_path: Path,
    scope: BrokerLedgerScope,
) -> None:
    scope.require_ledger_path("orders", orders_path)
    scope.require_ledger_path("fills", fills_path)
    paths = _scoped_paths([orders_path, fills_path], scope_path, scope)
    with _ledger_locks(paths):
        ensure_scope_manifest(
            scope_path,
            scope,
            legacy_ledgers_exist=orders_path.exists() or fills_path.exists(),
        )


def preflight_broker_ledger_scope(
    scope_path: Path,
    orders_path: Path,
    fills_path: Path,
    scope: BrokerLedgerScope,
) -> None:
    """Validate an existing binding before any broker adapter I/O."""
    scope.require_ledger_path("orders", orders_path)
    scope.require_ledger_path("fills", fills_path)
    paths = _scoped_paths([orders_path, fills_path], scope_path, scope)
    with _ledger_locks(paths):
        preflight_scope_manifest(
            scope_path,
            scope,
            legacy_ledgers_exist=orders_path.exists() or fills_path.exists(),
        )


def append_order_events(
    path: Path,
    orders: list[BrokerOrderSnapshot],
    *,
    scope_path: Path | None = None,
    scope: BrokerLedgerScope | None = None,
) -> None:
    if not orders:
        return
    _require_scope_ledger_path(scope, "orders", path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _ledger_locks(_scoped_paths([path], scope_path, scope)):
        _require_bound_scope(scope_path, scope)
        scope_id = scope.scope_id if scope is not None else None
        existing_rows = _read_rows(
            path,
            "event_id",
            allowed_schemas=(ORDER_EVENT_FIELDS,),
        )
        existing_events = _order_events_from_rows(existing_rows, scope_id=scope_id)
        if scope_id is None and _row_scope_ids(existing_rows):
            raise RuntimeError("Scoped broker order ledger requires its scope")
        recover_order_states(existing_events + orders)
        pending_orders = [order for order in orders if order not in existing_events]
        if pending_orders:
            _append_rows(
                path,
                [
                    _order_event_payload(order, scope_id=scope_id)
                    for order in pending_orders
                ],
                "event_id",
                existing_rows,
            )


def reserve_order_intents(
    path: Path,
    orders: list[BrokerOrderRequest],
    on_date: date,
    max_daily_notional: float,
    max_daily_orders: int | None = None,
    *,
    approval_id: str | None = None,
    batch_fingerprint: str | None = None,
    scope_path: Path | None = None,
    scope: BrokerLedgerScope | None = None,
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
    intent_message = _submission_intent_message(
        approval_id,
        batch_fingerprint,
    )
    timestamp = datetime.combine(on_date, time.min, tzinfo=CHINA_STANDARD_TIME)
    _require_scope_ledger_path(scope, "orders", path)
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
            message=intent_message,
        )
        for order in orders
    ]
    scope_id = scope.scope_id if scope is not None else None
    rows = [
        _order_event_payload(snapshot, scope_id=scope_id)
        for snapshot in snapshots
    ]
    batch_notional = sum(order.quantity * order.limit_price for order in orders)

    path.parent.mkdir(parents=True, exist_ok=True)
    with _ledger_locks(_scoped_paths([path], scope_path, scope)):
        _require_bound_scope(scope_path, scope)
        existing_rows = _read_rows(
            path,
            "event_id",
            allowed_schemas=(ORDER_EVENT_FIELDS,),
        )
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
        recover_order_states(
            _order_events_from_rows(existing_rows, scope_id=scope_id) + snapshots
        )
        if scope_id is None and _row_scope_ids(existing_rows):
            raise RuntimeError("Scoped broker order ledger requires its scope")
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


def _submission_intent_message(
    approval_id: str | None,
    batch_fingerprint: str | None,
) -> str:
    if approval_id is None and batch_fingerprint is None:
        return "Local submission intent reserved before broker I/O"
    if (
        not isinstance(approval_id, str)
        or not _APPROVAL_ID.fullmatch(approval_id)
        or not isinstance(batch_fingerprint, str)
        or not _SHA256.fullmatch(batch_fingerprint)
    ):
        raise ValueError(
            "Submission intent approval ID and batch fingerprint must be valid"
        )
    payload = json.dumps(
        {
            "approval_id": approval_id,
            "batch_fingerprint": batch_fingerprint,
        },
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return _LIVE_INTENT_PREFIX + payload


def append_fills(
    path: Path,
    fills: list[BrokerFill],
    *,
    scope_path: Path | None = None,
    scope: BrokerLedgerScope | None = None,
) -> None:
    if not fills:
        return
    _require_scope_ledger_path(scope, "fills", path)
    for fill in fills:
        validate_broker_fill(fill)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _ledger_locks(_scoped_paths([path], scope_path, scope)):
        _require_bound_scope(scope_path, scope)
        scope_id = scope.scope_id if scope is not None else None
        existing_rows = _read_rows(
            path,
            "fill_id",
            allowed_schemas=(FILL_FIELDS, LEGACY_FILL_FIELDS),
        )
        _fills_from_rows(existing_rows, scope_id=scope_id)
        if scope_id is None and _row_scope_ids(existing_rows):
            raise RuntimeError("Scoped broker fill ledger requires its scope")
        rows = _fill_rows_for_ledger(
            path,
            fills,
            existing_rows,
            scope_id=scope_id,
        )
        _append_rows(path, rows, "fill_id", existing_rows)


def append_broker_observation(
    orders_path: Path,
    fills_path: Path,
    orders: list[BrokerOrderSnapshot],
    fills: list[BrokerFill],
    *,
    scope_path: Path | None = None,
    scope: BrokerLedgerScope | None = None,
) -> dict[str, object]:
    """Validate and append one restart-safe broker polling observation."""
    if orders_path.resolve() == fills_path.resolve():
        raise ValueError("Broker order and fill ledgers must use different paths")
    _require_scope_ledger_path(scope, "orders", orders_path)
    _require_scope_ledger_path(scope, "fills", fills_path)
    for fill in fills:
        validate_broker_fill(fill)
    orders_path.parent.mkdir(parents=True, exist_ok=True)
    fills_path.parent.mkdir(parents=True, exist_ok=True)

    paths = _scoped_paths([orders_path, fills_path], scope_path, scope)
    with _ledger_locks(paths):
        scope_id = scope.scope_id if scope is not None else None
        if scope is not None and scope_path is not None:
            ensure_scope_manifest(
                scope_path,
                scope,
                legacy_ledgers_exist=orders_path.exists() or fills_path.exists(),
            )
        existing_order_rows = _read_rows(
            orders_path,
            "event_id",
            allowed_schemas=(ORDER_EVENT_FIELDS,),
        )
        existing_fill_rows = _read_rows(
            fills_path,
            "fill_id",
            allowed_schemas=(FILL_FIELDS, LEGACY_FILL_FIELDS),
        )
        fill_rows = _fill_rows_for_ledger(
            fills_path,
            fills,
            existing_fill_rows,
            scope_id=scope_id,
        )
        existing_events = _order_events_from_rows(
            existing_order_rows,
            scope_id=scope_id,
        )
        if scope_id is None and _row_scope_ids(existing_order_rows):
            raise RuntimeError("Scoped broker order ledger requires its scope")
        if scope_id is None and _row_scope_ids(existing_fill_rows):
            raise RuntimeError("Scoped broker fill ledger requires its scope")
        pending_orders = [order for order in orders if order not in existing_events]
        order_rows = [
            _order_event_payload(order, scope_id=scope_id)
            for order in pending_orders
        ]
        prospective_order_rows = _merged_rows(
            existing_order_rows, order_rows, "event_id"
        )
        prospective_fill_rows = _merged_rows(
            existing_fill_rows, fill_rows, "fill_id"
        )
        report = build_lifecycle_report(
            _order_events_from_rows(prospective_order_rows, scope_id=scope_id),
            _fills_from_rows(prospective_fill_rows, scope_id=scope_id),
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


def read_order_events(
    path: Path,
    *,
    scope_id: str | None = None,
) -> list[BrokerOrderSnapshot]:
    if not path.exists():
        return []
    with ledger_lock(path):
        return _order_events_from_rows(
            _read_rows(
                path,
                "event_id",
                allowed_schemas=(ORDER_EVENT_FIELDS,),
            ),
            scope_id=scope_id,
        )


def read_fills(
    path: Path,
    *,
    scope_id: str | None = None,
) -> list[BrokerFill]:
    if not path.exists():
        return []
    with ledger_lock(path):
        return _fills_from_rows(
            _read_rows(
                path,
                "fill_id",
                allowed_schemas=(FILL_FIELDS, LEGACY_FILL_FIELDS),
            ),
            scope_id=scope_id,
        )


def recover_order_lifecycle(
    orders_path: Path,
    fills_path: Path,
    *,
    scope_path: Path | None = None,
    expected_scope: BrokerLedgerScope | None = None,
) -> dict[str, object]:
    scope_report = (
        inspect_scope_manifest(
            scope_path,
            orders_path,
            fills_path,
            expected_scope,
        )
        if scope_path is not None
        else None
    )
    embedded_scope_id = None
    if scope_path is not None and scope_path.exists():
        try:
            embedded_scope_id = read_scope_manifest(scope_path).scope_id
        except RuntimeError:
            # Keep the lifecycle rows inspectable; scope_report below still
            # marks the manifest invalid and removes authority.
            embedded_scope_id = None
    try:
        events = read_order_events(orders_path, scope_id=embedded_scope_id)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _attach_scope_report(
            lifecycle_error_report(
                "order_ledger_invalid", f"Broker order ledger is invalid: {exc}"
            ),
            scope_report,
        )
    try:
        fills = read_fills(fills_path, scope_id=embedded_scope_id)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        return _attach_scope_report(
            lifecycle_error_report(
                "fill_ledger_invalid", f"Broker fill ledger is invalid: {exc}"
            ),
            scope_report,
        )
    try:
        return _attach_scope_report(
            build_lifecycle_report(events, fills),
            scope_report,
        )
    except (RuntimeError, TypeError, ValueError) as exc:
        return _attach_scope_report(
            lifecycle_error_report(
                "lifecycle_invalid", f"Broker order lifecycle is invalid: {exc}"
            ),
            scope_report,
        )


def submitted_order_notional(
    path: Path,
    on_date: date,
    *,
    scope_path: Path | None = None,
    scope: BrokerLedgerScope | None = None,
) -> float:
    """Return gross notional first submitted on a date, counted once per client order."""
    _require_scope_ledger_path(scope, "orders", path)
    if scope_path is None and scope is None and not path.exists():
        return 0.0
    with _ledger_locks(_scoped_paths([path], scope_path, scope)):
        if not path.exists():
            _require_scope_if_present(scope_path, scope)
            return 0.0
        _require_bound_scope(scope_path, scope)
        rows = _read_rows(
            path,
            "event_id",
            allowed_schemas=(ORDER_EVENT_FIELDS,),
        )
        scope_id = scope.scope_id if scope is not None else None
        recover_order_states(_order_events_from_rows(rows, scope_id=scope_id))
        return _submitted_order_notional(rows, on_date)


def submitted_order_count(
    path: Path,
    on_date: date,
    *,
    scope_path: Path | None = None,
    scope: BrokerLedgerScope | None = None,
) -> int:
    """Return client orders first reserved on a date, counted once per order."""
    _require_scope_ledger_path(scope, "orders", path)
    if scope_path is None and scope is None and not path.exists():
        return 0
    with _ledger_locks(_scoped_paths([path], scope_path, scope)):
        if not path.exists():
            _require_scope_if_present(scope_path, scope)
            return 0
        _require_bound_scope(scope_path, scope)
        rows = _read_rows(
            path,
            "event_id",
            allowed_schemas=(ORDER_EVENT_FIELDS,),
        )
        scope_id = scope.scope_id if scope is not None else None
        recover_order_states(_order_events_from_rows(rows, scope_id=scope_id))
        return sum(
            updated_at.date() == on_date
            for updated_at, _ in _first_order_events(rows).values()
        )


def _attach_scope_report(
    report: dict[str, object],
    scope_report: dict[str, object] | None,
) -> dict[str, object]:
    if scope_report is None:
        return report
    report["scope"] = scope_report
    scope_status = str(scope_report.get("status", "INVALID"))
    if scope_status in {"INVALID", "MISMATCH"}:
        errors = report.get("integrity_errors")
        if not isinstance(errors, list):
            errors = []
            report["integrity_errors"] = errors
        errors.append(
            {
                "code": "ledger_scope_invalid",
                "client_order_id": "",
                "message": str(scope_report.get("message") or "Ledger scope is invalid"),
            }
        )
        report["status"] = "INTEGRITY_ERROR"
    elif scope_status == "UNSCOPED":
        warnings = report.get("recovery_warnings")
        if not isinstance(warnings, list):
            warnings = []
            report["recovery_warnings"] = warnings
        warnings.append(
            {
                "code": "ledger_scope_missing",
                "client_order_id": "",
                "message": "Lifecycle ledgers predate broker scope binding",
            }
        )
        if report.get("status") == "VERIFIED":
            report["status"] = "RECOVERED"
    report["qualifying_evidence"] = False
    report["execution_enabled"] = False
    return report


def _scoped_paths(
    ledger_paths: list[Path],
    scope_path: Path | None,
    scope: BrokerLedgerScope | None,
) -> list[Path]:
    if (scope_path is None) != (scope is None):
        raise ValueError("Broker ledger scope path and scope must be supplied together")
    if scope_path is None:
        return ledger_paths
    scope_resolved = scope_path.resolve()
    if any(scope_resolved == path.resolve() for path in ledger_paths):
        raise ValueError("Broker ledger scope path must differ from CSV ledger paths")
    return [*ledger_paths, scope_path]


@contextmanager
def _ledger_locks(paths: list[Path]) -> Iterator[None]:
    indexed = {str(path.resolve()): path for path in paths}
    ordered = [indexed[key] for key in sorted(indexed)]
    with ExitStack() as stack:
        for path in ordered:
            stack.enter_context(ledger_lock(path))
        yield


def _require_bound_scope(
    scope_path: Path | None,
    scope: BrokerLedgerScope | None,
) -> None:
    if scope_path is not None and scope is not None:
        require_scope_manifest(scope_path, scope)


def _require_scope_if_present(
    scope_path: Path | None,
    scope: BrokerLedgerScope | None,
) -> None:
    if scope_path is not None and scope is not None and scope_path.exists():
        require_scope_manifest(scope_path, scope)


def _require_scope_ledger_path(
    scope: BrokerLedgerScope | None,
    role: str,
    path: Path,
) -> None:
    if scope is not None:
        scope.require_ledger_path(role, path)


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

    selected = []
    for row in rows:
        row_key = str(row[key])
        if row_key in pending:
            selected.append(row)
            pending.pop(row_key)
    atomic_append_csv(path, fieldnames, selected)


def atomic_append_csv(
    path: Path,
    fieldnames: list[str],
    rows: list[dict[str, object]],
) -> None:
    exists = path.exists()
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    needs_separator = False
    write_header = not exists
    try:
        with temporary.open("xb") as target:
            if exists:
                with path.open("rb") as source:
                    source.seek(0, os.SEEK_END)
                    size = source.tell()
                    if size:
                        write_header = False
                        source.seek(-1, os.SEEK_END)
                        needs_separator = source.read(1) not in {b"\r", b"\n"}
                        source.seek(0)
                        shutil.copyfileobj(source, target, length=1024 * 1024)
            target.flush()

        with temporary.open("a", encoding="utf-8", newline="") as handle:
            if needs_separator:
                handle.write("\r\n")
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            if write_header:
                writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _read_rows(
    path: Path,
    key: str,
    *,
    allowed_schemas: tuple[list[str], ...] | None = None,
) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        if (
            not fieldnames
            or key not in fieldnames
            or (
                allowed_schemas is not None
                and not any(fieldnames == schema for schema in allowed_schemas)
            )
        ):
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


def _order_event_payload(
    order: BrokerOrderSnapshot,
    *,
    scope_id: str | None = None,
) -> dict[str, object]:
    validate_order_snapshot(order)
    _validate_scope_id(scope_id)
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
    fingerprint_payload: object = payload
    if scope_id is not None:
        fingerprint_payload = {"scope_id": scope_id, "event": payload}
    raw = json.dumps(
        fingerprint_payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(raw.encode("ascii")).hexdigest()[:24]
    payload["event_id"] = (
        f"{_SCOPED_RECORD_PREFIX}{scope_id}:{digest}"
        if scope_id is not None
        else "v2_" + digest
    )
    return payload


def _fill_payload(
    fill: BrokerFill,
    *,
    scope_id: str | None = None,
) -> dict[str, object]:
    payload = _legacy_fill_payload(fill)
    _validate_scope_id(scope_id)
    fingerprint_payload: object = payload
    if scope_id is not None:
        fingerprint_payload = {"scope_id": scope_id, "fill": payload}
    raw = json.dumps(
        fingerprint_payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    digest = hashlib.sha256(raw.encode("ascii")).hexdigest()
    payload["record_sha256"] = (
        f"{_SCOPED_RECORD_PREFIX}{scope_id}:{digest}"
        if scope_id is not None
        else digest
    )
    return payload


def _legacy_fill_payload(fill: BrokerFill) -> dict[str, object]:
    validate_broker_fill(fill)
    return {
        "fill_id": fill.fill_id,
        "broker_order_id": fill.broker_order_id,
        "client_order_id": fill.client_order_id,
        "symbol": fill.symbol,
        "side": fill.side.value,
        "quantity": fill.quantity,
        "price": _canonical_float(fill.price),
        "commission": _canonical_float(fill.commission),
        "tax": _canonical_float(fill.tax),
        "filled_at": fill.filled_at.isoformat(),
    }


def _canonical_float(value: float) -> float:
    numeric = float(value)
    return 0.0 if numeric == 0.0 else numeric


def _fill_rows_for_ledger(
    path: Path,
    fills: list[BrokerFill],
    existing_rows: list[dict[str, str]],
    *,
    scope_id: str | None = None,
) -> list[dict[str, object]]:
    _validate_scope_id(scope_id)
    if existing_rows:
        schema = list(existing_rows[0])
    elif path.exists():
        with path.open("r", encoding="utf-8", newline="") as handle:
            schema = list(csv.DictReader(handle).fieldnames or [])
    else:
        schema = FILL_FIELDS
    if schema == FILL_FIELDS:
        return [_fill_payload(fill, scope_id=scope_id) for fill in fills]
    if schema == LEGACY_FILL_FIELDS:
        if scope_id is not None:
            raise RuntimeError(
                "Scoped broker fill ledgers require content-bound fill rows"
            )
        return [_legacy_fill_payload(fill) for fill in fills]
    raise RuntimeError("Invalid broker fill ledger schema")


def _order_events_from_rows(
    rows: list[dict[str, object]],
    *,
    scope_id: str | None = None,
) -> list[BrokerOrderSnapshot]:
    _validate_scope_id(scope_id)
    events = []
    embedded_scope_ids: set[str] = set()
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
        if actual_event_id.startswith(_SCOPED_RECORD_PREFIX):
            actual_scope_id, digest = _split_scoped_record_id(actual_event_id)
            embedded_scope_ids.add(actual_scope_id)
            if scope_id is not None and actual_scope_id != scope_id:
                raise RuntimeError(
                    f"Broker order row scope does not match client order {event.client_order_id}"
                )
            valid_event_id = actual_event_id == str(
                _order_event_payload(event, scope_id=actual_scope_id)["event_id"]
            )
        elif actual_event_id.startswith("v2_"):
            if scope_id is not None:
                raise RuntimeError(
                    "Unscoped broker order row cannot be used with a bound scope"
                )
            valid_event_id = actual_event_id == expected_event_id
        else:
            if scope_id is not None:
                raise RuntimeError(
                    "Legacy broker order row cannot be used with a bound scope"
                )
            valid_event_id = actual_event_id == _legacy_order_event_id(event)
        if not valid_event_id:
            raise RuntimeError(
                f"Broker order ledger row failed content validation: {event.client_order_id}"
            )
        events.append(event)
    if len(embedded_scope_ids) > 1:
        raise RuntimeError("Broker order ledger contains multiple scope IDs")
    return events


def _fills_from_rows(
    rows: list[dict[str, object]],
    *,
    scope_id: str | None = None,
) -> list[BrokerFill]:
    _validate_scope_id(scope_id)
    fills = []
    embedded_scope_ids: set[str] = set()
    for row in rows:
        schema = list(row)
        if schema not in (FILL_FIELDS, LEGACY_FILL_FIELDS):
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
            expected_record_sha256 = str(_fill_payload(fill)["record_sha256"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid broker fill ledger row: {exc}") from exc
        if schema == FILL_FIELDS:
            actual_record = str(row["record_sha256"])
            if actual_record.startswith(_SCOPED_RECORD_PREFIX):
                actual_scope_id, _ = _split_scoped_record_id(actual_record)
                embedded_scope_ids.add(actual_scope_id)
                if scope_id is not None and actual_scope_id != scope_id:
                    raise RuntimeError(
                        f"Broker fill row scope does not match fill {fill.fill_id}"
                    )
                expected_record_sha256 = str(
                    _fill_payload(fill, scope_id=actual_scope_id)["record_sha256"]
                )
            elif scope_id is not None:
                raise RuntimeError(
                    "Unscoped broker fill row cannot be used with a bound scope"
                )
            if actual_record != expected_record_sha256:
                raise RuntimeError(
                    f"Broker fill ledger row failed content validation: {fill.fill_id}"
                )
        elif scope_id is not None:
            raise RuntimeError(
                "Legacy broker fill row cannot be used with a bound scope"
            )
        fills.append(fill)
    if len(embedded_scope_ids) > 1:
        raise RuntimeError("Broker fill ledger contains multiple scope IDs")
    return fills


def _row_scope_ids(rows: list[dict[str, object]]) -> set[str]:
    scope_ids: set[str] = set()
    for row in rows:
        for name in ("event_id", "record_sha256"):
            value = row.get(name)
            if isinstance(value, str) and value.startswith(_SCOPED_RECORD_PREFIX):
                scope_id, _ = _split_scoped_record_id(value)
                scope_ids.add(scope_id)
    return scope_ids


def _split_scoped_record_id(value: str) -> tuple[str, str]:
    prefix, separator, remainder = value.partition(":")
    if prefix != _SCOPED_RECORD_PREFIX[:-1] or not separator:
        raise RuntimeError("Scoped broker ledger fingerprint has an invalid prefix")
    scope_id, separator, digest = remainder.partition(":")
    if (
        not separator
        or not _SCOPE_ID.fullmatch(scope_id)
        or not _SCOPED_DIGEST.fullmatch(digest)
    ):
        raise RuntimeError("Scoped broker ledger fingerprint is invalid")
    return scope_id, digest


def _validate_scope_id(scope_id: str | None) -> None:
    if scope_id is not None and not _SCOPE_ID.fullmatch(scope_id):
        raise ValueError("Broker ledger scope ID is invalid")


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
    with _LEDGER_THREAD_LOCK:
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


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
