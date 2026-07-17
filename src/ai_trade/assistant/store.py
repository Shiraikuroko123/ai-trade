from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from pathlib import Path
from typing import Any

from ..json_utils import load_unique_json

_ANALYSIS_ID = re.compile(r"[0-9a-f]{32}\Z")
MAX_ANALYSIS_RECORD_BYTES = 5 * 1024 * 1024


class AssistantRecordStore:
    """Per-user, atomic storage for public assistant results."""

    def __init__(self, project_root: Path):
        self.root = Path(project_root) / "state" / "assistant"
        self._lock = threading.RLock()

    def save(self, user_id: str, result: dict[str, Any]) -> Path:
        directory = self._user_directory(user_id)
        analysis_id = str(result.get("analysis_id", ""))
        if not _ANALYSIS_ID.fullmatch(analysis_id):
            raise ValueError("analysis_id must be a 32-character lowercase hex identifier")
        content = json.dumps(
            result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        if len(content) > MAX_ANALYSIS_RECORD_BYTES:
            raise ValueError("Assistant analysis record exceeds the 5 MiB storage limit")

        with self._lock:
            self.root.mkdir(parents=True, exist_ok=True)
            if self.root.is_symlink():
                raise RuntimeError("Assistant storage root must not be a symbolic link")
            directory.mkdir(exist_ok=True)
            if directory.is_symlink():
                raise RuntimeError("Assistant user storage must not be a symbolic link")
            target = directory / f"{analysis_id}.json"
            temporary = directory / f".{analysis_id}.{os.getpid()}.tmp"
            try:
                with temporary.open("xb") as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temporary, target)
            finally:
                try:
                    temporary.unlink(missing_ok=True)
                except OSError:
                    pass
        return target

    def history(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise ValueError("history limit must be an integer between 1 and 100")
        directory = self._user_directory(user_id)
        if not directory.exists():
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
                records.append(value)
                if len(records) >= limit:
                    break
        records.sort(
            key=lambda item: (str(item.get("created_at", "")), str(item["analysis_id"])),
            reverse=True,
        )
        return records[:limit]

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
