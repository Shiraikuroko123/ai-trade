from __future__ import annotations

from datetime import date, datetime
import json
import math
import re
from typing import Any, Mapping

from ..feature_store.schema import FINGERPRINT, SYMBOL, json_fingerprint


PORTFOLIO_PLAN_SCHEMA_VERSION = 1
PORTFOLIO_PLAN_ENGINE_VERSION = 1
PORTFOLIO_PLAN_ID = re.compile(r"pp_[0-9a-f]{32}\Z")
PORTFOLIO_PLAN_SAFETY = {
    "research_only": True,
    "creates_no_order": True,
    "requires_human_approval": True,
    "may_trade": False,
}

_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "engine_version",
        "portfolio_plan_id",
        "created_at",
        "decision_time",
        "execution_session",
        "prediction_snapshot",
        "account",
        "market_evidence",
        "cost_model",
        "constraints",
        "target_weights",
        "trades",
        "metrics",
        "diagnostics",
        "safety",
        "plan_fingerprint",
        "record_fingerprint",
    }
)
_PREDICTION_FIELDS = frozenset(
    {
        "prediction_snapshot_id",
        "created_at",
        "snapshot_fingerprint",
        "record_fingerprint",
    }
)
_ACCOUNT_FIELDS = frozenset({"fingerprint", "equity", "current_weights"})
_MARKET_EVIDENCE_FIELDS = frozenset(
    {
        "as_of_session",
        "cache_manifest_sha256",
        "market_snapshot_fingerprint",
        "instrument_metadata",
        "input_bindings",
        "fingerprint",
    }
)
_INSTRUMENT_METADATA_FIELDS = frozenset(
    {"asset_class", "sector", "average_amount", "annual_volatility"}
)
_COST_FIELDS = frozenset({"fingerprint", "on_date", "schedules"})
_TRADE_FIELDS = frozenset(
    {
        "symbol",
        "side",
        "current_weight",
        "target_weight",
        "delta_weight",
        "notional",
        "estimated_cost",
        "expected_return_bps",
        "expected_improvement",
    }
)
_METRIC_FIELDS = frozenset(
    {
        "turnover",
        "gross_exposure",
        "cash_weight",
        "gross_alpha_bps",
        "estimated_cost_bps",
        "net_alpha_bps",
        "estimated_cost_currency",
        "estimated_volatility",
    }
)
_DIAGNOSTIC_FIELDS = frozenset(
    {"excluded_symbols", "clipped_symbols", "infeasible_reasons"}
)


def finalize_portfolio_plan(draft: Mapping[str, Any]) -> dict[str, Any]:
    record = _clone(draft)
    forbidden = {"portfolio_plan_id", "plan_fingerprint", "record_fingerprint"}
    if not isinstance(record, dict) or forbidden & set(record):
        raise ValueError("Portfolio plan identifiers are assigned by the schema")
    record["portfolio_plan_id"] = None
    record["plan_fingerprint"] = None
    record["record_fingerprint"] = None
    fingerprint = portfolio_plan_fingerprint(record)
    record["portfolio_plan_id"] = "pp_" + fingerprint[:32]
    record["plan_fingerprint"] = fingerprint
    record["record_fingerprint"] = portfolio_plan_record_fingerprint(record)
    validate_portfolio_plan(record)
    return record


