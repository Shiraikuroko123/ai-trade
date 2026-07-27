from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
import json
import math
import re
from typing import Any, Mapping


FEATURE_SCHEMA_VERSION = 1
FEATURE_ENGINE_VERSION = 1
FEATURE_SNAPSHOT_ID = re.compile(r"fs_[0-9a-f]{32}\Z")
FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
FACTOR_ID = re.compile(r"[a-z][a-z0-9_]{2,60}\Z")
SYMBOL = re.compile(r"[A-Za-z0-9_-]{1,32}\Z")

FEATURE_SAFETY = {
    "research_only": True,
    "contains_no_label": True,
    "creates_no_signal": True,
    "may_trade": False,
}

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "engine_version",
        "snapshot_id",
        "created_at",
        "as_of_session",
        "knowledge_cutoff",
        "historical_reconstruction",
        "feature_set",
        "source",
        "universe",
        "rows",
        "safety",
        "snapshot_fingerprint",
        "record_fingerprint",
    }
)
_FEATURE_SET_FIELDS = frozenset(
    {"feature_set_id", "library_version", "factors", "fingerprint"}
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
_SOURCE_FIELDS = frozenset(
    {
        "provider",
        "adjustment",
        "completed_session_cutoff",
        "cache_manifest_sha256",
        "manifest_snapshot_id",
        "security_master_sha256",
        "as_of_market_fingerprint",
    }
)
_UNIVERSE_FIELDS = frozenset(
    {
        "name",
        "minimum_listing_days",
        "candidate_records",
        "active_symbols",
        "excluded",
    }
)
_EXCLUSION_FIELDS = frozenset({"symbol", "reasons"})
_ROW_FIELDS = frozenset(
    {
        "symbol",
        "session",
        "last_bar_session",
        "trading_status",
        "tradable",
        "input_sha256",
        "values",
        "missing",
    }
)


def finalize_feature_snapshot(draft: Mapping[str, Any]) -> dict[str, Any]:
    record = _json_clone(draft)
    if not isinstance(record, dict):
        raise ValueError("Feature snapshot must be an object")
    forbidden = {"snapshot_id", "snapshot_fingerprint", "record_fingerprint"}
    if forbidden & set(record):
        raise ValueError("Feature snapshot identifiers are assigned by the schema")
    record["snapshot_id"] = None
    record["snapshot_fingerprint"] = None
    record["record_fingerprint"] = None
    fingerprint = feature_snapshot_fingerprint(record)
    record["snapshot_id"] = "fs_" + fingerprint[:32]
    record["snapshot_fingerprint"] = fingerprint
    record["record_fingerprint"] = feature_record_fingerprint(record)
    validate_feature_snapshot(record)
    return record


def validate_feature_snapshot(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != _TOP_LEVEL_FIELDS:
        raise ValueError("Feature snapshot top-level fields are invalid")
    if value.get("schema_version") != FEATURE_SCHEMA_VERSION:
        raise ValueError("Feature snapshot schema version is invalid")
    if value.get("engine_version") != FEATURE_ENGINE_VERSION:
        raise ValueError("Feature snapshot engine version is invalid")
    snapshot_id = _identifier(value.get("snapshot_id"), FEATURE_SNAPSHOT_ID, "snapshot_id")
    created_at = _timestamp(value.get("created_at"), "created_at")
    as_of = _iso_date(value.get("as_of_session"), "as_of_session")
    cutoff = _timestamp(value.get("knowledge_cutoff"), "knowledge_cutoff")
    if created_at < cutoff:
        raise ValueError("Feature snapshot was created before its knowledge cutoff")
    if type(value.get("historical_reconstruction")) is not bool:
        raise ValueError("Feature snapshot historical_reconstruction must be boolean")

    feature_set = _object(value.get("feature_set"), _FEATURE_SET_FIELDS, "feature_set")
    feature_set_id = _text(feature_set.get("feature_set_id"), "feature_set_id", 160)
    if not feature_set_id.startswith("fset_"):
        raise ValueError("Feature snapshot feature_set_id is invalid")
    library_version = feature_set.get("library_version")
    if type(library_version) is not int or not 1 <= library_version <= 10_000:
        raise ValueError("Feature snapshot library version is invalid")
    factors = feature_set.get("factors")
    if not isinstance(factors, list) or not 1 <= len(factors) <= 64:
        raise ValueError("Feature snapshot factors are invalid")
    factor_ids: list[str] = []
    for item in factors:
        factor = _object(item, _FACTOR_FIELDS, "factor")
        factor_id = _identifier(factor.get("factor_id"), FACTOR_ID, "factor_id")
        if factor_id in factor_ids:
            raise ValueError("Feature snapshot factors must be unique")
        factor_ids.append(factor_id)
        if type(factor.get("version")) is not int or factor["version"] < 1:
            raise ValueError("Feature snapshot factor version is invalid")
        _text(factor.get("label"), "factor.label", 160)
        _text(factor.get("family"), "factor.family", 80)
        if factor.get("direction") not in {-1, 1}:
            raise ValueError("Feature snapshot factor direction is invalid")
        minimum = factor.get("minimum_history")
        if type(minimum) is not int or not 1 <= minimum <= 10_000:
            raise ValueError("Feature snapshot factor history is invalid")
        _text(factor.get("formula"), "factor.formula", 500)
    expected_feature_fingerprint = json_fingerprint(
        {
            "feature_set_id": feature_set_id,
            "library_version": library_version,
            "factors": factors,
        }
    )
    _fingerprint(feature_set.get("fingerprint"), "feature_set.fingerprint")
    if feature_set["fingerprint"] != expected_feature_fingerprint:
        raise ValueError("Feature snapshot feature-set fingerprint is inconsistent")

    source = _object(value.get("source"), _SOURCE_FIELDS, "source")
    _text(source.get("provider"), "source.provider", 120)
    if source.get("adjustment") not in {"none", "forward", "backward"}:
        raise ValueError("Feature snapshot adjustment is invalid")
    completed = _iso_date(
        source.get("completed_session_cutoff"), "source.completed_session_cutoff"
    )
    if completed < as_of:
        raise ValueError("Feature snapshot source cutoff predates the snapshot")
    _fingerprint(source.get("cache_manifest_sha256"), "source.cache_manifest_sha256")
    manifest_id = source.get("manifest_snapshot_id")
    if manifest_id is not None:
        _text(manifest_id, "source.manifest_snapshot_id", 200)
    _fingerprint(source.get("security_master_sha256"), "source.security_master_sha256")
    _fingerprint(source.get("as_of_market_fingerprint"), "source.as_of_market_fingerprint")

    universe = _object(value.get("universe"), _UNIVERSE_FIELDS, "universe")
    _text(universe.get("name"), "universe.name", 160)
    minimum_listing_days = universe.get("minimum_listing_days")
    if type(minimum_listing_days) is not int or not 0 <= minimum_listing_days <= 10_000:
        raise ValueError("Feature snapshot minimum listing days is invalid")
    candidate_records = universe.get("candidate_records")
    if type(candidate_records) is not int or not 1 <= candidate_records <= 10_000:
        raise ValueError("Feature snapshot candidate count is invalid")
    active = universe.get("active_symbols")
    if (
        not isinstance(active, list)
        or active != sorted(active)
        or len(active) != len(set(active))
        or any(SYMBOL.fullmatch(str(item)) is None for item in active)
    ):
        raise ValueError("Feature snapshot active symbols are invalid")
    excluded = universe.get("excluded")
    if not isinstance(excluded, list):
        raise ValueError("Feature snapshot exclusions are invalid")
    excluded_symbols: list[str] = []
    for item in excluded:
        exclusion = _object(item, _EXCLUSION_FIELDS, "exclusion")
        symbol = _identifier(exclusion.get("symbol"), SYMBOL, "excluded.symbol")
        reasons = exclusion.get("reasons")
        if (
            symbol in excluded_symbols
            or not isinstance(reasons, list)
            or not reasons
            or reasons != sorted(set(reasons))
            or any(not isinstance(reason, str) or not reason for reason in reasons)
        ):
            raise ValueError("Feature snapshot exclusion is invalid")
        excluded_symbols.append(symbol)
    if excluded_symbols != sorted(excluded_symbols):
        raise ValueError("Feature snapshot exclusions are out of order")
    if candidate_records != len(active) + len(excluded):
        raise ValueError("Feature snapshot universe counts are inconsistent")

    rows = value.get("rows")
    if not isinstance(rows, list) or len(rows) != len(active):
        raise ValueError("Feature snapshot rows do not match its active universe")
    row_symbols: list[str] = []
    factor_id_set = set(factor_ids)
    for item in rows:
        row = _object(item, _ROW_FIELDS, "row")
        symbol = _identifier(row.get("symbol"), SYMBOL, "row.symbol")
        row_symbols.append(symbol)
        if _iso_date(row.get("session"), "row.session") != as_of:
            raise ValueError("Feature snapshot row session is inconsistent")
        last_bar = row.get("last_bar_session")
        if last_bar is not None and _iso_date(last_bar, "row.last_bar_session") > as_of:
            raise ValueError("Feature snapshot row uses a future bar")
        _text(row.get("trading_status"), "row.trading_status", 80)
        if type(row.get("tradable")) is not bool:
            raise ValueError("Feature snapshot row tradable must be boolean")
        _fingerprint(row.get("input_sha256"), "row.input_sha256")
        values = row.get("values")
        missing = row.get("missing")
        if not isinstance(values, Mapping) or not isinstance(missing, Mapping):
            raise ValueError("Feature snapshot row values and missing must be objects")
        if set(values) & set(missing) or set(values) | set(missing) != factor_id_set:
            raise ValueError("Feature snapshot row factor coverage is inconsistent")
        for factor_id, number in values.items():
            if factor_id not in factor_id_set or not _finite_number(number):
                raise ValueError("Feature snapshot row contains an invalid value")
        for factor_id, reason in missing.items():
            if factor_id not in factor_id_set:
                raise ValueError("Feature snapshot row contains an unknown missing factor")
            _text(reason, "row.missing", 200)
    if row_symbols != active:
        raise ValueError("Feature snapshot rows are out of order")

    if value.get("safety") != FEATURE_SAFETY:
        raise ValueError("Feature snapshot safety boundary is invalid")
    fingerprint = _fingerprint(value.get("snapshot_fingerprint"), "snapshot_fingerprint")
    if snapshot_id != "fs_" + fingerprint[:32]:
        raise ValueError("Feature snapshot id is inconsistent")
    if fingerprint != feature_snapshot_fingerprint(value):
        raise ValueError("Feature snapshot fingerprint does not match its content")
    record_fingerprint = _fingerprint(
        value.get("record_fingerprint"), "record_fingerprint"
    )
    if record_fingerprint != feature_record_fingerprint(value):
        raise ValueError("Feature snapshot record fingerprint does not match its content")


def feature_snapshot_fingerprint(value: Mapping[str, Any]) -> str:
    source = dict(value.get("source") or {})
    # A later cache refresh may append future bars. The point-in-time identity
    # is bound to per-row input hashes, not to that later container manifest.
    source.pop("cache_manifest_sha256", None)
    source.pop("manifest_snapshot_id", None)
    source.pop("completed_session_cutoff", None)
    body = {
        "schema_version": value.get("schema_version"),
        "engine_version": value.get("engine_version"),
        "as_of_session": value.get("as_of_session"),
        "knowledge_cutoff": value.get("knowledge_cutoff"),
        "historical_reconstruction": value.get("historical_reconstruction"),
        "feature_set": value.get("feature_set"),
        "source": source,
        "universe": value.get("universe"),
        "rows": value.get("rows"),
        "safety": value.get("safety"),
    }
    return json_fingerprint(body)


def feature_record_fingerprint(value: Mapping[str, Any]) -> str:
    body = _json_clone(value)
    if not isinstance(body, dict):
        raise ValueError("Feature snapshot must be an object")
    body["record_fingerprint"] = None
    body.pop("reused", None)
    return json_fingerprint(body)


def is_genuine_pit_snapshot(value: Mapping[str, Any]) -> bool:
    """Return whether a snapshot was captured through the current cutoff."""

    source = value.get("source")
    return (
        value.get("historical_reconstruction") is False
        and isinstance(source, Mapping)
        and source.get("completed_session_cutoff") == value.get("as_of_session")
    )


def json_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _object(value: Any, fields: frozenset[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError(f"Feature snapshot {label} fields are invalid")
    return value


def _identifier(value: Any, pattern: re.Pattern[str], label: str) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"Feature snapshot {label} is invalid")
    return value


def _fingerprint(value: Any, label: str) -> str:
    return _identifier(value, FINGERPRINT, label)


def _text(value: Any, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"Feature snapshot {label} is invalid")
    return value


def _iso_date(value: Any, label: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"Feature snapshot {label} is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Feature snapshot {label} is invalid") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"Feature snapshot {label} is not canonical")
    return parsed


def _timestamp(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"Feature snapshot {label} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"Feature snapshot {label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"Feature snapshot {label} must include a timezone")
    return parsed


def _finite_number(value: Any) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


__all__ = [
    "FEATURE_ENGINE_VERSION",
    "FEATURE_SCHEMA_VERSION",
    "FEATURE_SAFETY",
    "FEATURE_SNAPSHOT_ID",
    "FINGERPRINT",
    "feature_record_fingerprint",
    "feature_snapshot_fingerprint",
    "finalize_feature_snapshot",
    "json_fingerprint",
    "is_genuine_pit_snapshot",
    "validate_feature_snapshot",
]
