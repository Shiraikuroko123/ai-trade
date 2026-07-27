from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping, Sequence

from ..config import AppConfig
from ..data.evidence_io import atomic_create_json, evidence_store_lock
from ..data.market import MarketData
from ..json_utils import load_unique_json
from .schema import (
    FINGERPRINT,
    SYMBOL,
    feature_snapshot_fingerprint,
    json_fingerprint,
    is_genuine_pit_snapshot,
    validate_feature_snapshot,
)
from .provenance import actual_snapshot_provider


LABEL_SCHEMA_VERSION = 1
LABEL_ENGINE_VERSION = 1
LABEL_SNAPSHOT_ID = re.compile(r"ls_[0-9a-f]{32}\Z")
LABEL_SAFETY = {
    "research_only": True,
    "separate_from_features": True,
    "requires_realized_at_cutoff": True,
    "creates_no_signal": True,
    "may_trade": False,
}
MAX_LABEL_SNAPSHOT_BYTES = 4 * 1024 * 1024
MAX_LABELS_PER_FEATURE = 64
CHINA_TIMEZONE = timezone(timedelta(hours=8))

_TOP_FIELDS = frozenset(
    {
        "schema_version",
        "engine_version",
        "label_snapshot_id",
        "created_at",
        "feature_snapshot_id",
        "feature_snapshot_fingerprint",
        "horizon",
        "as_of_session",
        "target_session",
        "realized_at",
        "source",
        "rows",
        "safety",
        "snapshot_fingerprint",
        "record_fingerprint",
    }
)
_SOURCE_FIELDS = frozenset(
    {
        "provider",
        "adjustment",
        "completed_session_cutoff",
        "cache_manifest_sha256",
        "as_of_target_market_fingerprint",
    }
)
_ROW_FIELDS = frozenset(
    {"symbol", "forward_return", "missing", "input_sha256"}
)


