from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import date, datetime
import json
from typing import Any, Mapping, Sequence

from .labels import LabelSnapshotStore, validate_label_snapshot
from .schema import (
    feature_snapshot_fingerprint,
    is_genuine_pit_snapshot,
    json_fingerprint,
    validate_feature_snapshot,
)
from .store import FeatureSnapshotStore


DATASET_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class SnapshotFactor:
    factor_id: str
    version: int
    label: str
    family: str
    direction: int
    minimum_history: int
    formula: str

    @classmethod
    def from_record(cls, value: Mapping[str, Any]) -> "SnapshotFactor":
        return cls(
            factor_id=str(value["factor_id"]),
            version=int(value["version"]),
            label=str(value["label"]),
            family=str(value["family"]),
            direction=int(value["direction"]),
            minimum_history=int(value["minimum_history"]),
            formula=str(value["formula"]),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor_id": self.factor_id,
            "version": self.version,
            "label": self.label,
            "family": self.family,
            "direction": self.direction,
            "minimum_history": self.minimum_history,
            "formula": self.formula,
        }


@dataclass(frozen=True)
class SnapshotDatasetRow:
    symbol: str
    values: tuple[float | None, ...]
    forward_return: float | None


@dataclass(frozen=True)
class SnapshotObservation:
    session: date
    knowledge_cutoff: datetime
    target_session: date
    realized_at: datetime
    horizon: int
    feature_snapshot_id: str
    label_snapshot_id: str
    rows: tuple[SnapshotDatasetRow, ...]


@dataclass(frozen=True)
class SnapshotDataset:
    """Validated, deterministic FeatureSnapshot/LabelSnapshot input boundary."""

    dataset_id: str
    fingerprint: str
    feature_set_id: str
    feature_set_fingerprint: str
    factors: tuple[SnapshotFactor, ...]
    horizons: tuple[int, ...]
    sessions: tuple[date, ...]
    observations: tuple[SnapshotObservation, ...]
    observation_keys: tuple[tuple[date, int], ...]
    feature_snapshot_ids: tuple[str, ...]
    label_snapshot_ids: tuple[str, ...]
    feature_providers: tuple[str, ...]
    label_providers: tuple[str, ...]
    adjustment: str
    universe_name: str
    minimum_listing_days: int
    security_master_sha256: str
    as_of_session: date
    genuine_pit_required: bool

    @property
    def factor_ids(self) -> tuple[str, ...]:
        return tuple(item.factor_id for item in self.factors)

    @property
    def provider(self) -> str:
        if len(self.feature_providers) == 1:
            return self.feature_providers[0]
        provider_hash = json_fingerprint(list(self.feature_providers))[:16]
        return f"mixed-feature-providers:{provider_hash}"

    def factor_index(self, factor_id: str) -> int:
        try:
            return self.factor_ids.index(factor_id)
        except ValueError as exc:
            raise ValueError(
                f"Snapshot dataset does not contain factor: {factor_id!r}"
            ) from exc

    def observation(
        self, session: date, horizon: int
    ) -> SnapshotObservation | None:
        key = (session, horizon)
        index = bisect_left(self.observation_keys, key)
        if index < len(self.observation_keys) and self.observation_keys[index] == key:
            return self.observations[index]
        return None

    def evidence(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.dataset_id,
            "kind": "feature_snapshot_dataset",
            "as_of": self.as_of_session.isoformat(),
            "provider": self.provider,
            "fingerprint": self.fingerprint,
        }

    def source_snapshot_ids(self) -> dict[str, list[str]]:
        return {
            "features": list(self.feature_snapshot_ids),
            "labels": list(self.label_snapshot_ids),
        }


