from __future__ import annotations

import csv
import hashlib
import io
import math
import os
import re
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterable

from .ledger import ledger_lock


CANONICAL_COLUMNS = (
    "fill_id",
    "order_id",
    "symbol",
    "side",
    "quantity",
    "price",
    "commission",
    "tax",
    "filled_at",
)
SHADOW_FILL_COLUMNS = (
    "shadow_fill_id",
    "owner_id",
    "source_label",
    "account_alias",
    "source_fill_id",
    "broker_order_id",
    "symbol",
    "side",
    "quantity",
    "price",
    "commission",
    "tax",
    "filled_at",
    "source_sha256",
    "import_id",
    "imported_at",
    "record_sha256",
)
SHADOW_IMPORT_COLUMNS = (
    "import_id",
    "owner_id",
    "source_label",
    "account_alias",
    "source_sha256",
    "row_count",
    "accepted_count",
    "duplicate_count",
    "imported_at",
    "record_sha256",
)
DEFAULT_MAX_IMPORT_BYTES = 1_000_000
MAX_IMPORT_ROWS = 5_000
_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SOURCE_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,63}\Z")
_SYMBOL_PATTERN = re.compile(r"[0-9]{6}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


class ShadowAccountConflictError(RuntimeError):
    """Raised when an immutable imported fill would change meaning."""


def import_shadow_csv(
    fills_path: Path,
    imports_path: Path,
    *,
    owner_id: str,
    source_label: str,
    account_alias: str,
    content: bytes,
    max_bytes: int = DEFAULT_MAX_IMPORT_BYTES,
    imported_at: datetime | None = None,
) -> dict[str, Any]:
    """Validate and append one canonical broker-export CSV without retaining it."""
    owner_id = _bounded_identity(owner_id, "owner_id", 128)
    source_label = _source_label(source_label)
    account_alias = _bounded_identity(account_alias, "account_alias", 64)
    if fills_path.resolve() == imports_path.resolve():
        raise ValueError("Shadow fill and import ledgers must use different paths")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError("max_bytes must be a positive integer")
    if not isinstance(content, bytes) or not content:
        raise ValueError("Shadow import must contain a non-empty CSV file")
    if len(content) > max_bytes:
        raise ValueError(f"Shadow import exceeds the {max_bytes}-byte limit")

    normalized = _parse_canonical_csv(content)
    source_sha256 = hashlib.sha256(content).hexdigest()
    import_id = _import_id(owner_id, source_label, account_alias, source_sha256)
    timestamp = imported_at or datetime.now(timezone.utc)
    if timestamp.tzinfo is None:
        raise ValueError("imported_at must include a timezone")
    imported_text = timestamp.astimezone(timezone.utc).isoformat(timespec="seconds")
    earliest_fill = datetime(1990, 1, 1, tzinfo=timezone.utc)
    latest_fill = timestamp.astimezone(timezone.utc) + timedelta(days=1)
    for value in normalized:
        filled_at = datetime.fromisoformat(value["filled_at"]).astimezone(timezone.utc)
        if not earliest_fill <= filled_at <= latest_fill:
            raise ValueError(
                "Shadow CSV filled_at must be between 1990-01-01 and one day "
                "after the import time"
            )

    fills_path.parent.mkdir(parents=True, exist_ok=True)
    imports_path.parent.mkdir(parents=True, exist_ok=True)
    with ledger_lock(imports_path):
        existing_fills = _read_csv(fills_path, SHADOW_FILL_COLUMNS)
        existing_imports = _read_csv(imports_path, SHADOW_IMPORT_COLUMNS)
        _validate_fill_ledger(existing_fills)
        _validate_import_ledger(existing_imports)
        _validate_import_links(
            existing_fills,
            existing_imports,
            recoverable_import_id=import_id,
        )

        prior_import = next(
            (row for row in existing_imports if row["import_id"] == import_id),
            None,
        )
        if prior_import is not None:
            return _import_result(prior_import, already_imported=True)

        indexed = {row["shadow_fill_id"]: row for row in existing_fills}
        pending: list[dict[str, str]] = []
        duplicate_count = 0
        recovered_count = 0
        for value in normalized:
            shadow_fill_id = _shadow_fill_id(
                owner_id, source_label, account_alias, value["source_fill_id"]
            )
            row = {
                "shadow_fill_id": shadow_fill_id,
                "owner_id": owner_id,
                "source_label": source_label,
                "account_alias": account_alias,
                **value,
                "source_sha256": source_sha256,
                "import_id": import_id,
                "imported_at": imported_text,
            }
            row["record_sha256"] = _record_sha256(row, SHADOW_FILL_COLUMNS)
            prior = indexed.get(shadow_fill_id)
            if prior is None:
                pending.append(row)
                indexed[shadow_fill_id] = row
                continue
            if _fill_identity(prior) != _fill_identity(row):
                raise ShadowAccountConflictError(
                    "A previously imported fill_id has different immutable values: "
                    + value["source_fill_id"]
                )
            if prior["import_id"] == import_id:
                recovered_count += 1
            else:
                duplicate_count += 1

        if pending:
            _append_csv(fills_path, SHADOW_FILL_COLUMNS, pending)
        accepted_count = len(pending) + recovered_count
        import_row = {
            "import_id": import_id,
            "owner_id": owner_id,
            "source_label": source_label,
            "account_alias": account_alias,
            "source_sha256": source_sha256,
            "row_count": str(len(normalized)),
            "accepted_count": str(accepted_count),
            "duplicate_count": str(duplicate_count),
            "imported_at": imported_text,
        }
        import_row["record_sha256"] = _record_sha256(
            import_row, SHADOW_IMPORT_COLUMNS
        )
        _append_csv(imports_path, SHADOW_IMPORT_COLUMNS, [import_row])
    return _import_result(import_row, already_imported=False)


def shadow_account_status(
    fills_path: Path,
    imports_path: Path,
    *,
    owner_id: str,
    expected_trades: Iterable[dict[str, object]] = (),
) -> dict[str, Any]:
    owner_id = _bounded_identity(owner_id, "owner_id", 128)
    errors: list[str] = []
    try:
        all_fills = _read_csv(fills_path, SHADOW_FILL_COLUMNS)
        _validate_fill_ledger(all_fills)
    except (OSError, RuntimeError, ValueError) as exc:
        all_fills = []
        errors.append(f"shadow fill ledger: {exc}")
    try:
        all_imports = _read_csv(imports_path, SHADOW_IMPORT_COLUMNS)
        _validate_import_ledger(all_imports)
    except (OSError, RuntimeError, ValueError) as exc:
        all_imports = []
        errors.append(f"shadow import ledger: {exc}")

    fills = [row for row in all_fills if row["owner_id"] == owner_id]
    imports = [row for row in all_imports if row["owner_id"] == owner_id]
    import_ids = {row["import_id"] for row in imports}
    orphaned = sorted(
        row["shadow_fill_id"] for row in fills if row["import_id"] not in import_ids
    )
    if orphaned:
        errors.append(
            f"{len(orphaned)} shadow fills are not linked to an import record"
        )
    for row in imports:
        linked = sum(1 for fill in fills if fill["import_id"] == row["import_id"])
        if linked != int(row["accepted_count"]):
            errors.append(
                "shadow import accepted_count does not match linked fills: "
                + row["import_id"]
            )

    parsed_expected: list[dict[str, object]] = []
    for index, row in enumerate(expected_trades, start=1):
        try:
            parsed_expected.append(_expected_trade(row))
        except (KeyError, TypeError, ValueError) as exc:
            errors.append(f"paper trade row {index}: {exc}")

    aliases = sorted(
        {row["account_alias"] for row in fills},
        key=lambda alias: max(
            row["imported_at"]
            for row in imports
            if row["account_alias"] == alias
        )
        if any(row["account_alias"] == alias for row in imports)
        else "",
        reverse=True,
    )
    reviews = [
        _review_account(
            alias,
            [row for row in fills if row["account_alias"] == alias],
            parsed_expected,
        )
        for alias in aliases
    ]
    latest_review = reviews[0] if reviews else _empty_review()
    recent_fills = sorted(fills, key=lambda row: row["filled_at"], reverse=True)[:100]
    recent_imports = sorted(
        imports, key=lambda row: row["imported_at"], reverse=True
    )[:20]
    return {
        "status": "INTEGRITY_ERROR"
        if errors
        else latest_review["verdict"],
        "fill_count": len(fills),
        "import_count": len(imports),
        "account_count": len(aliases),
        "latest_fill_at": recent_fills[0]["filled_at"] if recent_fills else None,
        "integrity_errors": errors,
        "review": latest_review,
        "account_reviews": reviews,
        "recent_fills": [_public_fill(row) for row in recent_fills],
        "imports": [_public_import(row) for row in recent_imports],
        "canonical_columns": list(CANONICAL_COLUMNS),
        "max_rows_per_import": MAX_IMPORT_ROWS,
        "qualifying_evidence": False,
        "execution_enabled": False,
        "source_files_retained": False,
        "disclosure": (
            "Shadow review compares imported fills with the local paper ledger. "
            "It cannot authorize, submit, cancel, or reconcile broker orders."
        ),
    }


def canonical_template() -> str:
    return ",".join(CANONICAL_COLUMNS) + "\r\n"


def _parse_canonical_csv(content: bytes) -> list[dict[str, str]]:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("Shadow CSV must be UTF-8 or UTF-8 with BOM") from exc
    if "\x00" in text:
        raise ValueError("Shadow CSV contains a NUL character")
    try:
        reader = csv.DictReader(io.StringIO(text, newline=""), strict=True)
        if reader.fieldnames != list(CANONICAL_COLUMNS):
            raise ValueError(
                "Shadow CSV header must exactly match: " + ",".join(CANONICAL_COLUMNS)
            )
        rows = list(reader)
    except csv.Error as exc:
        raise ValueError(f"Shadow CSV is malformed: {exc}") from exc
    if not rows:
        raise ValueError("Shadow CSV must contain at least one fill")
    if len(rows) > MAX_IMPORT_ROWS:
        raise ValueError(f"Shadow CSV exceeds the {MAX_IMPORT_ROWS}-row limit")

    normalized: list[dict[str, str]] = []
    seen: set[str] = set()
    for row_number, row in enumerate(rows, start=2):
        if None in row or any(value is None for value in row.values()):
            raise ValueError(f"Shadow CSV row {row_number} has the wrong column count")
        source_fill_id = _identifier(row["fill_id"], "fill_id", row_number)
        if source_fill_id in seen:
            raise ValueError(
                f"Shadow CSV row {row_number} repeats fill_id={source_fill_id}"
            )
        seen.add(source_fill_id)
        order_id = _identifier(row["order_id"], "order_id", row_number)
        symbol = _trimmed(row["symbol"], "symbol", row_number)
        if not _SYMBOL_PATTERN.fullmatch(symbol):
            raise ValueError(f"Shadow CSV row {row_number} has an invalid symbol")
        side = _trimmed(row["side"], "side", row_number)
        if side not in {"BUY", "SELL"}:
            raise ValueError(
                f"Shadow CSV row {row_number} side must be BUY or SELL"
            )
        quantity_text = _trimmed(row["quantity"], "quantity", row_number)
        if not quantity_text.isascii() or not quantity_text.isdigit():
            raise ValueError(
                f"Shadow CSV row {row_number} quantity must be a positive integer"
            )
        quantity = int(quantity_text)
        if quantity < 1 or quantity > 1_000_000_000:
            raise ValueError(
                f"Shadow CSV row {row_number} quantity is outside the allowed range"
            )
        price = _decimal(row["price"], "price", row_number, positive=True)
        commission = _decimal(
            row["commission"], "commission", row_number, positive=False
        )
        tax = _decimal(row["tax"], "tax", row_number, positive=False)
        notional = Decimal(quantity) * price
        if price > Decimal("1000000") or commission > notional or tax > notional:
            raise ValueError(
                f"Shadow CSV row {row_number} contains an implausible price or fee"
            )
        filled_at = _timestamp(row["filled_at"], row_number)
        normalized.append(
            {
                "source_fill_id": source_fill_id,
                "broker_order_id": order_id,
                "symbol": symbol,
                "side": side,
                "quantity": str(quantity),
                "price": _decimal_text(price),
                "commission": _decimal_text(commission),
                "tax": _decimal_text(tax),
                "filled_at": filled_at,
            }
        )
    return normalized


def _validate_fill_ledger(rows: list[dict[str, str]]) -> None:
    seen: dict[str, tuple[str, ...]] = {}
    for index, row in enumerate(rows, start=2):
        try:
            owner_id = _bounded_identity(row["owner_id"], "owner_id", 128)
            source_label = _source_label(row["source_label"])
            account_alias = _bounded_identity(
                row["account_alias"], "account_alias", 64
            )
            source_fill_id = _identifier(
                row["source_fill_id"], "source_fill_id", index
            )
            _identifier(row["broker_order_id"], "broker_order_id", index)
            if not _SYMBOL_PATTERN.fullmatch(row["symbol"]):
                raise ValueError("invalid symbol")
            if row["side"] not in {"BUY", "SELL"}:
                raise ValueError("invalid side")
            quantity = int(row["quantity"])
            if str(quantity) != row["quantity"] or quantity < 1:
                raise ValueError("invalid quantity")
            for field in ("price", "commission", "tax"):
                value = Decimal(row[field])
                if not value.is_finite() or value < 0 or _decimal_text(value) != row[field]:
                    raise ValueError(f"invalid {field}")
            if Decimal(row["price"]) <= 0:
                raise ValueError("invalid price")
            _ledger_timestamp(row["filled_at"])
            _ledger_timestamp(row["imported_at"])
            if not _SHA256_PATTERN.fullmatch(row["source_sha256"]):
                raise ValueError("invalid source_sha256")
            expected_import_id = _import_id(
                owner_id, source_label, account_alias, row["source_sha256"]
            )
            if row["import_id"] != expected_import_id:
                raise ValueError("import_id does not match fill provenance")
            expected_fill_id = _shadow_fill_id(
                owner_id, source_label, account_alias, source_fill_id
            )
            if row["shadow_fill_id"] != expected_fill_id:
                raise ValueError("shadow_fill_id does not match fill identity")
            if row["record_sha256"] != _record_sha256(row, SHADOW_FILL_COLUMNS):
                raise ValueError("record_sha256 does not match fill content")
        except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid shadow fill ledger row {index}: {exc}") from exc
        identity = _fill_identity(row)
        prior = seen.get(row["shadow_fill_id"])
        if prior is not None:
            if prior != identity:
                raise RuntimeError(
                    "Conflicting shadow fill ledger rows for " + row["shadow_fill_id"]
                )
            raise RuntimeError(
                "Duplicate shadow fill ledger row for " + row["shadow_fill_id"]
            )
        seen[row["shadow_fill_id"]] = identity


def _validate_import_ledger(rows: list[dict[str, str]]) -> None:
    seen: set[str] = set()
    for index, row in enumerate(rows, start=2):
        try:
            owner_id = _bounded_identity(row["owner_id"], "owner_id", 128)
            source_label = _source_label(row["source_label"])
            account_alias = _bounded_identity(
                row["account_alias"], "account_alias", 64
            )
            if not _SHA256_PATTERN.fullmatch(row["source_sha256"]):
                raise ValueError("invalid source_sha256")
            expected = _import_id(
                owner_id, source_label, account_alias, row["source_sha256"]
            )
            if row["import_id"] != expected:
                raise ValueError("import_id does not match import identity")
            row_count = int(row["row_count"])
            accepted = int(row["accepted_count"])
            duplicates = int(row["duplicate_count"])
            if (
                row_count < 1
                or accepted < 0
                or duplicates < 0
                or accepted + duplicates != row_count
            ):
                raise ValueError("invalid import counts")
            _ledger_timestamp(row["imported_at"])
            if row["record_sha256"] != _record_sha256(
                row, SHADOW_IMPORT_COLUMNS
            ):
                raise ValueError("record_sha256 does not match import content")
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"Invalid shadow import ledger row {index}: {exc}") from exc
        if row["import_id"] in seen:
            raise RuntimeError("Duplicate shadow import ledger row: " + row["import_id"])
        seen.add(row["import_id"])


