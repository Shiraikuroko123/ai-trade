from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Mapping

from ..data.evidence_io import atomic_create_json, evidence_store_lock
from ..json_utils import load_unique_json
from .dataset import (
    DATASET_SCHEMA_VERSION,
    SnapshotDataset,
    snapshot_dataset_identity,
)
from .labels import LABEL_SNAPSHOT_ID
from .schema import FEATURE_SNAPSHOT_ID, FINGERPRINT, json_fingerprint


SNAPSHOT_DATASET_ID = re.compile(r"fds_[0-9a-f]{32}\Z")
MAX_SNAPSHOT_DATASET_BYTES = 4 * 1024 * 1024
MAX_SNAPSHOT_DATASETS = 5_000
DATASET_MANIFEST_SAFETY = {
    "research_only": True,
    "contains_source_ids_only": True,
    "creates_no_signal": True,
    "may_trade": False,
}

_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "dataset_id",
        "created_at",
        "dataset_fingerprint",
        "genuine_pit_required",
        "feature_set",
        "horizons",
        "coverage",
        "source",
        "source_snapshots",
        "safety",
        "record_fingerprint",
    }
)
_FEATURE_SET_FIELDS = frozenset({"feature_set_id", "fingerprint", "factors"})
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
_COVERAGE_FIELDS = frozenset(
    {"start", "end", "as_of", "feature_sessions", "observations"}
)
_SOURCE_FIELDS = frozenset(
    {
        "adjustment",
        "universe_name",
        "minimum_listing_days",
        "security_master_sha256",
        "feature_providers",
        "label_providers",
    }
)
_SOURCE_SNAPSHOT_FIELDS = frozenset({"features", "labels"})


