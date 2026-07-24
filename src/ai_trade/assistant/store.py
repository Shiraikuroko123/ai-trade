from __future__ import annotations

import hashlib
import json
import re
import threading
from pathlib import Path
from typing import Any

from ..data.evidence_io import atomic_create_json
from ..json_utils import load_unique_json
from .governance import verify_call_audit_summary

_ANALYSIS_ID = re.compile(r"[0-9a-f]{32}\Z")
MAX_ANALYSIS_RECORD_BYTES = 5 * 1024 * 1024
CALL_AUDIT_BINDING_VERSION = 1
_CALL_ROLES = {
    "wording": "research_assistant_wording",
    "bull": "research_debate_bull",
    "bear": "research_debate_bear",
    "judge": "research_debate_judge",
}


class AssistantRecordStore:
    """Per-user, atomic storage for public assistant results."""

    def __init__(self, project_root: Path):
        self.project_root = Path(project_root)
        self.root = Path(project_root) / "state" / "assistant"
        self._lock = threading.RLock()

    def save(self, user_id: str, result: dict[str, Any]) -> Path:
        directory = self._user_directory(user_id)
        analysis_id = str(result.get("analysis_id", ""))
        if not _ANALYSIS_ID.fullmatch(analysis_id):
            raise ValueError("analysis_id must be a 32-character lowercase hex identifier")
        if "record_sha256" in result:
            raise ValueError("record_sha256 is assigned by the assistant record store")
        expected_binding = self.call_audit_binding(user_id, result)
        if result.get("call_audit_binding") != expected_binding:
            raise ValueError("call_audit_binding does not match immutable call evidence")
        stored = dict(result)
        stored["record_sha256"] = _record_sha256(stored)
        content = json.dumps(
            stored,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(content) + 1 > MAX_ANALYSIS_RECORD_BYTES:
            raise ValueError("Assistant analysis record exceeds the 5 MiB storage limit")

        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            if self.root.is_symlink():
                raise RuntimeError("Assistant storage root must not be a symbolic link")
            target = directory / f"{analysis_id}.json"
            atomic_create_json(
                self.root,
                target,
                stored,
                label="assistant analysis",
                maximum_bytes=MAX_ANALYSIS_RECORD_BYTES,
            )
        return target

    def history(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("history limit must be an integer between 1 and 100")
        directory = self._user_directory(user_id)
        if not directory.exists():
            return []
        if self.root.is_symlink() or not self.root.is_dir():
            return []
        if directory.is_symlink() or not directory.is_dir():
            return []

        records: list[dict[str, Any]] = []
        with self._lock:
            paths = sorted(
                (
                    path
                    for path in directory.glob("*.json")
                    if _ANALYSIS_ID.fullmatch(path.stem) and not path.is_symlink()
                ),
                key=lambda path: path.stat().st_mtime_ns,
                reverse=True,
            )
            for path in paths:
                try:
                    if path.stat().st_size > MAX_ANALYSIS_RECORD_BYTES:
                        continue
                    value = load_unique_json(
                        path,
                        max_bytes=MAX_ANALYSIS_RECORD_BYTES,
                    )
                except (OSError, UnicodeError, ValueError):
                    continue
                if not isinstance(value, dict) or value.get("schema_version") != 1:
                    continue
                if value.get("analysis_id") != path.stem:
                    continue
                record_sha256 = value.get("record_sha256")
                if record_sha256 is not None and (
                    not isinstance(record_sha256, str)
                    or record_sha256 != _record_sha256(value)
                ):
                    continue
                binding = value.get("call_audit_binding")
                if binding is not None:
                    try:
                        expected = self.call_audit_binding(user_id, value)
                    except (OSError, RuntimeError, TypeError, ValueError):
                        continue
                    if binding != expected:
                        continue
                records.append(value)
                if len(records) >= limit:
                    break
        records.sort(
            key=lambda item: (str(item.get("created_at", "")), str(item["analysis_id"])),
            reverse=True,
        )
        return records[:limit]

    def call_audit_binding(
        self, user_id: str, result: dict[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(result, dict):
            raise TypeError("assistant result must be an object")
        calls = _call_summaries(result)
        call_ids: set[str] = set()
        bound: list[dict[str, Any]] = []
        for slot, expected_role, summary in calls:
            if summary.get("role") != expected_role:
                raise ValueError(f"{slot} call role does not match its result slot")
            call_id = summary.get("call_id")
            if not isinstance(call_id, str) or call_id in call_ids:
                raise ValueError("assistant call identifiers must be unique")
            if not verify_call_audit_summary(self.project_root, user_id, summary):
                raise ValueError(f"{slot} call audit evidence is unavailable or invalid")
            call_ids.add(call_id)
            bound.append({"slot": slot, "summary": summary})

        mode = result.get("mode")
        if mode == "local" and bound:
            raise ValueError("local assistant results must not contain model calls")
        return {
            "schema_version": CALL_AUDIT_BINDING_VERSION,
            "status": "VERIFIED" if bound else "NO_CALLS",
            "call_count": len(bound),
            "calls_sha256": _json_sha256(bound),
        }

    def _user_directory(self, user_id: str) -> Path:
        normalized = _validate_user_id(user_id)
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return self.root / digest


def _validate_user_id(user_id: str) -> str:
    if not isinstance(user_id, str):
        raise TypeError("user_id must be a string")
    normalized = user_id.strip()
    if not normalized or len(normalized.encode("utf-8")) > 256 or "\x00" in normalized:
        raise ValueError("user_id must contain between 1 and 256 UTF-8 bytes")
    return normalized


def _record_sha256(value: dict[str, Any]) -> str:
    content = {key: item for key, item in value.items() if key != "record_sha256"}
    return hashlib.sha256(
        json.dumps(
            content,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _call_summaries(
    result: dict[str, Any],
) -> list[tuple[str, str, dict[str, Any]]]:
    calls: list[tuple[str, str, dict[str, Any]]] = []
    validation = result.get("validation")
    if isinstance(validation, dict):
        wording = validation.get("model_call")
        if wording is not None:
            if not isinstance(wording, dict):
                raise ValueError("wording call summary must be an object")
            calls.append(("wording", _CALL_ROLES["wording"], wording))
        if validation.get("model_enhanced") is True and wording is None:
            raise ValueError("model-enhanced result is missing its wording call audit")

    debate = result.get("debate")
    roles = debate.get("roles") if isinstance(debate, dict) else None
    if isinstance(roles, dict):
        for role in ("bull", "bear", "judge"):
            value = roles.get(role)
            call = value.get("call") if isinstance(value, dict) else None
            if call is None:
                continue
            if not isinstance(call, dict):
                raise ValueError(f"{role} call summary must be an object")
            calls.append((role, _CALL_ROLES[role], call))
    return calls


def _json_sha256(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
