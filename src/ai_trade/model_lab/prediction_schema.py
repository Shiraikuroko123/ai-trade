from __future__ import annotations

from datetime import date, datetime
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

from ..data.evidence_io import atomic_create_json, evidence_store_lock
from ..feature_store.schema import FINGERPRINT, SYMBOL, json_fingerprint
from ..json_utils import load_unique_json


PREDICTION_SCHEMA_VERSION = 1
PREDICTION_ENGINE_VERSION = 1
PREDICTION_SNAPSHOT_ID = re.compile(r"ps_[0-9a-f]{32}\Z")
MAX_PREDICTION_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_PREDICTIONS_PER_SESSION = 100
PREDICTION_SAFETY = {
    "research_only": True,
    "creates_no_order": True,
    "requires_portfolio_constraints": True,
    "may_trade": False,
}

_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "engine_version",
        "prediction_snapshot_id",
        "created_at",
        "model_artifact",
        "feature_snapshot",
        "horizon",
        "valid_from_session",
        "valid_until_session",
        "rows",
        "safety",
        "snapshot_fingerprint",
        "record_fingerprint",
    }
)
_ARTIFACT_FIELDS = frozenset(
    {"model_artifact_id", "artifact_fingerprint", "record_fingerprint"}
)
_FEATURE_FIELDS = frozenset(
    {"snapshot_id", "snapshot_fingerprint", "as_of_session", "knowledge_cutoff"}
)
_ROW_FIELDS = frozenset(
    {
        "symbol",
        "score",
        "expected_return_bps",
        "uncertainty_bps",
        "rank",
        "rejection_reason",
    }
)


class PredictionSnapshotStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    @property
    def predictions_root(self) -> Path:
        return self.root / "predictions"

    def publish(self, record: dict[str, Any]) -> dict[str, Any]:
        validate_prediction_snapshot(record)
        on_date = date.fromisoformat(str(record["feature_snapshot"]["as_of_session"]))
        prediction_id = str(record["prediction_snapshot_id"])
        target = self.predictions_root / on_date.isoformat() / f"{prediction_id}.json"
        with evidence_store_lock(self.root, "Prediction snapshot"):
            paths = self._paths(on_date, missing_ok=True)
            if target.exists() or target.is_symlink():
                existing = self._read(target)
                if existing["snapshot_fingerprint"] != record["snapshot_fingerprint"]:
                    raise RuntimeError("Prediction snapshot id collision")
                result = _clone(existing)
                result["reused"] = True
                return result
            if len(paths) >= MAX_PREDICTIONS_PER_SESSION:
                raise RuntimeError("Prediction snapshot session capacity reached")
            atomic_create_json(
                self.root,
                target,
                record,
                label="prediction snapshot",
                maximum_bytes=MAX_PREDICTION_SNAPSHOT_BYTES,
            )
        result = self.get(prediction_id, on_date=on_date)
        result["reused"] = False
        return result

    def get(self, prediction_id: str, *, on_date: date) -> dict[str, Any]:
        _prediction_id(prediction_id)
        path = self.predictions_root / on_date.isoformat() / f"{prediction_id}.json"
        if path.is_symlink() or not path.is_file():
            raise KeyError(prediction_id)
        return self._read(path)

    def _paths(self, on_date: date, *, missing_ok: bool) -> list[Path]:
        directory = self.predictions_root / on_date.isoformat()
        if not directory.exists():
            if missing_ok:
                return []
            raise RuntimeError("Prediction snapshot session is unavailable")
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("Prediction snapshot session is invalid")
        paths: list[Path] = []
        for path in directory.iterdir():
            if (
                path.is_symlink()
                or not path.is_file()
                or path.suffix != ".json"
                or PREDICTION_SNAPSHOT_ID.fullmatch(path.stem) is None
            ):
                raise RuntimeError("Unexpected prediction snapshot file")
            paths.append(path)
        if len(paths) > MAX_PREDICTIONS_PER_SESSION:
            raise RuntimeError("Prediction snapshot session exceeds capacity")
        return sorted(paths, key=lambda item: item.name)

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            value = load_unique_json(path, max_bytes=MAX_PREDICTION_SNAPSHOT_BYTES)
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeError(f"Invalid prediction snapshot {path.name}: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Prediction snapshot must be an object")
        try:
            validate_prediction_snapshot(value)
        except ValueError as exc:
            raise RuntimeError(f"Invalid prediction snapshot {path.name}: {exc}") from exc
        if value["prediction_snapshot_id"] != path.stem:
            raise RuntimeError("Prediction snapshot id does not match its file name")
        if value["feature_snapshot"]["as_of_session"] != path.parent.name:
            raise RuntimeError("Prediction snapshot date does not match its directory")
        return value


def finalize_prediction_snapshot(draft: Mapping[str, Any]) -> dict[str, Any]:
    record = _clone(draft)
    forbidden = {"prediction_snapshot_id", "snapshot_fingerprint", "record_fingerprint"}
    if not isinstance(record, dict) or forbidden & set(record):
        raise ValueError("Prediction snapshot identifiers are assigned by the schema")
    record["prediction_snapshot_id"] = None
    record["snapshot_fingerprint"] = None
    record["record_fingerprint"] = None
    fingerprint = prediction_snapshot_fingerprint(record)
    record["prediction_snapshot_id"] = "ps_" + fingerprint[:32]
    record["snapshot_fingerprint"] = fingerprint
    record["record_fingerprint"] = prediction_record_fingerprint(record)
    validate_prediction_snapshot(record)
    return record