class SnapshotDatasetStore:
    """Create-once manifests that preserve every dataset source snapshot id."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    @property
    def datasets_root(self) -> Path:
        return self.root / "datasets"

    def publish(self, dataset: SnapshotDataset) -> dict[str, Any]:
        record = snapshot_dataset_manifest(dataset)
        dataset_id = str(record["dataset_id"])
        target = self.datasets_root / f"{dataset_id}.json"
        with evidence_store_lock(self.root, "Snapshot dataset"):
            paths = self._paths(missing_ok=True)
            if target.exists() or target.is_symlink():
                existing = self._read(target)
                if existing["dataset_fingerprint"] != dataset.fingerprint:
                    raise RuntimeError("Snapshot dataset id collision")
                result = _clone(existing)
                result["reused"] = True
                return result
            if len(paths) >= MAX_SNAPSHOT_DATASETS:
                raise RuntimeError("Snapshot dataset capacity reached")
            atomic_create_json(
                self.root,
                target,
                record,
                label="snapshot dataset manifest",
                maximum_bytes=MAX_SNAPSHOT_DATASET_BYTES,
            )
        result = self.get(dataset_id)
        result["reused"] = False
        return result

    def get(self, dataset_id: str) -> dict[str, Any]:
        _dataset_id(dataset_id)
        path = self.datasets_root / f"{dataset_id}.json"
        if path.is_symlink() or not path.is_file():
            raise KeyError(dataset_id)
        return self._read(path)

    def _paths(self, *, missing_ok: bool) -> list[Path]:
        root = self.datasets_root
        if not root.exists():
            if missing_ok:
                return []
            raise RuntimeError("Snapshot dataset root is unavailable")
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError("Snapshot dataset root is invalid")
        paths: list[Path] = []
        for path in root.iterdir():
            if (
                path.is_symlink()
                or not path.is_file()
                or path.suffix != ".json"
                or SNAPSHOT_DATASET_ID.fullmatch(path.stem) is None
            ):
                raise RuntimeError("Unexpected snapshot dataset store member")
            paths.append(path)
        if len(paths) > MAX_SNAPSHOT_DATASETS:
            raise RuntimeError("Snapshot dataset store exceeds capacity")
        return sorted(paths, key=lambda item: item.name)

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            value = load_unique_json(path, max_bytes=MAX_SNAPSHOT_DATASET_BYTES)
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid snapshot dataset manifest {path.name}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise RuntimeError("Snapshot dataset manifest must be an object")
        try:
            validate_snapshot_dataset_manifest(value)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid snapshot dataset manifest {path.name}: {exc}"
            ) from exc
        if value["dataset_id"] != path.stem:
            raise RuntimeError("Snapshot dataset id does not match its file name")
        return value


def snapshot_dataset_manifest(dataset: SnapshotDataset) -> dict[str, Any]:
    factor_bindings = [item.to_dict() for item in dataset.factors]
    record = {
        "schema_version": DATASET_SCHEMA_VERSION,
        "dataset_id": dataset.dataset_id,
        "created_at": _utc_now(),
        "dataset_fingerprint": dataset.fingerprint,
        "genuine_pit_required": dataset.genuine_pit_required,
        "feature_set": {
            "feature_set_id": dataset.feature_set_id,
            "fingerprint": dataset.feature_set_fingerprint,
            "factors": factor_bindings,
        },
        "horizons": list(dataset.horizons),
        "coverage": {
            "start": dataset.sessions[0].isoformat(),
            "end": dataset.sessions[-1].isoformat(),
            "as_of": dataset.as_of_session.isoformat(),
            "feature_sessions": len(dataset.sessions),
            "observations": len(dataset.observations),
        },
        "source": {
            "adjustment": dataset.adjustment,
            "universe_name": dataset.universe_name,
            "minimum_listing_days": dataset.minimum_listing_days,
            "security_master_sha256": dataset.security_master_sha256,
            "feature_providers": list(dataset.feature_providers),
            "label_providers": list(dataset.label_providers),
        },
        "source_snapshots": dataset.source_snapshot_ids(),
        "safety": dict(DATASET_MANIFEST_SAFETY),
        "record_fingerprint": None,
    }
    record["record_fingerprint"] = snapshot_dataset_record_fingerprint(record)
    validate_snapshot_dataset_manifest(record)
    return record


def validate_snapshot_dataset_manifest(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != _TOP_FIELDS:
        raise ValueError("Snapshot dataset manifest fields are invalid")
    if value.get("schema_version") != DATASET_SCHEMA_VERSION:
        raise ValueError("Snapshot dataset schema version is invalid")
    dataset_id = _identifier(value.get("dataset_id"), SNAPSHOT_DATASET_ID, "dataset_id")
    _timestamp(value.get("created_at"), "created_at")
    fingerprint = _identifier(
        value.get("dataset_fingerprint"), FINGERPRINT, "dataset_fingerprint"
    )
    if dataset_id != "fds_" + fingerprint[:32]:
        raise ValueError("Snapshot dataset id is inconsistent")
    if type(value.get("genuine_pit_required")) is not bool:
        raise ValueError("Snapshot dataset genuine PIT marker is invalid")

    feature_set = _object(value.get("feature_set"), _FEATURE_SET_FIELDS, "feature_set")
    feature_set_id = feature_set.get("feature_set_id")
    if not isinstance(feature_set_id, str) or not feature_set_id.startswith("fset_"):
        raise ValueError("Snapshot dataset feature_set_id is invalid")
    _identifier(feature_set.get("fingerprint"), FINGERPRINT, "feature_set.fingerprint")
    factors = feature_set.get("factors")
    if not isinstance(factors, list) or not 1 <= len(factors) <= 64:
        raise ValueError("Snapshot dataset factors are invalid")
    factor_ids: list[str] = []
    for item in factors:
        factor = _object(item, _FACTOR_FIELDS, "factor")
        factor_id = factor.get("factor_id")
        if not isinstance(factor_id, str) or not factor_id or factor_id in factor_ids:
            raise ValueError("Snapshot dataset factor id is invalid")
        factor_ids.append(factor_id)
        if type(factor.get("version")) is not int or int(factor["version"]) < 1:
            raise ValueError("Snapshot dataset factor version is invalid")
        if factor.get("direction") not in {-1, 1}:
            raise ValueError("Snapshot dataset factor direction is invalid")
        for field in ("label", "family", "formula"):
            if not isinstance(factor.get(field), str) or not str(factor[field]):
                raise ValueError(f"Snapshot dataset factor {field} is invalid")
        history = factor.get("minimum_history")
        if type(history) is not int or not 1 <= int(history) <= 10_000:
            raise ValueError("Snapshot dataset factor minimum_history is invalid")

    horizons = value.get("horizons")
    if (
        not isinstance(horizons, list)
        or not 1 <= len(horizons) <= 4
        or horizons != sorted(set(horizons))
        or any(type(item) is not int or not 1 <= item <= 250 for item in horizons)
    ):
        raise ValueError("Snapshot dataset horizons are invalid")
    coverage = _object(value.get("coverage"), _COVERAGE_FIELDS, "coverage")
    start = _iso_date(coverage.get("start"), "coverage.start")
    end = _iso_date(coverage.get("end"), "coverage.end")
    as_of = _iso_date(coverage.get("as_of"), "coverage.as_of")
    if not start <= end <= as_of:
        raise ValueError("Snapshot dataset coverage dates are inconsistent")
    for field in ("feature_sessions", "observations"):
        count = coverage.get(field)
        if type(count) is not int or not 0 <= int(count) <= 1_000_000:
            raise ValueError(f"Snapshot dataset coverage {field} is invalid")
    if int(coverage["feature_sessions"]) < 1:
        raise ValueError("Snapshot dataset requires feature sessions")

    source = _object(value.get("source"), _SOURCE_FIELDS, "source")
    if source.get("adjustment") not in {"none", "forward", "backward"}:
        raise ValueError("Snapshot dataset adjustment is invalid")
    for field in ("universe_name",):
        if not isinstance(source.get(field), str) or not str(source[field]):
            raise ValueError(f"Snapshot dataset {field} is invalid")
    listing_days = source.get("minimum_listing_days")
    if type(listing_days) is not int or not 0 <= int(listing_days) <= 10_000:
        raise ValueError("Snapshot dataset minimum_listing_days is invalid")
    _identifier(
        source.get("security_master_sha256"),
        FINGERPRINT,
        "source.security_master_sha256",
    )
    for field, allow_empty in (("feature_providers", False), ("label_providers", True)):
        providers = source.get(field)
        if (
            not isinstance(providers, list)
            or (not allow_empty and not providers)
            or providers != sorted(set(providers))
            or any(not isinstance(item, str) or not item for item in providers)
        ):
            raise ValueError(f"Snapshot dataset {field} is invalid")

    snapshots = _object(
        value.get("source_snapshots"),
        _SOURCE_SNAPSHOT_FIELDS,
        "source_snapshots",
    )
    feature_ids = snapshots.get("features")
    label_ids = snapshots.get("labels")
    if (
        not isinstance(feature_ids, list)
        or len(feature_ids) != int(coverage["feature_sessions"])
        or len(feature_ids) != len(set(feature_ids))
        or any(
            not isinstance(item, str) or FEATURE_SNAPSHOT_ID.fullmatch(item) is None
            for item in feature_ids
        )
    ):
        raise ValueError("Snapshot dataset feature source ids are invalid")
    if (
        not isinstance(label_ids, list)
        or len(label_ids) != int(coverage["observations"])
        or len(label_ids) != len(set(label_ids))
        or any(
            not isinstance(item, str) or LABEL_SNAPSHOT_ID.fullmatch(item) is None
            for item in label_ids
        )
    ):
        raise ValueError("Snapshot dataset label source ids are invalid")
    if value.get("safety") != DATASET_MANIFEST_SAFETY:
        raise ValueError("Snapshot dataset safety boundary is invalid")
    expected_fingerprint = json_fingerprint(
        snapshot_dataset_identity(
            genuine_pit_required=bool(value["genuine_pit_required"]),
            feature_set=feature_set,
            horizons=horizons,
            coverage=coverage,
            source=source,
            source_snapshots=snapshots,
        )
    )
    if fingerprint != expected_fingerprint:
        raise ValueError("Snapshot dataset fingerprint does not match its sources")
    record_fingerprint = _identifier(
        value.get("record_fingerprint"), FINGERPRINT, "record_fingerprint"
    )
    if record_fingerprint != snapshot_dataset_record_fingerprint(value):
        raise ValueError("Snapshot dataset record fingerprint does not match content")


def snapshot_dataset_record_fingerprint(value: Mapping[str, Any]) -> str:
    body = _clone(value)
    body["record_fingerprint"] = None
    body.pop("reused", None)
    return json_fingerprint(body)


def _object(
    value: Any, fields: frozenset[str], label: str
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"Snapshot dataset {label} fields are invalid")
    return value


def _identifier(
    value: object, pattern: re.Pattern[str], label: str
) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"Snapshot dataset {label} is invalid")
    return value


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Snapshot dataset {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Snapshot dataset {label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Snapshot dataset {label} must include a timezone")
    return parsed


def _iso_date(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"Snapshot dataset {label} is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Snapshot dataset {label} is invalid") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"Snapshot dataset {label} is not canonical")
    return parsed


def _dataset_id(value: object) -> str:
    return _identifier(value, SNAPSHOT_DATASET_ID, "dataset_id")


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "DATASET_MANIFEST_SAFETY",
    "MAX_SNAPSHOT_DATASET_BYTES",
    "MAX_SNAPSHOT_DATASETS",
    "SNAPSHOT_DATASET_ID",
    "SnapshotDatasetStore",
    "snapshot_dataset_manifest",
    "snapshot_dataset_record_fingerprint",
    "validate_snapshot_dataset_manifest",
]
