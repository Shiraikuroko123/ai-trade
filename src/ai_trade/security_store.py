"""Immutable knowledge-time versions of the security master."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
from typing import Any, Mapping

from .data.evidence_io import atomic_create_json, evidence_store_lock
from .json_utils import load_unique_json
from .security import SecurityMaster


SECURITY_MASTER_VERSION_SCHEMA = 2
MAX_SECURITY_MASTER_VERSION_BYTES = 32 * 1024 * 1024
MAX_SECURITY_MASTER_VERSIONS = 10_000
SECURITY_MASTER_VERSION_ID = re.compile(r"smv_[0-9a-f]{32}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_MOBILE_NUMBER = re.compile(r"(?<!\d)1\d{10}(?!\d)")
_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "version_id",
        "known_at",
        "previous_version_id",
        "previous_record_sha256",
        "source_manifest",
        "security_master",
        "security_master_sha256",
        "record_sha256",
    }
)
_SOURCE_FIELDS = frozenset(
    {
        "provider",
        "dataset",
        "request",
        "rows",
        "response_sha256",
        "usage_scope",
    }
)
_USAGE_SCOPES = frozenset({"internal_research_only", "personal_research_only"})
_SECRET_KEY_FRAGMENTS = (
    "authorization",
    "cookie",
    "credential",
    "mobile",
    "password",
    "phone",
    "secret",
    "token",
    "username",
)


@dataclass(frozen=True)
class SecurityMasterVersion:
    """One verified knowledge-time version and its parsed business-time view."""

    master: SecurityMaster
    record: dict[str, Any]
    path: Path
    reused: bool = False

    @property
    def version_id(self) -> str:
        return str(self.record["version_id"])

    @property
    def known_at(self) -> datetime:
        return _parse_timestamp(self.record["known_at"])

    @property
    def security_master_sha256(self) -> str:
        return str(self.record["security_master_sha256"])

    @property
    def record_sha256(self) -> str:
        return str(self.record["record_sha256"])

    def summary(self) -> dict[str, Any]:
        source = self.record["source_manifest"]
        return {
            "version_id": self.version_id,
            "known_at": self.record["known_at"],
            "previous_version_id": self.record["previous_version_id"],
            "security_master_sha256": self.security_master_sha256,
            "record_sha256": self.record_sha256,
            "provider": source["provider"],
            "dataset": source["dataset"],
            "rows": source["rows"],
            "usage_scope": source["usage_scope"],
            "reused": self.reused,
        }


class SecurityMasterVersionStore:
    """Create-once security-master versions ordered by when they became known."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    @property
    def versions_root(self) -> Path:
        return self.root / "versions"

    def publish(
        self,
        master: SecurityMaster | Mapping[str, Any],
        *,
        known_at: datetime,
        source_manifest: Mapping[str, Any],
    ) -> SecurityMasterVersion:
        timestamp = _timestamp_text(known_at)
        parsed_master = _coerce_master(master)
        master_payload = parsed_master.to_dict()
        master_sha256 = parsed_master.fingerprint()
        source = _normalize_source_manifest(source_manifest)

        with evidence_store_lock(self.root, "Security master version"):
            chain = self._load_chain(missing_ok=True)
            if chain:
                latest = chain[-1]
                latest_timestamp = str(latest.record["known_at"])
                if timestamp < latest_timestamp:
                    raise ValueError(
                        "Security master known_at must not precede the latest version"
                    )
                if timestamp == latest_timestamp:
                    if (
                        latest.security_master_sha256 == master_sha256
                        and latest.record["source_manifest"] == source
                    ):
                        return SecurityMasterVersion(
                            latest.master,
                            _json_clone(latest.record),
                            latest.path,
                            reused=True,
                        )
                    raise ValueError(
                        "Security master known_at already identifies another version"
                    )
            if len(chain) >= MAX_SECURITY_MASTER_VERSIONS:
                raise RuntimeError("Security master version capacity reached")

            previous = chain[-1] if chain else None
            record = _finalize_record(
                known_at=timestamp,
                previous=previous,
                source_manifest=source,
                master_payload=master_payload,
                master_sha256=master_sha256,
            )
            target = self.versions_root / f"{record['version_id']}.json"
            atomic_create_json(
                self.root,
                target,
                record,
                label="security master version",
                maximum_bytes=MAX_SECURITY_MASTER_VERSION_BYTES,
            )
            return self.get(str(record["version_id"]))

    def resolve(self, knowledge_cutoff: datetime) -> SecurityMasterVersion:
        cutoff = _parse_aware_datetime(knowledge_cutoff, "knowledge_cutoff")
        eligible = [item for item in self._load_chain() if item.known_at <= cutoff]
        if not eligible:
            raise KeyError(
                "No security master version was known by the requested cutoff"
            )
        return eligible[-1]

    def get(self, version_id: str) -> SecurityMasterVersion:
        _version_id(version_id)
        matches = [item for item in self._load_chain() if item.version_id == version_id]
        if len(matches) != 1:
            raise KeyError(version_id)
        return matches[0]

    def versions(self) -> list[dict[str, Any]]:
        return [item.summary() for item in self._load_chain(missing_ok=True)]

    def _load_chain(self, *, missing_ok: bool = False) -> list[SecurityMasterVersion]:
        root = self.versions_root
        if not root.exists():
            if missing_ok:
                return []
            raise RuntimeError("Security master version store is unavailable")
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError("Security master versions root is invalid")

        paths: list[Path] = []
        for path in root.iterdir():
            if (
                path.is_symlink()
                or not path.is_file()
                or path.suffix != ".json"
                or SECURITY_MASTER_VERSION_ID.fullmatch(path.stem) is None
            ):
                raise RuntimeError("Unexpected security master version member")
            paths.append(path)
            if len(paths) > MAX_SECURITY_MASTER_VERSIONS:
                raise RuntimeError("Security master version store exceeds capacity")

        versions = [self._read(path) for path in paths]
        versions.sort(key=lambda item: (item.known_at, item.version_id))
        timestamps = [item.known_at for item in versions]
        if len(timestamps) != len(set(timestamps)):
            raise RuntimeError("Security master knowledge times must be unique")

        previous: SecurityMasterVersion | None = None
        for item in versions:
            expected_id = previous.version_id if previous else None
            expected_sha = previous.record_sha256 if previous else None
            if (
                item.record["previous_version_id"] != expected_id
                or item.record["previous_record_sha256"] != expected_sha
            ):
                raise RuntimeError("Security master version chain is invalid")
            previous = item
        if not versions and not missing_ok:
            raise RuntimeError("Security master version store is empty")
        return versions

    def _read(self, path: Path) -> SecurityMasterVersion:
        try:
            value = load_unique_json(path, max_bytes=MAX_SECURITY_MASTER_VERSION_BYTES)
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeError(
                f"Invalid security master version {path.name}: {exc}"
            ) from exc
        if not isinstance(value, dict):
            raise RuntimeError("Security master version must be an object")
        try:
            master = validate_security_master_version(value)
        except ValueError as exc:
            raise RuntimeError(
                f"Invalid security master version {path.name}: {exc}"
            ) from exc
        if value["version_id"] != path.stem:
            raise RuntimeError(
                "Security master version id does not match its file name"
            )
        return SecurityMasterVersion(master, _json_clone(value), path)