class LabelSnapshotBuilder:
    """Materialize forward labels only after their target session is known."""

    def __init__(
        self,
        config: AppConfig,
        store: "LabelSnapshotStore | None" = None,
    ) -> None:
        self.config = config
        self.store = store or LabelSnapshotStore(config.feature_store_dir)

    def build(
        self,
        feature_snapshot: Mapping[str, Any],
        market: MarketData,
        *,
        horizon: int,
        realized_at: datetime | None = None,
        publish: bool = True,
    ) -> dict[str, Any]:
        feature_record = _without_reused(feature_snapshot)
        validate_feature_snapshot(feature_record)
        if isinstance(horizon, bool) or not isinstance(horizon, int) or not 1 <= horizon <= 250:
            raise ValueError("Label horizon must be an integer between 1 and 250")
        as_of = date.fromisoformat(str(feature_record["as_of_session"]))
        try:
            start_index = market.calendar.index(as_of)
            target = market.calendar[start_index + horizon]
        except (ValueError, IndexError) as exc:
            raise RuntimeError("Label target session is not yet completed") from exc
        if target > market.completed_through:
            raise RuntimeError("Label target session is not yet completed")
        market_close = time.fromisoformat(
            str(self.config.raw["data"].get("market_close_time", "15:30"))
        )
        target_close = datetime.combine(target, market_close, CHINA_TIMEZONE)
        realized = realized_at or target_close
        if realized.tzinfo is None or realized.utcoffset() is None:
            raise ValueError("Label realized_at must include a timezone")
        if realized < target_close:
            raise ValueError("Label realized_at precedes the target close")

        rows: list[dict[str, Any]] = []
        input_bindings: dict[str, str] = {}
        for feature_row in feature_record["rows"]:
            symbol = str(feature_row["symbol"])
            entry = market.bar(symbol, as_of)
            exit_bar = market.bar(symbol, target)
            binding = json_fingerprint(
                {
                    "feature_input_sha256": feature_row["input_sha256"],
                    "entry": _bar_payload(entry),
                    "target": _bar_payload(exit_bar),
                }
            )
            input_bindings[symbol] = binding
            if entry is None:
                forward_return = None
                missing = "missing_entry_bar"
            elif exit_bar is None:
                forward_return = None
                missing = "missing_target_bar"
            elif entry.close <= 0 or exit_bar.close <= 0:
                forward_return = None
                missing = "nonpositive_close"
            else:
                forward_return = exit_bar.close / entry.close - 1.0
                missing = None
            rows.append(
                {
                    "symbol": symbol,
                    "forward_return": forward_return,
                    "missing": missing,
                    "input_sha256": binding,
                }
            )

        metadata = market.snapshot_metadata()
        if not isinstance(metadata, dict):
            raise RuntimeError("Market snapshot metadata must be an object")
        source_provider = actual_snapshot_provider(metadata)
        manifest_sha256 = getattr(market, "manifest_sha256", None)
        if not isinstance(manifest_sha256, str) or FINGERPRINT.fullmatch(manifest_sha256) is None:
            raise RuntimeError("Label snapshots require a verified cache manifest")
        source = {
            "provider": source_provider,
            "adjustment": str(metadata.get("adjustment") or "none"),
            "completed_session_cutoff": market.completed_through.isoformat(),
            "cache_manifest_sha256": manifest_sha256,
            "as_of_target_market_fingerprint": json_fingerprint(
                {
                    "as_of_session": as_of.isoformat(),
                    "target_session": target.isoformat(),
                    "inputs": input_bindings,
                }
            ),
        }
        record = finalize_label_snapshot(
            {
                "schema_version": LABEL_SCHEMA_VERSION,
                "engine_version": LABEL_ENGINE_VERSION,
                "created_at": _utc_now(),
                "feature_snapshot_id": feature_record["snapshot_id"],
                "feature_snapshot_fingerprint": feature_record["snapshot_fingerprint"],
                "horizon": horizon,
                "as_of_session": as_of.isoformat(),
                "target_session": target.isoformat(),
                "realized_at": realized.isoformat(),
                "source": source,
                "rows": rows,
                "safety": dict(LABEL_SAFETY),
            }
        )
        return self.store.publish(record) if publish else record


class LabelSnapshotStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    @property
    def labels_root(self) -> Path:
        return self.root / "labels"

    def publish(self, record: dict[str, Any]) -> dict[str, Any]:
        validate_label_snapshot(record)
        feature_id = str(record["feature_snapshot_id"])
        label_id = str(record["label_snapshot_id"])
        target = self.labels_root / feature_id / f"{label_id}.json"
        with evidence_store_lock(self.root, "Label snapshot"):
            directory = target.parent
            if directory.exists():
                paths = self._paths(feature_id)
                if len(paths) >= MAX_LABELS_PER_FEATURE and not target.exists():
                    raise RuntimeError("Label snapshot capacity reached for this feature")
            if target.exists() or target.is_symlink():
                existing = self._read(target)
                if existing["snapshot_fingerprint"] != record["snapshot_fingerprint"]:
                    raise RuntimeError("Label snapshot id collision")
                result = _clone(existing)
                result["reused"] = True
                return result
            atomic_create_json(
                self.root,
                target,
                record,
                label="label snapshot",
                maximum_bytes=MAX_LABEL_SNAPSHOT_BYTES,
            )
        result = self.get(feature_id, label_id)
        result["reused"] = False
        return result

    def get(self, feature_snapshot_id: str, label_snapshot_id: str) -> dict[str, Any]:
        _feature_snapshot_id(feature_snapshot_id)
        _label_snapshot_id(label_snapshot_id)
        path = self.labels_root / feature_snapshot_id / f"{label_snapshot_id}.json"
        if path.is_symlink() or not path.is_file():
            raise KeyError(label_snapshot_id)
        return self._read(path)

    def list_for_feature(self, feature_snapshot_id: str) -> list[dict[str, Any]]:
        return [self._read(path) for path in self._paths(feature_snapshot_id)]

    def _paths(self, feature_snapshot_id: str) -> list[Path]:
        _feature_snapshot_id(feature_snapshot_id)
        directory = self.labels_root / feature_snapshot_id
        if not directory.exists():
            return []
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("Label snapshot feature directory is invalid")
        paths: list[Path] = []
        for path in directory.iterdir():
            if (
                path.is_symlink()
                or not path.is_file()
                or path.suffix != ".json"
                or LABEL_SNAPSHOT_ID.fullmatch(path.stem) is None
            ):
                raise RuntimeError("Unexpected label snapshot file")
            paths.append(path)
        if len(paths) > MAX_LABELS_PER_FEATURE:
            raise RuntimeError("Label snapshot feature directory exceeds capacity")
        return sorted(paths, key=lambda item: item.name)

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            value = load_unique_json(path, max_bytes=MAX_LABEL_SNAPSHOT_BYTES)
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeError(f"Invalid label snapshot {path.name}: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Label snapshot must be an object")
        try:
            validate_label_snapshot(value)
        except ValueError as exc:
            raise RuntimeError(f"Invalid label snapshot {path.name}: {exc}") from exc
        if value["label_snapshot_id"] != path.stem:
            raise RuntimeError("Label snapshot id does not match its file name")
        if value["feature_snapshot_id"] != path.parent.name:
            raise RuntimeError("Label snapshot feature binding is inconsistent")
        return value


