from __future__ import annotations

import csv
import hashlib
import json
import math
import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .base import BrokerAccount, BrokerPosition
from .ledger import ledger_lock


@dataclass(frozen=True)
class ReconciliationIssue:
    kind: str
    key: str
    expected: float
    actual: float


def reconcile_account(
    expected_cash: float,
    expected_positions: dict[str, int],
    broker_account: BrokerAccount,
    broker_positions: list[BrokerPosition],
    cash_tolerance: float = 0.01,
) -> list[ReconciliationIssue]:
    if not math.isfinite(expected_cash) or not math.isfinite(broker_account.cash):
        raise ValueError("Reconciliation cash values must be finite")
    if cash_tolerance < 0 or not math.isfinite(cash_tolerance):
        raise ValueError("cash_tolerance must be finite and non-negative")
    issues: list[ReconciliationIssue] = []
    if not math.isclose(
        expected_cash, broker_account.cash, rel_tol=0.0, abs_tol=cash_tolerance
    ):
        issues.append(
            ReconciliationIssue(
                "cash", "CNY", expected_cash, broker_account.cash
            )
        )
    actual_positions: dict[str, int] = {}
    for value in broker_positions:
        if not value.symbol or isinstance(value.quantity, bool) or not isinstance(
            value.quantity, int
        ) or value.quantity < 0:
            raise ValueError("Broker positions require a symbol and integer quantity")
        actual_positions[value.symbol] = (
            actual_positions.get(value.symbol, 0) + value.quantity
        )
    for symbol, quantity in expected_positions.items():
        if (
            not symbol
            or isinstance(quantity, bool)
            or not isinstance(quantity, int)
            or quantity < 0
        ):
            raise ValueError("Expected positions require a symbol and non-negative integer")
    for symbol in sorted(set(expected_positions) | set(actual_positions)):
        expected = int(expected_positions.get(symbol, 0))
        actual = int(actual_positions.get(symbol, 0))
        if expected != actual:
            issues.append(ReconciliationIssue("position", symbol, expected, actual))
    return issues


def append_reconciliation(
    path: Path,
    *,
    on_date: date,
    adapter: str,
    account_id: str,
    config_fingerprint: str,
    expected_cash: float,
    broker_cash: float,
    issues: list[ReconciliationIssue],
) -> str:
    if not adapter or not account_id or not config_fingerprint:
        raise ValueError("Reconciliation identity fields must be non-empty")
    if not math.isfinite(expected_cash) or not math.isfinite(broker_cash):
        raise ValueError("Reconciliation cash values must be finite")
    if any(
        not isinstance(value, ReconciliationIssue)
        or not value.kind
        or not value.key
        or not math.isfinite(value.expected)
        or not math.isfinite(value.actual)
        for value in issues
    ):
        raise ValueError("Reconciliation issues contain invalid values")
    path.parent.mkdir(parents=True, exist_ok=True)
    issue_payload = [value.__dict__ for value in issues]
    raw_id = "|".join(
        [adapter, account_id, on_date.isoformat(), config_fingerprint]
    )
    reconciliation_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:24]
    fieldnames = [
        "reconciliation_id",
        "date",
        "adapter",
        "account_id",
        "config_fingerprint",
        "expected_cash",
        "broker_cash",
        "issue_count",
        "issues",
    ]
    row = {
        "reconciliation_id": reconciliation_id,
        "date": on_date.isoformat(),
        "adapter": adapter,
        "account_id": account_id,
        "config_fingerprint": config_fingerprint,
        "expected_cash": f"{expected_cash:.6f}",
        "broker_cash": f"{broker_cash:.6f}",
        "issue_count": str(len(issues)),
        "issues": json.dumps(issue_payload, ensure_ascii=False, sort_keys=True),
    }
    conflicting = False
    with ledger_lock(path):
        existing_rows: list[dict[str, str]] = []
        if path.exists():
            with path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                if reader.fieldnames != fieldnames:
                    raise RuntimeError(f"Invalid reconciliation ledger schema: {path}")
                existing_rows = list(reader)
        matches = [
            value
            for value in existing_rows
            if value["reconciliation_id"] == reconciliation_id
        ]
        if any(value == row for value in matches):
            return reconciliation_id
        conflicting = bool(matches)
        exists = path.exists()
        with path.open("a", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames)
            if not exists:
                writer.writeheader()
            writer.writerow(row)
            handle.flush()
            os.fsync(handle.fileno())
    if conflicting:
        raise RuntimeError(
            "Conflicting reconciliation for the same account, date, and configuration"
        )
    return reconciliation_id


