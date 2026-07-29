from __future__ import annotations

from datetime import datetime
import json
import math
from pathlib import Path
import re
import statistics
from typing import Any, List, Mapping, Sequence

from ..data.evidence_io import atomic_create_json, evidence_store_lock
from ..feature_store.labels import validate_label_snapshot
from ..feature_store.schema import (
    FINGERPRINT,
    json_fingerprint,
    validate_feature_snapshot,
)
from ..json_utils import load_unique_json
from ..numeric import sample_standard_deviation
from .engine import _feature_stats, _fit_ridge
from .library import model_definition
from .schema import validate_evaluation


ARTIFACT_SCHEMA_VERSION = 1
ARTIFACT_ENGINE_VERSION = 1
MODEL_ARTIFACT_ID = re.compile(r"ma_[0-9a-f]{32}\Z")
MAX_MODEL_ARTIFACT_BYTES = 2 * 1024 * 1024
MAX_MODEL_ARTIFACTS = 1_000
ARTIFACT_SAFETY = {
    "research_only": True,
    "qualified_evidence_required": True,
    "creates_no_order": True,
    "may_trade": False,
}

_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "engine_version",
        "model_artifact_id",
        "created_at",
        "model",
        "evaluation",
        "feature_set",
        "training",
        "parameters",
        "safety",
        "artifact_fingerprint",
        "record_fingerprint",
    }
)
_MODEL_FIELDS = frozenset(
    {"model_id", "version", "kind", "hyperparameters"}
)
_EVALUATION_FIELDS = frozenset(
    {
        "evaluation_id",
        "record_fingerprint",
        "schema_version",
        "engine_version",
        "model_id",
        "factor_ids",
        "horizon",
        "as_of_session",
        "snapshot_fingerprint",
        "mean_ic",
        "model_minus_best_factor_ic",
        "model_adjusted_p_value",
        "comparison_adjusted_p_value",
        "qualified",
    }
)
_FEATURE_SET_FIELDS = frozenset(
    {"feature_set_id", "fingerprint", "factor_ids"}
)
_TRAINING_FIELDS = frozenset(
    {
        "start_session",
        "end_session",
        "knowledge_cutoff",
        "horizon",
        "observations",
        "feature_snapshots",
        "label_snapshots",
        "evidence_fingerprint",
    }
)
_PARAMETER_FIELDS = frozenset(
    {"feature_means", "feature_stds", "coefficients", "residual_std"}
)


class ModelArtifactStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    @property
    def artifacts_root(self) -> Path:
        return self.root / "artifacts"

    def publish(self, record: dict[str, Any]) -> dict[str, Any]:
        validate_model_artifact(record)
        artifact_id = str(record["model_artifact_id"])
        target = self.artifacts_root / f"{artifact_id}.json"
        with evidence_store_lock(self.root, "Model artifact"):
            paths = self._paths(missing_ok=True)
            if target.exists() or target.is_symlink():
                existing = self._read(target)
                if existing["artifact_fingerprint"] != record["artifact_fingerprint"]:
                    raise RuntimeError("Model artifact id collision")
                result = _clone(existing)
                result["reused"] = True
                return result
            if len(paths) >= MAX_MODEL_ARTIFACTS:
                raise RuntimeError("Model artifact capacity reached")
            atomic_create_json(
                self.root,
                target,
                record,
                label="model artifact",
                maximum_bytes=MAX_MODEL_ARTIFACT_BYTES,
            )
        result = self.get(artifact_id)
        result["reused"] = False
        return result

    def get(self, artifact_id: str) -> dict[str, Any]:
        _artifact_id(artifact_id)
        path = self.artifacts_root / f"{artifact_id}.json"
        if path.is_symlink() or not path.is_file():
            raise KeyError(artifact_id)
        return self._read(path)

    def list(self) -> List[dict[str, Any]]:
        return [self._read(path) for path in self._paths(missing_ok=True)]

    def _paths(self, *, missing_ok: bool) -> List[Path]:
        root = self.artifacts_root
        if not root.exists():
            if missing_ok:
                return []
            raise RuntimeError("Model artifact root is unavailable")
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError("Model artifact root is invalid")
        paths: list[Path] = []
        for path in root.iterdir():
            if (
                path.is_symlink()
                or not path.is_file()
                or path.suffix != ".json"
                or MODEL_ARTIFACT_ID.fullmatch(path.stem) is None
            ):
                raise RuntimeError("Unexpected model artifact store member")
            paths.append(path)
        if len(paths) > MAX_MODEL_ARTIFACTS:
            raise RuntimeError("Model artifact store exceeds capacity")
        return sorted(paths, key=lambda item: item.name)

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            value = load_unique_json(path, max_bytes=MAX_MODEL_ARTIFACT_BYTES)
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeError(f"Invalid model artifact {path.name}: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Model artifact must be an object")
        try:
            validate_model_artifact(value)
        except ValueError as exc:
            raise RuntimeError(f"Invalid model artifact {path.name}: {exc}") from exc
        if value["model_artifact_id"] != path.stem:
            raise RuntimeError("Model artifact id does not match its file name")
        return value


