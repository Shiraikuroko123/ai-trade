from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ..json_utils import loads_unique_json
from .base import BrokerAccount, BrokerPosition
from .ledger import atomic_append_csv, ledger_lock
from .runtime import validated_broker_account, validated_broker_positions


LEGACY_RECONCILIATION_FIELDS = [
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
RECONCILIATION_FIELDS = [
    *LEGACY_RECONCILIATION_FIELDS[:7],
    "expected_positions",
    "broker_positions",
    *LEGACY_RECONCILIATION_FIELDS[7:],
]
RECONCILIATION_V2_PREFIX = "v2_"
RECONCILIATION_V3_PREFIX = "v3_"
RECONCILIATION_CASH_TOLERANCE = 0.01
CHINA_STANDARD_TIME = timezone(timedelta(hours=8))


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
    base_content: tuple[tuple[str, str], ...]
    on_date: date
    issue_count: int
    qualifying_evidence: bool


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
    expected_positions: dict[str, int],
    broker_positions: dict[str, int],
    issues: list[ReconciliationIssue],
) -> str:
    row = _reconciliation_row(
        on_date=on_date,
        adapter=adapter,
        account_id=account_id,
        config_fingerprint=config_fingerprint,
        expected_cash=expected_cash,
        broker_cash=broker_cash,
        expected_positions=expected_positions,
        broker_positions=broker_positions,
        issues=issues,
    )
    candidate = _validate_reconciliation_row(row)
    if candidate.on_date > _china_today():
        raise ValueError("Reconciliation date cannot be in the future")
    path.parent.mkdir(parents=True, exist_ok=True)
    conflicting = False
    with ledger_lock(path):
        existing = _read_reconciliations(path)
        existing_schema = _reconciliation_schema(path)
        same_session = [
            value for value in existing if value.logical_key == candidate.logical_key
        ]
        exact = [value for value in same_session if value.content == candidate.content]
        legacy_exact = [
            value
            for value in same_session
            if not value.qualifying_evidence
            and value.base_content == candidate.base_content
        ]
        if exact:
            return exact[0].reconciliation_id
        if legacy_exact:
            return legacy_exact[0].reconciliation_id
        conflicting = bool(same_session)
        if existing_schema == LEGACY_RECONCILIATION_FIELDS:
            raise RuntimeError(
                "Legacy reconciliation ledgers cannot accept position-bound evidence"
            )
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
    *,
    completed_through: date | None = None,
) -> dict[str, object]:
    if (
        isinstance(minimum_sessions, bool)
        or not isinstance(minimum_sessions, int)
        or minimum_sessions < 1
    ):
        raise ValueError("minimum_sessions must be a positive integer")
    today = _china_today()
    if completed_through is None:
        completed_through = today - timedelta(days=1)
    if (
        not isinstance(completed_through, date)
        or isinstance(completed_through, datetime)
        or completed_through > today
    ):
        return _empty_audit(
            minimum_sessions,
            errors=["completed market date is invalid or in the future"],
        )
    if not path.exists() or not adapter or not account_id or not config_fingerprint:
        return _empty_audit(
            minimum_sessions,
            completed_through=completed_through,
        )
    try:
        with ledger_lock(path):
            validated = _read_reconciliations(path)
    except (OSError, RuntimeError, UnicodeError) as exc:
        return _empty_audit(
            minimum_sessions,
            errors=[f"reconciliation ledger is invalid: {exc}"],
            completed_through=completed_through,
        )

    future_dates = sorted({value.on_date for value in validated if value.on_date > today})
    if future_dates:
        return _empty_audit(
            minimum_sessions,
            errors=[
                "reconciliation ledger contains future-dated evidence: "
                + ", ".join(value.isoformat() for value in future_dates)
            ],
            completed_through=completed_through,
        )

    logical_key_prefix = (adapter, account_id)
    matching_rows = [
        value
        for value in validated
        if value.logical_key[:2] == logical_key_prefix
        and value.logical_key[3] == config_fingerprint
    ]
    ignored_legacy_sessions = sum(
        not value.qualifying_evidence for value in matching_rows
    )
    qualifying_rows = [value for value in matching_rows if value.qualifying_evidence]
    ignored_incomplete_sessions = sum(
        value.on_date > completed_through for value in qualifying_rows
    )
    errors: list[str] = []
    previous: date | None = None
    for row in qualifying_rows:
        current = row.on_date
        if previous is not None and current <= previous:
            errors.append("reconciliation dates are not strictly increasing")
        previous = current
    clean_sessions = 0
    last_completed: date | None = None
    for row in qualifying_rows:
        if row.on_date > completed_through:
            continue
        last_completed = row.on_date
        clean_sessions = clean_sessions + 1 if row.issue_count == 0 else 0
    eligible = not errors and clean_sessions >= minimum_sessions
    return {
        "eligible": eligible,
        "clean_sessions": clean_sessions,
        "minimum_sessions": minimum_sessions,
        "remaining_sessions": max(0, minimum_sessions - clean_sessions),
        "last_date": last_completed.isoformat() if last_completed else None,
        "errors": errors,
        "ignored_legacy_sessions": ignored_legacy_sessions,
        "ignored_incomplete_sessions": ignored_incomplete_sessions,
        "completed_through": completed_through.isoformat(),
    }


