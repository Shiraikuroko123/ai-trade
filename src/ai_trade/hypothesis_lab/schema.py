from __future__ import annotations

from datetime import datetime
from hashlib import sha256
import json
import math
import re
from typing import Any, Mapping


SCHEMA_VERSION = 1
ENGINE_VERSION = 1
TEMPLATE_VERSION = "local-objective-v1"

HYPOTHESIS_ID = re.compile(r"hyp_[0-9a-f]{32}\Z")
FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
CANDIDATE_ID = re.compile(r"cand_[0-9a-f]{32}\Z")
EVIDENCE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")

OBJECTIVES = frozenset({"balanced", "drawdown", "turnover"})
PREDICTION_METRICS = frozenset(
    {
        "cost_stress.total_return_delta",
        "full.max_drawdown_delta",
        "full.sharpe_delta",
        "full.turnover_ratio",
        "holdout.sharpe_delta",
        "stability.minimum_sharpe_delta",
    }
)
OPERATORS = frozenset({">=", "<="})
WINDOWS = frozenset({"full", "holdout", "cost_stress", "sensitivity"})
QUALITY_LEVELS = frozenset({"HIGH", "MEDIUM", "LOW"})

TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "engine_version",
        "template_version",
        "hypothesis_id",
        "owner",
        "created_at",
        "source",
        "title",
        "observation",
        "mechanism",
        "scope",
        "assumptions",
        "predictions",
        "falsification_criteria",
        "competing_explanations",
        "confounds",
        "evidence",
        "baseline",
        "experiment_plan",
        "quality_assessment",
        "safety",
        "design_fingerprint",
        "record_fingerprint",
    }
)

SAFETY = {
    "research_only": True,
    "may_create_candidate": False,
    "may_approve": False,
    "may_activate": False,
    "may_trade": False,
    "may_change_broker_configuration": False,
    "may_weaken_validation_gates": False,
}

_SOURCE_FIELDS = frozenset(
    {"kind", "objective", "selection_reason", "model_used"}
)
_SCOPE_FIELDS = frozenset(
    {"universe", "instrument_types", "start", "end", "regime", "exclusions"}
)
_PREDICTION_FIELDS = frozenset(
    {
        "prediction_id",
        "metric",
        "operator",
        "threshold",
        "baseline",
        "window",
        "rationale",
    }
)
_FALSIFICATION_FIELDS = frozenset(
    {
        "criterion_id",
        "prediction_id",
        "metric",
        "operator",
        "threshold",
        "window",
    }
)
_EXPLANATION_FIELDS = frozenset(
    {"explanation_id", "statement", "distinguishing_test"}
)
_CONFOUND_FIELDS = frozenset({"confound_id", "risk", "control"})
_EVIDENCE_FIELDS = frozenset({"snapshot", "references"})
_SNAPSHOT_FIELDS = frozenset(
    {"snapshot_id", "kind", "as_of", "provider", "fingerprint"}
)
_REFERENCE_FIELDS = frozenset(
    {"evidence_id", "kind", "as_of", "fingerprint"}
)
_BASELINE_FIELDS = frozenset(
    {
        "strategy_lab_candidate_id",
        "settings_fingerprint",
        "config_context_fingerprint",
        "settings",
        "metrics",
    }
)
_BASELINE_METRIC_FIELDS = frozenset(
    {
        "start",
        "end",
        "total_return",
        "cagr",
        "sharpe",
        "max_drawdown",
        "turnover",
        "transaction_costs",
    }
)
_EXPERIMENT_FIELDS = frozenset(
    {
        "design",
        "proposed_changes",
        "candidate_settings_fingerprint",
        "minimum_sessions",
        "holdout_fraction",
        "rolling_folds",
        "cost_multipliers",
        "sensitivity_fraction",
        "tests",
        "multiple_testing",
    }
)
_MULTIPLE_TESTING_FIELDS = frozenset(
    {"family_id", "maximum_hypotheses", "alpha", "correction"}
)
_QUALITY_FIELDS = frozenset(
    {
        "testability",
        "falsifiability",
        "parsimony",
        "explanatory_power",
        "scope",
        "consistency",
        "novelty",
        "distinguishable",
        "limitations",
    }
)
_TESTS = (
    "same_snapshot_baseline",
    "holdout",
    "rolling_out_of_sample",
    "cost_stress",
    "parameter_sensitivity",
    "independent_replication",
)
_OPPOSITE = {">=": "<", "<=": ">"}