def _validate_import_links(
    fills: list[dict[str, str]],
    imports: list[dict[str, str]],
    *,
    recoverable_import_id: str | None = None,
) -> None:
    known = {row["import_id"]: row for row in imports}
    linked_counts: dict[str, int] = defaultdict(int)
    for row in fills:
        linked_counts[row["import_id"]] += 1
        if (
            row["import_id"] not in known
            and row["import_id"] != recoverable_import_id
        ):
            raise RuntimeError(
                "Shadow fill is not linked to an import record: "
                + row["shadow_fill_id"]
            )
    for import_id, row in known.items():
        if linked_counts[import_id] != int(row["accepted_count"]):
            raise RuntimeError(
                "Shadow import accepted_count does not match linked fills: "
                + import_id
            )


def _review_account(
    account_alias: str,
    fills: list[dict[str, str]],
    expected_trades: list[dict[str, object]],
) -> dict[str, Any]:
    actual_groups: dict[tuple[str, str, str], dict[str, Decimal]] = defaultdict(
        lambda: {"quantity": Decimal(0), "notional": Decimal(0)}
    )
    for row in fills:
        key = (row["filled_at"][:10], row["symbol"], row["side"])
        quantity = Decimal(row["quantity"])
        actual_groups[key]["quantity"] += quantity
        actual_groups[key]["notional"] += quantity * Decimal(row["price"])

    if not actual_groups:
        return {**_empty_review(), "account_alias": account_alias}
    start = min(key[0] for key in actual_groups)
    end = max(key[0] for key in actual_groups)
    expected_groups: dict[tuple[str, str, str], dict[str, Decimal]] = defaultdict(
        lambda: {"quantity": Decimal(0), "notional": Decimal(0)}
    )
    for row in expected_trades:
        on_date = str(row["date"])
        if start <= on_date <= end:
            key = (on_date, str(row["symbol"]), str(row["side"]))
            quantity = Decimal(int(row["quantity"]))
            expected_groups[key]["quantity"] += quantity
            expected_groups[key]["notional"] += quantity * Decimal(str(row["price"]))

    actual_keys = set(actual_groups)
    expected_keys = set(expected_groups)
    matched = sorted(actual_keys & expected_keys)
    unexpected = sorted(actual_keys - expected_keys)
    missed = sorted(expected_keys - actual_keys)
    opposite_actual = {
        (date_text, symbol, "SELL" if side == "BUY" else "BUY")
        for date_text, symbol, side in expected_keys
    }
    direction_mismatches = sorted(actual_keys & opposite_actual)
    quantity_deviations = 0
    slippage_weight = Decimal(0)
    slippage_total = Decimal(0)
    group_rows: list[dict[str, Any]] = []
    for key in sorted(actual_keys | expected_keys, reverse=True):
        actual = actual_groups.get(key)
        expected = expected_groups.get(key)
        actual_quantity = actual["quantity"] if actual else Decimal(0)
        expected_quantity = expected["quantity"] if expected else Decimal(0)
        actual_price = (
            actual["notional"] / actual_quantity if actual_quantity else None
        )
        expected_price = (
            expected["notional"] / expected_quantity if expected_quantity else None
        )
        adverse_bps: Decimal | None = None
        quantity_deviation: Decimal | None = None
        if actual and expected and expected_quantity:
            quantity_deviation = (
                actual_quantity - expected_quantity
            ) / expected_quantity
            if abs(quantity_deviation) > Decimal("0.05"):
                quantity_deviations += 1
            sign = Decimal(1) if key[2] == "BUY" else Decimal(-1)
            adverse_bps = (
                (actual_price - expected_price) / expected_price * Decimal(10_000) * sign
            )
            slippage_total += adverse_bps * actual["notional"]
            slippage_weight += actual["notional"]
        outcome = (
            "MATCHED"
            if actual and expected
            else "UNEXPECTED"
            if actual
            else "MISSED"
        )
        group_rows.append(
            {
                "date": key[0],
                "symbol": key[1],
                "side": key[2],
                "actual_quantity": int(actual_quantity),
                "expected_quantity": int(expected_quantity),
                "actual_price": _float_or_none(actual_price),
                "expected_price": _float_or_none(expected_price),
                "quantity_deviation": _float_or_none(quantity_deviation),
                "adverse_price_bps": _float_or_none(adverse_bps),
                "outcome": outcome,
            }
        )

    actual_by_symbol = _notional_by_symbol(actual_groups)
    expected_by_symbol = _notional_by_symbol(expected_groups)
    actual_total = sum(actual_by_symbol.values(), Decimal(0))
    expected_total = sum(expected_by_symbol.values(), Decimal(0))
    allocation_rows = []
    allocation_distance: Decimal | None = (
        Decimal(0) if actual_total and expected_total else None
    )
    for symbol in sorted(set(actual_by_symbol) | set(expected_by_symbol)):
        actual_weight = actual_by_symbol.get(symbol, Decimal(0)) / actual_total if actual_total else Decimal(0)
        expected_weight = expected_by_symbol.get(symbol, Decimal(0)) / expected_total if expected_total else Decimal(0)
        difference = actual_weight - expected_weight
        if allocation_distance is not None:
            allocation_distance += abs(difference)
        allocation_rows.append(
            {
                "symbol": symbol,
                "actual_weight": float(actual_weight),
                "expected_weight": float(expected_weight),
                "difference": float(difference),
            }
        )
    if allocation_distance is not None:
        allocation_distance /= Decimal(2)
    weighted_adverse_bps = (
        slippage_total / slippage_weight if slippage_weight else None
    )
    reasons = []
    if not expected_groups:
        reasons.append("paper_comparison_unavailable")
    else:
        if direction_mismatches:
            reasons.append("direction_mismatch")
        if unexpected:
            reasons.append("unexpected_fill")
        if missed:
            reasons.append("missed_model_fill")
        if quantity_deviations:
            reasons.append("quantity_deviation_above_5pct")
        if weighted_adverse_bps is not None and weighted_adverse_bps > Decimal(25):
            reasons.append("adverse_price_deviation_above_25bps")
        if allocation_distance is not None and allocation_distance > Decimal("0.10"):
            reasons.append("trade_allocation_deviation_above_10pct")
    verdict = (
        "INSUFFICIENT_DATA"
        if not expected_groups or not matched
        else "REVIEW_REQUIRED"
        if reasons
        else "CONSISTENT_WITH_MODEL"
    )
    return {
        "account_alias": account_alias,
        "verdict": verdict,
        "period": [start, end],
        "fill_count": len(fills),
        "actual_groups": len(actual_groups),
        "expected_groups": len(expected_groups),
        "matched_groups": len(matched),
        "unexpected_groups": len(unexpected),
        "missed_groups": len(missed),
        "direction_mismatch_groups": len(direction_mismatches),
        "quantity_deviation_groups": quantity_deviations,
        "match_rate": len(matched) / len(expected_groups) if expected_groups else None,
        "weighted_adverse_price_bps": _float_or_none(weighted_adverse_bps),
        "trade_allocation_deviation": _float_or_none(allocation_distance),
        "review_reasons": reasons,
        "allocation": allocation_rows,
        "groups": group_rows[:100],
        "comparison_basis": "local_paper_fills_in_imported_date_window",
    }


