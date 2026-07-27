from __future__ import annotations

from datetime import date, datetime, timezone
import json
import math
from typing import Any, Mapping

from ..feature_store.schema import json_fingerprint
from ..model_lab.prediction_schema import validate_prediction_snapshot
from .constraints import PortfolioConstraints
from .cost_model import TransactionCostModel
from .schema import (
    PORTFOLIO_PLAN_ENGINE_VERSION,
    PORTFOLIO_PLAN_SAFETY,
    PORTFOLIO_PLAN_SCHEMA_VERSION,
    finalize_portfolio_plan,
)
from .store import PortfolioPlanStore


def construct_portfolio_plan(
    prediction_snapshot: Mapping[str, Any],
    *,
    equity: float,
    current_weights: Mapping[str, float],
    instrument_metadata: Mapping[str, Mapping[str, Any]],
    market_evidence: Mapping[str, Any],
    cost_model: TransactionCostModel,
    constraints: PortfolioConstraints,
    decision_time: datetime,
    execution_session: date,
    store: PortfolioPlanStore | None = None,
) -> dict[str, Any]:
    """Build a deterministic long-only plan from net alpha and hard limits."""

    prediction = _without_reused(prediction_snapshot)
    validate_prediction_snapshot(prediction)
    if not math.isfinite(equity) or equity <= 0:
        raise ValueError("Portfolio construction equity must be positive")
    if decision_time.tzinfo is None or decision_time.utcoffset() is None:
        raise ValueError("Portfolio decision_time must include a timezone")
    if decision_time > datetime.now(timezone.utc):
        raise ValueError("Portfolio decision_time cannot be in the future")
    knowledge_cutoff = datetime.fromisoformat(
        str(prediction["feature_snapshot"]["knowledge_cutoff"]).replace("Z", "+00:00")
    )
    if decision_time < knowledge_cutoff:
        raise ValueError("Portfolio decision predates its feature knowledge cutoff")
    prediction_created = datetime.fromisoformat(
        str(prediction["created_at"]).replace("Z", "+00:00")
    )
    if decision_time < prediction_created:
        raise ValueError("Portfolio decision predates its prediction snapshot")
    valid_from = date.fromisoformat(str(prediction["valid_from_session"]))
    valid_until = date.fromisoformat(str(prediction["valid_until_session"]))
    if not valid_from <= execution_session <= valid_until:
        raise ValueError("Portfolio execution session is outside prediction validity")
    current = _weights(current_weights)
    metadata = _metadata(instrument_metadata, set(current) | {
        str(item["symbol"]) for item in prediction["rows"]
    })
    evidence = _market_evidence(
        market_evidence,
        metadata,
        expected_session=str(prediction["feature_snapshot"]["as_of_session"]),
    )
    current_violations = _constraint_violations(
        current,
        current,
        metadata,
        equity,
        constraints,
    )
    if current_violations:
        raise ValueError(
            "Current account violates portfolio constraints: "
            + "; ".join(current_violations)
        )

    expected: dict[str, float] = {}
    adjusted: dict[str, float] = {}
    excluded: list[str] = []
    for row in prediction["rows"]:
        symbol = str(row["symbol"])
        if row["rejection_reason"] is not None:
            excluded.append(f"{symbol}:{row['rejection_reason']}")
            continue
        expected[symbol] = float(row["expected_return_bps"])
        adjusted[symbol] = expected[symbol] - (
            constraints.uncertainty_penalty * float(row["uncertainty_bps"])
        )

    positive = {symbol: max(0.0, alpha) for symbol, alpha in adjusted.items()}
    total_score = sum(positive.values())
    desired: dict[str, float] = {}
    clipped: set[str] = set()
    gross_budget = 1.0 - constraints.minimum_cash_weight
    if total_score > 0:
        desired = {
            symbol: gross_budget * score / total_score
            for symbol, score in positive.items()
            if score > 0
        }
        for symbol in list(desired):
            cap = min(
                constraints.max_position_weight,
                _capacity_weight(metadata[symbol], equity, constraints),
            )
            if desired[symbol] > cap:
                desired[symbol] = cap
                clipped.add(symbol)
        _scale_groups(
            desired,
            metadata,
            "asset_class",
            constraints.max_asset_class_weight,
            clipped,
        )
        _scale_groups(
            desired,
            metadata,
            "sector",
            constraints.max_sector_weight,
            clipped,
        )
        volatility = _portfolio_volatility(desired, metadata)
        if constraints.target_annual_volatility > 0 and volatility > constraints.target_annual_volatility:
            scale = constraints.target_annual_volatility / volatility
            desired = {symbol: weight * scale for symbol, weight in desired.items()}
            clipped.update(desired)
        desired_delta = sum(
            abs(desired.get(symbol, 0.0) - current.get(symbol, 0.0))
            for symbol in set(desired) | set(current)
        )
        if desired_delta > constraints.max_turnover > 0:
            scale = constraints.max_turnover / desired_delta
            desired = {
                symbol: current.get(symbol, 0.0)
                + scale * (desired.get(symbol, 0.0) - current.get(symbol, 0.0))
                for symbol in set(desired) | set(current)
            }
            clipped.update(desired)
    else:
        desired = dict(current)

    target = dict(current)
    infeasible: list[str] = []
    changes: list[tuple[float, str, float, float]] = []
    for symbol in sorted(set(desired) | set(current)):
        old = current.get(symbol, 0.0)
        new = max(0.0, desired.get(symbol, 0.0))
        if abs(new - old) <= 1e-12:
            continue
        alpha = adjusted.get(symbol, 0.0)
        improvement = (new - old) * alpha / 10_000.0 * equity
        estimate = cost_model.estimate(
            symbol,
            on_date=execution_session,
            current_weight=old,
            target_weight=new,
            equity=equity,
        )
        hurdle = (
            constraints.minimum_net_alpha_bps / 10_000.0 * estimate.notional
        )
        net_improvement = improvement - estimate.total - hurdle
        changes.append((net_improvement, symbol, old, new))

    # Beneficial reductions free risk and cash before beneficial increases.
    ordered_changes = sorted(
        changes,
        key=lambda item: (item[3] >= item[2], -item[0], item[1]),
    )
    for net_improvement, symbol, old, new in ordered_changes:
        if net_improvement <= 0:
            excluded.append(f"{symbol}:alpha_does_not_cover_cost")
            continue
        candidate = dict(target)
        if new > 1e-12:
            candidate[symbol] = new
        else:
            candidate.pop(symbol, None)
        violations = _constraint_violations(
            candidate,
            current,
            metadata,
            equity,
            constraints,
        )
        if violations:
            infeasible.append(f"{symbol}:" + ";".join(violations))
            continue
        target = candidate

    target = {
        symbol: weight
        for symbol, weight in sorted(target.items())
        if weight > 1e-12
    }
    trades: list[dict[str, Any]] = []
    total_cost = 0.0
    for symbol in sorted(set(current) | set(target)):
        old = current.get(symbol, 0.0)
        new = target.get(symbol, 0.0)
        delta = new - old
        if abs(delta) <= 1e-12:
            continue
        estimate = cost_model.estimate(
            symbol,
            on_date=execution_session,
            current_weight=old,
            target_weight=new,
            equity=equity,
        )
        total_cost += estimate.total
        alpha = adjusted.get(symbol, 0.0)
        trades.append(
            {
                "symbol": symbol,
                "side": estimate.side,
                "current_weight": old,
                "target_weight": new,
                "delta_weight": delta,
                "notional": estimate.notional,
                "estimated_cost": estimate.total,
                "expected_return_bps": expected.get(symbol, 0.0),
                "expected_improvement": delta * alpha / 10_000.0 * equity,
            }
        )
    gross_exposure = sum(target.values())
    gross_alpha_bps = sum(
        weight * expected.get(symbol, 0.0) for symbol, weight in target.items()
    )
    estimated_cost_bps = total_cost / equity * 10_000.0
    turnover = sum(
        abs(target.get(symbol, 0.0) - current.get(symbol, 0.0))
        for symbol in set(target) | set(current)
    )
    account = {
        "equity": float(equity),
        "current_weights": dict(sorted(current.items())),
    }
    account["fingerprint"] = json_fingerprint(account)
    assumptions = cost_model.assumptions(execution_session)
    record = finalize_portfolio_plan(
        {
            "schema_version": PORTFOLIO_PLAN_SCHEMA_VERSION,
            "engine_version": PORTFOLIO_PLAN_ENGINE_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "decision_time": decision_time.isoformat(),
            "execution_session": execution_session.isoformat(),
            "prediction_snapshot": {
                "prediction_snapshot_id": prediction["prediction_snapshot_id"],
                "created_at": prediction["created_at"],
                "snapshot_fingerprint": prediction["snapshot_fingerprint"],
                "record_fingerprint": prediction["record_fingerprint"],
            },
            "account": {
                "fingerprint": account["fingerprint"],
                "equity": account["equity"],
                "current_weights": account["current_weights"],
            },
            "market_evidence": evidence,
            "cost_model": assumptions,
            "constraints": constraints.to_dict(),
            "target_weights": target,
            "trades": trades,
            "metrics": {
                "turnover": turnover,
                "gross_exposure": gross_exposure,
                "cash_weight": 1.0 - gross_exposure,
                "gross_alpha_bps": gross_alpha_bps,
                "estimated_cost_bps": estimated_cost_bps,
                "net_alpha_bps": gross_alpha_bps - estimated_cost_bps,
                "estimated_cost_currency": total_cost,
                "estimated_volatility": _portfolio_volatility(target, metadata),
            },
            "diagnostics": {
                "excluded_symbols": sorted(set(excluded)),
                "clipped_symbols": sorted(clipped),
                "infeasible_reasons": sorted(infeasible),
            },
            "safety": dict(PORTFOLIO_PLAN_SAFETY),
        }
    )
    return store.publish(record) if store is not None else record