def finalize_record(draft: Mapping[str, Any]) -> dict[str, Any]:
    record = _json_clone(draft)
    if not isinstance(record, dict):
        raise ValueError("Hypothesis record must be an object")
    if "design_fingerprint" in record or "record_fingerprint" in record:
        raise ValueError("Hypothesis fingerprints are assigned by the schema")
    record["design_fingerprint"] = design_fingerprint(record)
    record["record_fingerprint"] = None
    record["record_fingerprint"] = record_fingerprint(record)
    validate_record(record)
    return record


def validate_record(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != TOP_LEVEL_FIELDS:
        raise ValueError("Hypothesis top-level schema fields are invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Hypothesis schema version is invalid")
    if value.get("engine_version") != ENGINE_VERSION:
        raise ValueError("Hypothesis engine version is invalid")
    if value.get("template_version") != TEMPLATE_VERSION:
        raise ValueError("Hypothesis template version is invalid")
    _identifier(value.get("hypothesis_id"), HYPOTHESIS_ID, "hypothesis_id")
    _identifier(value.get("owner"), FINGERPRINT, "owner")
    _timestamp(value.get("created_at"), "created_at")
    _text(value.get("title"), "title", 120)
    _text(value.get("observation"), "observation", 2_000)
    _text(value.get("mechanism"), "mechanism", 2_000)

    source = _object(value.get("source"), _SOURCE_FIELDS, "source")
    if source.get("kind") != "local_deterministic":
        raise ValueError("Hypothesis source kind is invalid")
    if source.get("objective") not in OBJECTIVES:
        raise ValueError("Hypothesis source objective is invalid")
    _text(source.get("selection_reason"), "selection_reason", 500)
    if source.get("model_used") is not False:
        raise ValueError("Local deterministic hypothesis cannot claim a model call")

    scope = _object(value.get("scope"), _SCOPE_FIELDS, "scope")
    _text(scope.get("universe"), "scope.universe", 200)
    _string_list(scope.get("instrument_types"), "instrument_types", 1, 30, 80)
    _iso_date(scope.get("start"), "scope.start")
    _iso_date(scope.get("end"), "scope.end")
    if str(scope["start"]) > str(scope["end"]):
        raise ValueError("Hypothesis scope start is after end")
    _text(scope.get("regime"), "scope.regime", 500)
    _string_list(scope.get("exclusions"), "exclusions", 1, 20, 300)

    _string_list(value.get("assumptions"), "assumptions", 2, 12, 500)
    predictions = _predictions(value.get("predictions"))
    _falsification(value.get("falsification_criteria"), predictions)
    _explanations(value.get("competing_explanations"))
    _confounds(value.get("confounds"))
    _validate_evidence(value.get("evidence"))
    _validate_baseline(value.get("baseline"))
    _validate_experiment(value.get("experiment_plan"))
    _validate_quality(value.get("quality_assessment"))

    if value.get("safety") != SAFETY:
        raise ValueError("Hypothesis safety contract is invalid")
    _identifier(value.get("design_fingerprint"), FINGERPRINT, "design_fingerprint")
    _identifier(value.get("record_fingerprint"), FINGERPRINT, "record_fingerprint")
    if value["design_fingerprint"] != design_fingerprint(value):
        raise ValueError("Hypothesis design fingerprint does not match content")
    if value["record_fingerprint"] != record_fingerprint(value):
        raise ValueError("Hypothesis record fingerprint does not match content")


def design_fingerprint(value: Mapping[str, Any]) -> str:
    body = {
        key: _json_clone(value[key])
        for key in (
            "schema_version",
            "engine_version",
            "template_version",
            "source",
            "observation",
            "mechanism",
            "scope",
            "assumptions",
            "predictions",
            "falsification_criteria",
            "competing_explanations",
            "confounds",
            "evidence",
            "baseline",
            "experiment_plan",
            "quality_assessment",
            "safety",
        )
        if key in value
    }
    return json_fingerprint(body)


def record_fingerprint(value: Mapping[str, Any]) -> str:
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


def _predictions(value: Any) -> dict[str, Mapping[str, Any]]:
    if not isinstance(value, list) or not 3 <= len(value) <= 8:
        raise ValueError("Hypothesis predictions must contain 3 to 8 items")
    output: dict[str, Mapping[str, Any]] = {}
    for item in value:
        prediction = _object(item, _PREDICTION_FIELDS, "prediction")
        prediction_id = _short_id(prediction.get("prediction_id"), "pred")
        if prediction_id in output:
            raise ValueError("Hypothesis prediction identifiers must be unique")
        if prediction.get("metric") not in PREDICTION_METRICS:
            raise ValueError("Hypothesis prediction metric is invalid")
        if prediction.get("operator") not in OPERATORS:
            raise ValueError("Hypothesis prediction operator is invalid")
        _finite(prediction.get("threshold"), "prediction.threshold")
        _finite(prediction.get("baseline"), "prediction.baseline")
        if prediction.get("window") not in WINDOWS:
            raise ValueError("Hypothesis prediction window is invalid")
        _text(prediction.get("rationale"), "prediction.rationale", 500)
        output[prediction_id] = prediction
    return output


def _falsification(value: Any, predictions: Mapping[str, Mapping[str, Any]]) -> None:
    if not isinstance(value, list) or len(value) != len(predictions):
        raise ValueError("Each prediction must have one falsification criterion")
    seen: set[str] = set()
    linked: set[str] = set()
    for item in value:
        criterion = _object(item, _FALSIFICATION_FIELDS, "falsification criterion")
        criterion_id = _short_id(criterion.get("criterion_id"), "fals")
        if criterion_id in seen:
            raise ValueError("Falsification criterion identifiers must be unique")
        seen.add(criterion_id)
        prediction_id = criterion.get("prediction_id")
        prediction = predictions.get(str(prediction_id))
        if prediction is None or prediction_id in linked:
            raise ValueError("Falsification criterion prediction binding is invalid")
        linked.add(str(prediction_id))
        if (
            criterion.get("metric") != prediction.get("metric")
            or criterion.get("threshold") != prediction.get("threshold")
            or criterion.get("window") != prediction.get("window")
            or criterion.get("operator") != _OPPOSITE[prediction["operator"]]
        ):
            raise ValueError("Falsification criterion is not the prediction's negation")


def _explanations(value: Any) -> None:
    if not isinstance(value, list) or not 2 <= len(value) <= 4:
        raise ValueError("Hypothesis requires 2 to 4 competing explanations")
    identifiers: set[str] = set()
    for item in value:
        explanation = _object(item, _EXPLANATION_FIELDS, "competing explanation")
        identifier = _short_id(explanation.get("explanation_id"), "alt")
        if identifier in identifiers:
            raise ValueError("Competing explanation identifiers must be unique")
        identifiers.add(identifier)
        _text(explanation.get("statement"), "explanation.statement", 700)
        _text(
            explanation.get("distinguishing_test"),
            "explanation.distinguishing_test",
            700,
        )


def _confounds(value: Any) -> None:
    if not isinstance(value, list) or not 3 <= len(value) <= 8:
        raise ValueError("Hypothesis requires 3 to 8 confound controls")
    identifiers: set[str] = set()
    for item in value:
        confound = _object(item, _CONFOUND_FIELDS, "confound")
        identifier = _short_id(confound.get("confound_id"), "conf")
        if identifier in identifiers:
            raise ValueError("Confound identifiers must be unique")
        identifiers.add(identifier)
        _text(confound.get("risk"), "confound.risk", 700)
        _text(confound.get("control"), "confound.control", 700)


def _validate_evidence(value: Any) -> None:
    evidence = _object(value, _EVIDENCE_FIELDS, "evidence")
    snapshot = _object(evidence.get("snapshot"), _SNAPSHOT_FIELDS, "snapshot")
    _identifier(snapshot.get("snapshot_id"), EVIDENCE_ID, "snapshot_id")
    if snapshot.get("kind") != "market_cache":
        raise ValueError("Hypothesis snapshot kind is invalid")
    _iso_date(snapshot.get("as_of"), "snapshot.as_of")
    _text(snapshot.get("provider"), "snapshot.provider", 120)
    _identifier(snapshot.get("fingerprint"), FINGERPRINT, "snapshot.fingerprint")
    references = evidence.get("references")
    if not isinstance(references, list) or not 1 <= len(references) <= 250:
        raise ValueError("Hypothesis evidence references must contain 1 to 250 items")
    identifiers: set[str] = set()
    for item in references:
        reference = _object(item, _REFERENCE_FIELDS, "evidence reference")
        identifier = _identifier(
            reference.get("evidence_id"), EVIDENCE_ID, "evidence_id"
        )
        if identifier in identifiers:
            raise ValueError("Hypothesis evidence identifiers must be unique")
        identifiers.add(identifier)
        if reference.get("kind") not in {
            "market_snapshot",
            "daily_bars",
            "cache_manifest",
            "security_master",
        }:
            raise ValueError("Hypothesis evidence kind is invalid")
        _iso_date(reference.get("as_of"), "evidence.as_of")
        _identifier(reference.get("fingerprint"), FINGERPRINT, "evidence.fingerprint")


def _validate_baseline(value: Any) -> None:
    baseline = _object(value, _BASELINE_FIELDS, "baseline")
    candidate_id = baseline.get("strategy_lab_candidate_id")
    if candidate_id is not None:
        _identifier(candidate_id, CANDIDATE_ID, "strategy_lab_candidate_id")
    _identifier(
        baseline.get("settings_fingerprint"), FINGERPRINT, "settings_fingerprint"
    )
    _identifier(
        baseline.get("config_context_fingerprint"),
        FINGERPRINT,
        "config_context_fingerprint",
    )
    settings = baseline.get("settings")
    if (
        not isinstance(settings, Mapping)
        or set(settings) != {"strategy", "risk"}
        or not isinstance(settings.get("strategy"), Mapping)
        or not isinstance(settings.get("risk"), Mapping)
    ):
        raise ValueError("Hypothesis baseline settings are invalid")
    metrics = _object(baseline.get("metrics"), _BASELINE_METRIC_FIELDS, "metrics")
    _iso_date(metrics.get("start"), "metrics.start")
    _iso_date(metrics.get("end"), "metrics.end")
    for field in _BASELINE_METRIC_FIELDS - {"start", "end"}:
        _finite(metrics.get(field), f"metrics.{field}")


def _validate_experiment(value: Any) -> None:
    plan = _object(value, _EXPERIMENT_FIELDS, "experiment_plan")
    if plan.get("design") != "champion_challenger":
        raise ValueError("Hypothesis experiment design is invalid")
    changes = plan.get("proposed_changes")
    if (
        not isinstance(changes, Mapping)
        or set(changes) != {"strategy", "risk"}
        or not isinstance(changes.get("strategy"), Mapping)
        or not isinstance(changes.get("risk"), Mapping)
        or not any(changes[scope] for scope in ("strategy", "risk"))
    ):
        raise ValueError("Hypothesis proposed changes are invalid")
    _identifier(
        plan.get("candidate_settings_fingerprint"),
        FINGERPRINT,
        "candidate_settings_fingerprint",
    )
    minimum_sessions = plan.get("minimum_sessions")
    rolling_folds = plan.get("rolling_folds")
    if type(minimum_sessions) is not int or not 40 <= minimum_sessions <= 5_000:
        raise ValueError("Hypothesis minimum_sessions is invalid")
    if type(rolling_folds) is not int or not 2 <= rolling_folds <= 20:
        raise ValueError("Hypothesis rolling_folds is invalid")
    holdout = _finite(plan.get("holdout_fraction"), "holdout_fraction")
    sensitivity = _finite(plan.get("sensitivity_fraction"), "sensitivity_fraction")
    if not 0.1 <= holdout <= 0.5 or not 0.01 <= sensitivity <= 0.5:
        raise ValueError("Hypothesis experiment fractions are invalid")
    multipliers = plan.get("cost_multipliers")
    if (
        not isinstance(multipliers, list)
        or not 2 <= len(multipliers) <= 5
        or any(not 1.0 <= _finite(item, "cost_multiplier") <= 10.0 for item in multipliers)
        or multipliers != sorted(set(multipliers))
    ):
        raise ValueError("Hypothesis cost multipliers are invalid")
    if plan.get("tests") != list(_TESTS):
        raise ValueError("Hypothesis experiment tests are invalid")
    testing = _object(
        plan.get("multiple_testing"),
        _MULTIPLE_TESTING_FIELDS,
        "multiple_testing",
    )
    _identifier(testing.get("family_id"), EVIDENCE_ID, "family_id")
    if testing.get("maximum_hypotheses") != 3:
        raise ValueError("Hypothesis family budget must be three")
    alpha = _finite(testing.get("alpha"), "multiple_testing.alpha")
    if not 0 < alpha <= 0.05 or testing.get("correction") != "holm":
        raise ValueError("Hypothesis multiple-testing policy is invalid")


def _validate_quality(value: Any) -> None:
    quality = _object(value, _QUALITY_FIELDS, "quality_assessment")
    for field in (
        "testability",
        "falsifiability",
        "parsimony",
        "explanatory_power",
        "scope",
        "consistency",
        "novelty",
    ):
        if quality.get(field) not in QUALITY_LEVELS:
            raise ValueError(f"Hypothesis quality field {field} is invalid")
    if quality.get("distinguishable") is not True:
        raise ValueError("Hypothesis must be distinguishable from alternatives")
    _string_list(quality.get("limitations"), "quality limitations", 1, 8, 500)


def _object(value: Any, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"Hypothesis {label} schema fields are invalid")
    return value


def _identifier(value: Any, pattern: re.Pattern[str], field: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"Hypothesis {field} is invalid")
    return value


def _short_id(value: Any, prefix: str) -> str:
    pattern = re.compile(rf"{re.escape(prefix)}_[0-9]{{2}}\Z")
    return _identifier(value, pattern, prefix)


def _text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"Hypothesis {field} must contain 1 to {maximum} characters")
    return value