def _empty_review() -> dict[str, Any]:
    return {
        "account_alias": None,
        "verdict": "INSUFFICIENT_DATA",
        "period": [None, None],
        "fill_count": 0,
        "actual_groups": 0,
        "expected_groups": 0,
        "matched_groups": 0,
        "unexpected_groups": 0,
        "missed_groups": 0,
        "direction_mismatch_groups": 0,
        "quantity_deviation_groups": 0,
        "match_rate": None,
        "weighted_adverse_price_bps": None,
        "trade_allocation_deviation": None,
        "review_reasons": [],
        "allocation": [],
        "groups": [],
        "comparison_basis": "local_paper_fills_in_imported_date_window",
    }


def _expected_trade(row: dict[str, object]) -> dict[str, object]:
    on_date = str(row["date"])
    datetime.fromisoformat(on_date)
    symbol = str(row["symbol"])
    side = str(row["side"])
    quantity = int(str(row["quantity"]))
    price = Decimal(str(row["price"]))
    if (
        not _SYMBOL_PATTERN.fullmatch(symbol)
        or side not in {"BUY", "SELL"}
        or quantity < 1
        or not price.is_finite()
        or price <= 0
    ):
        raise ValueError("invalid expected trade values")
    return {
        "date": on_date,
        "symbol": symbol,
        "side": side,
        "quantity": quantity,
        "price": price,
    }