def validate_security_master_version(
    value: Mapping[str, Any],
) -> SecurityMaster:
    if not isinstance(value, Mapping) or set(value) != _TOP_LEVEL_FIELDS:
        raise ValueError("Security master version top-level fields are invalid")
    if value.get("schema_version") != SECURITY_MASTER_VERSION_SCHEMA:
        raise ValueError("Security master version schema is invalid")

    version_id = _version_id(value.get("version_id"))
    canonical_timestamp = _timestamp_text(_parse_timestamp(value.get("known_at")))
    if value.get("known_at") != canonical_timestamp:
        raise ValueError("Security master known_at is not canonical UTC")

    previous_id = value.get("previous_version_id")
    previous_sha = value.get("previous_record_sha256")
    if previous_id is None or previous_sha is None:
        if previous_id is not None or previous_sha is not None:
            raise ValueError("Security master previous-version fields disagree")
    else:
        _version_id(previous_id)
        _fingerprint(previous_sha, "previous_record_sha256")

    source = _normalize_source_manifest(value.get("source_manifest"))
    if source != value.get("source_manifest"):
        raise ValueError("Security master source manifest is not canonical")

    master_payload = value.get("security_master")
    if not isinstance(master_payload, Mapping):
        raise ValueError("Security master version payload is invalid")
    master = SecurityMaster.from_dict(dict(master_payload))
    if master.to_dict() != master_payload:
        raise ValueError("Security master version payload is not canonical")
    master_sha256 = _fingerprint(
        value.get("security_master_sha256"), "security_master_sha256"
    )
    if master_sha256 != master.fingerprint():
        raise ValueError("Security master payload fingerprint does not match")

    expected_id = "smv_" + _version_fingerprint(value)[:32]
    if version_id != expected_id:
        raise ValueError("Security master version id does not match content")
    record_sha256 = _fingerprint(value.get("record_sha256"), "record_sha256")
    if record_sha256 != _record_fingerprint(value):
        raise ValueError("Security master record fingerprint does not match")
    return master


