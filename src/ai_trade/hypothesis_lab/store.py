from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, List

from ..data.evidence_io import atomic_create_json, evidence_store_lock
from ..json_utils import load_unique_json
from .run_schema import RUN_ID, RUN_TOP_LEVEL_FIELDS, validate_run_record
from .schema import HYPOTHESIS_ID, TOP_LEVEL_FIELDS, validate_record


MAX_HYPOTHESIS_RECORD_BYTES = 512 * 1024
MAX_HYPOTHESES_PER_OWNER = 500
MAX_HYPOTHESES_PER_SNAPSHOT = 3
MAX_LIST_LIMIT = 100
MAX_RUN_RECORD_BYTES = 256 * 1024
MAX_RUNS_PER_OWNER = 500
MAX_RUNS_PER_HYPOTHESIS = 20


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

    def family_position(
        self, owner: str, snapshot_fingerprint: str, hypothesis_id: str
    ) -> int:
        """Return the 1-based registration order inside one snapshot family."""

        hypothesis_id = _hypothesis_id(hypothesis_id)
        members = sorted(
            (
                (str(item["created_at"]), str(item["hypothesis_id"]))
                for item in self._records_unlocked(owner, missing_ok=True)
                if item["evidence"]["snapshot"]["fingerprint"]
                == snapshot_fingerprint
            ),
        )
        for position, (_, member_id) in enumerate(members, start=1):
            if member_id == hypothesis_id:
                return position
        raise KeyError(hypothesis_id)

    def publish_run(self, owner: str, record: dict[str, Any]) -> dict[str, Any]:
        validate_run_record(record)
        owner_id = self.owner_id(owner)
        if record.get("owner") != owner_id:
            raise ValueError("Hypothesis run owner binding is invalid")
        run_id = _run_id(record.get("run_id"))
        hypothesis = self.get(owner, str(record["hypothesis_id"]))
        if hypothesis["record_fingerprint"] != record["hypothesis_record_fingerprint"]:
            raise ValueError("Hypothesis run is not bound to the stored hypothesis")
        if hypothesis["design_fingerprint"] != record["hypothesis_design_fingerprint"]:
            raise ValueError("Hypothesis run design binding is invalid")
        target = self.owner_directory(owner) / "runs" / f"{run_id}.json"

        with evidence_store_lock(self.root, "Hypothesis lab"):
            runs = self._run_records_unlocked(owner, missing_ok=True)
            execution = record["execution_fingerprint"]
            for existing in runs:
                if existing["execution_fingerprint"] == execution:
                    result = _clone(existing)
                    result["reused"] = True
                    return result
            if len(runs) >= MAX_RUNS_PER_OWNER:
                raise HypothesisLabCapacityError(
                    "Hypothesis run owner capacity reached "
                    f"({MAX_RUNS_PER_OWNER}); archive the owner directory first"
                )
            hypothesis_runs = sum(
                item["hypothesis_id"] == record["hypothesis_id"] for item in runs
            )
            if hypothesis_runs >= MAX_RUNS_PER_HYPOTHESIS:
                raise HypothesisLabCapacityError(
                    "Hypothesis run capacity reached for this hypothesis "
                    f"({MAX_RUNS_PER_HYPOTHESIS})"
                )
            atomic_create_json(
                self.root,
                target,
                record,
                label="hypothesis run record",
                maximum_bytes=MAX_RUN_RECORD_BYTES,
            )

        stored = self.get_run(owner, run_id)
        stored["reused"] = False
        return stored

    def get_run(self, owner: str, run_id: str) -> dict[str, Any]:
        run_id = _run_id(run_id)
        path = self.owner_directory(owner) / "runs" / f"{run_id}.json"
        if path.is_symlink() or not path.is_file():
            raise KeyError(run_id)
        record = _read_run_record(path)
        if record.get("run_id") != run_id:
            raise RuntimeError("Hypothesis run id does not match its file name")
        if record.get("owner") != self.owner_id(owner):
            raise RuntimeError("Hypothesis run owner binding is invalid")
        return record

    def list_runs(
        self,
        owner: str,
        *,
        limit: int = 50,
        hypothesis_id: str | None = None,
    ) -> dict[str, Any]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_LIST_LIMIT
        ):
            raise ValueError(
                f"Hypothesis run list limit must be between 1 and {MAX_LIST_LIMIT}"
            )
        if hypothesis_id is not None:
            hypothesis_id = _hypothesis_id(hypothesis_id)
        records = self._run_records_unlocked(owner, missing_ok=True)
        if hypothesis_id is not None:
            records = [
                item for item in records if item["hypothesis_id"] == hypothesis_id
            ]
        ordered = sorted(
            records,
            key=lambda item: (str(item["created_at"]), str(item["run_id"])),
            reverse=True,
        )
        visible = [
            {
                "run_id": item["run_id"],
                "hypothesis_id": item["hypothesis_id"],
                "mode": item["mode"],
                "created_at": item["created_at"],
                "executed_as_of": item["executed_snapshot"]["as_of"],
                "verdict": _clone(item["verdict"]),
            }
            for item in ordered[:limit]
        ]
        return {
            "schema_version": 1,
            "runs": visible,
            "summary": {
                "total": len(ordered),
                "returned": len(visible),
                "limit": limit,
                "maximum": MAX_RUNS_PER_OWNER,
                "truncated": len(ordered) > len(visible),
            },
            "safety": {
                "research_only": True,
                "verdict_grants_no_authority": True,
                "candidate_created": False,
                "approval_granted": False,
                "strategy_changed": False,
                "orders_created": False,
            },
        }

    def _run_records_unlocked(
        self, owner: str, *, missing_ok: bool
    ) -> List[dict[str, Any]]:
        directory = self.owner_directory(owner) / "runs"
        if not directory.exists():
            if missing_ok:
                return []
            raise RuntimeError("Hypothesis run owner directory is unavailable")
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("Hypothesis run owner directory is invalid")
        records: List[dict[str, Any]] = []
        for path in directory.iterdir():
            if (
                path.is_symlink()
                or not path.is_file()
                or path.suffix != ".json"
                or RUN_ID.fullmatch(path.stem) is None
            ):
                raise RuntimeError("Unexpected hypothesis run store member")
            record = _read_run_record(path)
            if record.get("run_id") != path.stem:
                raise RuntimeError("Hypothesis run id does not match its file name")
            if record.get("owner") != self.owner_id(owner):
                raise RuntimeError("Hypothesis run owner binding is invalid")
            records.append(record)
            if len(records) > MAX_RUNS_PER_OWNER:
                raise RuntimeError("Hypothesis run store exceeds its capacity")
        return records

    def _records_unlocked(
        self, owner: str, *, missing_ok: bool
    ) -> List[dict[str, Any]]:
        directory = self.owner_directory(owner) / "hypotheses"
        if not directory.exists():
            if missing_ok:
                return []
            raise RuntimeError("Hypothesis owner directory is unavailable")
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("Hypothesis owner directory is invalid")
        records: List[dict[str, Any]] = []
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


def _read_run_record(path: Path) -> dict[str, Any]:
    try:
        value = load_unique_json(path, max_bytes=MAX_RUN_RECORD_BYTES)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Invalid hypothesis run record: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Hypothesis run record must be an object")
    unsupported = sorted(set(value) - RUN_TOP_LEVEL_FIELDS)
    if unsupported:
        raise RuntimeError(
            "Hypothesis run schema fields are invalid: " + ", ".join(unsupported)
        )
    try:
        validate_run_record(value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid hypothesis run record: {path}: {exc}") from exc
    return value


def _hypothesis_id(value: Any) -> str:
    if not isinstance(value, str) or HYPOTHESIS_ID.fullmatch(value) is None:
        raise ValueError("Invalid hypothesis id")
    return value


def _run_id(value: Any) -> str:
    if not isinstance(value, str) or RUN_ID.fullmatch(value) is None:
        raise ValueError("Invalid hypothesis run id")
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
    "MAX_RUN_RECORD_BYTES",
    "MAX_RUNS_PER_HYPOTHESIS",
    "MAX_RUNS_PER_OWNER",
]