def evaluation_binding(evaluation: Mapping[str, Any]) -> dict[str, Any]:
    record = _without_reused(evaluation)
    validate_evaluation(record)
    if record.get("schema_version") != 2:
        raise ValueError("Model artifacts require a current v2 model evaluation")
    results = record["results"]
    statistical = results["statistical_validation"]
    model_test = statistical["model_ic"]
    comparison = next(
        item
        for item in statistical["factor_comparisons"]
        if item["factor_id"] == results["best_factor_id"]
    )["validation"]
    qualified = all(
        (
            bool(model_test["reject_null"]),
            bool(comparison["reject_null"]),
            float(model_test["ci_low"]) > 0,
            float(comparison["ci_low"]) > 0,
            int(model_test["positive_subperiods"]) == 3,
            int(comparison["positive_subperiods"]) == 3,
        )
    )
    if not qualified:
        raise ValueError(
            "Model artifact requires significant, positive, stable evidence "
            "against the best factor"
        )
    return {
        "evaluation_id": record["evaluation_id"],
        "record_fingerprint": record["record_fingerprint"],
        "schema_version": record["schema_version"],
        "engine_version": record["engine_version"],
        "model_id": record["model"]["model_id"],
        "factor_ids": [item["factor_id"] for item in record["factors"]],
        "horizon": record["parameters"]["horizon"],
        "as_of_session": record["evidence"]["snapshot"]["as_of"],
        "snapshot_fingerprint": record["evidence"]["snapshot"]["fingerprint"],
        "mean_ic": results["model"]["mean_ic"],
        "model_minus_best_factor_ic": results["model_minus_best_factor_ic"],
        "model_adjusted_p_value": model_test["adjusted_p_value"],
        "comparison_adjusted_p_value": comparison["adjusted_p_value"],
        "qualified": True,
    }