def _constraint_violations(
    weights: Mapping[str, float],
    current: Mapping[str, float],
    metadata: Mapping[str, Mapping[str, Any]],
    equity: float,
    constraints: PortfolioConstraints,
) -> list[str]:
    violations = []
    if sum(weights.values()) > 1.0 - constraints.minimum_cash_weight + 1e-10:
        violations.append("minimum_cash")
    if any(weight > constraints.max_position_weight + 1e-10 for weight in weights.values()):
        violations.append("max_position")
    for field, cap, label in (
        ("asset_class", constraints.max_asset_class_weight, "asset_class"),
        ("sector", constraints.max_sector_weight, "sector"),
    ):
        groups: dict[str, float] = {}
        for symbol, weight in weights.items():
            group = str(metadata[symbol][field])
            groups[group] = groups.get(group, 0.0) + weight
        if any(value > cap + 1e-10 for value in groups.values()):
            violations.append(label)
    if any(
        weight > _capacity_weight(metadata[symbol], equity, constraints) + 1e-10
        for symbol, weight in weights.items()
    ):
        violations.append("capacity")
    volatility = _portfolio_volatility(weights, metadata)
    if (
        constraints.target_annual_volatility > 0
        and volatility > constraints.target_annual_volatility + 1e-10
    ):
        violations.append("volatility")
    turnover = sum(
        abs(weights.get(symbol, 0.0) - current.get(symbol, 0.0))
        for symbol in set(weights) | set(current)
    )
    if turnover > constraints.max_turnover + 1e-10:
        violations.append("turnover")
    return violations


