from __future__ import annotations

from decimal import Decimal
from typing import Any, Mapping

from .shadow_projection import validate_shadow_projection
from .shadow_schema import canonical_payload, decimal_text, json_fingerprint


RECONCILIATION_SAFETY = {
    "shadow_only": True,
    "qualifying_broker_sandbox_evidence": False,
    "execution_enabled": False,
}
MAX_CASH_TOLERANCE = Decimal("0.01")


def reconcile_shadow_projection(
    projection: Mapping[str, Any],
    *,
    broker_cash: object,
    broker_positions: Mapping[str, object],
    cash_tolerance: object = "0.01",
) -> dict[str, Any]:
    validate_shadow_projection(projection)
    expected_cash = Decimal(str(projection.get("cash")))
    broker_snapshot = canonical_payload(
        "account_snapshot",
        {"cash": broker_cash, "positions": broker_positions},
    )
    actual_cash = Decimal(str(broker_snapshot["cash"]))
    tolerance = Decimal(decimal_text(cash_tolerance, nonnegative=True))
    if tolerance > MAX_CASH_TOLERANCE:
        raise ValueError("Shadow cash tolerance cannot exceed 0.01 CNY")
    expected_positions = {
        str(symbol): Decimal(str(item["quantity"]))
        for symbol, item in dict(projection.get("positions") or {}).items()
    }
    actual_positions = {
        str(symbol): Decimal(str(quantity))
        for symbol, quantity in dict(broker_snapshot["positions"]).items()
    }
    issues = []
    if abs(expected_cash - actual_cash) > tolerance:
        issues.append(
            {
                "kind": "cash",
                "key": "CNY",
                "expected": decimal_text(expected_cash),
                "actual": decimal_text(actual_cash),
                "difference": decimal_text(actual_cash - expected_cash),
            }
        )
    for symbol in sorted(set(expected_positions) | set(actual_positions)):
        expected = expected_positions.get(symbol, Decimal("0"))
        actual = actual_positions.get(symbol, Decimal("0"))
        if expected != actual:
            issues.append(
                {
                    "kind": "position",
                    "key": symbol,
                    "expected": decimal_text(expected),
                    "actual": decimal_text(actual),
                    "difference": decimal_text(actual - expected),
                }
            )
    result = {
        "schema_version": 1,
        "ledger_id": projection.get("ledger_id"),
        "projection_fingerprint": projection.get("projection_fingerprint"),
        "broker": {
            "cash": decimal_text(actual_cash),
            "positions": {
                symbol: decimal_text(quantity)
                for symbol, quantity in sorted(actual_positions.items())
            },
        },
        "cash_tolerance": decimal_text(tolerance),
        "issues": issues,
        "clean": not issues,
        "safety": dict(RECONCILIATION_SAFETY),
    }
    result["reconciliation_fingerprint"] = json_fingerprint(result)
    return result


__all__ = [
    "MAX_CASH_TOLERANCE",
    "RECONCILIATION_SAFETY",
    "reconcile_shadow_projection",
]
