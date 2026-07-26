from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import math
import re
from typing import Any, Mapping


SCHEMA_VERSION = 1
ENGINE_VERSION = 1

EVALUATION_ID = re.compile(r"mdl_[0-9a-f]{32}\Z")
FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_EVIDENCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")
_FACTOR_ID = re.compile(r"[a-z0-9][a-z0-9_]{2,60}\Z")
_MODEL_ID = re.compile(r"[a-z0-9][a-z0-9_]{2,60}\Z")

SAFETY = {
    "research_only": True,
    "creates_no_signal": True,
    "may_create_candidate": False,
    "may_approve": False,
    "may_activate": False,
    "may_trade": False,
}

TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "engine_version",
        "evaluation_id",
        "owner",
        "created_at",
        "model",
        "factors",
        "parameters",
        "protocol",
        "evidence",
        "coverage",
        "results",
        "coefficients",
        "evaluation_fingerprint",
        "safety",
        "record_fingerprint",
    }
)

_MODEL_FIELDS = frozenset(
    {"model_id", "version", "label", "kind", "formula", "hyperparameters"}
)
_FACTOR_FIELDS = frozenset({"factor_id", "version", "direction"})
_PARAMETER_FIELDS = frozenset(
    {
        "horizon",
        "step",
        "start",
        "end",
        "minimum_cross_section",
        "minimum_train_dates",
        "minimum_train_observations",
    }
)
_PROTOCOL_FIELDS = frozenset(
    {"target", "standardization", "training", "leakage_guard"}
)
_EVIDENCE_FIELDS = frozenset(
    {"snapshot", "universe", "config_context_fingerprint"}
)
_SNAPSHOT_FIELDS = frozenset(
    {"snapshot_id", "kind", "as_of", "provider", "fingerprint"}
)
_UNIVERSE_FIELDS = frozenset({"name", "security_master_sha256"})
_COVERAGE_FIELDS = frozenset(
    {
        "calendar_sessions",
        "sampled_dates",
        "evaluated_dates",
        "warmup_dates",
        "skipped_dates",
        "average_cross_section",
        "final_train_observations",
        "symbols",
    }
)
_RESULT_FIELDS = frozenset(
    {
        "model",
        "factor_baselines",
        "best_factor_id",
        "best_factor_direction_adjusted_mean_ic",
        "model_minus_best_factor_ic",
    }
)
_MODEL_RESULT_FIELDS = frozenset(
    {
        "dates",
        "mean_ic",
        "ic_std",
        "ic_ir",
        "positive_share",
        "mean_spread",
        "spread_std",
    }
)
_BASELINE_FIELDS = frozenset(
    {"factor_id", "direction", "mean_ic", "direction_adjusted_mean_ic", "ic_ir"}
)
_COEFFICIENT_FIELDS = frozenset({"factor_id", "mean", "mean_abs", "final"})


def finalize_evaluation(draft: Mapping[str, Any]) -> dict[str, Any]:
    record = _json_clone(draft)
    if not isinstance(record, dict):
        raise ValueError("Model evaluation record must be an object")
    if "record_fingerprint" in record:
        raise ValueError("Model evaluation fingerprints are assigned by the schema")
    record["record_fingerprint"] = None
    record["record_fingerprint"] = evaluation_record_fingerprint(record)
    validate_evaluation(record)
    return record


def evaluation_record_fingerprint(value: Mapping[str, Any]) -> str:
    body = _json_clone(value)
    body["record_fingerprint"] = None
    body.pop("reused", None)
    return json_fingerprint(body)