def audit_reconciliations(
    path: Path,
    adapter: str,
    account_id: str,
    minimum_sessions: int,
    config_fingerprint: str = "",
) -> dict[str, object]:
    if not path.exists() or not adapter or not account_id or not config_fingerprint:
        return {
            "eligible": False,
            "clean_sessions": 0,
            "minimum_sessions": minimum_sessions,
            "remaining_sessions": minimum_sessions,
            "last_date": None,
            "errors": [],
        }
    errors: list[str] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "reconciliation_id",
            "date",
            "adapter",
            "account_id",
            "config_fingerprint",
            "expected_cash",
            "broker_cash",
            "issue_count",
            "issues",
        }
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            return {
                "eligible": False,
                "clean_sessions": 0,
                "minimum_sessions": minimum_sessions,
                "remaining_sessions": minimum_sessions,
                "last_date": None,
                "errors": ["reconciliation ledger schema is invalid"],
            }
        rows = [
            row
            for row in reader
            if row["adapter"] == adapter
            and row["account_id"] == account_id
            and row["config_fingerprint"] == config_fingerprint
        ]
    clean_sessions = 0
    previous: date | None = None
    seen_ids: set[str] = set()
    for row in rows:
        try:
            current = date.fromisoformat(row["date"])
            issue_count = int(row["issue_count"])
            expected_cash = float(row["expected_cash"])
            broker_cash = float(row["broker_cash"])
            issue_payload = json.loads(row["issues"])
            raw_id = "|".join(
                [adapter, account_id, current.isoformat(), config_fingerprint]
            )
            expected_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:24]
            if row["reconciliation_id"] != expected_id:
                raise ValueError("reconciliation_id does not match row identity")
            if row["reconciliation_id"] in seen_ids:
                raise ValueError("duplicate reconciliation_id")
            if issue_count < 0 or not isinstance(issue_payload, list):
                raise ValueError("invalid reconciliation issues")
            if len(issue_payload) != issue_count:
                raise ValueError("issue_count does not match issues")
            for issue in issue_payload:
                if not isinstance(issue, dict) or not {
                    "kind",
                    "key",
                    "expected",
                    "actual",
                }.issubset(issue):
                    raise ValueError("invalid reconciliation issue payload")
                if (
                    not str(issue["kind"])
                    or not str(issue["key"])
                    or not math.isfinite(float(issue["expected"]))
                    or not math.isfinite(float(issue["actual"]))
                ):
                    raise ValueError("invalid reconciliation issue values")
            if not math.isfinite(expected_cash) or not math.isfinite(broker_cash):
                raise ValueError("cash values must be finite")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"invalid reconciliation row: {exc}")
            continue
        seen_ids.add(row["reconciliation_id"])
        if previous is not None and current <= previous:
            errors.append("reconciliation dates are not strictly increasing")
        previous = current
        clean_sessions = clean_sessions + 1 if issue_count == 0 else 0
    eligible = not errors and clean_sessions >= minimum_sessions
    return {
        "eligible": eligible,
        "clean_sessions": clean_sessions,
        "minimum_sessions": minimum_sessions,
        "remaining_sessions": max(0, minimum_sessions - clean_sessions),
        "last_date": previous.isoformat() if previous else None,
        "errors": errors,
    }