def validate_portfolio_plan(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != _TOP_FIELDS:
        raise ValueError("Portfolio plan top-level fields are invalid")
    if value.get("schema_version") != PORTFOLIO_PLAN_SCHEMA_VERSION or value.get("engine_version") != PORTFOLIO_PLAN_ENGINE_VERSION:
        raise ValueError("Portfolio plan version is invalid")
    plan_id = _identifier(value.get("portfolio_plan_id"), PORTFOLIO_PLAN_ID, "plan id")
    created = _timestamp(value.get("created_at"), "created_at")
    decision = _timestamp(value.get("decision_time"), "decision_time")
    if created < decision:
        raise ValueError("Portfolio plan was created before its decision time")
    _iso_date(value.get("execution_session"), "execution_session")
    prediction = _object(value.get("prediction_snapshot"), _PREDICTION_FIELDS, "prediction")
    if re.fullmatch(r"ps_[0-9a-f]{32}", str(prediction.get("prediction_snapshot_id"))) is None:
        raise ValueError("Portfolio plan prediction id is invalid")
    prediction_created = _timestamp(
        prediction.get("created_at"), "prediction created_at"
    )
    if decision < prediction_created:
        raise ValueError("Portfolio decision predates its prediction snapshot")
    _fingerprint(prediction.get("snapshot_fingerprint"), "prediction fingerprint")
    _fingerprint(prediction.get("record_fingerprint"), "prediction record fingerprint")
    account = _object(value.get("account"), _ACCOUNT_FIELDS, "account")
    _fingerprint(account.get("fingerprint"), "account fingerprint")
    equity = _finite(account.get("equity"), "account equity")
    if equity <= 0:
        raise ValueError("Portfolio plan equity must be positive")
    current = _weights(account.get("current_weights"), "current weights")
    if account["fingerprint"] != json_fingerprint({"equity": equity, "current_weights": current}):
        raise ValueError("Portfolio plan account fingerprint is inconsistent")
    market = _object(
        value.get("market_evidence"), _MARKET_EVIDENCE_FIELDS, "market evidence"
    )
    _iso_date(market.get("as_of_session"), "market evidence session")
    _fingerprint(
        market.get("cache_manifest_sha256"), "market evidence cache manifest"
    )
    _fingerprint(
        market.get("market_snapshot_fingerprint"),
        "market evidence snapshot fingerprint",
    )
    instrument_metadata = market.get("instrument_metadata")
    bindings = market.get("input_bindings")
    if (
        not isinstance(instrument_metadata, Mapping)
        or not instrument_metadata
        or list(instrument_metadata) != sorted(instrument_metadata)
        or not isinstance(bindings, Mapping)
        or list(bindings) != sorted(bindings)
        or set(bindings) != set(instrument_metadata)
    ):
        raise ValueError("Portfolio plan market evidence instruments are invalid")
    for symbol, item in instrument_metadata.items():
        _identifier(symbol, SYMBOL, "market evidence symbol")
        metadata = _object(
            item, _INSTRUMENT_METADATA_FIELDS, "instrument metadata"
        )
        for field in ("asset_class", "sector"):
            if not isinstance(metadata.get(field), str) or not metadata[field]:
                raise ValueError("Portfolio plan market metadata group is invalid")
        for field in ("average_amount", "annual_volatility"):
            if _finite(metadata.get(field), f"market metadata {field}") < 0:
                raise ValueError("Portfolio plan market metadata value is invalid")
        _fingerprint(bindings[symbol], "market evidence input binding")
    market_fingerprint = _fingerprint(
        market.get("fingerprint"), "market evidence fingerprint"
    )
    if market_fingerprint != json_fingerprint(
        {key: market[key] for key in _MARKET_EVIDENCE_FIELDS - {"fingerprint"}}
    ):
        raise ValueError("Portfolio plan market evidence fingerprint is inconsistent")
    cost = _object(value.get("cost_model"), _COST_FIELDS, "cost model")
    _fingerprint(cost.get("fingerprint"), "cost fingerprint")
    _iso_date(cost.get("on_date"), "cost date")
    if not isinstance(cost.get("schedules"), Mapping):
        raise ValueError("Portfolio plan cost schedules are invalid")
    if cost["fingerprint"] != json_fingerprint(
        {"on_date": cost["on_date"], "schedules": cost["schedules"]}
    ):
        raise ValueError("Portfolio plan cost fingerprint is inconsistent")
    constraints = value.get("constraints")
    if not isinstance(constraints, Mapping) or not constraints:
        raise ValueError("Portfolio plan constraints are invalid")
    for name, item in constraints.items():
        if not isinstance(name, str) or not _is_finite(item):
            raise ValueError("Portfolio plan constraint value is invalid")
    target = _weights(value.get("target_weights"), "target weights")
    trades = value.get("trades")
    if not isinstance(trades, list):
        raise ValueError("Portfolio plan trades are invalid")
    trade_symbols: list[str] = []
    for item in trades:
        trade = _object(item, _TRADE_FIELDS, "trade")
        symbol = _identifier(trade.get("symbol"), SYMBOL, "trade symbol")
        trade_symbols.append(symbol)
        current_weight = _finite(trade.get("current_weight"), "current_weight")
        target_weight = _finite(trade.get("target_weight"), "target_weight")
        delta = _finite(trade.get("delta_weight"), "delta_weight")
        if abs(delta - (target_weight - current_weight)) > 1e-10:
            raise ValueError("Portfolio plan trade delta is inconsistent")
        expected_side = "BUY" if delta > 0 else "SELL"
        if trade.get("side") != expected_side:
            raise ValueError("Portfolio plan trade side is inconsistent")
        for field in ("notional", "estimated_cost", "expected_return_bps", "expected_improvement"):
            _finite(trade.get(field), f"trade.{field}")
        if abs(target.get(symbol, 0.0) - target_weight) > 1e-10:
            raise ValueError("Portfolio plan trade target is inconsistent")
    if trade_symbols != sorted(trade_symbols) or len(trade_symbols) != len(set(trade_symbols)):
        raise ValueError("Portfolio plan trades are out of order")
    if not (set(current) | set(target) | set(trade_symbols)) <= set(
        instrument_metadata
    ):
        raise ValueError("Portfolio plan market evidence is missing an instrument")
    metrics = _object(value.get("metrics"), _METRIC_FIELDS, "metrics")
    for name in _METRIC_FIELDS:
        _finite(metrics.get(name), f"metrics.{name}")
    if abs(float(metrics["gross_exposure"]) - sum(target.values())) > 1e-9:
        raise ValueError("Portfolio plan gross exposure is inconsistent")
    if abs(float(metrics["cash_weight"]) - (1.0 - sum(target.values()))) > 1e-9:
        raise ValueError("Portfolio plan cash weight is inconsistent")
    diagnostics = _object(value.get("diagnostics"), _DIAGNOSTIC_FIELDS, "diagnostics")
    if any(not isinstance(diagnostics[name], list) for name in _DIAGNOSTIC_FIELDS):
        raise ValueError("Portfolio plan diagnostics are invalid")
    if value.get("safety") != PORTFOLIO_PLAN_SAFETY:
        raise ValueError("Portfolio plan safety boundary is invalid")
    fingerprint = _fingerprint(value.get("plan_fingerprint"), "plan fingerprint")
    if plan_id != "pp_" + fingerprint[:32] or fingerprint != portfolio_plan_fingerprint(value):
        raise ValueError("Portfolio plan fingerprint is inconsistent")
    if value.get("record_fingerprint") != portfolio_plan_record_fingerprint(value):
        raise ValueError("Portfolio plan record fingerprint is inconsistent")


def portfolio_plan_fingerprint(value: Mapping[str, Any]) -> str:
    return json_fingerprint(
        {
            key: value.get(key)
            for key in (
                "schema_version",
                "engine_version",
                "decision_time",
                "execution_session",
                "prediction_snapshot",
                "account",
                "market_evidence",
                "cost_model",
                "constraints",
                "target_weights",
                "trades",
                "metrics",
                "diagnostics",
                "safety",
            )
        }
    )


def portfolio_plan_record_fingerprint(value: Mapping[str, Any]) -> str:
    body = _clone(value)
    body["record_fingerprint"] = None
    body.pop("reused", None)
    return json_fingerprint(body)


def _weights(value: object, label: str) -> dict[str, float]:
    if not isinstance(value, Mapping):
        raise ValueError(f"Portfolio plan {label} is invalid")
    result: dict[str, float] = {}
    for raw_symbol, raw_weight in value.items():
        symbol = _identifier(raw_symbol, SYMBOL, label)
        weight = _finite(raw_weight, label)
        if weight <= 0 or weight > 1:
            raise ValueError(f"Portfolio plan {label} is invalid")
        result[symbol] = weight
    if list(result) != sorted(result) or sum(result.values()) > 1 + 1e-9:
        raise ValueError(f"Portfolio plan {label} is invalid")
    return result


def _object(value: Any, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"Portfolio plan {label} fields are invalid")
    return value


def _identifier(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"Portfolio plan {label} is invalid")
    return value


def _fingerprint(value: object, label: str) -> str:
    return _identifier(value, FINGERPRINT, label)


def _finite(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"Portfolio plan {label} is invalid")
    return float(value)


def _is_finite(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _iso_date(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"Portfolio plan {label} is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Portfolio plan {label} is invalid") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"Portfolio plan {label} is invalid")
    return parsed


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Portfolio plan {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Portfolio plan {label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Portfolio plan {label} must include a timezone")
    return parsed


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


__all__ = [
    "PORTFOLIO_PLAN_ENGINE_VERSION",
    "PORTFOLIO_PLAN_SCHEMA_VERSION",
    "PORTFOLIO_PLAN_SAFETY",
    "finalize_portfolio_plan",
    "portfolio_plan_fingerprint",
    "portfolio_plan_record_fingerprint",
    "validate_portfolio_plan",
]