def _string_list(
    value: Any,
    field: str,
    minimum: int,
    maximum: int,
    maximum_length: int,
) -> None:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"Hypothesis {field} item count is invalid")
    if any(not isinstance(item, str) for item in value):
        raise ValueError(f"Hypothesis {field} items must be text")
    if len(value) != len(set(value)):
        raise ValueError(f"Hypothesis {field} items must be unique")
    for item in value:
        _text(item, field, maximum_length)


def _finite(value: Any, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"Hypothesis {field} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"Hypothesis {field} must be finite")
    return parsed


def _timestamp(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"Hypothesis {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Hypothesis {field} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Hypothesis {field} must include a timezone")


def _iso_date(value: Any, field: str) -> None:
    if not isinstance(value, str):
        raise ValueError(f"Hypothesis {field} is invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Hypothesis {field} must use YYYY-MM-DD") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError(f"Hypothesis {field} must use YYYY-MM-DD")


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


__all__ = [
    "ENGINE_VERSION",
    "FINGERPRINT",
    "HYPOTHESIS_ID",
    "OBJECTIVES",
    "SAFETY",
    "SCHEMA_VERSION",
    "TEMPLATE_VERSION",
    "TOP_LEVEL_FIELDS",
    "design_fingerprint",
    "finalize_record",
    "json_fingerprint",
    "record_fingerprint",
    "validate_record",
]
