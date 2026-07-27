from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
from typing import Any, List

from ..data.evidence_io import atomic_create_json, evidence_store_lock
from ..json_utils import load_unique_json
from .schema import EVALUATION_ID, TOP_LEVEL_FIELDS, validate_evaluation


MAX_EVALUATION_RECORD_BYTES = 256 * 1024
MAX_EVALUATIONS_PER_OWNER = 500
MAX_EVALUATIONS_PER_MODEL = 50
MAX_LIST_LIMIT = 100


class ModelLabCapacityError(RuntimeError):
    pass


class ModelLabStore:
    """Owner-isolated, create-once storage for model evaluation evidence."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def owner_id(self, owner: str) -> str:
        normalized = _normalize_owner(owner)
        return sha256(normalized.encode("utf-8")).hexdigest()

    def owner_directory(self, owner: str) -> Path:
        return self.root / "users" / self.owner_id(owner)

    def publish(self, owner: str, record: dict[str, Any]) -> dict[str, Any]:
        validate_evaluation(record)
        owner_id = self.owner_id(owner)
        if record.get("owner") != owner_id:
            raise ValueError("Model evaluation owner binding is invalid")
        evaluation_id = _evaluation_id(record.get("evaluation_id"))
        target = (
            self.owner_directory(owner)
            / "evaluations"
            / f"{evaluation_id}.json"
        )

        with evidence_store_lock(self.root, "Model lab"):
            records = self._records_unlocked(owner, missing_ok=True)
            evaluation = record["evaluation_fingerprint"]
            for existing in records:
                if existing["evaluation_fingerprint"] == evaluation:
                    result = _clone(existing)
                    result["reused"] = True
                    return result
            if len(records) >= MAX_EVALUATIONS_PER_OWNER:
                raise ModelLabCapacityError(
                    "Model evaluation owner capacity reached "
                    f"({MAX_EVALUATIONS_PER_OWNER}); archive the owner "
                    "directory first"
                )
            model_count = sum(
                item["model"]["model_id"] == record["model"]["model_id"]
                for item in records
            )
            if model_count >= MAX_EVALUATIONS_PER_MODEL:
                raise ModelLabCapacityError(
                    "Model evaluation capacity reached for this model "
                    f"({MAX_EVALUATIONS_PER_MODEL})"
                )
            atomic_create_json(
                self.root,
                target,
                record,
                label="model evaluation record",
                maximum_bytes=MAX_EVALUATION_RECORD_BYTES,
            )

        stored = self.get(owner, evaluation_id)
        stored["reused"] = False
        return stored

    def get(self, owner: str, evaluation_id: str) -> dict[str, Any]:
        evaluation_id = _evaluation_id(evaluation_id)
        path = (
            self.owner_directory(owner)
            / "evaluations"
            / f"{evaluation_id}.json"
        )
        if path.is_symlink() or not path.is_file():
            raise KeyError(evaluation_id)
        record = _read_record(path)
        if record.get("evaluation_id") != evaluation_id:
            raise RuntimeError("Model evaluation id does not match its file name")
        if record.get("owner") != self.owner_id(owner):
            raise RuntimeError("Model evaluation owner binding is invalid")
        return record

    def list(
        self,
        owner: str,
        *,
        limit: int = 50,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_LIST_LIMIT
        ):
            raise ValueError(
                f"Model evaluation list limit must be between 1 and {MAX_LIST_LIMIT}"
            )
        records = self._records_unlocked(owner, missing_ok=True)
        if model_id is not None:
            records = [
                item for item in records if item["model"]["model_id"] == model_id
            ]
        ordered = sorted(
            records,
            key=lambda item: (
                str(item["created_at"]),
                str(item["evaluation_id"]),
            ),
            reverse=True,
        )
        visible = [
            {
                "evaluation_id": item["evaluation_id"],
                "model_id": item["model"]["model_id"],
                "created_at": item["created_at"],
                "as_of": item["evidence"]["snapshot"]["as_of"],
                "horizon": item["parameters"]["horizon"],
                "mean_ic": item["results"]["model"]["mean_ic"],
                "ic_ir": item["results"]["model"]["ic_ir"],
                "model_minus_best_factor_ic": item["results"][
                    "model_minus_best_factor_ic"
                ],
            }
            for item in ordered[:limit]
        ]
        return {
            "schema_version": 1,
            "evaluations": visible,
            "summary": {
                "total": len(ordered),
                "returned": len(visible),
                "limit": limit,
                "maximum": MAX_EVALUATIONS_PER_OWNER,
                "truncated": len(ordered) > len(visible),
            },
            "safety": {
                "research_only": True,
                "creates_no_signal": True,
                "candidate_created": False,
                "orders_created": False,
            },
        }

    def _records_unlocked(
        self, owner: str, *, missing_ok: bool
    ) -> List[dict[str, Any]]:
        directory = self.owner_directory(owner) / "evaluations"
        if not directory.exists():
            if missing_ok:
                return []
            raise RuntimeError("Model evaluation owner directory is unavailable")
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("Model evaluation owner directory is invalid")
        records: List[dict[str, Any]] = []
        for path in directory.iterdir():
            if (
                path.is_symlink()
                or not path.is_file()
                or path.suffix != ".json"
                or EVALUATION_ID.fullmatch(path.stem) is None
            ):
                raise RuntimeError("Unexpected model evaluation store member")
            record = _read_record(path)
            if record.get("evaluation_id") != path.stem:
                raise RuntimeError(
                    "Model evaluation id does not match its file name"
                )
            if record.get("owner") != self.owner_id(owner):
                raise RuntimeError("Model evaluation owner binding is invalid")
            records.append(record)
            if len(records) > MAX_EVALUATIONS_PER_OWNER:
                raise RuntimeError("Model evaluation store exceeds its capacity")
        return records


def _read_record(path: Path) -> dict[str, Any]:
    try:
        value = load_unique_json(path, max_bytes=MAX_EVALUATION_RECORD_BYTES)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(
            f"Invalid model evaluation record: {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeError("Model evaluation record must be an object")
    unsupported = sorted(set(value) - TOP_LEVEL_FIELDS)
    if unsupported:
        raise RuntimeError(
            "Model evaluation schema fields are invalid: "
            + ", ".join(unsupported)
        )
    try:
        validate_evaluation(value)
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid model evaluation record: {path}: {exc}"
        ) from exc
    return value


def _evaluation_id(value: Any) -> str:
    if not isinstance(value, str) or EVALUATION_ID.fullmatch(value) is None:
        raise ValueError("Invalid model evaluation id")
    return value


def _normalize_owner(owner: str) -> str:
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("Model evaluation owner must be a non-empty string")
    normalized = owner.strip().casefold()
    if len(normalized.encode("utf-8")) > 256 or "\x00" in normalized:
        raise ValueError(
            "Model evaluation owner is too long or contains a null byte"
        )
    return normalized


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


__all__ = [
    "MAX_EVALUATION_RECORD_BYTES",
    "MAX_EVALUATIONS_PER_MODEL",
    "MAX_EVALUATIONS_PER_OWNER",
    "ModelLabCapacityError",
    "ModelLabStore",
]