def snapshot_dataset_identity(
    *,
    genuine_pit_required: bool,
    feature_set: Mapping[str, Any],
    horizons: Sequence[int],
    coverage: Mapping[str, Any],
    source: Mapping[str, Any],
    source_snapshots: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the complete self-validating identity stored by the manifest."""

    return {
        "schema_version": DATASET_SCHEMA_VERSION,
        "genuine_pit_required": genuine_pit_required,
        "feature_set": dict(feature_set),
        "horizons": list(horizons),
        "coverage": dict(coverage),
        "source": dict(source),
        "source_snapshots": dict(source_snapshots),
    }


def build_snapshot_dataset(
    features: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
    *,
    horizons: Sequence[int],
    require_genuine_pit: bool = True,
) -> SnapshotDataset:
    """Build a stable dataset without constructing or refreshing MarketData."""

    selected_horizons = _validated_horizons(horizons)
    if not features:
        raise ValueError("Snapshot dataset requires at least one FeatureSnapshot")

    feature_records = [_without_reused(item) for item in features]
    for record in feature_records:
        validate_feature_snapshot(record)
        if require_genuine_pit and bool(record["historical_reconstruction"]):
            raise ValueError(
                "Historical reconstruction cannot enter a genuine snapshot dataset"
            )
        if require_genuine_pit and not is_genuine_pit_snapshot(record):
            raise ValueError(
                "Stale feature capture cannot enter a genuine snapshot dataset"
            )
    feature_records.sort(
        key=lambda item: (str(item["as_of_session"]), str(item["snapshot_id"]))
    )
    sessions = [date.fromisoformat(str(item["as_of_session"])) for item in feature_records]
    if len(sessions) != len(set(sessions)):
        raise ValueError("Snapshot dataset has multiple FeatureSnapshots for one session")

    anchor = feature_records[0]
    feature_set = anchor["feature_set"]
    feature_set_fingerprint = str(feature_set["fingerprint"])
    factors = tuple(
        SnapshotFactor.from_record(item) for item in feature_set["factors"]
    )
    factor_ids = tuple(item.factor_id for item in factors)
    adjustment = str(anchor["source"]["adjustment"])
    universe_name = str(anchor["universe"]["name"])
    minimum_listing_days = int(anchor["universe"]["minimum_listing_days"])
    security_master_sha256 = str(anchor["source"]["security_master_sha256"])
    for record in feature_records[1:]:
        if str(record["feature_set"]["fingerprint"]) != feature_set_fingerprint:
            raise ValueError(
                "Snapshot dataset FeatureSnapshots must use one ordered feature set"
            )
        if (
            str(record["source"]["adjustment"]) != adjustment
            or str(record["universe"]["name"]) != universe_name
            or int(record["universe"]["minimum_listing_days"])
            != minimum_listing_days
            or str(record["source"]["security_master_sha256"])
            != security_master_sha256
        ):
            raise ValueError(
                "Snapshot dataset source, universe, or security-master identity changed"
            )

    feature_by_id = {str(item["snapshot_id"]): item for item in feature_records}
    label_by_key: dict[tuple[str, int], dict[str, Any]] = {}
    for value in labels:
        record = _without_reused(value)
        validate_label_snapshot(record)
        feature_id = str(record["feature_snapshot_id"])
        if feature_id not in feature_by_id:
            raise ValueError("LabelSnapshot refers to a FeatureSnapshot outside the dataset")
        horizon = int(record["horizon"])
        if horizon not in selected_horizons:
            raise ValueError("LabelSnapshot horizon is outside the requested dataset")
        key = (feature_id, horizon)
        if key in label_by_key:
            raise ValueError(
                "Snapshot dataset has multiple LabelSnapshots for one feature and horizon"
            )
        feature = feature_by_id[feature_id]
        if (
            record["feature_snapshot_fingerprint"]
            != feature_snapshot_fingerprint(feature)
            or record["as_of_session"] != feature["as_of_session"]
        ):
            raise ValueError("LabelSnapshot does not match its FeatureSnapshot")
        if str(record["source"]["adjustment"]) != adjustment:
            raise ValueError("LabelSnapshot adjustment differs from its feature dataset")
        feature_symbols = [str(item["symbol"]) for item in feature["rows"]]
        label_symbols = [str(item["symbol"]) for item in record["rows"]]
        if label_symbols != feature_symbols:
            raise ValueError(
                "LabelSnapshot symbols do not match its FeatureSnapshot rows"
            )
        label_by_key[key] = record

    observations: list[SnapshotObservation] = []
    for feature in feature_records:
        feature_id = str(feature["snapshot_id"])
        feature_rows = feature["rows"]
        knowledge_cutoff = _timestamp(feature["knowledge_cutoff"])
        for horizon in selected_horizons:
            label = label_by_key.get((feature_id, horizon))
            if label is None:
                continue
            rows: list[SnapshotDatasetRow] = []
            for feature_row, label_row in zip(feature_rows, label["rows"]):
                values = tuple(
                    (
                        float(feature_row["values"][factor_id])
                        if factor_id in feature_row["values"]
                        else None
                    )
                    for factor_id in factor_ids
                )
                forward = label_row["forward_return"]
                rows.append(
                    SnapshotDatasetRow(
                        symbol=str(feature_row["symbol"]),
                        values=values,
                        forward_return=(float(forward) if forward is not None else None),
                    )
                )
            observations.append(
                SnapshotObservation(
                    session=date.fromisoformat(str(feature["as_of_session"])),
                    knowledge_cutoff=knowledge_cutoff,
                    target_session=date.fromisoformat(str(label["target_session"])),
                    realized_at=_timestamp(label["realized_at"]),
                    horizon=horizon,
                    feature_snapshot_id=feature_id,
                    label_snapshot_id=str(label["label_snapshot_id"]),
                    rows=tuple(rows),
                )
            )
    observations.sort(key=lambda item: (item.session, item.horizon, item.label_snapshot_id))

    label_records = sorted(
        label_by_key.values(),
        key=lambda item: (
            str(item["as_of_session"]),
            int(item["horizon"]),
            str(item["label_snapshot_id"]),
        ),
    )
    as_of = max(
        (item.target_session for item in observations),
        default=max(sessions),
    )
    feature_snapshot_ids = tuple(
        str(item["snapshot_id"]) for item in feature_records
    )
    label_snapshot_ids = tuple(
        str(item["label_snapshot_id"]) for item in label_records
    )
    feature_providers = tuple(
        sorted({str(item["source"]["provider"]) for item in feature_records})
    )
    label_providers = tuple(
        sorted({str(item["source"]["provider"]) for item in label_records})
    )
    identity = snapshot_dataset_identity(
        genuine_pit_required=require_genuine_pit,
        feature_set={
            "feature_set_id": feature_set["feature_set_id"],
            "fingerprint": feature_set_fingerprint,
            "factors": [item.to_dict() for item in factors],
        },
        horizons=selected_horizons,
        coverage={
            "start": min(sessions).isoformat(),
            "end": max(sessions).isoformat(),
            "as_of": as_of.isoformat(),
            "feature_sessions": len(sessions),
            "observations": len(observations),
        },
        source={
            "adjustment": adjustment,
            "universe_name": universe_name,
            "minimum_listing_days": minimum_listing_days,
            "security_master_sha256": security_master_sha256,
            "feature_providers": list(feature_providers),
            "label_providers": list(label_providers),
        },
        source_snapshots={
            "features": list(feature_snapshot_ids),
            "labels": list(label_snapshot_ids),
        },
    )
    fingerprint = json_fingerprint(identity)
    return SnapshotDataset(
        dataset_id="fds_" + fingerprint[:32],
        fingerprint=fingerprint,
        feature_set_id=str(feature_set["feature_set_id"]),
        feature_set_fingerprint=feature_set_fingerprint,
        factors=factors,
        horizons=selected_horizons,
        sessions=tuple(sessions),
        observations=tuple(observations),
        observation_keys=tuple(
            (item.session, item.horizon) for item in observations
        ),
        feature_snapshot_ids=feature_snapshot_ids,
        label_snapshot_ids=label_snapshot_ids,
        feature_providers=feature_providers,
        label_providers=label_providers,
        adjustment=adjustment,
        universe_name=universe_name,
        minimum_listing_days=minimum_listing_days,
        security_master_sha256=security_master_sha256,
        as_of_session=as_of,
        genuine_pit_required=require_genuine_pit,
    )


def load_snapshot_dataset(
    feature_store: FeatureSnapshotStore,
    label_store: LabelSnapshotStore,
    *,
    horizons: Sequence[int],
    required_factor_ids: Sequence[str] | None = None,
    exact_factor_set: bool = False,
    feature_set_id: str | None = None,
    start: date | None = None,
    end: date | None = None,
    require_genuine_pit: bool = True,
) -> SnapshotDataset:
    """Select one feature-set revision across the immutable local stores."""

    selected_horizons = _validated_horizons(horizons)
    required = _validated_factor_ids(required_factor_ids)
    if exact_factor_set and required is None:
        raise ValueError("exact_factor_set requires explicit factor identifiers")
    if start is not None and end is not None and start > end:
        raise ValueError("Snapshot dataset start is after end")

    candidates: list[dict[str, Any]] = []
    for session in feature_store.sessions():
        if (start is not None and session < start) or (end is not None and session > end):
            continue
        for record in feature_store.list_for_session(session):
            if require_genuine_pit and not is_genuine_pit_snapshot(record):
                continue
            if feature_set_id is not None and str(
                record["feature_set"]["feature_set_id"]
            ) != feature_set_id:
                continue
            ids = tuple(
                str(item["factor_id"]) for item in record["feature_set"]["factors"]
            )
            if required is not None:
                if exact_factor_set and ids != required:
                    continue
                if not exact_factor_set and any(item not in ids for item in required):
                    continue
            candidates.append(record)
    if not candidates:
        qualifier = "genuine " if require_genuine_pit else ""
        raise ValueError(
            f"No {qualifier}FeatureSnapshots match the requested feature set"
        )

    anchor = max(
        candidates,
        key=lambda item: (
            str(item["as_of_session"]),
            str(item["created_at"]),
            str(item["snapshot_id"]),
        ),
    )
    selected_fingerprint = str(anchor["feature_set"]["fingerprint"])
    by_session: dict[str, list[dict[str, Any]]] = {}
    for record in candidates:
        if str(record["feature_set"]["fingerprint"]) == selected_fingerprint:
            by_session.setdefault(str(record["as_of_session"]), []).append(record)
    features = [
        max(
            records,
            key=lambda item: (str(item["created_at"]), str(item["snapshot_id"])),
        )
        for _session, records in sorted(by_session.items())
    ]

    labels: list[dict[str, Any]] = []
    for feature in features:
        feature_id = str(feature["snapshot_id"])
        available = label_store.list_for_feature(feature_id)
        for horizon in selected_horizons:
            revisions = [item for item in available if int(item["horizon"]) == horizon]
            if not revisions:
                continue
            identities = {
                json_fingerprint(
                    {
                        "target_session": item["target_session"],
                        "realized_at": item["realized_at"],
                        "rows": item["rows"],
                    }
                )
                for item in revisions
            }
            if len(identities) != 1:
                raise RuntimeError(
                    "Conflicting LabelSnapshot revisions require explicit resolution"
                )
            labels.append(
                max(
                    revisions,
                    key=lambda item: (
                        str(item["created_at"]),
                        str(item["label_snapshot_id"]),
                    ),
                )
            )
    return build_snapshot_dataset(
        features,
        labels,
        horizons=selected_horizons,
        require_genuine_pit=require_genuine_pit,
    )


def _validated_horizons(value: Sequence[int]) -> tuple[int, ...]:
    if (
        not isinstance(value, (list, tuple))
        or not 1 <= len(value) <= 4
        or any(
            isinstance(item, bool) or not isinstance(item, int) or not 1 <= item <= 250
            for item in value
        )
    ):
        raise ValueError("Snapshot dataset horizons must be 1 to 4 ints in 1..250")
    ordered = tuple(sorted(set(value)))
    if tuple(value) != ordered:
        raise ValueError("Snapshot dataset horizons must be unique and ascending")
    return ordered


def _validated_factor_ids(
    value: Sequence[str] | None,
) -> tuple[str, ...] | None:
    if value is None:
        return None
    if (
        not isinstance(value, (list, tuple))
        or not 1 <= len(value) <= 64
        or any(not isinstance(item, str) or not item for item in value)
        or len(set(value)) != len(value)
    ):
        raise ValueError("Snapshot dataset factor identifiers are invalid")
    return tuple(value)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Snapshot dataset timestamp is invalid")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("Snapshot dataset timestamp must include a timezone")
    return parsed


def _without_reused(value: Mapping[str, Any]) -> dict[str, Any]:
    result = json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))
    if not isinstance(result, dict):
        raise ValueError("Snapshot evidence must be an object")
    reused = result.pop("reused", None)
    if reused is not None and type(reused) is not bool:
        raise ValueError("Snapshot evidence reused marker must be boolean")
    return result


__all__ = [
    "DATASET_SCHEMA_VERSION",
    "SnapshotDataset",
    "SnapshotDatasetRow",
    "SnapshotFactor",
    "SnapshotObservation",
    "build_snapshot_dataset",
    "load_snapshot_dataset",
    "snapshot_dataset_identity",
]
