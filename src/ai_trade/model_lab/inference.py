from __future__ import annotations

from datetime import date, datetime, timezone
import json
from typing import Any, Mapping, Sequence

from ..feature_store.schema import validate_feature_snapshot
from .artifact import validate_model_artifact
from .prediction_schema import (
    PREDICTION_ENGINE_VERSION,
    PREDICTION_SCHEMA_VERSION,
    PREDICTION_SAFETY,
    PredictionSnapshotStore,
    finalize_prediction_snapshot,
)


def predict_snapshot(
    artifact: Mapping[str, Any],
    feature_snapshot: Mapping[str, Any],
    *,
    valid_from_session: date,
    valid_until_session: date,
    trading_calendar: Sequence[date],
    store: PredictionSnapshotStore | None = None,
) -> dict[str, Any]:
    """Run an inference-complete artifact over one immutable feature snapshot."""

    artifact_record = _without_reused(artifact)
    feature_record = _without_reused(feature_snapshot)
    validate_model_artifact(artifact_record)
    validate_feature_snapshot(feature_record)
    if (
        artifact_record["feature_set"]["fingerprint"]
        != feature_record["feature_set"]["fingerprint"]
    ):
        raise ValueError("Model artifact and feature snapshot use different feature sets")
    as_of = date.fromisoformat(str(feature_record["as_of_session"]))
    training_end = date.fromisoformat(str(artifact_record["training"]["end_session"]))
    if as_of <= training_end:
        raise ValueError("Prediction feature session must be out of sample")
    allowed_sessions = _prediction_sessions(
        trading_calendar,
        after=as_of,
        count=int(artifact_record["training"]["horizon"]),
    )
    if (
        valid_from_session != allowed_sessions[0]
        or valid_until_session not in allowed_sessions
        or valid_until_session < valid_from_session
    ):
        raise ValueError("Prediction validity window is invalid")
    factor_ids = list(artifact_record["feature_set"]["factor_ids"])
    means = [float(item) for item in artifact_record["parameters"]["feature_means"]]
    stds = [float(item) for item in artifact_record["parameters"]["feature_stds"]]
    coefficients = [
        float(item) for item in artifact_record["parameters"]["coefficients"]
    ]
    uncertainty_bps = float(artifact_record["parameters"]["residual_std"]) * 10_000.0
    rows: list[dict[str, Any]] = []
    accepted: list[tuple[str, float]] = []
    for feature_row in feature_record["rows"]:
        symbol = str(feature_row["symbol"])
        values = feature_row["values"]
        missing = [factor_id for factor_id in factor_ids if factor_id not in values]
        if missing:
            rows.append(
                {
                    "symbol": symbol,
                    "score": None,
                    "expected_return_bps": None,
                    "uncertainty_bps": None,
                    "rank": None,
                    "rejection_reason": "missing_features:" + ",".join(missing),
                }
            )
            continue
        standardized = [
            (float(values[factor_id]) - means[index]) / stds[index]
            if stds[index] > 0
            else 0.0
            for index, factor_id in enumerate(factor_ids)
        ]
        score = sum(
            coefficients[index] * standardized[index]
            for index in range(len(factor_ids))
        )
        accepted.append((symbol, score))
        rows.append(
            {
                "symbol": symbol,
                "score": score,
                "expected_return_bps": score * 10_000.0,
                "uncertainty_bps": uncertainty_bps,
                "rank": None,
                "rejection_reason": None,
            }
        )
    ranks = {
        symbol: rank
        for rank, (symbol, _score) in enumerate(
            sorted(accepted, key=lambda item: (-item[1], item[0])), start=1
        )
    }
    for row in rows:
        if row["rejection_reason"] is None:
            row["rank"] = ranks[str(row["symbol"])]
    rows.sort(key=lambda item: str(item["symbol"]))
    record = finalize_prediction_snapshot(
        {
            "schema_version": PREDICTION_SCHEMA_VERSION,
            "engine_version": PREDICTION_ENGINE_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "model_artifact": {
                "model_artifact_id": artifact_record["model_artifact_id"],
                "artifact_fingerprint": artifact_record["artifact_fingerprint"],
                "record_fingerprint": artifact_record["record_fingerprint"],
            },
            "feature_snapshot": {
                "snapshot_id": feature_record["snapshot_id"],
                "snapshot_fingerprint": feature_record["snapshot_fingerprint"],
                "as_of_session": feature_record["as_of_session"],
                "knowledge_cutoff": feature_record["knowledge_cutoff"],
            },
            "horizon": artifact_record["training"]["horizon"],
            "valid_from_session": valid_from_session.isoformat(),
            "valid_until_session": valid_until_session.isoformat(),
            "rows": rows,
            "safety": dict(PREDICTION_SAFETY),
        }
    )
    return store.publish(record) if store is not None else record


def _without_reused(value: Mapping[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))
    if not isinstance(result, dict):
        raise ValueError("Evidence record must be an object")
    result.pop("reused", None)
    return result


def _prediction_sessions(
    trading_calendar: Sequence[date], *, after: date, count: int
) -> list[date]:
    if isinstance(trading_calendar, (str, bytes)):
        raise ValueError("Prediction trading calendar is invalid")
    sessions = sorted(set(trading_calendar))
    if (
        not sessions
        or any(not isinstance(item, date) for item in sessions)
        or isinstance(count, bool)
        or not 1 <= count <= 250
    ):
        raise ValueError("Prediction trading calendar is invalid")
    future = [item for item in sessions if item > after]
    if len(future) < count:
        raise ValueError(
            "Prediction trading calendar does not contain the full future horizon"
        )
    return future[:count]


__all__ = ["predict_snapshot"]