def validate_prediction_snapshot(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != _TOP_FIELDS:
        raise ValueError("Prediction snapshot top-level fields are invalid")
    if value.get("schema_version") != PREDICTION_SCHEMA_VERSION or value.get("engine_version") != PREDICTION_ENGINE_VERSION:
        raise ValueError("Prediction snapshot version is invalid")
    prediction_id = _prediction_id(value.get("prediction_snapshot_id"))
    created_at = _timestamp(value.get("created_at"), "created_at")
    artifact = _object(value.get("model_artifact"), _ARTIFACT_FIELDS, "model_artifact")
    if re.fullmatch(r"ma_[0-9a-f]{32}", str(artifact.get("model_artifact_id"))) is None:
        raise ValueError("Prediction snapshot model artifact id is invalid")
    _fingerprint(artifact.get("artifact_fingerprint"), "artifact fingerprint")
    _fingerprint(artifact.get("record_fingerprint"), "artifact record fingerprint")
    feature = _object(value.get("feature_snapshot"), _FEATURE_FIELDS, "feature_snapshot")
    if re.fullmatch(r"fs_[0-9a-f]{32}", str(feature.get("snapshot_id"))) is None:
        raise ValueError("Prediction snapshot feature id is invalid")
    _fingerprint(feature.get("snapshot_fingerprint"), "feature fingerprint")
    as_of = _iso_date(feature.get("as_of_session"), "feature as_of_session")
    knowledge_cutoff = _timestamp(
        feature.get("knowledge_cutoff"), "feature knowledge_cutoff"
    )
    if knowledge_cutoff > created_at:
        raise ValueError("Prediction snapshot predates its feature knowledge cutoff")
    horizon = value.get("horizon")
    if type(horizon) is not int or not 1 <= horizon <= 250:
        raise ValueError("Prediction snapshot horizon is invalid")
    valid_from = _iso_date(value.get("valid_from_session"), "valid_from_session")
    valid_until = _iso_date(value.get("valid_until_session"), "valid_until_session")
    if (
        valid_from <= as_of
        or valid_until < valid_from
        or valid_from.weekday() >= 5
        or valid_until.weekday() >= 5
        or (valid_until - as_of).days > horizon * 2 + 10
    ):
        raise ValueError("Prediction snapshot validity window is invalid")
    rows = value.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Prediction snapshot rows are invalid")
    symbols: list[str] = []
    ranks: list[int] = []
    for item in rows:
        row = _object(item, _ROW_FIELDS, "row")
        symbol = _identifier(row.get("symbol"), SYMBOL, "row.symbol")
        symbols.append(symbol)
        reason = row.get("rejection_reason")
        numeric = [
            row.get("score"),
            row.get("expected_return_bps"),
            row.get("uncertainty_bps"),
        ]
        if reason is None:
            if any(not _is_finite(item) for item in numeric):
                raise ValueError("Prediction snapshot accepted row is invalid")
            rank = row.get("rank")
            if type(rank) is not int or rank < 1:
                raise ValueError("Prediction snapshot rank is invalid")
            if float(row["uncertainty_bps"]) < 0:
                raise ValueError("Prediction snapshot uncertainty is invalid")
            ranks.append(rank)
        else:
            _text(reason, "row.rejection_reason", 200)
            if any(item is not None for item in numeric) or row.get("rank") is not None:
                raise ValueError("Prediction snapshot rejected row contains a prediction")
    if symbols != sorted(symbols) or len(symbols) != len(set(symbols)):
        raise ValueError("Prediction snapshot rows are out of order")
    if sorted(ranks) != list(range(1, len(ranks) + 1)):
        raise ValueError("Prediction snapshot ranks are not contiguous")
    if value.get("safety") != PREDICTION_SAFETY:
        raise ValueError("Prediction snapshot safety boundary is invalid")
    fingerprint = _fingerprint(value.get("snapshot_fingerprint"), "snapshot fingerprint")
    if prediction_id != "ps_" + fingerprint[:32] or fingerprint != prediction_snapshot_fingerprint(value):
        raise ValueError("Prediction snapshot fingerprint is inconsistent")
    if value.get("record_fingerprint") != prediction_record_fingerprint(value):
        raise ValueError("Prediction snapshot record fingerprint is inconsistent")


def prediction_snapshot_fingerprint(value: Mapping[str, Any]) -> str:
    return json_fingerprint(
        {
            key: value.get(key)
            for key in (
                "schema_version",
                "engine_version",
                "model_artifact",
                "feature_snapshot",
                "horizon",
                "valid_from_session",
                "valid_until_session",
                "rows",
                "safety",
            )
        }
    )


def prediction_record_fingerprint(value: Mapping[str, Any]) -> str:
    body = _clone(value)
    body["record_fingerprint"] = None
    body.pop("reused", None)
    return json_fingerprint(body)


def _prediction_id(value: object) -> str:
    return _identifier(value, PREDICTION_SNAPSHOT_ID, "prediction_snapshot_id")


def _object(value: Any, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"Prediction snapshot {label} fields are invalid")
    return value


def _identifier(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"Prediction snapshot {label} is invalid")
    return value


def _fingerprint(value: object, label: str) -> str:
    return _identifier(value, FINGERPRINT, label)


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise ValueError(f"Prediction snapshot {label} is invalid")
    return value


def _iso_date(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"Prediction snapshot {label} is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Prediction snapshot {label} is invalid") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"Prediction snapshot {label} is invalid")
    return parsed


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Prediction snapshot {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Prediction snapshot {label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Prediction snapshot {label} must include a timezone")
    return parsed


def _is_finite(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


__all__ = [
    "PREDICTION_ENGINE_VERSION",
    "PREDICTION_SCHEMA_VERSION",
    "PREDICTION_SNAPSHOT_ID",
    "PredictionSnapshotStore",
    "finalize_prediction_snapshot",
    "prediction_record_fingerprint",
    "prediction_snapshot_fingerprint",
    "validate_prediction_snapshot",
]