def json_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def validate_evaluation(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != TOP_LEVEL_FIELDS:
        raise ValueError("Model evaluation top-level schema fields are invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Model evaluation schema version is invalid")
    if value.get("engine_version") != ENGINE_VERSION:
        raise ValueError("Model evaluation engine version is invalid")
    _identifier(value.get("evaluation_id"), EVALUATION_ID, "evaluation_id")
    _identifier(value.get("owner"), FINGERPRINT, "owner")
    _timestamp(value.get("created_at"), "created_at")

    model = _object(value.get("model"), _MODEL_FIELDS, "model")
    _identifier(model.get("model_id"), _MODEL_ID, "model_id")
    if type(model.get("version")) is not int or not 1 <= model["version"] <= 1000:
        raise ValueError("Model evaluation model version is invalid")
    _text(model.get("label"), "model.label", 120)
    if model.get("kind") not in {"ridge", "factor_mean", "gbdt"}:
        raise ValueError("Model evaluation model kind is invalid")
    _text(model.get("formula"), "model.formula", 300)
    hyper = model.get("hyperparameters")
    if not isinstance(hyper, Mapping) or len(hyper) > 8:
        raise ValueError("Model evaluation hyperparameters are invalid")
    for key, item in hyper.items():
        if not isinstance(key, str) or not key:
            raise ValueError("Model evaluation hyperparameter name is invalid")
        _finite(item, f"hyperparameters.{key}")

    factors = value.get("factors")
    if not isinstance(factors, list) or not 1 <= len(factors) <= 24:
        raise ValueError("Model evaluation factors are invalid")
    factor_ids: list[str] = []
    for item in factors:
        factor = _object(item, _FACTOR_FIELDS, "factor")
        factor_id = _identifier(factor.get("factor_id"), _FACTOR_ID, "factor_id")
        if factor_id in factor_ids:
            raise ValueError("Model evaluation factors must be unique")
        factor_ids.append(factor_id)
        if type(factor.get("version")) is not int or factor["version"] < 1:
            raise ValueError("Model evaluation factor version is invalid")
        if factor.get("direction") not in (-1, 1):
            raise ValueError("Model evaluation factor direction is invalid")

    parameters = _object(value.get("parameters"), _PARAMETER_FIELDS, "parameters")
    horizon = parameters.get("horizon")
    step = parameters.get("step")
    if type(horizon) is not int or not 1 <= horizon <= 250:
        raise ValueError("Model evaluation horizon is invalid")
    if type(step) is not int or not 1 <= step <= 21:
        raise ValueError("Model evaluation step is invalid")
    _iso_date(parameters.get("start"), "parameters.start")
    _iso_date(parameters.get("end"), "parameters.end")
    if str(parameters["start"]) > str(parameters["end"]):
        raise ValueError("Model evaluation start is after end")
    for name, low, high in (
        ("minimum_cross_section", 3, 500),
        ("minimum_train_dates", 2, 5_000),
        ("minimum_train_observations", 8, 500_000),
    ):
        item = parameters.get(name)
        if type(item) is not int or not low <= item <= high:
            raise ValueError(f"Model evaluation {name} is invalid")

    protocol = _object(value.get("protocol"), _PROTOCOL_FIELDS, "protocol")
    for name in _PROTOCOL_FIELDS:
        _text(protocol.get(name), f"protocol.{name}", 300)

    evidence = _object(value.get("evidence"), _EVIDENCE_FIELDS, "evidence")
    snapshot = _object(evidence.get("snapshot"), _SNAPSHOT_FIELDS, "snapshot")
    _identifier(snapshot.get("snapshot_id"), _EVIDENCE_ID, "snapshot_id")
    if snapshot.get("kind") != "market_cache":
        raise ValueError("Model evaluation snapshot kind is invalid")
    _iso_date(snapshot.get("as_of"), "snapshot.as_of")
    _text(snapshot.get("provider"), "snapshot.provider", 120)
    _identifier(snapshot.get("fingerprint"), FINGERPRINT, "snapshot.fingerprint")
    universe = _object(evidence.get("universe"), _UNIVERSE_FIELDS, "universe")
    _text(universe.get("name"), "universe.name", 200)
    _identifier(
        universe.get("security_master_sha256"),
        FINGERPRINT,
        "universe.security_master_sha256",
    )
    _identifier(
        evidence.get("config_context_fingerprint"),
        FINGERPRINT,
        "config_context_fingerprint",
    )

    coverage = _object(value.get("coverage"), _COVERAGE_FIELDS, "coverage")
    counts = {}
    for name in (
        "calendar_sessions",
        "sampled_dates",
        "evaluated_dates",
        "warmup_dates",
        "skipped_dates",
        "final_train_observations",
    ):
        item = coverage.get(name)
        if type(item) is not int or not 0 <= item <= 1_000_000:
            raise ValueError(f"Model evaluation coverage {name} is invalid")
        counts[name] = item
    if (
        counts["evaluated_dates"] + counts["warmup_dates"] + counts["skipped_dates"]
        != counts["sampled_dates"]
        or counts["sampled_dates"] > counts["calendar_sessions"]
    ):
        raise ValueError("Model evaluation coverage counts are inconsistent")
    if counts["evaluated_dates"] < 1:
        raise ValueError("Model evaluation must evaluate at least one date")
    average = _finite(
        coverage.get("average_cross_section"), "coverage.average_cross_section"
    )
    if average < 0:
        raise ValueError("Model evaluation average cross-section is invalid")
    symbols = coverage.get("symbols")
    if (
        not isinstance(symbols, Mapping)
        or not 1 <= len(symbols) <= 500
        or any(
            not isinstance(key, str)
            or not key
            or type(item) is not int
            or item < 1
            for key, item in symbols.items()
        )
    ):
        raise ValueError("Model evaluation symbol coverage is invalid")

    results = _object(value.get("results"), _RESULT_FIELDS, "results")
    model_result = _object(
        results.get("model"), _MODEL_RESULT_FIELDS, "results.model"
    )
    if model_result.get("dates") != counts["evaluated_dates"]:
        raise ValueError("Model evaluation result dates are inconsistent")
    for name in _MODEL_RESULT_FIELDS - {"dates"}:
        _finite(model_result.get(name), f"results.model.{name}")
    if not -1.0 <= float(model_result["mean_ic"]) <= 1.0:
        raise ValueError("Model evaluation mean IC is out of range")
    if not 0.0 <= float(model_result["positive_share"]) <= 1.0:
        raise ValueError("Model evaluation positive share is out of range")

    baselines = results.get("factor_baselines")
    if not isinstance(baselines, list) or len(baselines) != len(factor_ids):
        raise ValueError("Model evaluation factor baselines are invalid")
    adjusted: dict[str, float] = {}
    for item in baselines:
        baseline = _object(item, _BASELINE_FIELDS, "factor baseline")
        factor_id = str(baseline.get("factor_id"))
        if factor_id not in factor_ids or factor_id in adjusted:
            raise ValueError("Model evaluation baseline factors are inconsistent")
        if baseline.get("direction") not in (-1, 1):
            raise ValueError("Model evaluation baseline direction is invalid")
        mean_ic = _finite(baseline.get("mean_ic"), "baseline.mean_ic")
        adjusted_ic = _finite(
            baseline.get("direction_adjusted_mean_ic"),
            "baseline.direction_adjusted_mean_ic",
        )
        _finite(baseline.get("ic_ir"), "baseline.ic_ir")
        if abs(adjusted_ic - mean_ic * int(baseline["direction"])) > 1e-9:
            raise ValueError(
                "Model evaluation baseline direction adjustment is inconsistent"
            )
        adjusted[factor_id] = adjusted_ic
    best_id = results.get("best_factor_id")
    if best_id not in adjusted:
        raise ValueError("Model evaluation best factor is unknown")
    best_value = _finite(
        results.get("best_factor_direction_adjusted_mean_ic"),
        "results.best_factor_direction_adjusted_mean_ic",
    )
    if abs(best_value - max(adjusted.values())) > 1e-9 or abs(
        adjusted[str(best_id)] - best_value
    ) > 1e-9:
        raise ValueError("Model evaluation best factor value is inconsistent")
    delta = _finite(
        results.get("model_minus_best_factor_ic"),
        "results.model_minus_best_factor_ic",
    )
    if abs(delta - (float(model_result["mean_ic"]) - best_value)) > 1e-9:
        raise ValueError("Model evaluation best-factor delta is inconsistent")

    coefficients = value.get("coefficients")
    if not isinstance(coefficients, list) or len(coefficients) != len(factor_ids):
        raise ValueError("Model evaluation coefficients are invalid")
    seen: set[str] = set()
    for item in coefficients:
        coefficient = _object(item, _COEFFICIENT_FIELDS, "coefficient")
        factor_id = str(coefficient.get("factor_id"))
        if factor_id not in factor_ids or factor_id in seen:
            raise ValueError("Model evaluation coefficient factors are inconsistent")
        seen.add(factor_id)
        for name in ("mean", "mean_abs", "final"):
            _finite(coefficient.get(name), f"coefficient.{name}")
        if float(coefficient["mean_abs"]) < 0:
            raise ValueError("Model evaluation coefficient magnitude is invalid")

    _identifier(
        value.get("evaluation_fingerprint"), FINGERPRINT, "evaluation_fingerprint"
    )
    if value.get("safety") != SAFETY:
        raise ValueError("Model evaluation safety contract is invalid")
    _identifier(value.get("record_fingerprint"), FINGERPRINT, "record_fingerprint")
    if value["record_fingerprint"] != evaluation_record_fingerprint(value):
        raise ValueError("Model evaluation record fingerprint does not match content")


def _object(value: Any, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"Model evaluation {label} schema fields are invalid")
    return value


def _identifier(value: Any, pattern: re.Pattern[str], field: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"Model evaluation {field} is invalid")
    return value


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(
            f"Model evaluation {field} must contain 1 to {maximum} characters"
        )
    return value


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Model evaluation {field} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"Model evaluation {field} must be finite")
    return parsed


def _timestamp(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"Model evaluation {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Model evaluation {field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Model evaluation {field} must include a timezone")


def _iso_date(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"Model evaluation {field} is invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Model evaluation {field} must use YYYY-MM-DD") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError(f"Model evaluation {field} must use YYYY-MM-DD")


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


__all__ = [
    "ENGINE_VERSION",
    "EVALUATION_ID",
    "SAFETY",
    "SCHEMA_VERSION",
    "TOP_LEVEL_FIELDS",
    "evaluation_record_fingerprint",
    "finalize_evaluation",
    "json_fingerprint",
    "validate_evaluation",
]