def fit_linear_artifact(
    pairs: Sequence[Mapping[str, Any]],
    *,
    model_id: str,
    evaluation: Mapping[str, Any],
    store: ModelArtifactStore | None = None,
) -> dict[str, Any]:
    """Fit an inference-complete ridge or directional factor-mean artifact."""

    definition = model_definition(model_id)
    if definition.kind not in {"ridge", "factor_mean"}:
        raise ValueError("Only inference-complete linear models can be artifacted")
    binding = evaluation_binding(evaluation)
    clean_pairs = _validated_pairs(pairs)
    first_feature = clean_pairs[0]["feature"]
    factor_ids = [
        str(item["factor_id"]) for item in first_feature["feature_set"]["factors"]
    ]
    rows: list[tuple[list[float], float]] = []
    feature_ids: list[str] = []
    label_ids: list[str] = []
    sessions: list[str] = []
    horizons: set[int] = set()
    cutoffs: list[datetime] = []
    for pair in clean_pairs:
        feature = pair["feature"]
        label = pair["label"]
        if feature["feature_set"]["fingerprint"] != first_feature["feature_set"]["fingerprint"]:
            raise ValueError("Training feature snapshots use different feature sets")
        feature_rows = {item["symbol"]: item for item in feature["rows"]}
        valid_labels = [
            float(item["forward_return"])
            for item in label["rows"]
            if item["forward_return"] is not None
        ]
        if len(valid_labels) < 2:
            continue
        date_mean = statistics.fmean(valid_labels)
        for label_row in label["rows"]:
            symbol = str(label_row["symbol"])
            feature_row = feature_rows.get(symbol)
            if feature_row is None or label_row["forward_return"] is None:
                continue
            values = feature_row["values"]
            if any(factor_id not in values for factor_id in factor_ids):
                continue
            rows.append(
                (
                    [float(values[factor_id]) for factor_id in factor_ids],
                    float(label_row["forward_return"]) - date_mean,
                )
            )
        feature_ids.append(str(feature["snapshot_id"]))
        label_ids.append(str(label["label_snapshot_id"]))
        sessions.append(str(feature["as_of_session"]))
        horizons.add(int(label["horizon"]))
        cutoffs.append(_timestamp(label["realized_at"], "label.realized_at"))
    if len(rows) < max(20, len(factor_ids) * 4):
        raise ValueError("Model artifact training evidence is too small")
    if len(horizons) != 1:
        raise ValueError("Model artifact labels must use one horizon")
    means, stds = _feature_stats(rows, len(factor_ids))
    if definition.kind == "ridge":
        coefficients = _fit_ridge(
            rows,
            means,
            stds,
            float(definition.hyperparameters["lambda"]),
        )
    else:
        directions = {
            str(item["factor_id"]): int(item["direction"])
            for item in first_feature["feature_set"]["factors"]
        }
        coefficients = [directions[factor_id] / len(factor_ids) for factor_id in factor_ids]
    residuals = []
    for raw_values, target in rows:
        standardized = [
            (raw_values[index] - means[index]) / stds[index]
            if stds[index] > 0
            else 0.0
            for index in range(len(factor_ids))
        ]
        prediction = sum(
            coefficients[index] * standardized[index]
            for index in range(len(factor_ids))
        )
        residuals.append(target - prediction)
    residual_std = (
        sample_standard_deviation(residuals) if len(residuals) > 1 else 0.0
    )
    training_evidence = {
        "feature_snapshots": feature_ids,
        "label_snapshots": label_ids,
        "observations": len(rows),
        "horizon": next(iter(horizons)),
    }
    record = finalize_model_artifact(
        {
            "schema_version": ARTIFACT_SCHEMA_VERSION,
            "engine_version": ARTIFACT_ENGINE_VERSION,
            "created_at": _utc_now(),
            "model": {
                "model_id": definition.model_id,
                "version": definition.version,
                "kind": definition.kind,
                "hyperparameters": dict(definition.hyperparameters),
            },
            "evaluation": binding,
            "feature_set": {
                "feature_set_id": first_feature["feature_set"]["feature_set_id"],
                "fingerprint": first_feature["feature_set"]["fingerprint"],
                "factor_ids": factor_ids,
            },
            "training": {
                "start_session": min(sessions),
                "end_session": max(sessions),
                "knowledge_cutoff": max(cutoffs).isoformat(),
                "horizon": next(iter(horizons)),
                "observations": len(rows),
                "feature_snapshots": feature_ids,
                "label_snapshots": label_ids,
                "evidence_fingerprint": json_fingerprint(training_evidence),
            },
            "parameters": {
                "feature_means": means,
                "feature_stds": stds,
                "coefficients": coefficients,
                "residual_std": residual_std,
            },
            "safety": dict(ARTIFACT_SAFETY),
        }
    )
    return store.publish(record) if store is not None else record


def finalize_model_artifact(draft: Mapping[str, Any]) -> dict[str, Any]:
    record = _clone(draft)
    forbidden = {"model_artifact_id", "artifact_fingerprint", "record_fingerprint"}
    if not isinstance(record, dict) or forbidden & set(record):
        raise ValueError("Model artifact identifiers are assigned by the schema")
    record["model_artifact_id"] = None
    record["artifact_fingerprint"] = None
    record["record_fingerprint"] = None
    fingerprint = model_artifact_fingerprint(record)
    record["model_artifact_id"] = "ma_" + fingerprint[:32]
    record["artifact_fingerprint"] = fingerprint
    record["record_fingerprint"] = model_artifact_record_fingerprint(record)
    validate_model_artifact(record)
    return record


