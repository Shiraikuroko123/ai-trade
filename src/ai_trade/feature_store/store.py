from __future__ import annotations

from datetime import date
import json
from pathlib import Path
import re
from typing import Any

from ..data.evidence_io import atomic_create_json, evidence_store_lock
from ..json_utils import load_unique_json
from .schema import (
    FEATURE_SNAPSHOT_ID,
    validate_feature_snapshot,
)


MAX_FEATURE_SNAPSHOT_BYTES = 8 * 1024 * 1024
MAX_FEATURE_SESSIONS = 5_000
MAX_FEATURE_REVISIONS_PER_SESSION = 100
_DATE_DIRECTORY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")


class FeatureSnapshotStore:
    """Create-once storage for point-in-time feature cross-sections."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    @property
    def snapshots_root(self) -> Path:
        return self.root / "snapshots"

    def publish(self, record: dict[str, Any]) -> dict[str, Any]:
        validate_feature_snapshot(record)
        snapshot_id = str(record["snapshot_id"])
        on_date = date.fromisoformat(str(record["as_of_session"]))
        target = self._path(on_date, snapshot_id)
        with evidence_store_lock(self.root, "Feature snapshot"):
            if target.exists() or target.is_symlink():
                existing = self._read(target)
                if existing["snapshot_fingerprint"] != record["snapshot_fingerprint"]:
                    raise RuntimeError("Feature snapshot id collision")
                result = _clone(existing)
                result["reused"] = True
                return result
            sessions = self.sessions()
            if on_date not in sessions and len(sessions) >= MAX_FEATURE_SESSIONS:
                raise RuntimeError("Feature snapshot session capacity reached")
            existing_for_session = self._session_paths(on_date, missing_ok=True)
            if len(existing_for_session) >= MAX_FEATURE_REVISIONS_PER_SESSION:
                raise RuntimeError("Feature snapshot revision capacity reached")
            atomic_create_json(
                self.root,
                target,
                record,
                label="feature snapshot",
                maximum_bytes=MAX_FEATURE_SNAPSHOT_BYTES,
            )
        result = self.get(snapshot_id, on_date=on_date)
        result["reused"] = False
        return result

    def get(
        self, snapshot_id: str, *, on_date: date | None = None
    ) -> dict[str, Any]:
        _snapshot_id(snapshot_id)
        if on_date is not None:
            path = self._path(on_date, snapshot_id)
            if path.is_symlink() or not path.is_file():
                raise KeyError(snapshot_id)
            return self._read(path)
        matches = [
            path
            for session in self.sessions()
            for path in self._session_paths(session)
            if path.stem == snapshot_id
        ]
        if len(matches) != 1:
            raise KeyError(snapshot_id)
        return self._read(matches[0])

    def latest(
        self,
        *,
        on_or_before: date | None = None,
        feature_set_id: str | None = None,
        historical_reconstruction: bool | None = None,
    ) -> dict[str, Any] | None:
        sessions = [
            item
            for item in self.sessions()
            if on_or_before is None or item <= on_or_before
        ]
        for session in reversed(sessions):
            records = [self._read(path) for path in self._session_paths(session)]
            if feature_set_id is not None:
                records = [
                    item
                    for item in records
                    if item["feature_set"]["feature_set_id"] == feature_set_id
                ]
            if historical_reconstruction is not None:
                records = [
                    item
                    for item in records
                    if item["historical_reconstruction"]
                    is historical_reconstruction
                ]
            if records:
                return max(
                    records,
                    key=lambda item: (str(item["created_at"]), str(item["snapshot_id"])),
                )
        return None

    def sessions(self) -> list[date]:
        root = self.snapshots_root
        if not root.exists():
            return []
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError("Feature snapshot root is invalid")
        sessions: list[date] = []
        for path in root.iterdir():
            if (
                path.is_symlink()
                or not path.is_dir()
                or _DATE_DIRECTORY.fullmatch(path.name) is None
            ):
                raise RuntimeError("Unexpected feature snapshot session member")
            sessions.append(date.fromisoformat(path.name))
            if len(sessions) > MAX_FEATURE_SESSIONS:
                raise RuntimeError("Feature snapshot store exceeds its capacity")
        return sorted(sessions)

    def _session_paths(
        self, on_date: date, *, missing_ok: bool = False
    ) -> list[Path]:
        directory = self.snapshots_root / on_date.isoformat()
        if not directory.exists():
            if missing_ok:
                return []
            raise RuntimeError("Feature snapshot session is unavailable")
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("Feature snapshot session is invalid")
        paths: list[Path] = []
        for path in directory.iterdir():
            if (
                path.is_symlink()
                or not path.is_file()
                or path.suffix != ".json"
                or FEATURE_SNAPSHOT_ID.fullmatch(path.stem) is None
            ):
                raise RuntimeError("Unexpected feature snapshot file")
            paths.append(path)
        if len(paths) > MAX_FEATURE_REVISIONS_PER_SESSION:
            raise RuntimeError("Feature snapshot session exceeds its capacity")
        return sorted(paths, key=lambda item: item.name)

    def _path(self, on_date: date, snapshot_id: str) -> Path:
        _snapshot_id(snapshot_id)
        return self.snapshots_root / on_date.isoformat() / f"{snapshot_id}.json"

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            value = load_unique_json(path, max_bytes=MAX_FEATURE_SNAPSHOT_BYTES)
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeError(f"Invalid feature snapshot {path.name}: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Feature snapshot must be an object")
        try:
            validate_feature_snapshot(value)
        except ValueError as exc:
            raise RuntimeError(f"Invalid feature snapshot {path.name}: {exc}") from exc
        if value["snapshot_id"] != path.stem:
            raise RuntimeError("Feature snapshot id does not match its file name")
        if value["as_of_session"] != path.parent.name:
            raise RuntimeError("Feature snapshot date does not match its directory")
        return value


def _snapshot_id(value: object) -> str:
    if not isinstance(value, str) or FEATURE_SNAPSHOT_ID.fullmatch(value) is None:
        raise ValueError("Invalid feature snapshot id")
    return value


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


__all__ = [
    "FeatureSnapshotStore",
    "MAX_FEATURE_REVISIONS_PER_SESSION",
    "MAX_FEATURE_SESSIONS",
    "MAX_FEATURE_SNAPSHOT_BYTES",
]
