from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any

from ..data.evidence_io import atomic_create_json, evidence_store_lock
from ..json_utils import load_unique_json
from .schema import HYPOTHESIS_ID, TOP_LEVEL_FIELDS, validate_record


MAX_HYPOTHESIS_RECORD_BYTES = 512 * 1024
MAX_HYPOTHESES_PER_OWNER = 500
MAX_HYPOTHESES_PER_SNAPSHOT = 3
MAX_LIST_LIMIT = 100


class HypothesisLabCapacityError(RuntimeError):
    pass


class HypothesisLabStore:
    """Owner-isolated, create-once storage for pre-registered hypotheses."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def owner_id(self, owner: str) -> str:
        normalized = _normalize_owner(owner)
        return sha256(normalized.encode("utf-8")).hexdigest()

    def owner_directory(self, owner: str) -> Path:
        return self.root / "users" / self.owner_id(owner)

    def publish(self, owner: str, record: dict[str, Any]) -> dict[str, Any]:
        validate_record(record)
        owner_id = self.owner_id(owner)
        if record.get("owner") != owner_id:
            raise ValueError("Hypothesis owner binding is invalid")
        hypothesis_id = _hypothesis_id(record.get("hypothesis_id"))
        target = self.owner_directory(owner) / "hypotheses" / f"{hypothesis_id}.json"

        with evidence_store_lock(self.root, "Hypothesis lab"):
            records = self._records_unlocked(owner, missing_ok=True)
            design = record["design_fingerprint"]
            for existing in records:
                if existing["design_fingerprint"] == design:
                    result = _clone(existing)
                    result["reused"] = True
                    return result
            if len(records) >= MAX_HYPOTHESES_PER_OWNER:
                raise HypothesisLabCapacityError(
                    "Hypothesis owner capacity reached "
                    f"({MAX_HYPOTHESES_PER_OWNER}); archive the owner directory first"
                )
            snapshot_fingerprint = record["evidence"]["snapshot"]["fingerprint"]
            snapshot_count = sum(
                item["evidence"]["snapshot"]["fingerprint"]
                == snapshot_fingerprint
                for item in records
            )
            if snapshot_count >= MAX_HYPOTHESES_PER_SNAPSHOT:
                raise HypothesisLabCapacityError(
                    "Hypothesis multiple-testing budget reached for this snapshot "
                    f"({MAX_HYPOTHESES_PER_SNAPSHOT})"
                )
            atomic_create_json(
                self.root,
                target,
                record,
                label="hypothesis record",
                maximum_bytes=MAX_HYPOTHESIS_RECORD_BYTES,
            )

        stored = self.get(owner, hypothesis_id)
        stored["reused"] = False
        return stored

    def get(self, owner: str, hypothesis_id: str) -> dict[str, Any]:
        hypothesis_id = _hypothesis_id(hypothesis_id)
        path = self.owner_directory(owner) / "hypotheses" / f"{hypothesis_id}.json"
        if path.is_symlink() or not path.is_file():
            raise KeyError(hypothesis_id)
        record = _read_record(path)
        if record.get("hypothesis_id") != hypothesis_id:
            raise RuntimeError("Hypothesis id does not match its file name")
        if record.get("owner") != self.owner_id(owner):
            raise RuntimeError("Hypothesis owner binding is invalid")
        return record

    def list(self, owner: str, *, limit: int = 50) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIST_LIMIT:
            raise ValueError(f"Hypothesis list limit must be between 1 and {MAX_LIST_LIMIT}")
        records = self._records_unlocked(owner, missing_ok=True)
        ordered = sorted(
            records,
            key=lambda item: (str(item["created_at"]), str(item["hypothesis_id"])),
            reverse=True,
        )
        visible = ordered[:limit]
        return {
            "schema_version": 1,
            "hypotheses": visible,
            "summary": {
                "total": len(ordered),
                "returned": len(visible),
                "limit": limit,
                "maximum": MAX_HYPOTHESES_PER_OWNER,
                "truncated": len(ordered) > len(visible),
            },
            "safety": {
                "research_only": True,
                "candidate_created": False,
                "approval_granted": False,
                "strategy_changed": False,
                "orders_created": False,
            },
        }

    def _records_unlocked(
        self, owner: str, *, missing_ok: bool
    ) -> list[dict[str, Any]]:
        directory = self.owner_directory(owner) / "hypotheses"
        if not directory.exists():
            if missing_ok:
                return []
            raise RuntimeError("Hypothesis owner directory is unavailable")
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("Hypothesis owner directory is invalid")
        records: list[dict[str, Any]] = []
        for path in directory.iterdir():
            if (
                path.is_symlink()
                or not path.is_file()
                or path.suffix != ".json"
                or HYPOTHESIS_ID.fullmatch(path.stem) is None
            ):
                raise RuntimeError("Unexpected hypothesis store member")
            record = _read_record(path)
            if record.get("hypothesis_id") != path.stem:
                raise RuntimeError("Hypothesis id does not match its file name")
            if record.get("owner") != self.owner_id(owner):
                raise RuntimeError("Hypothesis owner binding is invalid")
            records.append(record)
            if len(records) > MAX_HYPOTHESES_PER_OWNER:
                raise RuntimeError("Hypothesis owner store exceeds its capacity")
        return records


def _read_record(path: Path) -> dict[str, Any]:
    try:
        value = load_unique_json(path, max_bytes=MAX_HYPOTHESIS_RECORD_BYTES)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Invalid hypothesis record: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Hypothesis record must be an object")
    unsupported = sorted(set(value) - TOP_LEVEL_FIELDS)
    if unsupported:
        raise RuntimeError(
            "Hypothesis schema fields are invalid: " + ", ".join(unsupported)
        )
    try:
        validate_record(value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid hypothesis record: {path}: {exc}") from exc
    return value


def _hypothesis_id(value: Any) -> str:
    if not isinstance(value, str) or HYPOTHESIS_ID.fullmatch(value) is None:
        raise ValueError("Invalid hypothesis id")
    return value


def _normalize_owner(owner: str) -> str:
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("Hypothesis owner must be a non-empty string")
    normalized = owner.strip().casefold()
    if len(normalized.encode("utf-8")) > 256 or "\x00" in normalized:
        raise ValueError("Hypothesis owner is too long or contains a null byte")
    return normalized


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


__all__ = [
    "HypothesisLabCapacityError",
    "HypothesisLabStore",
    "MAX_HYPOTHESES_PER_OWNER",
    "MAX_HYPOTHESES_PER_SNAPSHOT",
    "MAX_HYPOTHESIS_RECORD_BYTES",
]
