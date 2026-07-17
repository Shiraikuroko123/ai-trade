from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .base import BrokerAccount, BrokerPosition
from .ledger import atomic_append_csv, ledger_lock
from .runtime import validated_broker_account, validated_broker_positions


RECONCILIATION_FIELDS = [
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
RECONCILIATION_V2_PREFIX = "v2_"
RECONCILIATION_CASH_TOLERANCE = 0.01


@dataclass(frozen=True)
class ReconciliationIssue:
    kind: str
    key: str
    expected: float
    actual: float


@dataclass(frozen=True)
class _ValidatedReconciliation:
    reconciliation_id: str
    logical_key: tuple[str, str, str, str]
    content: tuple[tuple[str, str], ...]
    on_date: date
    issue_count: int
    content_bound: bool


def reconcile_account(
    expected_cash: float,
    expected_positions: dict[str, int],
    broker_account: BrokerAccount,
    broker_positions: list[BrokerPosition],
    cash_tolerance: float = RECONCILIATION_CASH_TOLERANCE,
) -> list[ReconciliationIssue]:
    if (
        isinstance(expected_cash, bool)
        or not isinstance(expected_cash, (int, float))
        or not math.isfinite(expected_cash)
        or expected_cash < 0
    ):
        raise ValueError("Expected reconciliation cash must be finite and non-negative")
    if (
        isinstance(cash_tolerance, bool)
        or not isinstance(cash_tolerance, (int, float))
        or cash_tolerance < 0
        or not math.isfinite(cash_tolerance)
    ):
        raise ValueError("cash_tolerance must be finite and non-negative")
    if not isinstance(expected_positions, dict):
        raise ValueError("Expected positions must be a dictionary")
    broker_account = validated_broker_account(broker_account)
    broker_positions = validated_broker_positions(broker_positions)
    if broker_account.currency != "CNY":
        raise ValueError("Reconciliation requires a CNY broker account")
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
    row = _reconciliation_row(
        on_date=on_date,
        adapter=adapter,
        account_id=account_id,
        config_fingerprint=config_fingerprint,
        expected_cash=expected_cash,
        broker_cash=broker_cash,
        issues=issues,
    )
    candidate = _validate_reconciliation_row(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    conflicting = False
    with ledger_lock(path):
        existing = _read_reconciliations(path)
        same_session = [
            value for value in existing if value.logical_key == candidate.logical_key
        ]
        exact = [value for value in same_session if value.content == candidate.content]
        if exact:
            return exact[0].reconciliation_id
        conflicting = bool(same_session)
        atomic_append_csv(path, RECONCILIATION_FIELDS, [row])
    if conflicting:
        raise RuntimeError(
            "Conflicting reconciliation for the same account, date, and configuration"
        )
    return candidate.reconciliation_id


def audit_reconciliations(
    path: Path,
    adapter: str,
    account_id: str,
    minimum_sessions: int,
    config_fingerprint: str = "",
) -> dict[str, object]:
    if (
        isinstance(minimum_sessions, bool)
        or not isinstance(minimum_sessions, int)
        or minimum_sessions < 1
    ):
        raise ValueError("minimum_sessions must be a positive integer")
    if not path.exists() or not adapter or not account_id or not config_fingerprint:
        return _empty_audit(minimum_sessions)
    try:
        with ledger_lock(path):
            validated = _read_reconciliations(path)
    except (OSError, RuntimeError, UnicodeError) as exc:
        return _empty_audit(
            minimum_sessions,
            errors=[f"reconciliation ledger is invalid: {exc}"],
        )

    logical_key_prefix = (adapter, account_id)
    matching_rows = [
        value
        for value in validated
        if value.logical_key[:2] == logical_key_prefix
        and value.logical_key[3] == config_fingerprint
    ]
    ignored_legacy_sessions = sum(
        not value.content_bound for value in matching_rows
    )
    rows = [value for value in matching_rows if value.content_bound]
    errors: list[str] = []
    clean_sessions = 0
    previous: date | None = None
    for row in rows:
        current = row.on_date
        if previous is not None and current <= previous:
            errors.append("reconciliation dates are not strictly increasing")
        previous = current
        clean_sessions = clean_sessions + 1 if row.issue_count == 0 else 0
    eligible = not errors and clean_sessions >= minimum_sessions
    return {
        "eligible": eligible,
        "clean_sessions": clean_sessions,
        "minimum_sessions": minimum_sessions,
        "remaining_sessions": max(0, minimum_sessions - clean_sessions),
        "last_date": previous.isoformat() if previous else None,
        "errors": errors,
        "ignored_legacy_sessions": ignored_legacy_sessions,
    }


def _reconciliation_row(
    *,
    on_date: date,
    adapter: str,
    account_id: str,
    config_fingerprint: str,
    expected_cash: float,
    broker_cash: float,
    issues: list[ReconciliationIssue],
) -> dict[str, str]:
    if not isinstance(on_date, date) or isinstance(on_date, datetime):
        raise ValueError("Reconciliation date must be a date")
    adapter = _identity_value(adapter, "adapter")
    account_id = _identity_value(account_id, "account_id")
    config_fingerprint = _identity_value(
        config_fingerprint, "config_fingerprint"
    )
    expected_cash_value = _nonnegative_number(expected_cash, "expected_cash")
    broker_cash_value = _nonnegative_number(broker_cash, "broker_cash")
    issue_payload = _canonical_issues(issues)
    _validate_clean_cash_evidence(
        expected_cash_value,
        broker_cash_value,
        len(issue_payload),
    )
    content = {
        "date": on_date.isoformat(),
        "adapter": adapter,
        "account_id": account_id,
        "config_fingerprint": config_fingerprint,
        "expected_cash": _format_number(expected_cash_value),
        "broker_cash": _format_number(broker_cash_value),
        "issue_count": str(len(issue_payload)),
        "issues": _issue_json(issue_payload),
    }
    return {
        "reconciliation_id": _v2_reconciliation_id(content),
        **content,
    }


def _read_reconciliations(path: Path) -> list[_ValidatedReconciliation]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != RECONCILIATION_FIELDS:
                raise RuntimeError("reconciliation ledger schema is invalid")
            raw_rows = list(reader)
    except (csv.Error, OSError, UnicodeError) as exc:
        raise RuntimeError(f"reconciliation ledger cannot be read: {exc}") from exc

    validated = []
    seen_ids: set[str] = set()
    seen_sessions: set[tuple[str, str, str, str]] = set()
    for index, row in enumerate(raw_rows, start=2):
        try:
            value = _validate_reconciliation_row(row)
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"invalid reconciliation row {index}: {exc}"
            ) from exc
        if value.reconciliation_id in seen_ids:
            raise RuntimeError(
                f"duplicate reconciliation_id at row {index}"
            )
        if value.logical_key in seen_sessions:
            raise RuntimeError(
                f"conflicting reconciliation session at row {index}"
            )
        seen_ids.add(value.reconciliation_id)
        seen_sessions.add(value.logical_key)
        validated.append(value)
    return validated


def _validate_reconciliation_row(
    row: dict[str, object],
) -> _ValidatedReconciliation:
    if list(row) != RECONCILIATION_FIELDS:
        raise ValueError("row schema does not match the reconciliation ledger")
    if any(value is None for value in row.values()):
        raise ValueError("row has missing or unexpected fields")
    reconciliation_id = str(row["reconciliation_id"])
    raw_date = str(row["date"])
    on_date = date.fromisoformat(raw_date)
    if raw_date != on_date.isoformat():
        raise ValueError("date is not in canonical ISO format")
    adapter = _identity_value(row["adapter"], "adapter")
    account_id = _identity_value(row["account_id"], "account_id")
    config_fingerprint = _identity_value(
        row["config_fingerprint"], "config_fingerprint"
    )
    expected_cash = _nonnegative_number(row["expected_cash"], "expected_cash")
    broker_cash = _nonnegative_number(row["broker_cash"], "broker_cash")
    raw_issue_count = str(row["issue_count"])
    try:
        issue_count = int(raw_issue_count)
    except ValueError as exc:
        raise ValueError("issue_count must be an integer") from exc
    if issue_count < 0 or raw_issue_count.strip() != raw_issue_count:
        raise ValueError("issue_count must be a non-negative integer")
    try:
        raw_issues = json.loads(str(row["issues"]))
    except json.JSONDecodeError as exc:
        raise ValueError("issues must be valid JSON") from exc
    issue_payload = _canonical_issues(raw_issues)
    if len(issue_payload) != issue_count:
        raise ValueError("issue_count does not match issues")

    content = {
        "date": on_date.isoformat(),
        "adapter": adapter,
        "account_id": account_id,
        "config_fingerprint": config_fingerprint,
        "expected_cash": _format_number(expected_cash),
        "broker_cash": _format_number(broker_cash),
        "issue_count": str(issue_count),
        "issues": _issue_json(issue_payload),
    }
    expected_v2_id = _v2_reconciliation_id(content)
    expected_legacy_id = _legacy_reconciliation_id(
        adapter, account_id, on_date, config_fingerprint
    )
    content_bound = reconciliation_id.startswith(RECONCILIATION_V2_PREFIX)
    if content_bound:
        valid_id = reconciliation_id == expected_v2_id
    else:
        valid_id = reconciliation_id == expected_legacy_id
    if not valid_id:
        raise ValueError("reconciliation_id failed content validation")
    _validate_clean_cash_evidence(expected_cash, broker_cash, issue_count)
    return _ValidatedReconciliation(
        reconciliation_id=reconciliation_id,
        logical_key=(adapter, account_id, on_date.isoformat(), config_fingerprint),
        content=tuple(content.items()),
        on_date=on_date,
        issue_count=issue_count,
        content_bound=content_bound,
    )


def _canonical_issues(
    issues: object,
) -> list[dict[str, object]]:
    if not isinstance(issues, list):
        raise ValueError("Reconciliation issues must be a list")
    payload = []
    for issue in issues:
        if isinstance(issue, ReconciliationIssue):
            kind = issue.kind
            key = issue.key
            expected = issue.expected
            actual = issue.actual
        elif isinstance(issue, dict) and set(issue) == {
            "kind",
            "key",
            "expected",
            "actual",
        }:
            kind = issue["kind"]
            key = issue["key"]
            expected = issue["expected"]
            actual = issue["actual"]
        else:
            raise ValueError("Reconciliation issues have an invalid schema")
        payload.append(
            {
                "kind": _identity_value(kind, "issue kind"),
                "key": _identity_value(key, "issue key"),
                "expected": _issue_number(expected, "issue expected"),
                "actual": _issue_number(actual, "issue actual"),
            }
        )
    return sorted(
        payload,
        key=lambda value: (
            str(value["kind"]),
            str(value["key"]),
            float(value["expected"]),
            float(value["actual"]),
        ),
    )


def _identity_value(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"Reconciliation {label} must be a non-empty string")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"Reconciliation {label} contains a control character")
    return value


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"Reconciliation {label} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Reconciliation {label} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"Reconciliation {label} must be finite")
    return 0.0 if number == 0 else number