def _scale_groups(
    weights: dict[str, float],
    metadata: Mapping[str, Mapping[str, Any]],
    field: str,
    cap: float,
    clipped: set[str],
) -> None:
    groups: dict[str, list[str]] = {}
    for symbol in weights:
        groups.setdefault(str(metadata[symbol][field]), []).append(symbol)
    for symbols in groups.values():
        total = sum(weights[symbol] for symbol in symbols)
        if total <= cap:
            continue
        scale = cap / total
        for symbol in symbols:
            weights[symbol] *= scale
            clipped.add(symbol)


def _portfolio_volatility(
    weights: Mapping[str, float], metadata: Mapping[str, Mapping[str, Any]]
) -> float:
    # Conservative sum is deterministic and cannot hide correlation risk.
    return sum(
        weight * float(metadata[symbol]["annual_volatility"])
        for symbol, weight in weights.items()
    )


def _capacity_weight(
    metadata: Mapping[str, Any],
    equity: float,
    constraints: PortfolioConstraints,
) -> float:
    return min(
        1.0,
        float(metadata["average_amount"])
        * constraints.max_average_amount_participation
        * constraints.capacity_days
        / equity,
    )


def _metadata(
    value: Mapping[str, Mapping[str, Any]], symbols: set[str]
) -> dict[str, dict[str, Any]]:
    result = {}
    for symbol in sorted(symbols):
        item = value.get(symbol)
        if not isinstance(item, Mapping):
            raise ValueError(f"Portfolio metadata is missing for {symbol}")
        if set(item) != {"asset_class", "sector", "average_amount", "annual_volatility"}:
            raise ValueError(f"Portfolio metadata fields are invalid for {symbol}")
        average_amount = item["average_amount"]
        volatility = item["annual_volatility"]
        if (
            isinstance(average_amount, bool)
            or not isinstance(average_amount, (int, float))
            or not math.isfinite(float(average_amount))
            or float(average_amount) < 0
            or isinstance(volatility, bool)
            or not isinstance(volatility, (int, float))
            or not math.isfinite(float(volatility))
            or float(volatility) < 0
        ):
            raise ValueError(f"Portfolio metadata values are invalid for {symbol}")
        result[symbol] = {
            "asset_class": str(item["asset_class"]),
            "sector": str(item["sector"]),
            "average_amount": float(average_amount),
            "annual_volatility": float(volatility),
        }
    return result


