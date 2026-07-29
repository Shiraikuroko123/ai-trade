from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import math
import re
from typing import Any, Mapping, cast

from ..research_statistics import (
    DEFAULT_BOOTSTRAP_RESAMPLES,
    apply_holm_correction,
    deterministic_seed,
)


SCHEMA_VERSION = 2
ENGINE_VERSION = 2

EVALUATION_ID = re.compile(r"eval_[0-9a-f]{32}\Z")
FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_EVIDENCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")
_FACTOR_ID = re.compile(r"[a-z0-9][a-z0-9_]{2,60}\Z")

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
        "factor",
        "parameters",
        "evidence",
        "coverage",
        "results",
        "evaluation_fingerprint",
        "safety",
        "record_fingerprint",
    }
)

_FACTOR_FIELDS = frozenset(
    {
        "factor_id",
        "version",
        "label",
        "family",
        "direction",
        "minimum_history",
        "formula",
    }
)
_PARAMETER_FIELDS = frozenset(
    {"start", "end", "step", "horizons", "minimum_cross_section"}
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
        "skipped_dates",
        "average_cross_section",
        "symbols",
    }
)
_RESULT_FIELDS = frozenset(
    {
        "horizon",
        "dates",
        "mean_ic",
        "ic_std",
        "ic_ir",
        "positive_share",
        "direction_hit_rate",
        "mean_spread",
        "spread_std",
        "direction_adjusted_mean_spread",
        "statistical_validation",
    }
)
_RESULT_FIELDS_V1 = _RESULT_FIELDS - {"statistical_validation"}
_STATISTICAL_VALIDATION_FIELDS = frozenset(
    {
        "method",
        "alternative",
        "observations",
        "block_size",
        "resamples",
        "seed",
        "confidence_level",
        "effect_size",
        "standard_error",
        "ci_low",
        "ci_high",
        "p_value",
        "subperiods",
        "subperiod_means",
        "positive_subperiods",
        "minimum_subperiod_mean",
        "alpha",
        "correction",
        "family_size",
        "adjusted_p_value",
        "reject_null",
    }
)