def _nonnegative_number(value: object, label: str) -> float:
    number = _finite_number(value, label)
    if number < 0:
        raise ValueError(f"Reconciliation {label} must be non-negative")
    return number


def _validate_clean_cash_evidence(
    expected_cash: float,
    broker_cash: float,
    issue_count: int,
) -> None:
    if issue_count == 0 and not math.isclose(
        expected_cash,
        broker_cash,
        rel_tol=0.0,
        abs_tol=RECONCILIATION_CASH_TOLERANCE,
    ):
        raise ValueError(
            "A clean reconciliation cannot contain mismatched cash balances"
        )


def _format_number(value: float) -> str:
    return f"{value:.6f}"


def _issue_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Reconciliation {label} must be a JSON number")
    return _finite_number(value, label)


def _issue_json(issues: list[dict[str, object]]) -> str:
    return json.dumps(
        issues,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _v2_reconciliation_id(content: dict[str, str]) -> str:
    raw = json.dumps(
        content,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return RECONCILIATION_V2_PREFIX + hashlib.sha256(
        raw.encode("ascii")
    ).hexdigest()[:24]


def _legacy_reconciliation_id(
    adapter: str,
    account_id: str,
    on_date: date,
    config_fingerprint: str,
) -> str:
    raw = "|".join(
        [adapter, account_id, on_date.isoformat(), config_fingerprint]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _empty_audit(
    minimum_sessions: int,
    *,
    errors: list[str] | None = None,
) -> dict[str, object]:
    return {
        "eligible": False,
        "clean_sessions": 0,
        "minimum_sessions": minimum_sessions,
        "remaining_sessions": minimum_sessions,
        "last_date": None,
        "errors": errors or [],
        "ignored_legacy_sessions": 0,
    }