def _market_evidence(
    value: Mapping[str, Any],
    metadata: Mapping[str, Mapping[str, Any]],
    *,
    expected_session: str,
) -> dict[str, Any]:
    fields = {
        "as_of_session",
        "cache_manifest_sha256",
        "market_snapshot_fingerprint",
        "instrument_metadata",
        "input_bindings",
        "fingerprint",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("Portfolio market evidence fields are invalid")
    if value.get("as_of_session") != expected_session:
        raise ValueError("Portfolio market evidence session is inconsistent")
    evidence_metadata = value.get("instrument_metadata")
    if evidence_metadata != metadata:
        raise ValueError("Portfolio market evidence metadata is inconsistent")
    bindings = value.get("input_bindings")
    if (
        not isinstance(bindings, Mapping)
        or list(bindings) != sorted(metadata)
        or set(bindings) != set(metadata)
        or any(
            not isinstance(item, str)
            or len(item) != 64
            or any(character not in "0123456789abcdef" for character in item)
            for item in bindings.values()
        )
    ):
        raise ValueError("Portfolio market evidence input bindings are invalid")
    for field in ("cache_manifest_sha256", "market_snapshot_fingerprint"):
        item = value.get(field)
        if (
            not isinstance(item, str)
            or len(item) != 64
            or any(character not in "0123456789abcdef" for character in item)
        ):
            raise ValueError(f"Portfolio market evidence {field} is invalid")
    body = {key: value[key] for key in fields - {"fingerprint"}}
    if value.get("fingerprint") != json_fingerprint(body):
        raise ValueError("Portfolio market evidence fingerprint is inconsistent")
    canonical_body = {
        "as_of_session": value["as_of_session"],
        "cache_manifest_sha256": value["cache_manifest_sha256"],
        "market_snapshot_fingerprint": value["market_snapshot_fingerprint"],
        "instrument_metadata": dict(sorted(metadata.items())),
        "input_bindings": dict(sorted(bindings.items())),
    }
    result = {**canonical_body, "fingerprint": json_fingerprint(canonical_body)}
    return json.loads(json.dumps(result, ensure_ascii=True, allow_nan=False))


def _weights(value: Mapping[str, float]) -> dict[str, float]:
    result = {}
    for symbol, raw_weight in value.items():
        if (
            not isinstance(symbol, str)
            or not symbol
            or isinstance(raw_weight, bool)
            or not isinstance(raw_weight, (int, float))
            or not math.isfinite(float(raw_weight))
            or not 0 <= float(raw_weight) <= 1
        ):
            raise ValueError("Current portfolio weights are invalid")
        if raw_weight > 0:
            result[symbol] = float(raw_weight)
    if sum(result.values()) > 1 + 1e-10:
        raise ValueError("Current portfolio weights exceed one")
    return dict(sorted(result.items()))


def _without_reused(value: Mapping[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))
    if not isinstance(result, dict):
        raise ValueError("Prediction snapshot must be an object")
    result.pop("reused", None)
    return result


__all__ = ["construct_portfolio_plan"]