def finalize_evaluation(draft: Mapping[str, Any]) -> dict[str, Any]:
    record = _json_clone(draft)
    if not isinstance(record, dict):
        raise ValueError("Factor evaluation record must be an object")
    if "record_fingerprint" in record:
        raise ValueError("Factor evaluation fingerprints are assigned by the schema")
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
        raise ValueError("Factor evaluation top-level schema fields are invalid")
    schema_version = value.get("schema_version")
    if schema_version not in {1, SCHEMA_VERSION}:
        raise ValueError("Factor evaluation schema version is invalid")
    expected_engine = 1 if schema_version == 1 else ENGINE_VERSION
    if value.get("engine_version") != expected_engine:
        raise ValueError("Factor evaluation engine version is invalid")
    _identifier(value.get("evaluation_id"), EVALUATION_ID, "evaluation_id")
    _identifier(value.get("owner"), FINGERPRINT, "owner")
    _timestamp(value.get("created_at"), "created_at")

    factor = _object(value.get("factor"), _FACTOR_FIELDS, "factor")
    _identifier(factor.get("factor_id"), _FACTOR_ID, "factor_id")
    if type(factor.get("version")) is not int or not 1 <= factor["version"] <= 1000:
        raise ValueError("Factor evaluation factor version is invalid")
    _text(factor.get("label"), "factor.label", 120)
    _text(factor.get("family"), "factor.family", 40)
    if factor.get("direction") not in (-1, 1):
        raise ValueError("Factor evaluation direction must be -1 or 1")
    minimum_history = factor.get("minimum_history")
    if type(minimum_history) is not int or not 2 <= minimum_history <= 2_000:
        raise ValueError("Factor evaluation minimum_history is invalid")
    _text(factor.get("formula"), "factor.formula", 200)

    parameters = _object(value.get("parameters"), _PARAMETER_FIELDS, "parameters")
    _iso_date(parameters.get("start"), "parameters.start")
    _iso_date(parameters.get("end"), "parameters.end")
    if str(parameters["start"]) > str(parameters["end"]):
        raise ValueError("Factor evaluation start is after end")
    step = parameters.get("step")
    if type(step) is not int or not 1 <= step <= 21:
        raise ValueError("Factor evaluation step is invalid")
    horizons = parameters.get("horizons")
    if (
        not isinstance(horizons, list)
        or not 1 <= len(horizons) <= 4
        or any(type(item) is not int or not 1 <= item <= 250 for item in horizons)
        or horizons != sorted(set(horizons))
    ):
        raise ValueError("Factor evaluation horizons are invalid")
    minimum_cross_section = parameters.get("minimum_cross_section")
    if (
        type(minimum_cross_section) is not int
        or not 3 <= minimum_cross_section <= 500
    ):
        raise ValueError("Factor evaluation minimum_cross_section is invalid")

    evidence = _object(value.get("evidence"), _EVIDENCE_FIELDS, "evidence")
    snapshot = _object(evidence.get("snapshot"), _SNAPSHOT_FIELDS, "snapshot")
    _identifier(snapshot.get("snapshot_id"), _EVIDENCE_ID, "snapshot_id")
    if snapshot.get("kind") not in {"market_cache", "feature_snapshot_dataset"}:
        raise ValueError("Factor evaluation snapshot kind is invalid")
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
    calendar_sessions = coverage.get("calendar_sessions")
    sampled = coverage.get("sampled_dates")
    evaluated = coverage.get("evaluated_dates")
    skipped = coverage.get("skipped_dates")
    for name, item in (
        ("calendar_sessions", calendar_sessions),
        ("sampled_dates", sampled),
        ("evaluated_dates", evaluated),
        ("skipped_dates", skipped),
    ):
        if type(item) is not int or not 0 <= item <= 200_000:
            raise ValueError(f"Factor evaluation coverage {name} is invalid")
    calendar_sessions = cast(int, calendar_sessions)
    sampled = cast(int, sampled)
    evaluated = cast(int, evaluated)
    skipped = cast(int, skipped)
    if evaluated + skipped != sampled or sampled > calendar_sessions:
        raise ValueError("Factor evaluation coverage counts are inconsistent")
    if evaluated < 1:
        raise ValueError("Factor evaluation must evaluate at least one date")
    average = _finite(
        coverage.get("average_cross_section"), "coverage.average_cross_section"
    )
    if average < 0:
        raise ValueError("Factor evaluation average cross-section is invalid")
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
        raise ValueError("Factor evaluation symbol coverage is invalid")

    results = value.get("results")
    if not isinstance(results, list) or len(results) != len(horizons):
        raise ValueError("Factor evaluation results must match its horizons")
    validation_family: list[Mapping[str, Any]] = []
    for expected_horizon, item in zip(horizons, results):
        result = _object(
            item,
            _RESULT_FIELDS if schema_version == SCHEMA_VERSION else _RESULT_FIELDS_V1,
            "result",
        )
        if result.get("horizon") != expected_horizon:
            raise ValueError("Factor evaluation result horizons are out of order")
        dates = result.get("dates")
        if type(dates) is not int or not 1 <= dates <= 200_000:
            raise ValueError("Factor evaluation result dates are invalid")
        for name in (
            "mean_ic",
            "ic_std",
            "ic_ir",
            "positive_share",
            "direction_hit_rate",
            "mean_spread",
            "spread_std",
            "direction_adjusted_mean_spread",
        ):
            _finite(result.get(name), f"result.{name}")
        if not -1.0 <= float(result["mean_ic"]) <= 1.0:
            raise ValueError("Factor evaluation mean IC is out of range")
        for name in ("positive_share", "direction_hit_rate"):
            if not 0.0 <= float(result[name]) <= 1.0:
                raise ValueError(f"Factor evaluation {name} is out of range")
        if schema_version == SCHEMA_VERSION:
            expected_effect = float(result["mean_ic"]) * int(factor["direction"])
            validation_family.append(_statistical_validation(
                result.get("statistical_validation"),
                observations=int(dates),
                family_size=len(horizons),
                expected_effect=expected_effect,
                expected_block_size=min(
                    int(dates), max(1, math.ceil(expected_horizon / int(step)))
                ),
                expected_seed=deterministic_seed(
                    "factor-ic",
                    snapshot["fingerprint"],
                    factor["factor_id"],
                    expected_horizon,
                    step,
                    SCHEMA_VERSION,
                    ENGINE_VERSION,
                ),
                label="result.statistical_validation",
            ))
    if validation_family:
        _validate_holm_family(validation_family)

    _identifier(
        value.get("evaluation_fingerprint"), FINGERPRINT, "evaluation_fingerprint"
    )
    if value.get("safety") != SAFETY:
        raise ValueError("Factor evaluation safety contract is invalid")
    _identifier(value.get("record_fingerprint"), FINGERPRINT, "record_fingerprint")
    if value["record_fingerprint"] != evaluation_record_fingerprint(value):
        raise ValueError("Factor evaluation record fingerprint does not match content")


def _object(value: Any, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"Factor evaluation {label} schema fields are invalid")
    return value