def _reconciliation_row(
    *,
    on_date: date,
    adapter: str,
    account_id: str,
    config_fingerprint: str,
    expected_cash: float,
    broker_cash: float,
    expected_positions: dict[str, int],
    broker_positions: dict[str, int],
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
    expected_position_values = _canonical_positions(
        expected_positions, "expected_positions"
    )
    broker_position_values = _canonical_positions(
        broker_positions, "broker_positions"
    )
    issue_payload = _canonical_issues(issues)
    _validate_clean_cash_evidence(
        expected_cash_value,
        broker_cash_value,
        len(issue_payload),
    )
    _validate_reconciliation_issues(
        expected_cash_value,
        broker_cash_value,
        expected_position_values,
        broker_position_values,
        issue_payload,
    )
    base_content = {
        "date": on_date.isoformat(),
        "adapter": adapter,
        "account_id": account_id,
        "config_fingerprint": config_fingerprint,
        "expected_cash": _format_number(expected_cash_value),
        "broker_cash": _format_number(broker_cash_value),
        "issue_count": str(len(issue_payload)),
        "issues": _issue_json(issue_payload),
    }
    content = _position_bound_content(
        base_content,
        expected_position_values,
        broker_position_values,
    )
    return {
        "reconciliation_id": _v3_reconciliation_id(content),
        **content,
    }


def _read_reconciliations(path: Path) -> list[_ValidatedReconciliation]:
    if not path.exists():
        return []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames not in (
                RECONCILIATION_FIELDS,
                LEGACY_RECONCILIATION_FIELDS,
            ):
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


def _reconciliation_schema(path: Path) -> list[str] | None:
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8", newline="") as handle:
        fieldnames = csv.DictReader(handle).fieldnames
    return list(fieldnames) if fieldnames is not None else None


def _validate_reconciliation_row(
    row: dict[str, object],
) -> _ValidatedReconciliation:
    schema = list(row)
    if schema not in (RECONCILIATION_FIELDS, LEGACY_RECONCILIATION_FIELDS):
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
    if schema == RECONCILIATION_FIELDS:
        expected_positions = _parse_positions(
            row["expected_positions"], "expected_positions"
        )
        broker_positions = _parse_positions(
            row["broker_positions"], "broker_positions"
        )
    else:
        expected_positions = None
        broker_positions = None
    raw_issue_count = str(row["issue_count"])
    try:
        issue_count = int(raw_issue_count)
    except ValueError as exc:
        raise ValueError("issue_count must be an integer") from exc
    if issue_count < 0 or raw_issue_count.strip() != raw_issue_count:
        raise ValueError("issue_count must be a non-negative integer")
    try:
        raw_issues = loads_unique_json(str(row["issues"]))
    except ValueError as exc:
        raise ValueError("issues must be unambiguous valid JSON") from exc
    issue_payload = _canonical_issues(raw_issues)
    if len(issue_payload) != issue_count:
        raise ValueError("issue_count does not match issues")

    base_content = {
        "date": on_date.isoformat(),
        "adapter": adapter,
        "account_id": account_id,
        "config_fingerprint": config_fingerprint,
        "expected_cash": _format_number(expected_cash),
        "broker_cash": _format_number(broker_cash),
        "issue_count": str(issue_count),
        "issues": _issue_json(issue_payload),
    }
    if expected_positions is not None and broker_positions is not None:
        content = _position_bound_content(
            base_content,
            expected_positions,
            broker_positions,
        )
    else:
        content = base_content
    expected_v3_id = _v3_reconciliation_id(content)
    expected_v2_id = _v2_reconciliation_id(base_content)
    expected_legacy_id = _legacy_reconciliation_id(
        adapter, account_id, on_date, config_fingerprint
    )
    if schema == RECONCILIATION_FIELDS:
        valid_id = reconciliation_id == expected_v3_id
        qualifying_evidence = valid_id
    elif reconciliation_id.startswith(RECONCILIATION_V2_PREFIX):
        valid_id = reconciliation_id == expected_v2_id
        qualifying_evidence = False
    else:
        valid_id = reconciliation_id == expected_legacy_id
        qualifying_evidence = False
    if not valid_id:
        raise ValueError("reconciliation_id failed content validation")
    if expected_positions is not None and broker_positions is not None:
        _validate_clean_cash_evidence(expected_cash, broker_cash, issue_count)
        _validate_reconciliation_issues(
            expected_cash,
            broker_cash,
            expected_positions,
            broker_positions,
            issue_payload,
        )
    else:
        _validate_clean_cash_evidence(expected_cash, broker_cash, issue_count)
    return _ValidatedReconciliation(
        reconciliation_id=reconciliation_id,
        logical_key=(adapter, account_id, on_date.isoformat(), config_fingerprint),
        content=tuple(content.items()),
        base_content=tuple(base_content.items()),
        on_date=on_date,
        issue_count=issue_count,
        qualifying_evidence=qualifying_evidence,
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


def _canonical_positions(value: object, label: str) -> dict[str, int]:
    if not isinstance(value, dict):
        raise ValueError(f"Reconciliation {label} must be an object")
    positions: dict[str, int] = {}
    for raw_symbol, raw_quantity in value.items():
        symbol = _identity_value(raw_symbol, f"{label} symbol")
        if (
            isinstance(raw_quantity, bool)
            or not isinstance(raw_quantity, int)
            or raw_quantity < 0
        ):
            raise ValueError(
                f"Reconciliation {label} quantities must be non-negative integers"
            )
        if raw_quantity:
            positions[symbol] = raw_quantity
    return dict(sorted(positions.items()))


def _parse_positions(value: object, label: str) -> dict[str, int]:
    if not isinstance(value, str):
        raise ValueError(f"Reconciliation {label} must be JSON")
    try:
        parsed = loads_unique_json(value)
    except ValueError as exc:
        raise ValueError(
            f"Reconciliation {label} must be unambiguous valid JSON"
        ) from exc
    return _canonical_positions(parsed, label)


def _positions_json(positions: dict[str, int]) -> str:
    return json.dumps(
        positions,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _position_bound_content(
    base: dict[str, str],
    expected_positions: dict[str, int],
    broker_positions: dict[str, int],
) -> dict[str, str]:
    return {
        "date": base["date"],
        "adapter": base["adapter"],
        "account_id": base["account_id"],
        "config_fingerprint": base["config_fingerprint"],
        "expected_cash": base["expected_cash"],
        "broker_cash": base["broker_cash"],
        "expected_positions": _positions_json(expected_positions),
        "broker_positions": _positions_json(broker_positions),
        "issue_count": base["issue_count"],
        "issues": base["issues"],
    }


def _validate_reconciliation_issues(
    expected_cash: float,
    broker_cash: float,
    expected_positions: dict[str, int],
    broker_positions: dict[str, int],
    issues: list[dict[str, object]],
) -> None:
    derived: list[ReconciliationIssue] = []
    if not math.isclose(
        expected_cash,
        broker_cash,
        rel_tol=0.0,
        abs_tol=RECONCILIATION_CASH_TOLERANCE,
    ):
        derived.append(
            ReconciliationIssue("cash", "CNY", expected_cash, broker_cash)
        )
    for symbol in sorted(set(expected_positions) | set(broker_positions)):
        expected = expected_positions.get(symbol, 0)
        actual = broker_positions.get(symbol, 0)
        if expected != actual:
            derived.append(
                ReconciliationIssue("position", symbol, expected, actual)
            )
    if issues != _canonical_issues(derived):
        raise ValueError(
            "Reconciliation issues do not match the supplied cash and positions"
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


def _v3_reconciliation_id(content: dict[str, str]) -> str:
    raw = json.dumps(
        content,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return RECONCILIATION_V3_PREFIX + hashlib.sha256(
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
    completed_through: date | None = None,
) -> dict[str, object]:
    return {
        "eligible": False,
        "clean_sessions": 0,
        "minimum_sessions": minimum_sessions,
        "remaining_sessions": minimum_sessions,
        "last_date": None,
        "errors": errors or [],
        "ignored_legacy_sessions": 0,
        "ignored_incomplete_sessions": 0,
        "completed_through": (
            completed_through.isoformat() if completed_through is not None else None
        ),
    }


def _china_today() -> date:
    return datetime.now(CHINA_STANDARD_TIME).date()