def _notional_by_symbol(
    groups: dict[tuple[str, str, str], dict[str, Decimal]],
) -> dict[str, Decimal]:
    result: dict[str, Decimal] = defaultdict(Decimal)
    for (_, symbol, _), value in groups.items():
        result[symbol] += value["notional"]
    return result


def _public_fill(row: dict[str, str]) -> dict[str, object]:
    return {
        "shadow_fill_id": row["shadow_fill_id"],
        "source_label": row["source_label"],
        "account_alias": row["account_alias"],
        "source_fill_id": row["source_fill_id"],
        "broker_order_id": row["broker_order_id"],
        "symbol": row["symbol"],
        "side": row["side"],
        "quantity": int(row["quantity"]),
        "price": float(row["price"]),
        "commission": float(row["commission"]),
        "tax": float(row["tax"]),
        "filled_at": row["filled_at"],
    }


def _public_import(row: dict[str, str]) -> dict[str, object]:
    return {
        "import_id": row["import_id"],
        "source_label": row["source_label"],
        "account_alias": row["account_alias"],
        "source_sha256": row["source_sha256"],
        "row_count": int(row["row_count"]),
        "accepted_count": int(row["accepted_count"]),
        "duplicate_count": int(row["duplicate_count"]),
        "imported_at": row["imported_at"],
    }