def _statistical_validation(
    value: Any,
    *,
    observations: int,
    family_size: int,
    expected_effect: float,
    expected_block_size: int,
    expected_seed: int,
    label: str,
) -> Mapping[str, Any]:
    validation = _object(value, _STATISTICAL_VALIDATION_FIELDS, label)
    if (
        validation.get("method") != "circular_moving_block_bootstrap"
        or validation.get("alternative") != "greater"
        or validation.get("correction") != "holm"
    ):
        raise ValueError(f"Factor evaluation {label} method is invalid")
    if validation.get("observations") != observations:
        raise ValueError(f"Factor evaluation {label} observations are invalid")
    block_size = validation.get("block_size")
    if block_size != expected_block_size:
        raise ValueError(f"Factor evaluation {label} block size is invalid")
    resamples = validation.get("resamples")
    if resamples != DEFAULT_BOOTSTRAP_RESAMPLES:
        raise ValueError(f"Factor evaluation {label} resamples are invalid")
    seed = validation.get("seed")
    if seed != expected_seed:
        raise ValueError(f"Factor evaluation {label} seed is invalid")
    confidence = _finite(
        validation.get("confidence_level"), f"{label}.confidence_level"
    )
    if abs(confidence - 0.95) > 1e-12:
        raise ValueError(f"Factor evaluation {label} confidence level is invalid")
    effect = _finite(validation.get("effect_size"), f"{label}.effect_size")
    if abs(effect - expected_effect) > 1e-9:
        raise ValueError(f"Factor evaluation {label} effect size is inconsistent")
    standard_error = _finite(
        validation.get("standard_error"), f"{label}.standard_error"
    )
    low = _finite(validation.get("ci_low"), f"{label}.ci_low")
    high = _finite(validation.get("ci_high"), f"{label}.ci_high")
    if standard_error < 0 or low > high or not -1 <= low <= high <= 1:
        raise ValueError(f"Factor evaluation {label} uncertainty is invalid")
    p_value = _finite(validation.get("p_value"), f"{label}.p_value")
    adjusted = _finite(
        validation.get("adjusted_p_value"), f"{label}.adjusted_p_value"
    )
    if not 1 / (int(resamples) + 1) <= p_value <= adjusted <= 1:
        raise ValueError(f"Factor evaluation {label} p-values are invalid")
    alpha = _finite(validation.get("alpha"), f"{label}.alpha")
    if abs(alpha - 0.05) > 1e-12:
        raise ValueError(f"Factor evaluation {label} alpha is invalid")
    if validation.get("family_size") != family_size:
        raise ValueError(f"Factor evaluation {label} family size is invalid")
    if validation.get("subperiods") != 3:
        raise ValueError(f"Factor evaluation {label} subperiod count is invalid")
    period_means = validation.get("subperiod_means")
    if (
        not isinstance(period_means, list)
        or len(period_means) != 3
        or any(
            isinstance(item, bool)
            or not isinstance(item, (int, float))
            or not math.isfinite(float(item))
            or not -1 <= float(item) <= 1
            for item in period_means
        )
    ):
        raise ValueError(f"Factor evaluation {label} subperiod means are invalid")
    positive = validation.get("positive_subperiods")
    expected_positive = sum(float(item) > 0 for item in period_means)
    if positive != expected_positive:
        raise ValueError(f"Factor evaluation {label} stability is invalid")
    minimum = _finite(
        validation.get("minimum_subperiod_mean"),
        f"{label}.minimum_subperiod_mean",
    )
    if abs(minimum - min(float(item) for item in period_means)) > 1e-12:
        raise ValueError(f"Factor evaluation {label} stability is inconsistent")
    rejected = validation.get("reject_null")
    if type(rejected) is not bool or rejected != (adjusted <= alpha):
        raise ValueError(f"Factor evaluation {label} rejection is inconsistent")
    return validation


def _validate_holm_family(family: list[Mapping[str, Any]]) -> None:
    expected = apply_holm_correction(family)
    for observed, corrected in zip(family, expected):
        if (
            abs(
                float(observed["adjusted_p_value"])
                - float(corrected["adjusted_p_value"])
            )
            > 1e-12
            or observed["reject_null"] != corrected["reject_null"]
        ):
            raise ValueError(
                "Factor evaluation Holm correction is inconsistent"
            )


def _identifier(value: Any, pattern: re.Pattern[str], field: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"Factor evaluation {field} is invalid")
    return value


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(
            f"Factor evaluation {field} must contain 1 to {maximum} characters"
        )
    return value


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Factor evaluation {field} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"Factor evaluation {field} must be finite")
    return parsed


def _timestamp(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"Factor evaluation {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Factor evaluation {field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Factor evaluation {field} must include a timezone")


def _iso_date(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"Factor evaluation {field} is invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Factor evaluation {field} must use YYYY-MM-DD") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError(f"Factor evaluation {field} must use YYYY-MM-DD")


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
