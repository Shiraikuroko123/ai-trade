from __future__ import annotations

from datetime import datetime
import json
import math
import re
from typing import Any, Mapping

from .schema import (
    FINGERPRINT,
    HYPOTHESIS_ID,
    PREDICTION_METRICS,
    json_fingerprint,
)


RUN_SCHEMA_VERSION = 1
RUNNER_VERSION = 1

RUN_ID = re.compile(r"run_[0-9a-f]{32}\Z")
_EVIDENCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")
_PREDICTION_ID = re.compile(r"pred_[0-9]{2}\Z")
_CRITERION_ID = re.compile(r"fals_[0-9]{2}\Z")

MODES = frozenset({"same_snapshot", "independent_replication"})
_MODE_STATUS = {
    "same_snapshot": frozenset({"SUPPORTED", "FALSIFIED"}),
    "independent_replication": frozenset({"REPLICATED", "NOT_REPLICATED"}),
}
_JUDGMENT_OUTCOMES = frozenset({"SUPPORTED", "FALSIFIED"})
_OPERATORS = frozenset({">=", "<="})

RUN_SAFETY = {
    "research_only": True,
    "verdict_grants_no_authority": True,
    "may_create_candidate": False,
    "may_approve": False,
    "may_activate": False,
    "may_trade": False,
    "may_change_broker_configuration": False,
    "may_weaken_validation_gates": False,
}

RUN_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "runner_version",
        "run_id",
        "owner",
        "created_at",
        "hypothesis_id",
        "hypothesis_record_fingerprint",
        "hypothesis_design_fingerprint",
        "mode",
        "execution_fingerprint",
        "registered_snapshot",
        "executed_snapshot",
        "config_context_fingerprint",
        "baseline_settings_fingerprint",
        "candidate_settings_fingerprint",
        "period",
        "results",
        "observations",
        "judgments",
        "verdict",
        "multiple_testing",
        "safety",
        "record_fingerprint",
    }
)

_REGISTERED_SNAPSHOT_FIELDS = frozenset({"snapshot_id", "as_of", "fingerprint"})
_EXECUTED_SNAPSHOT_FIELDS = frozenset(
    {"snapshot_id", "as_of", "provider", "fingerprint", "sessions_after_registration"}
)
_PERIOD_FIELDS = frozenset(
    {"start", "end", "sessions", "holdout_start", "holdout_sessions"}
)
_RESULT_FIELDS = frozenset(
    {"full", "holdout", "cost_stress", "rolling", "sensitivity"}
)
_COMPARISON_FIELDS = frozenset({"baseline", "candidate"})
_COST_STRESS_FIELDS = frozenset({"multiplier", "baseline", "candidate"})
_ROLLING_FIELDS = frozenset(
    {"fold_count", "consistent_folds", "direction_rule", "folds"}
)
_FOLD_FIELDS = frozenset(
    {
        "fold",
        "start",
        "end",
        "sessions",
        "baseline_value",
        "candidate_value",
        "delta",
        "direction_consistent",
    }
)
_SENSITIVITY_FIELDS = frozenset(
    {"fraction", "parameters", "variant_count", "minimum_sharpe", "variants"}
)
_VARIANT_FIELDS = frozenset({"parameter", "value", "sharpe"})
_OBSERVATION_FIELDS = frozenset({"metric", "value", "formula"})
_JUDGMENT_FIELDS = frozenset(
    {
        "prediction_id",
        "criterion_id",
        "metric",
        "operator",
        "threshold",
        "observed",
        "outcome",
    }
)
_VERDICT_FIELDS = frozenset(
    {"status", "predictions_total", "predictions_supported", "falsified_criteria"}
)
_MULTIPLE_TESTING_FIELDS = frozenset(
    {"family_id", "family_position", "maximum_hypotheses", "alpha", "correction", "note"}
)