def _import_result(row: dict[str, str], *, already_imported: bool) -> dict[str, Any]:
    return {**_public_import(row), "already_imported": already_imported}


def _fill_identity(row: dict[str, str]) -> tuple[str, ...]:
    return tuple(
        row[name]
        for name in (
            "owner_id",
            "source_label",
            "account_alias",
            "source_fill_id",
            "broker_order_id",
            "symbol",
            "side",
            "quantity",
            "price",
            "commission",
            "tax",
            "filled_at",
        )
    )


def _read_csv(path: Path, columns: tuple[str, ...]) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, strict=True)
        if reader.fieldnames != list(columns):
            raise RuntimeError(f"Invalid ledger schema: {path}")
        try:
            rows = list(reader)
        except csv.Error as exc:
            raise RuntimeError(f"Malformed ledger: {path}: {exc}") from exc
    if any(None in row or any(value is None for value in row.values()) for row in rows):
        raise RuntimeError(f"Invalid ledger row width: {path}")
    return rows


def _append_csv(
    path: Path, columns: tuple[str, ...], rows: list[dict[str, str]]
) -> None:
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="raise")
        if not exists:
            writer.writeheader()
        writer.writerows(rows)
        handle.flush()
        os.fsync(handle.fileno())


def _record_sha256(row: dict[str, str], columns: tuple[str, ...]) -> str:
    raw = "|".join(row[name] for name in columns if name != "record_sha256")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _shadow_fill_id(
    owner_id: str, source_label: str, account_alias: str, source_fill_id: str
) -> str:
    raw = "|".join((owner_id, source_label, account_alias, source_fill_id))
    return "shadow_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _import_id(
    owner_id: str, source_label: str, account_alias: str, source_sha256: str
) -> str:
    raw = "|".join((owner_id, source_label, account_alias, source_sha256))
    return "import_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _bounded_identity(value: object, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
        or value[0] in "=+-@"
    ):
        raise ValueError(f"{field} must be 1-{maximum} trimmed printable characters")
    return value


