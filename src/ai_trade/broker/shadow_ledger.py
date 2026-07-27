from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Mapping

from ..data.evidence_io import atomic_create_json, evidence_store_lock
from ..json_utils import load_unique_json
from .shadow_schema import (
    SHADOW_EVENT_SCHEMA_VERSION,
    canonical_payload,
    finalize_shadow_event,
    ledger_id,
    shadow_event_fingerprint,
    validate_shadow_event,
)


MAX_SHADOW_EVENTS = 100_000
MAX_SHADOW_EVENT_BYTES = 256 * 1024
_EVENT_FILE = re.compile(r"(\d{12})_(se_[0-9a-f]{32})\.json\Z")


class ShadowLedgerConflictError(RuntimeError):
    pass


class ShadowEventLedger:
    """Append-only hash chain for one pseudonymous shadow account."""

    def __init__(self, root: str | Path, account_reference: str):
        self.root = Path(root).resolve()
        self.ledger_id = ledger_id(account_reference)
        self.account_root = self.root / "accounts" / self.ledger_id

    @property
    def events_root(self) -> Path:
        return self.account_root / "events"

    def append(
        self,
        event_type: str,
        *,
        occurred_at: datetime,
        trading_session: date,
        source: str,
        external_id: str,
        payload: Mapping[str, Any],
        observed_at: datetime | None = None,
    ) -> dict[str, Any]:
        if occurred_at.tzinfo is None or occurred_at.utcoffset() is None:
            raise ValueError("Shadow event occurred_at must include a timezone")
        observed = observed_at or datetime.now(timezone.utc)
        if observed.tzinfo is None or observed.utcoffset() is None:
            raise ValueError("Shadow event observed_at must include a timezone")
        draft = {
            "schema_version": SHADOW_EVENT_SCHEMA_VERSION,
            "ledger_id": self.ledger_id,
            "event_type": event_type,
            "occurred_at": occurred_at.isoformat(),
            "observed_at": observed.isoformat(),
            "trading_session": trading_session.isoformat(),
            "source": source,
            "external_id": external_id,
            "payload": canonical_payload(event_type, payload),
        }
        candidate_fingerprint = shadow_event_fingerprint(draft)
        with evidence_store_lock(self.account_root, "Shadow event ledger"):
            events = self._events_unlocked(missing_ok=True)
            for existing in events:
                if (
                    existing["source"] == source
                    and existing["external_id"] == external_id
                ):
                    if existing["event_fingerprint"] != candidate_fingerprint:
                        raise ShadowLedgerConflictError(
                            "Shadow external event id conflicts with existing content"
                        )
                    result = _clone(existing)
                    result["reused"] = True
                    return result
            if len(events) >= MAX_SHADOW_EVENTS:
                raise RuntimeError("Shadow event ledger capacity reached")
            if events:
                previous_occurred = datetime.fromisoformat(
                    str(events[-1]["occurred_at"]).replace("Z", "+00:00")
                )
                previous_session = date.fromisoformat(
                    str(events[-1]["trading_session"])
                )
                if occurred_at < previous_occurred:
                    raise ShadowLedgerConflictError(
                        "Shadow events must be appended in occurrence-time order"
                    )
                if trading_session < previous_session:
                    raise ShadowLedgerConflictError(
                        "Shadow event trading sessions must be monotonic"
                    )
            previous = events[-1]["record_fingerprint"] if events else None
            record = finalize_shadow_event(
                draft,
                sequence=len(events) + 1,
                previous_record_fingerprint=previous,
            )
            target = self.events_root / (
                f"{record['sequence']:012d}_{record['event_id']}.json"
            )
            atomic_create_json(
                self.account_root,
                target,
                record,
                label="shadow event",
                maximum_bytes=MAX_SHADOW_EVENT_BYTES,
            )
        result = self.events()[-1]
        result["reused"] = False
        return result

    def events(self) -> list[dict[str, Any]]:
        return self._events_unlocked(missing_ok=True)

    def _events_unlocked(self, *, missing_ok: bool) -> list[dict[str, Any]]:
        root = self.events_root
        if not root.exists():
            if missing_ok:
                return []
            raise RuntimeError("Shadow event directory is unavailable")
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError("Shadow event directory is invalid")
        paths: list[tuple[int, str, Path]] = []
        for path in root.iterdir():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("Shadow event must be a regular file")
            match = _EVENT_FILE.fullmatch(path.name)
            if match is None:
                raise RuntimeError("Unexpected shadow event file")
            paths.append((int(match.group(1)), match.group(2), path))
        paths.sort()
        if len(paths) > MAX_SHADOW_EVENTS:
            raise RuntimeError("Shadow event ledger exceeds capacity")
        expected_sequences = list(range(1, len(paths) + 1))
        if [item[0] for item in paths] != expected_sequences:
            raise RuntimeError("Shadow event sequence is not contiguous")
        result: list[dict[str, Any]] = []
        previous: str | None = None
        external_ids: set[tuple[str, str]] = set()
        event_ids: set[str] = set()
        previous_occurred: datetime | None = None
        previous_session: date | None = None
        for sequence, event_id, path in paths:
            try:
                value = load_unique_json(path, max_bytes=MAX_SHADOW_EVENT_BYTES)
            except (OSError, UnicodeError, ValueError) as exc:
                raise RuntimeError(f"Invalid shadow event {path.name}: {exc}") from exc
            if not isinstance(value, dict):
                raise RuntimeError("Shadow event must be an object")
            try:
                validate_shadow_event(value)
            except ValueError as exc:
                raise RuntimeError(f"Invalid shadow event {path.name}: {exc}") from exc
            if (
                value["ledger_id"] != self.ledger_id
                or value["sequence"] != sequence
                or value["event_id"] != event_id
                or value["previous_record_fingerprint"] != previous
            ):
                raise RuntimeError("Shadow event hash chain is inconsistent")
            logical_key = (str(value["source"]), str(value["external_id"]))
            if logical_key in external_ids or event_id in event_ids:
                raise RuntimeError("Shadow event ledger contains a duplicate event")
            occurred = datetime.fromisoformat(
                str(value["occurred_at"]).replace("Z", "+00:00")
            )
            trading_session = date.fromisoformat(str(value["trading_session"]))
            if previous_occurred is not None and occurred < previous_occurred:
                raise RuntimeError("Shadow event occurrence times are out of order")
            if previous_session is not None and trading_session < previous_session:
                raise RuntimeError("Shadow event trading sessions are out of order")
            external_ids.add(logical_key)
            event_ids.add(event_id)
            result.append(value)
            previous = str(value["record_fingerprint"])
            previous_occurred = occurred
            previous_session = trading_session
        return result


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


__all__ = [
    "MAX_SHADOW_EVENTS",
    "ShadowEventLedger",
    "ShadowLedgerConflictError",
]