def finalize_label_snapshot(draft: Mapping[str, Any]) -> dict[str, Any]:
    record = _clone(draft)
    if not isinstance(record, dict):
        raise ValueError("Label snapshot must be an object")
    forbidden = {"label_snapshot_id", "snapshot_fingerprint", "record_fingerprint"}
    if forbidden & set(record):
        raise ValueError("Label snapshot identifiers are assigned by the schema")
    record["label_snapshot_id"] = None
    record["snapshot_fingerprint"] = None
    record["record_fingerprint"] = None
    fingerprint = label_snapshot_fingerprint(record)
    record["label_snapshot_id"] = "ls_" + fingerprint[:32]
    record["snapshot_fingerprint"] = fingerprint
    record["record_fingerprint"] = label_record_fingerprint(record)
    validate_label_snapshot(record)
    return record


def validate_label_snapshot(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != _TOP_FIELDS:
        raise ValueError("Label snapshot top-level fields are invalid")
    if value.get("schema_version") != LABEL_SCHEMA_VERSION:
        raise ValueError("Label snapshot schema version is invalid")
    if value.get("engine_version") != LABEL_ENGINE_VERSION:
        raise ValueError("Label snapshot engine version is invalid")
    label_id = _label_snapshot_id(value.get("label_snapshot_id"))
    _feature_snapshot_id(value.get("feature_snapshot_id"))
    _fingerprint(value.get("feature_snapshot_fingerprint"), "feature fingerprint")
    horizon = value.get("horizon")
    if type(horizon) is not int or not 1 <= horizon <= 250:
        raise ValueError("Label snapshot horizon is invalid")
    as_of = _iso_date(value.get("as_of_session"), "as_of_session")
    target = _iso_date(value.get("target_session"), "target_session")
    if target <= as_of:
        raise ValueError("Label snapshot target must follow its feature session")
    realized = _timestamp(value.get("realized_at"), "realized_at")
    created = _timestamp(value.get("created_at"), "created_at")
    if created < realized:
        raise ValueError("Label snapshot was created before its label was realized")
    source = _object(value.get("source"), _SOURCE_FIELDS, "source")
    _text(source.get("provider"), "source.provider", 120)
    if source.get("adjustment") not in {"none", "forward", "backward"}:
        raise ValueError("Label snapshot adjustment is invalid")
    if _iso_date(source.get("completed_session_cutoff"), "source cutoff") < target:
        raise ValueError("Label snapshot source cutoff predates its target")
    _fingerprint(source.get("cache_manifest_sha256"), "cache manifest")
    _fingerprint(source.get("as_of_target_market_fingerprint"), "market fingerprint")
    rows = value.get("rows")
    if not isinstance(rows, list) or not rows:
        raise ValueError("Label snapshot rows are invalid")
    symbols: list[str] = []
    for item in rows:
        row = _object(item, _ROW_FIELDS, "row")
        symbol = _identifier(row.get("symbol"), SYMBOL, "row.symbol")
        symbols.append(symbol)
        forward = row.get("forward_return")
        missing = row.get("missing")
        if (forward is None) == (missing is None):
            raise ValueError("Label snapshot row must contain a value or a missing reason")
        if forward is not None and (
            isinstance(forward, bool)
            or not isinstance(forward, (int, float))
            or not math.isfinite(float(forward))
        ):
            raise ValueError("Label snapshot forward return is invalid")
        if missing is not None:
            _text(missing, "row.missing", 160)
        _fingerprint(row.get("input_sha256"), "row.input_sha256")
    if symbols != sorted(symbols) or len(symbols) != len(set(symbols)):
        raise ValueError("Label snapshot rows are out of order")
    if value.get("safety") != LABEL_SAFETY:
        raise ValueError("Label snapshot safety boundary is invalid")
    fingerprint = _fingerprint(value.get("snapshot_fingerprint"), "snapshot fingerprint")
    if label_id != "ls_" + fingerprint[:32]:
        raise ValueError("Label snapshot id is inconsistent")
    if fingerprint != label_snapshot_fingerprint(value):
        raise ValueError("Label snapshot fingerprint does not match content")
    if value.get("record_fingerprint") != label_record_fingerprint(value):
        raise ValueError("Label snapshot record fingerprint does not match content")


def training_pairs(
    features: Sequence[Mapping[str, Any]],
    labels: Sequence[Mapping[str, Any]],
    *,
    training_cutoff: datetime,
    require_genuine_pit: bool = False,
    evidence_cutoff: datetime | None = None,
) -> list[dict[str, Any]]:
    """Pair only labels that were observable by the training cutoff."""

    if training_cutoff.tzinfo is None or training_cutoff.utcoffset() is None:
        raise ValueError("training_cutoff must include a timezone")
    if evidence_cutoff is not None and (
        evidence_cutoff.tzinfo is None or evidence_cutoff.utcoffset() is None
    ):
        raise ValueError("evidence_cutoff must include a timezone")
    if evidence_cutoff is not None and training_cutoff > evidence_cutoff:
        raise ValueError("training_cutoff cannot follow the evaluation evidence cutoff")
    feature_by_id: dict[str, Mapping[str, Any]] = {}
    for feature in features:
        feature_record = _without_reused(feature)
        validate_feature_snapshot(feature_record)
        if require_genuine_pit and bool(
            feature_record["historical_reconstruction"]
        ):
            raise ValueError(
                "Historical reconstruction cannot be used as deployable PIT training evidence"
            )
        if require_genuine_pit and not is_genuine_pit_snapshot(feature_record):
            raise ValueError(
                "Stale feature capture cannot be used as deployable PIT training evidence"
            )
        feature_created = _timestamp(feature_record["created_at"], "created_at")
        feature_cutoff = _timestamp(
            feature_record["knowledge_cutoff"], "knowledge_cutoff"
        )
        if feature_created > training_cutoff or feature_cutoff > training_cutoff:
            continue
        if evidence_cutoff is not None and (
            feature_created > evidence_cutoff or feature_cutoff > evidence_cutoff
        ):
            continue
        feature_by_id[str(feature_record["snapshot_id"])] = feature_record
    result: list[dict[str, Any]] = []
    for label in labels:
        label_record = _without_reused(label)
        validate_label_snapshot(label_record)
        label_created = _timestamp(label_record["created_at"], "created_at")
        label_realized = _timestamp(label_record["realized_at"], "realized_at")
        if label_realized > training_cutoff or label_created > training_cutoff:
            continue
        if evidence_cutoff is not None and (
            label_realized > evidence_cutoff or label_created > evidence_cutoff
        ):
            continue
        bound_feature = feature_by_id.get(str(label_record["feature_snapshot_id"]))
        if bound_feature is None:
            continue
        if (
            label_record["feature_snapshot_fingerprint"]
            != feature_snapshot_fingerprint(bound_feature)
            or label_record["as_of_session"] != bound_feature["as_of_session"]
        ):
            raise ValueError("Label snapshot does not match its feature snapshot")
        result.append({"feature": bound_feature, "label": label_record})
    result.sort(
        key=lambda item: (
            str(item["feature"]["as_of_session"]),
            int(item["label"]["horizon"]),
            str(item["label"]["label_snapshot_id"]),
        )
    )
    return result


def label_snapshot_fingerprint(value: Mapping[str, Any]) -> str:
    source = dict(value.get("source") or {})
    source.pop("cache_manifest_sha256", None)
    source.pop("completed_session_cutoff", None)
    return json_fingerprint(
        {
            "schema_version": value.get("schema_version"),
            "engine_version": value.get("engine_version"),
            "feature_snapshot_id": value.get("feature_snapshot_id"),
            "feature_snapshot_fingerprint": value.get("feature_snapshot_fingerprint"),
            "horizon": value.get("horizon"),
            "as_of_session": value.get("as_of_session"),
            "target_session": value.get("target_session"),
            "realized_at": value.get("realized_at"),
            "source": source,
            "rows": value.get("rows"),
            "safety": value.get("safety"),
        }
    )


def label_record_fingerprint(value: Mapping[str, Any]) -> str:
    body = _clone(value)
    body["record_fingerprint"] = None
    body.pop("reused", None)
    return json_fingerprint(body)


def _bar_payload(bar: Any) -> list[Any] | None:
    if bar is None:
        return None
    return [
        bar.date.isoformat(),
        bar.open,
        bar.close,
        bar.high,
        bar.low,
        bar.volume,
        bar.amount,
    ]


def _label_snapshot_id(value: object) -> str:
    return _identifier(value, LABEL_SNAPSHOT_ID, "label_snapshot_id")


def _feature_snapshot_id(value: object) -> str:
    pattern = re.compile(r"fs_[0-9a-f]{32}\Z")
    return _identifier(value, pattern, "feature_snapshot_id")


def _object(value: Any, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"Label snapshot {label} fields are invalid")
    return value


def _identifier(value: object, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"Label snapshot {label} is invalid")
    return value


def _fingerprint(value: object, label: str) -> str:
    return _identifier(value, FINGERPRINT, label)


def _text(value: object, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"Label snapshot {label} is invalid")
    return value


def _iso_date(value: object, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"Label snapshot {label} is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Label snapshot {label} is invalid") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"Label snapshot {label} is not canonical")
    return parsed


def _timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Label snapshot {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Label snapshot {label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Label snapshot {label} must include a timezone")
    return parsed


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


def _without_reused(value: Mapping[str, Any]) -> dict[str, Any]:
    result = _clone(value)
    if not isinstance(result, dict):
        raise ValueError("Evidence record must be an object")
    reused = result.pop("reused", None)
    if reused is not None and type(reused) is not bool:
        raise ValueError("Evidence reused marker must be boolean")
    return result


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "LABEL_ENGINE_VERSION",
    "LABEL_SCHEMA_VERSION",
    "LABEL_SNAPSHOT_ID",
    "LabelSnapshotBuilder",
    "LabelSnapshotStore",
    "finalize_label_snapshot",
    "label_record_fingerprint",
    "label_snapshot_fingerprint",
    "training_pairs",
    "validate_label_snapshot",
]