def _finalize_record(
    *,
    known_at: str,
    previous: SecurityMasterVersion | None,
    source_manifest: dict[str, Any],
    master_payload: dict[str, Any],
    master_sha256: str,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": SECURITY_MASTER_VERSION_SCHEMA,
        "version_id": None,
        "known_at": known_at,
        "previous_version_id": previous.version_id if previous else None,
        "previous_record_sha256": previous.record_sha256 if previous else None,
        "source_manifest": source_manifest,
        "security_master": master_payload,
        "security_master_sha256": master_sha256,
        "record_sha256": None,
    }
    record["version_id"] = "smv_" + _version_fingerprint(record)[:32]
    record["record_sha256"] = _record_fingerprint(record)
    validate_security_master_version(record)
    return record


def _coerce_master(
    value: SecurityMaster | Mapping[str, Any],
) -> SecurityMaster:
    if isinstance(value, SecurityMaster):
        return SecurityMaster.from_dict(value.to_dict())
    if not isinstance(value, Mapping):
        raise ValueError("Security master payload must be an object")
    return SecurityMaster.from_dict(dict(value))


def _normalize_source_manifest(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _SOURCE_FIELDS:
        raise ValueError("Security master source manifest fields are invalid")
    provider = _text(value.get("provider"), "source provider", 80).lower()
    dataset = _text(value.get("dataset"), "source dataset", 160)
    usage_scope = _text(value.get("usage_scope"), "source usage_scope", 80).lower()
    if usage_scope not in _USAGE_SCOPES:
        raise ValueError("Security master source usage_scope is invalid")
    if provider == "jqdata" and usage_scope != "personal_research_only":
        raise ValueError(
            "JQData security master data must remain personal research only"
        )

    rows = value.get("rows")
    if type(rows) is not int or not 1 <= rows <= 5_000_000:
        raise ValueError("Security master source row count is invalid")
    response_sha256 = _fingerprint(
        value.get("response_sha256"), "source response_sha256"
    )
    request = _json_clone(value.get("request"))
    _validate_request_value(request, depth=0, budget=[0])
    encoded = json.dumps(
        request,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise ValueError("Security master source request is too large")
    return {
        "provider": provider,
        "dataset": dataset,
        "request": request,
        "rows": rows,
        "response_sha256": response_sha256,
        "usage_scope": usage_scope,
    }


def _validate_request_value(value: object, *, depth: int, budget: list[int]) -> None:
    if depth > 6:
        raise ValueError("Security master source request is too deeply nested")
    budget[0] += 1
    if budget[0] > 1_000:
        raise ValueError("Security master source request has too many values")
    if value is None or type(value) is bool:
        return
    if isinstance(value, str):
        if (
            len(value) > 500
            or any(ord(character) < 32 for character in value)
            or _MOBILE_NUMBER.search(value)
        ):
            raise ValueError("Security master source request contains unsafe text")
        return
    if type(value) in {int, float}:
        if isinstance(value, float) and not math.isfinite(value):
            raise ValueError("Security master source request number is invalid")
        return
    if isinstance(value, list):
        for item in value:
            _validate_request_value(item, depth=depth + 1, budget=budget)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 100:
                raise ValueError("Security master source request key is invalid")
            normalized = re.sub(r"[^a-z0-9]", "", key.casefold())
            if any(fragment in normalized for fragment in _SECRET_KEY_FRAGMENTS):
                raise ValueError(
                    "Security master source request contains a credential field"
                )
            _validate_request_value(item, depth=depth + 1, budget=budget)
        return
    raise ValueError("Security master source request contains unsupported data")


def _version_fingerprint(value: Mapping[str, Any]) -> str:
    body = _json_clone(value)
    body["version_id"] = None
    body["record_sha256"] = None
    return _json_fingerprint(body)


def _record_fingerprint(value: Mapping[str, Any]) -> str:
    body = _json_clone(value)
    body["record_sha256"] = None
    return _json_fingerprint(body)


def _json_fingerprint(value: object) -> str:
    from hashlib import sha256

    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _parse_aware_datetime(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{label} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Security master known_at is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Security master known_at is invalid") from exc
    return _parse_aware_datetime(parsed, "known_at")


def _timestamp_text(value: datetime) -> str:
    return (
        _parse_aware_datetime(value, "known_at")
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _version_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or SECURITY_MASTER_VERSION_ID.fullmatch(value) is None
    ):
        raise ValueError("Security master version id is invalid")
    return value


def _fingerprint(value: object, label: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise ValueError(f"Security master {label} is invalid")
    return value


def _text(value: object, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"Security master {label} is invalid")
    return value


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


__all__ = [
    "MAX_SECURITY_MASTER_VERSION_BYTES",
    "MAX_SECURITY_MASTER_VERSIONS",
    "SECURITY_MASTER_VERSION_ID",
    "SECURITY_MASTER_VERSION_SCHEMA",
    "SecurityMasterVersion",
    "SecurityMasterVersionStore",
    "validate_security_master_version",
]