def validate_model_artifact(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != _TOP_FIELDS:
        raise ValueError("Model artifact top-level fields are invalid")
    if value.get("schema_version") != ARTIFACT_SCHEMA_VERSION or value.get("engine_version") != ARTIFACT_ENGINE_VERSION:
        raise ValueError("Model artifact version is invalid")
    artifact_id = _artifact_id(value.get("model_artifact_id"))
    created_at = _timestamp(value.get("created_at"), "created_at")
    model = _object(value.get("model"), _MODEL_FIELDS, "model")
    definition = model_definition(str(model.get("model_id")))
    if (
        model.get("version") != definition.version
        or model.get("kind") != definition.kind
        or model.get("hyperparameters") != definition.hyperparameters
        or definition.kind not in {"ridge", "factor_mean"}
    ):
        raise ValueError("Model artifact model binding is invalid")
    evaluation = _object(value.get("evaluation"), _EVALUATION_FIELDS, "evaluation")
    if evaluation.get("qualified") is not True:
        raise ValueError("Model artifact evaluation is not qualified")
    if evaluation.get("model_id") != model.get("model_id"):
        raise ValueError("Model artifact evaluation model binding is invalid")
    if not re.fullmatch(r"mdl_[0-9a-f]{32}", str(evaluation.get("evaluation_id"))):
        raise ValueError("Model artifact evaluation id is invalid")
    _fingerprint(evaluation.get("record_fingerprint"), "evaluation fingerprint")
    _fingerprint(
        evaluation.get("snapshot_fingerprint"), "evaluation snapshot fingerprint"
    )
    evaluation_as_of = _iso_date(
        evaluation.get("as_of_session"), "evaluation.as_of_session"
    )
    if type(evaluation.get("horizon")) is not int or not 1 <= evaluation["horizon"] <= 250:
        raise ValueError("Model artifact evaluation horizon is invalid")
    for field in (
        "mean_ic",
        "model_minus_best_factor_ic",
        "model_adjusted_p_value",
        "comparison_adjusted_p_value",
    ):
        _finite(evaluation.get(field), f"evaluation.{field}")
    feature_set = _object(value.get("feature_set"), _FEATURE_SET_FIELDS, "feature_set")
    _text(feature_set.get("feature_set_id"), "feature_set_id", 160)
    _fingerprint(feature_set.get("fingerprint"), "feature_set fingerprint")
    factor_ids = feature_set.get("factor_ids")
    if (
        not isinstance(factor_ids, list)
        or not factor_ids
        or factor_ids != list(dict.fromkeys(factor_ids))
        or any(not isinstance(item, str) or not item for item in factor_ids)
    ):
        raise ValueError("Model artifact factor ids are invalid")
    if evaluation.get("factor_ids") != factor_ids:
        raise ValueError("Model artifact evaluation factor binding is invalid")
    training = _object(value.get("training"), _TRAINING_FIELDS, "training")
    start = _iso_date(training.get("start_session"), "training.start_session")
    end = _iso_date(training.get("end_session"), "training.end_session")
    if start > end:
        raise ValueError("Model artifact training dates are inconsistent")
    knowledge_cutoff = _timestamp(
        training.get("knowledge_cutoff"), "training.knowledge_cutoff"
    )
    if knowledge_cutoff > created_at:
        raise ValueError("Model artifact predates its training knowledge cutoff")
    if type(training.get("horizon")) is not int or not 1 <= training["horizon"] <= 250:
        raise ValueError("Model artifact horizon is invalid")
    if training["horizon"] != evaluation["horizon"]:
        raise ValueError("Model artifact evaluation horizon binding is invalid")
    if end > evaluation_as_of:
        raise ValueError("Model artifact training extends past its evaluation evidence")
    observations = training.get("observations")
    if type(observations) is not int or observations < 20:
        raise ValueError("Model artifact observations are invalid")
    feature_ids = training.get("feature_snapshots")
    label_ids = training.get("label_snapshots")
    if (
        not isinstance(feature_ids, list)
        or not isinstance(label_ids, list)
        or not feature_ids
        or len(feature_ids) != len(label_ids)
        or any(re.fullmatch(r"fs_[0-9a-f]{32}", str(item)) is None for item in feature_ids)
        or any(re.fullmatch(r"ls_[0-9a-f]{32}", str(item)) is None for item in label_ids)
    ):
        raise ValueError("Model artifact training snapshot bindings are invalid")
    expected_evidence = json_fingerprint(
        {
            "feature_snapshots": feature_ids,
            "label_snapshots": label_ids,
            "observations": observations,
            "horizon": training["horizon"],
        }
    )
    if training.get("evidence_fingerprint") != expected_evidence:
        raise ValueError("Model artifact training fingerprint is invalid")
    parameters = _object(value.get("parameters"), _PARAMETER_FIELDS, "parameters")
    for field in ("feature_means", "feature_stds", "coefficients"):
        items = parameters.get(field)
        if (
            not isinstance(items, list)
            or len(items) != len(factor_ids)
            or any(not _is_finite(item) for item in items)
        ):
            raise ValueError(f"Model artifact {field} is invalid")
    if any(float(item) < 0 for item in parameters["feature_stds"]):
        raise ValueError("Model artifact feature stds are invalid")
    if _finite(parameters.get("residual_std"), "residual_std") < 0:
        raise ValueError("Model artifact residual std is invalid")
    if value.get("safety") != ARTIFACT_SAFETY:
        raise ValueError("Model artifact safety boundary is invalid")
    fingerprint = _fingerprint(value.get("artifact_fingerprint"), "artifact fingerprint")
    if artifact_id != "ma_" + fingerprint[:32] or fingerprint != model_artifact_fingerprint(value):
        raise ValueError("Model artifact fingerprint is inconsistent")
    if value.get("record_fingerprint") != model_artifact_record_fingerprint(value):
        raise ValueError("Model artifact record fingerprint is inconsistent")


def model_artifact_fingerprint(value: Mapping[str, Any]) -> str:
    return json_fingerprint(
        {
            key: value.get(key)
            for key in (
                "schema_version",
                "engine_version",
                "model",
                "evaluation",
                "feature_set",
                "training",
                "parameters",
                "safety",
            )
        }
    )


def model_artifact_record_fingerprint(value: Mapping[str, Any]) -> str:
    body = _clone(value)
    body["record_fingerprint"] = None
    body.pop("reused", None)
    return json_fingerprint(body)


def _validated_pairs(pairs: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if not isinstance(pairs, (list, tuple)) or not pairs:
        raise ValueError("Model artifact training pairs are required")
    result = []
    for pair in pairs:
        if not isinstance(pair, Mapping) or set(pair) != {"feature", "label"}:
            raise ValueError("Model artifact training pair is invalid")
        feature = _without_reused(pair["feature"])
        label = _without_reused(pair["label"])
        validate_feature_snapshot(feature)
        validate_label_snapshot(label)
        if (
            label["feature_snapshot_id"] != feature["snapshot_id"]
            or label["feature_snapshot_fingerprint"] != feature["snapshot_fingerprint"]
        ):
            raise ValueError("Model artifact label is not bound to its feature")
        result.append({"feature": feature, "label": label})
    result.sort(key=lambda item: str(item["feature"]["as_of_session"]))
    return result


def _without_reused(value: Any) -> dict[str, Any]:
    result = _clone(value)
    if not isinstance(result, dict):
        raise ValueError("Evidence record must be an object")
    result.pop("reused", None)
    return result


def _artifact_id(value: object) -> str:
    if not isinstance(value, str) or MODEL_ARTIFACT_ID.fullmatch(value) is None:
        raise ValueError("Invalid model artifact id")
    return value


def _object(value: Any, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"Model artifact {label} fields are invalid")
    return value


def _fingerprint(value: object, label: str) -> str:
    if not isinstance(value, str) or FINGERPRINT.fullmatch(value) is None:
        raise ValueError(f"Model artifact {label} is invalid")
    return value


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"Model artifact {label} is invalid")
    return value


def _iso_date(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"Model artifact {label} is invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError(f"Model artifact {label} is invalid") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError(f"Model artifact {label} is invalid")
    return value


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Model artifact {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Model artifact {label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Model artifact {label} must include a timezone")
    return parsed


def _finite(value: object, label: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
    ):
        raise ValueError(f"Model artifact {label} is invalid")
    return float(value)


def _is_finite(value: object) -> bool:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value)


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


def _utc_now() -> str:
    from datetime import timezone

    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "ARTIFACT_ENGINE_VERSION",
    "ARTIFACT_SCHEMA_VERSION",
    "MODEL_ARTIFACT_ID",
    "ModelArtifactStore",
    "evaluation_binding",
    "finalize_model_artifact",
    "fit_linear_artifact",
    "model_artifact_fingerprint",
    "model_artifact_record_fingerprint",
    "validate_model_artifact",
]