def _source_label(value: object) -> str:
    value = _bounded_identity(value, "source_label", 64)
    if not _SOURCE_PATTERN.fullmatch(value):
        raise ValueError("source_label contains unsupported characters")
    return value


def _identifier(value: object, field: str, row_number: int) -> str:
    value = _trimmed(value, field, row_number)
    if not _IDENTIFIER_PATTERN.fullmatch(value):
        raise ValueError(f"Shadow CSV row {row_number} has an invalid {field}")
    return value


def _trimmed(value: object, field: str, row_number: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(
            f"Shadow CSV row {row_number} {field} must be non-empty and trimmed"
        )
    return value


def _decimal(
    value: object, field: str, row_number: int, *, positive: bool
) -> Decimal:
    text = _trimmed(value, field, row_number)
    try:
        result = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(
            f"Shadow CSV row {row_number} {field} must be a decimal"
        ) from exc
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        condition = "positive" if positive else "non-negative"
        raise ValueError(
            f"Shadow CSV row {row_number} {field} must be finite and {condition}"
        )
    if result.as_tuple().exponent < -8:
        raise ValueError(
            f"Shadow CSV row {row_number} {field} supports at most 8 decimals"
        )
    return result


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text or "0"


def _timestamp(value: object, row_number: int) -> str:
    text = _trimmed(value, "filled_at", row_number)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(
            f"Shadow CSV row {row_number} filled_at must be ISO-8601"
        ) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(
            f"Shadow CSV row {row_number} filled_at must include a timezone"
        )
    return parsed.isoformat(timespec="microseconds" if parsed.microsecond else "seconds")


def _ledger_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    if parsed.isoformat(timespec="microseconds" if parsed.microsecond else "seconds") != value:
        raise ValueError("timestamp is not normalized")
    return parsed


def _float_or_none(value: Decimal | None) -> float | None:
    if value is None:
        return None
    result = float(value)
    if not math.isfinite(result):
        return None
    return round(result, 6)
