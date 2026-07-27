from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import re
from typing import Any, Mapping
from uuid import uuid4

from ..data.evidence_io import atomic_create_json, evidence_store_lock
from ..json_utils import load_unique_json
from .schema import (
    EVENT_ID,
    LOOP_ID,
    LOOP_SAFETY,
    finalize_event,
    validate_event,
)


MAX_EVENT_BYTES = 256 * 1024
MAX_EVENTS_PER_LOOP = 100
MAX_LOOPS_PER_OWNER = 100
_EVENT_FILE = re.compile(r"(\d{6})_(rle_[0-9a-f]{32})\.json\Z")


class ResearchLoopLedger:
    """Append-only hash chain for one bounded research loop."""

    def __init__(
        self,
        root: str | Path,
        owner: str,
        loop_id: str | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.owner = _normalize_owner(owner)
        self.owner_id = sha256(self.owner.encode("utf-8")).hexdigest()
        selected = loop_id or f"loop_{uuid4().hex}"
        if LOOP_ID.fullmatch(selected) is None:
            raise ValueError("Invalid research loop id")
        self.loop_id = selected
        self.owner_root = self.root / "users" / self.owner_id
        self.loop_root = self.owner_root / "loops" / self.loop_id
        self.events_root = self.loop_root / "events"

    def append(self, event_type: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        draft = {
            "loop_id": self.loop_id,
            "owner": self.owner_id,
            "event_id": f"rle_{uuid4().hex}",
            "created_at": _utc_now(),
            "event_type": event_type,
            "payload": _clone(payload),
        }
        with evidence_store_lock(self.owner_root, "Research loop ledger"):
            events = self._events_unlocked(missing_ok=True)
            if not events:
                self._check_owner_capacity_unlocked()
            if len(events) >= MAX_EVENTS_PER_LOOP:
                raise RuntimeError("Research loop event capacity reached")
            previous = events[-1]["record_fingerprint"] if events else None
            record = finalize_event(
                draft,
                sequence=len(events) + 1,
                previous_record_fingerprint=previous,
            )
            target = self.events_root / (
                f"{record['sequence']:06d}_{record['event_id']}.json"
            )
            atomic_create_json(
                self.owner_root,
                target,
                record,
                label="research loop event",
                maximum_bytes=MAX_EVENT_BYTES,
            )
        return self.events()[-1]

    def events(self) -> list[dict[str, Any]]:
        return self._events_unlocked(missing_ok=True)

    def snapshot(self) -> dict[str, Any]:
        events = self.events()
        if not events:
            raise KeyError(self.loop_id)
        finished = next(
            (
                item
                for item in reversed(events)
                if item["event_type"] == "loop_finished"
            ),
            None,
        )
        return {
            "schema_version": 1,
            "loop_id": self.loop_id,
            "owner": self.owner_id,
            "status": (
                finished["payload"].get("status") if finished is not None else "incomplete"
            ),
            "event_count": len(events),
            "events": events,
            "safety": dict(LOOP_SAFETY),
        }

    def _events_unlocked(self, *, missing_ok: bool) -> list[dict[str, Any]]:
        root = self.events_root
        if not root.exists():
            if missing_ok:
                return []
            raise RuntimeError("Research loop event directory is unavailable")
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError("Research loop event directory is invalid")
        paths: list[tuple[int, str, Path]] = []
        for path in root.iterdir():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("Research loop event must be a regular file")
            match = _EVENT_FILE.fullmatch(path.name)
            if match is None:
                raise RuntimeError("Unexpected research loop event file")
            paths.append((int(match.group(1)), match.group(2), path))
        paths.sort()
        if len(paths) > MAX_EVENTS_PER_LOOP:
            raise RuntimeError("Research loop ledger exceeds capacity")
        if [item[0] for item in paths] != list(range(1, len(paths) + 1)):
            raise RuntimeError("Research loop event sequence is not contiguous")

        records: list[dict[str, Any]] = []
        previous: str | None = None
        event_ids: set[str] = set()
        for sequence, event_id, path in paths:
            try:
                value = load_unique_json(path, max_bytes=MAX_EVENT_BYTES)
            except (OSError, UnicodeError, ValueError) as exc:
                raise RuntimeError(
                    f"Invalid research loop event {path.name}: {exc}"
                ) from exc
            if not isinstance(value, dict):
                raise RuntimeError("Research loop event must be an object")
            try:
                validate_event(value)
            except ValueError as exc:
                raise RuntimeError(
                    f"Invalid research loop event {path.name}: {exc}"
                ) from exc
            if (
                value["loop_id"] != self.loop_id
                or value["owner"] != self.owner_id
                or value["sequence"] != sequence
                or value["event_id"] != event_id
                or value["previous_record_fingerprint"] != previous
            ):
                raise RuntimeError("Research loop event hash chain is inconsistent")
            if event_id in event_ids or EVENT_ID.fullmatch(event_id) is None:
                raise RuntimeError("Research loop ledger contains a duplicate event")
            event_ids.add(event_id)
            records.append(value)
            previous = str(value["record_fingerprint"])
        return records

    def _check_owner_capacity_unlocked(self) -> None:
        loops_root = self.owner_root / "loops"
        if not loops_root.exists():
            return
        if loops_root.is_symlink() or not loops_root.is_dir():
            raise RuntimeError("Research loop owner directory is invalid")
        directories = list(loops_root.iterdir())
        if any(
            item.is_symlink()
            or not item.is_dir()
            or LOOP_ID.fullmatch(item.name) is None
            for item in directories
        ):
            raise RuntimeError("Unexpected research loop owner store member")
        if self.loop_root not in directories and len(directories) >= MAX_LOOPS_PER_OWNER:
            raise RuntimeError("Research loop owner capacity reached")


class ResearchLoopStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()

    def get(self, owner: str, loop_id: str) -> dict[str, Any]:
        return ResearchLoopLedger(self.root, owner, loop_id).snapshot()

    def list(self, owner: str, *, limit: int = 50) -> dict[str, Any]:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("Research loop list limit must be between 1 and 100")
        probe = ResearchLoopLedger(self.root, owner, f"loop_{'0' * 32}")
        loops_root = probe.owner_root / "loops"
        if not loops_root.exists():
            directories: list[Path] = []
        else:
            if loops_root.is_symlink() or not loops_root.is_dir():
                raise RuntimeError("Research loop owner directory is invalid")
            directories = list(loops_root.iterdir())
        if any(
            item.is_symlink()
            or not item.is_dir()
            or LOOP_ID.fullmatch(item.name) is None
            for item in directories
        ):
            raise RuntimeError("Unexpected research loop owner store member")
        if len(directories) > MAX_LOOPS_PER_OWNER:
            raise RuntimeError("Research loop owner store exceeds capacity")

        rows: list[dict[str, Any]] = []
        for directory in directories:
            snapshot = ResearchLoopLedger(self.root, owner, directory.name).snapshot()
            events = snapshot["events"]
            rows.append(
                {
                    "loop_id": directory.name,
                    "status": snapshot["status"],
                    "created_at": events[0]["created_at"],
                    "updated_at": events[-1]["created_at"],
                    "event_count": len(events),
                }
            )
        rows.sort(key=lambda item: (item["created_at"], item["loop_id"]), reverse=True)
        visible = rows[:limit]
        return {
            "schema_version": 1,
            "loops": visible,
            "summary": {
                "total": len(rows),
                "returned": len(visible),
                "limit": limit,
                "maximum": MAX_LOOPS_PER_OWNER,
                "truncated": len(rows) > len(visible),
            },
            "safety": dict(LOOP_SAFETY),
        }


def _normalize_owner(owner: str) -> str:
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("Research loop owner must be a non-empty string")
    normalized = owner.strip().casefold()
    if len(normalized.encode("utf-8")) > 256 or "\x00" in normalized:
        raise ValueError("Research loop owner is invalid")
    return normalized


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


__all__ = [
    "MAX_EVENTS_PER_LOOP",
    "MAX_LOOPS_PER_OWNER",
    "ResearchLoopLedger",
    "ResearchLoopStore",
]