RUN_METRIC_FIELDS = frozenset(
    {
        "total_return",
        "cagr",
        "sharpe",
        "max_drawdown",
        "turnover",
        "transaction_costs",
    }
)


def finalize_run_record(draft: Mapping[str, Any]) -> dict[str, Any]:
    record = _json_clone(draft)
    if not isinstance(record, dict):
        raise ValueError("Hypothesis run record must be an object")
    if "record_fingerprint" in record:
        raise ValueError("Hypothesis run fingerprints are assigned by the schema")
    record["record_fingerprint"] = None
    record["record_fingerprint"] = run_record_fingerprint(record)
    validate_run_record(record)
    return record


def run_record_fingerprint(value: Mapping[str, Any]) -> str:
    body = _json_clone(value)
    body["record_fingerprint"] = None
    body.pop("reused", None)
    return json_fingerprint(body)


def validate_run_record(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != RUN_TOP_LEVEL_FIELDS:
        raise ValueError("Hypothesis run top-level schema fields are invalid")
    if value.get("schema_version") != RUN_SCHEMA_VERSION:
        raise ValueError("Hypothesis run schema version is invalid")
    if value.get("runner_version") != RUNNER_VERSION:
        raise ValueError("Hypothesis run runner version is invalid")
    _identifier(value.get("run_id"), RUN_ID, "run_id")
    _identifier(value.get("owner"), FINGERPRINT, "owner")
    _timestamp(value.get("created_at"), "created_at")
    _identifier(value.get("hypothesis_id"), HYPOTHESIS_ID, "hypothesis_id")
    _identifier(
        value.get("hypothesis_record_fingerprint"),
        FINGERPRINT,
        "hypothesis_record_fingerprint",
    )
    _identifier(
        value.get("hypothesis_design_fingerprint"),
        FINGERPRINT,
        "hypothesis_design_fingerprint",
    )
    mode = value.get("mode")
    if mode not in MODES:
        raise ValueError("Hypothesis run mode is invalid")
    _identifier(
        value.get("execution_fingerprint"), FINGERPRINT, "execution_fingerprint"
    )

    registered = _object(
        value.get("registered_snapshot"),
        _REGISTERED_SNAPSHOT_FIELDS,
        "registered_snapshot",
    )
    _identifier(registered.get("snapshot_id"), _EVIDENCE_ID, "registered snapshot_id")
    _iso_date(registered.get("as_of"), "registered_snapshot.as_of")
    _identifier(
        registered.get("fingerprint"), FINGERPRINT, "registered_snapshot.fingerprint"
    )

    executed = _object(
        value.get("executed_snapshot"),
        _EXECUTED_SNAPSHOT_FIELDS,
        "executed_snapshot",
    )
    _identifier(executed.get("snapshot_id"), _EVIDENCE_ID, "executed snapshot_id")
    _iso_date(executed.get("as_of"), "executed_snapshot.as_of")
    _text(executed.get("provider"), "executed_snapshot.provider", 120)
    _identifier(
        executed.get("fingerprint"), FINGERPRINT, "executed_snapshot.fingerprint"
    )
    after = executed.get("sessions_after_registration")
    if type(after) is not int or not 0 <= after <= 100_000:
        raise ValueError("Hypothesis run sessions_after_registration is invalid")
    if mode == "same_snapshot":
        if executed["fingerprint"] != registered["fingerprint"]:
            raise ValueError(
                "Same-snapshot run must execute the registered snapshot fingerprint"
            )
    elif executed["fingerprint"] == registered["fingerprint"]:
        raise ValueError(
            "Replication run must execute a later snapshot, not the registered one"
        )

    _identifier(
        value.get("config_context_fingerprint"),
        FINGERPRINT,
        "config_context_fingerprint",
    )
    _identifier(
        value.get("baseline_settings_fingerprint"),
        FINGERPRINT,
        "baseline_settings_fingerprint",
    )
    _identifier(
        value.get("candidate_settings_fingerprint"),
        FINGERPRINT,
        "candidate_settings_fingerprint",
    )

    period = _object(value.get("period"), _PERIOD_FIELDS, "period")
    _iso_date(period.get("start"), "period.start")
    _iso_date(period.get("end"), "period.end")
    _iso_date(period.get("holdout_start"), "period.holdout_start")
    if str(period["start"]) > str(period["end"]):
        raise ValueError("Hypothesis run period start is after end")
    if not str(period["start"]) <= str(period["holdout_start"]) <= str(period["end"]):
        raise ValueError("Hypothesis run holdout_start is outside the period")
    sessions = period.get("sessions")
    holdout_sessions = period.get("holdout_sessions")
    if type(sessions) is not int or not 2 <= sessions <= 100_000:
        raise ValueError("Hypothesis run period sessions are invalid")
    if (
        type(holdout_sessions) is not int
        or not 1 <= holdout_sessions < sessions
    ):
        raise ValueError("Hypothesis run holdout sessions are invalid")

    results = _object(value.get("results"), _RESULT_FIELDS, "results")
    for window in ("full", "holdout"):
        comparison = _object(
            results.get(window), _COMPARISON_FIELDS, f"results.{window}"
        )
        _metrics_block(comparison.get("baseline"), f"results.{window}.baseline")
        _metrics_block(comparison.get("candidate"), f"results.{window}.candidate")
    stress_rows = results.get("cost_stress")
    if not isinstance(stress_rows, list) or not 1 <= len(stress_rows) <= 4:
        raise ValueError("Hypothesis run cost_stress rows are invalid")
    multipliers: list[float] = []
    for row in stress_rows:
        stress = _object(row, _COST_STRESS_FIELDS, "cost_stress row")
        multiplier = _finite(stress.get("multiplier"), "cost_stress.multiplier")
        if not 1.0 < multiplier <= 10.0:
            raise ValueError("Hypothesis run cost multiplier is invalid")
        multipliers.append(multiplier)
        _metrics_block(stress.get("baseline"), "cost_stress.baseline")
        _metrics_block(stress.get("candidate"), "cost_stress.candidate")
    if multipliers != sorted(set(multipliers)):
        raise ValueError("Hypothesis run cost multipliers must be unique and ascending")

    rolling = _object(results.get("rolling"), _ROLLING_FIELDS, "results.rolling")
    fold_count = rolling.get("fold_count")
    consistent = rolling.get("consistent_folds")
    if type(fold_count) is not int or not 2 <= fold_count <= 20:
        raise ValueError("Hypothesis run rolling fold_count is invalid")
    _text(rolling.get("direction_rule"), "rolling.direction_rule", 300)
    folds = rolling.get("folds")
    if not isinstance(folds, list) or len(folds) != fold_count:
        raise ValueError("Hypothesis run rolling folds are invalid")
    consistent_count = 0
    for index, item in enumerate(folds, start=1):
        fold = _object(item, _FOLD_FIELDS, "rolling fold")
        if fold.get("fold") != index:
            raise ValueError("Hypothesis run rolling folds must be sequential")
        _iso_date(fold.get("start"), "fold.start")
        _iso_date(fold.get("end"), "fold.end")
        if str(fold["start"]) > str(fold["end"]):
            raise ValueError("Hypothesis run fold start is after end")
        fold_sessions = fold.get("sessions")
        if type(fold_sessions) is not int or not 2 <= fold_sessions <= 100_000:
            raise ValueError("Hypothesis run fold sessions are invalid")
        _finite(fold.get("baseline_value"), "fold.baseline_value")
        _finite(fold.get("candidate_value"), "fold.candidate_value")
        _finite(fold.get("delta"), "fold.delta")
        if not isinstance(fold.get("direction_consistent"), bool):
            raise ValueError("Hypothesis run fold direction flag is invalid")
        consistent_count += bool(fold["direction_consistent"])
    if consistent != consistent_count:
        raise ValueError("Hypothesis run consistent_folds does not match its folds")

    sensitivity = _object(
        results.get("sensitivity"), _SENSITIVITY_FIELDS, "results.sensitivity"
    )
    fraction = _finite(sensitivity.get("fraction"), "sensitivity.fraction")
    if not 0.01 <= fraction <= 0.5:
        raise ValueError("Hypothesis run sensitivity fraction is invalid")
    parameters = sensitivity.get("parameters")
    if (
        not isinstance(parameters, list)
        or not 1 <= len(parameters) <= 24
        or any(not isinstance(item, str) or not item for item in parameters)
        or len(parameters) != len(set(parameters))
    ):
        raise ValueError("Hypothesis run sensitivity parameters are invalid")
    variants = sensitivity.get("variants")
    if not isinstance(variants, list) or not 1 <= len(variants) <= 48:
        raise ValueError("Hypothesis run sensitivity variants are invalid")
    minimum_sharpe = _finite(
        sensitivity.get("minimum_sharpe"), "sensitivity.minimum_sharpe"
    )
    observed_minimum = math.inf
    for item in variants:
        variant = _object(item, _VARIANT_FIELDS, "sensitivity variant")
        parameter = variant.get("parameter")
        if parameter not in parameters:
            raise ValueError("Hypothesis run variant parameter is unknown")
        _finite(variant.get("value"), "variant.value")
        observed_minimum = min(
            observed_minimum, _finite(variant.get("sharpe"), "variant.sharpe")
        )
    if sensitivity.get("variant_count") != len(variants):
        raise ValueError("Hypothesis run variant_count does not match its variants")
    if minimum_sharpe != observed_minimum:
        raise ValueError("Hypothesis run minimum_sharpe does not match its variants")

    observations = value.get("observations")
    if not isinstance(observations, list) or not 1 <= len(observations) <= 8:
        raise ValueError("Hypothesis run observations are invalid")
    observed_metrics: dict[str, float] = {}
    for item in observations:
        observation = _object(item, _OBSERVATION_FIELDS, "observation")
        metric = observation.get("metric")
        if metric not in PREDICTION_METRICS or metric in observed_metrics:
            raise ValueError("Hypothesis run observation metric is invalid")
        observed_metrics[str(metric)] = _finite(
            observation.get("value"), "observation.value"
        )
        _text(observation.get("formula"), "observation.formula", 200)

    judgments = value.get("judgments")
    if not isinstance(judgments, list) or not 3 <= len(judgments) <= 8:
        raise ValueError("Hypothesis run judgments are invalid")
    supported = 0
    falsified_ids: list[str] = []
    seen_predictions: set[str] = set()
    for item in judgments:
        judgment = _object(item, _JUDGMENT_FIELDS, "judgment")
        prediction_id = _identifier(
            judgment.get("prediction_id"), _PREDICTION_ID, "judgment.prediction_id"
        )
        if prediction_id in seen_predictions:
            raise ValueError("Hypothesis run judgments must be unique per prediction")
        seen_predictions.add(prediction_id)
        _identifier(
            judgment.get("criterion_id"), _CRITERION_ID, "judgment.criterion_id"
        )
        metric = judgment.get("metric")
        if metric not in observed_metrics:
            raise ValueError("Hypothesis run judgment metric has no observation")
        operator = judgment.get("operator")
        if operator not in _OPERATORS:
            raise ValueError("Hypothesis run judgment operator is invalid")
        threshold = _finite(judgment.get("threshold"), "judgment.threshold")
        observed = _finite(judgment.get("observed"), "judgment.observed")
        if observed != observed_metrics[str(metric)]:
            raise ValueError("Hypothesis run judgment observed value is inconsistent")
        outcome = judgment.get("outcome")
        if outcome not in _JUDGMENT_OUTCOMES:
            raise ValueError("Hypothesis run judgment outcome is invalid")
        holds = observed >= threshold if operator == ">=" else observed <= threshold
        expected = "SUPPORTED" if holds else "FALSIFIED"
        if outcome != expected:
            raise ValueError(
                "Hypothesis run judgment outcome does not match its observation"
            )
        if outcome == "SUPPORTED":
            supported += 1
        else:
            falsified_ids.append(str(judgment["criterion_id"]))

    verdict = _object(value.get("verdict"), _VERDICT_FIELDS, "verdict")
    if verdict.get("status") not in _MODE_STATUS[str(mode)]:
        raise ValueError("Hypothesis run verdict status is invalid for its mode")
    if verdict.get("predictions_total") != len(judgments):
        raise ValueError("Hypothesis run verdict totals are inconsistent")
    if verdict.get("predictions_supported") != supported:
        raise ValueError("Hypothesis run verdict supported count is inconsistent")
    if verdict.get("falsified_criteria") != falsified_ids:
        raise ValueError("Hypothesis run verdict criteria are inconsistent")
    positive = supported == len(judgments)
    expected_status = (
        ("SUPPORTED" if positive else "FALSIFIED")
        if mode == "same_snapshot"
        else ("REPLICATED" if positive else "NOT_REPLICATED")
    )
    if verdict["status"] != expected_status:
        raise ValueError("Hypothesis run verdict status does not match its judgments")

    testing = _object(
        value.get("multiple_testing"),
        _MULTIPLE_TESTING_FIELDS,
        "multiple_testing",
    )
    _identifier(testing.get("family_id"), _EVIDENCE_ID, "family_id")
    position = testing.get("family_position")
    if testing.get("maximum_hypotheses") != 3:
        raise ValueError("Hypothesis run family budget must be three")
    if type(position) is not int or not 1 <= position <= 3:
        raise ValueError("Hypothesis run family position is invalid")
    alpha = _finite(testing.get("alpha"), "multiple_testing.alpha")
    if not 0 < alpha <= 0.05 or testing.get("correction") != "holm":
        raise ValueError("Hypothesis run multiple-testing policy is invalid")
    _text(testing.get("note"), "multiple_testing.note", 500)

    if value.get("safety") != RUN_SAFETY:
        raise ValueError("Hypothesis run safety contract is invalid")
    _identifier(value.get("record_fingerprint"), FINGERPRINT, "record_fingerprint")
    if value["record_fingerprint"] != run_record_fingerprint(value):
        raise ValueError("Hypothesis run record fingerprint does not match content")


def _metrics_block(value: Any, label: str) -> None:
    block = _object(value, RUN_METRIC_FIELDS, label)
    for field in RUN_METRIC_FIELDS:
        _finite(block.get(field), f"{label}.{field}")


def _object(value: Any, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"Hypothesis run {label} schema fields are invalid")
    return value


def _identifier(value: Any, pattern: re.Pattern[str], field: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"Hypothesis run {field} is invalid")
    return value


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(
            f"Hypothesis run {field} must contain 1 to {maximum} characters"
        )
    return value


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Hypothesis run {field} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"Hypothesis run {field} must be finite")
    return parsed


def _timestamp(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"Hypothesis run {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Hypothesis run {field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Hypothesis run {field} must include a timezone")


def _iso_date(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"Hypothesis run {field} is invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Hypothesis run {field} must use YYYY-MM-DD") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError(f"Hypothesis run {field} must use YYYY-MM-DD")


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


__all__ = [
    "MODES",
    "RUN_ID",
    "RUN_METRIC_FIELDS",
    "RUN_SAFETY",
    "RUN_SCHEMA_VERSION",
    "RUN_TOP_LEVEL_FIELDS",
    "RUNNER_VERSION",
    "finalize_run_record",
    "run_record_fingerprint",
    "validate_run_record",
]
