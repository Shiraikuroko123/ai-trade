from __future__ import annotations

from contextlib import contextmanager
from hashlib import sha256
from heapq import heappush, heapreplace
import json
import os
from pathlib import Path
import re
import tempfile
from threading import Lock, RLock
from typing import Any, Callable, Iterator

from ..json_utils import load_unique_json


_CANDIDATE_ID = re.compile(r"cand_[0-9a-f]{32}\Z")
_MONITOR_ID = re.compile(r"monitor_[0-9a-f]{32}\Z")
_EVENT_ID = re.compile(r"event_[0-9a-f]{32}\Z")
_ACTIVE_TRANSITION_ACTIONS = frozenset(
    {"activate", "rollback", "suspend", "resume", "retire"}
)
_ACTIVE_TRANSACTION_SCHEMA_VERSION = 1

# Strategy-lab files are local, but they are still an input boundary: a damaged
# or manually edited record must not be allowed to consume unbounded memory.
MAX_STRATEGY_LAB_RECORD_BYTES = 2 * 1024 * 1024
MAX_CANDIDATE_RECORD_BYTES = 256 * 1024
MAX_VALIDATION_RECORD_BYTES = 2 * 1024 * 1024
MAX_APPROVAL_RECORD_BYTES = 128 * 1024
MAX_EXPORT_RECORD_BYTES = 5 * 1024 * 1024
MAX_MONITOR_RECORD_BYTES = 512 * 1024
MAX_EVENT_RECORD_BYTES = 128 * 1024
MAX_ACTIVE_RECORD_BYTES = 512 * 1024
MAX_TRANSACTION_RECORD_BYTES = 1 * 1024 * 1024

_CANDIDATE_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "owner",
        "source",
        "title",
        "hypothesis",
        "reason",
        "created_at",
        "parent_candidate_id",
        "parent_fingerprint",
        "config_context_fingerprint",
        "baseline",
        "changes",
        "candidate",
        "candidate_fingerprint",
        "status",
        "proposal",
        "safety",
    }
)
_VALIDATION_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "candidate_fingerprint",
        "config_context_fingerprint",
        "parent_candidate_id",
        "parent_fingerprint",
        "validated_at",
        "market_snapshot",
        "period",
        "baseline_metrics",
        "candidate_metrics",
        "holdout",
        "cost_stress",
        "stability",
        "gates",
        "live_ready",
    }
)
_APPROVAL_FIELDS = frozenset(
    {
        "schema_version",
        "candidate_id",
        "candidate_fingerprint",
        "config_context_fingerprint",
        "parent_candidate_id",
        "parent_fingerprint",
        "validation_fingerprint",
        "approved_at",
        "approved_by",
        "note",
        "explicit_human_approval",
        "live_trading_authorized",
    }
)
_MONITOR_FIELDS = frozenset(
    {
        "schema_version",
        "monitor_id",
        "created_at",
        "actor",
        "candidate_id",
        "candidate_fingerprint",
        "active_lifecycle_state",
        "market_snapshot",
        "period",
        "validation_reference",
        "recent_candidate_metrics",
        "recent_parent_metrics",
        "evidence",
        "state_changed",
        "live_trading_authorized",
        "evidence_fingerprint",
    }
)
_EVENT_FIELDS = frozenset(
    {
        "schema_version",
        "event_id",
        "action",
        "created_at",
        "candidate_id",
        "candidate_fingerprint",
        "actor",
        "source",
        "note",
        "from_candidate_id",
        "from_fingerprint",
        "from_lifecycle_state",
        "to_candidate_id",
        "to_fingerprint",
        "to_lifecycle_state",
        "monitor_id",
        "evidence_fingerprint",
        "verdict",
        "monitoring_evidence",
        "state_changed",
        "affects_broker_configuration",
        "live_trading_authorized",
    }
)
_ACTIVE_FIELDS = frozenset(
    {
        "candidate_id",
        "fingerprint",
        "snapshot",
        "activated_at",
        "activated_by",
        "rollback_stack",
        "lifecycle_state",
        "lifecycle_updated_at",
        "lifecycle_updated_by",
        "retired_candidates",
    }
)
_TRANSACTION_FIELDS = frozenset(
    {"schema_version", "owner_id", "active", "event"}
)
_LOCKS_GUARD = Lock()
_LOCKS: dict[str, _OwnerLockState] = {}


class StrategyLabConflictError(RuntimeError):
    pass


class StrategyLabCapacityError(StrategyLabConflictError):
    pass


class _OwnerLockState:
    def __init__(self) -> None:
        self.thread_lock = RLock()
        self.depth = 0


class StrategyLabStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def owner_id(self, owner: str) -> str:
        normalized = _normalize_owner(owner)
        return sha256(normalized.encode("utf-8")).hexdigest()

    def owner_directory(self, owner: str) -> Path:
        return self.root / "users" / self.owner_id(owner)

    def write_candidate(
        self,
        owner: str,
        candidate: dict[str, Any],
        *,
        expected_active_fingerprint: str | None = None,
        empty_active_fingerprint: str | None = None,
        max_records: int | None = None,
    ) -> Path:
        if max_records is not None and (
            not isinstance(max_records, int)
            or isinstance(max_records, bool)
            or max_records < 1
        ):
            raise ValueError("max_records must be a positive integer")
        candidate_id = _valid_candidate_id(candidate.get("candidate_id"))
        return self._write_once(
            owner,
            "candidates",
            candidate_id,
            candidate,
            expected_active_fingerprint=expected_active_fingerprint,
            empty_active_fingerprint=empty_active_fingerprint,
            max_records=max_records,
        )

    def read_candidate(self, owner: str, candidate_id: str) -> dict[str, Any]:
        return self._read_required(
            owner, "candidates", _valid_candidate_id(candidate_id)
        )

    def list_candidates(self, owner: str) -> list[dict[str, Any]]:
        return self._list_records(owner, "candidates", "created_at", reverse=True)

    def write_validation(
        self,
        owner: str,
        candidate_id: str,
        value: dict[str, Any],
        *,
        expected_active_fingerprint: str | None = None,
        empty_active_fingerprint: str | None = None,
    ) -> Path:
        return self._write_once(
            owner,
            "validations",
            _valid_candidate_id(candidate_id),
            value,
            expected_active_fingerprint=expected_active_fingerprint,
            empty_active_fingerprint=empty_active_fingerprint,
        )

    def read_validation(self, owner: str, candidate_id: str) -> dict[str, Any] | None:
        return self._read_optional(
            owner, "validations", _valid_candidate_id(candidate_id)
        )

    def write_approval(
        self,
        owner: str,
        candidate_id: str,
        value: dict[str, Any],
        *,
        expected_active_fingerprint: str | None = None,
        empty_active_fingerprint: str | None = None,
    ) -> Path:
        return self._write_once(
            owner,
            "approvals",
            _valid_candidate_id(candidate_id),
            value,
            expected_active_fingerprint=expected_active_fingerprint,
            empty_active_fingerprint=empty_active_fingerprint,
        )

    def read_approval(self, owner: str, candidate_id: str) -> dict[str, Any] | None:
        return self._read_optional(
            owner, "approvals", _valid_candidate_id(candidate_id)
        )

    def export_path(self, owner: str, candidate_id: str) -> Path:
        candidate_id = _valid_candidate_id(candidate_id)
        return self.owner_directory(owner) / "paper_configs" / f"{candidate_id}.json"

    def write_export(
        self,
        owner: str,
        candidate_id: str,
        value: dict[str, Any],
        *,
        expected_active_fingerprint: str | None = None,
        empty_active_fingerprint: str | None = None,
    ) -> Path:
        return self._write_once(
            owner,
            "paper_configs",
            _valid_candidate_id(candidate_id),
            value,
            expected_active_fingerprint=expected_active_fingerprint,
            empty_active_fingerprint=empty_active_fingerprint,
        )

    def read_export_config(
        self, owner: str, candidate_id: str
    ) -> dict[str, Any] | None:
        path = self.export_path(owner, candidate_id)
        return _read_json(path) if path.exists() else None

    def read_export(self, owner: str, candidate_id: str) -> dict[str, Any] | None:
        candidate_id = _valid_candidate_id(candidate_id)
        path = self.export_path(owner, candidate_id)
        if not path.exists():
            return None
        raw = _read_json(path)
        metadata = raw.get("_strategy_lab", {})
        return {
            "candidate_id": candidate_id,
            "path": str(path),
            "config_fingerprint": metadata.get("config_fingerprint"),
            "broker_mode": raw.get("broker", {}).get("mode"),
            "exported_at": metadata.get("exported_at"),
        }

    def write_monitor(
        self,
        owner: str,
        value: dict[str, Any],
        *,
        expected_active_candidate_id: str,
        expected_active_fingerprint: str,
        expected_lifecycle_state: str,
        max_records: int | None = None,
    ) -> Path:
        monitor_id = _valid_monitor_id(value.get("monitor_id"))
        return self._write_once(
            owner,
            "monitors",
            monitor_id,
            value,
            expected_active_candidate_id=_valid_candidate_id(
                expected_active_candidate_id
            ),
            expected_active_fingerprint=expected_active_fingerprint,
            expected_lifecycle_state=expected_lifecycle_state,
            max_records=max_records,
            capacity_label="monitoring record",
        )

    def read_monitor(self, owner: str, monitor_id: str) -> dict[str, Any]:
        return self._read_required(owner, "monitors", _valid_monitor_id(monitor_id))

    def list_monitors(self, owner: str) -> list[dict[str, Any]]:
        return self._list_records(owner, "monitors", "created_at")

    def read_active(self, owner: str) -> dict[str, Any] | None:
        path = self.owner_directory(owner) / "active.json"
        with self._owner_lock(owner):
            self._recover_active_transaction_unlocked(owner)
            return _read_json(path) if path.exists() else None

    def write_active(self, owner: str, value: dict[str, Any]) -> Path:
        path = self.owner_directory(owner) / "active.json"
        with self._owner_lock(owner):
            self._recover_active_transaction_unlocked(owner)
            _atomic_write_json(path, value)
        return path

    def transition_active(
        self,
        owner: str,
        transition: Callable[
            [dict[str, Any] | None],
            tuple[dict[str, Any], dict[str, Any] | None],
        ],
        *,
        expected_active_fingerprint: str | None = None,
        empty_active_fingerprint: str | None = None,
        max_transition_events: int | None = None,
    ) -> dict[str, Any]:
        if max_transition_events is not None and (
            not isinstance(max_transition_events, int)
            or isinstance(max_transition_events, bool)
            or max_transition_events < 1
        ):
            raise ValueError("max_transition_events must be a positive integer")
        path = self.owner_directory(owner) / "active.json"
        with self._owner_lock(owner):
            self._recover_active_transaction_unlocked(owner)
            current = _read_json(path) if path.exists() else None
            self._assert_active_fingerprint(
                current,
                expected_active_fingerprint,
                empty_active_fingerprint,
            )
            active, event = transition(current)
            if event is None:
                return active
            if max_transition_events is not None:
                count = self._count_transition_events_unlocked(owner)
                if count >= max_transition_events:
                    raise StrategyLabCapacityError(
                        "Strategy lab transition event limit reached "
                        f"({max_transition_events}); archive this workspace first"
                    )
            transaction = {
                "schema_version": _ACTIVE_TRANSACTION_SCHEMA_VERSION,
                "owner_id": self.owner_id(owner),
                "active": active,
                "event": event,
            }
            _atomic_write_json(
                self._active_transaction_path(owner),
                transaction,
                replace_existing=False,
            )
            self._complete_active_transaction_unlocked(owner, transaction)
            return active

    def _count_transition_events_unlocked(self, owner: str) -> int:
        path = self.owner_directory(owner) / "events"
        if not path.exists():
            return 0
        return sum(
            _read_json(item).get("action") in _ACTIVE_TRANSITION_ACTIONS
            for item in path.glob("*.json")
            if item.is_file()
        )

    def write_event(self, owner: str, event: dict[str, Any]) -> Path:
        with self._owner_lock(owner):
            self._recover_active_transaction_unlocked(owner)
            return self._write_event_unlocked(owner, event)

    def _write_event_unlocked(self, owner: str, event: dict[str, Any]) -> Path:
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
            raise ValueError("Invalid strategy-lab event id")
        path = self.owner_directory(owner) / "events" / f"{event_id}.json"
        if path.exists():
            raise FileExistsError(
                f"Immutable strategy-lab record already exists: {event_id}"
            )
        _atomic_write_json(path, event, replace_existing=False)
        return path

    def list_events(self, owner: str) -> list[dict[str, Any]]:
        with self._owner_lock(owner):
            self._recover_active_transaction_unlocked(owner)
            return self._list_records(owner, "events", "created_at")

    def recent_events(self, owner: str, limit: int) -> tuple[list[dict[str, Any]], int]:
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
            raise ValueError("limit must be a positive integer")
        with self._owner_lock(owner):
            self._recover_active_transaction_unlocked(owner)
            path = self.owner_directory(owner) / "events"
            if not path.exists():
                return [], 0

            total = 0
            recent: list[tuple[str, str, str, dict[str, Any]]] = []
            for item in path.glob("*.json"):
                event = _read_json(item)
                total += 1
                entry = (
                    str(event.get("created_at", "")),
                    str(event.get("event_id", "")),
                    item.name,
                    event,
                )
                if len(recent) < limit:
                    heappush(recent, entry)
                elif entry[:3] > recent[0][:3]:
                    heapreplace(recent, entry)

            ordered = sorted(recent, key=lambda entry: entry[:3])
            return [entry[3] for entry in ordered], total

    def _active_transaction_path(self, owner: str) -> Path:
        return self.owner_directory(owner) / ".active-transition.json"

    def _recover_active_transaction_unlocked(self, owner: str) -> None:
        path = self._active_transaction_path(owner)
        if path.exists():
            self._complete_active_transaction_unlocked(owner, _read_json(path))

    def _complete_active_transaction_unlocked(
        self, owner: str, transaction: dict[str, Any]
    ) -> None:
        schema_version = transaction.get("schema_version")
        transaction_owner = transaction.get("owner_id")
        if (
            schema_version != _ACTIVE_TRANSACTION_SCHEMA_VERSION
            or transaction_owner != self.owner_id(owner)
        ):
            raise RuntimeError("Invalid strategy-lab active transaction")
        active = transaction.get("active")
        event = transaction.get("event")
        if not isinstance(active, dict) or not isinstance(event, dict):
            raise RuntimeError("Invalid strategy-lab active transaction payload")
        event_id = event.get("event_id")
        if not isinstance(event_id, str) or not _EVENT_ID.fullmatch(event_id):
            raise RuntimeError("Invalid strategy-lab active transaction event")

        event_path = self.owner_directory(owner) / "events" / f"{event_id}.json"
        if event_path.exists():
            if _read_json(event_path) != event:
                raise RuntimeError(
                    "Strategy-lab active transaction conflicts with its event"
                )
        else:
            self._write_event_unlocked(owner, event)
        _atomic_write_json(self.owner_directory(owner) / "active.json", active)
        transaction_path = self._active_transaction_path(owner)
        transaction_path.unlink(missing_ok=True)
        _fsync_directory(transaction_path.parent)

    def _write_once(
        self,
        owner: str,
        directory: str,
        record_id: str,
        value: dict[str, Any],
        *,
        expected_active_candidate_id: str | None = None,
        expected_active_fingerprint: str | None = None,
        empty_active_fingerprint: str | None = None,
        expected_lifecycle_state: str | None = None,
        max_records: int | None = None,
        capacity_label: str = "candidate",
    ) -> Path:
        path = self.owner_directory(owner) / directory / f"{record_id}.json"
        with self._owner_lock(owner):
            self._recover_active_transaction_unlocked(owner)
            active_path = self.owner_directory(owner) / "active.json"
            active = _read_json(active_path) if active_path.exists() else None
            self._assert_active_candidate(active, expected_active_candidate_id)
            self._assert_active_fingerprint(
                active,
                expected_active_fingerprint,
                empty_active_fingerprint,
            )
            self._assert_lifecycle_state(active, expected_lifecycle_state)
            if path.exists():
                raise FileExistsError(
                    f"Immutable strategy-lab record already exists: {record_id}"
                )
            if max_records is not None:
                count = sum(1 for item in path.parent.glob("*.json") if item.is_file())
                if count >= max_records:
                    raise StrategyLabCapacityError(
                        f"Strategy lab {capacity_label} limit reached "
                        f"({max_records}); remove or archive old records first"
                    )
            _atomic_write_json(path, value, replace_existing=False)
        return path

    @staticmethod
    def _assert_active_candidate(
        active: dict[str, Any] | None, expected: str | None
    ) -> None:
        if expected is None:
            return
        actual = active.get("candidate_id") if active is not None else None
        if actual != expected:
            raise StrategyLabConflictError(
                "Active strategy candidate changed; refresh before recording evidence"
            )

    @staticmethod
    def _assert_active_fingerprint(
        active: dict[str, Any] | None,
        expected: str | None,
        empty: str | None,
    ) -> None:
        if expected is None:
            return
        actual = active.get("fingerprint") if active is not None else empty
        if actual != expected:
            raise StrategyLabConflictError(
                "Active strategy-lab baseline changed; create and validate a new candidate"
            )

    @staticmethod
    def _assert_lifecycle_state(
        active: dict[str, Any] | None, expected: str | None
    ) -> None:
        if expected is None:
            return
        actual = "CONFIGURED"
        if active is not None:
            actual = str(
                active.get("lifecycle_state")
                or ("ACTIVE" if active.get("candidate_id") else "CONFIGURED")
            )
        if actual != expected:
            raise StrategyLabConflictError(
                "Active strategy lifecycle changed; refresh before recording evidence"
            )

    def _read_required(
        self, owner: str, directory: str, record_id: str
    ) -> dict[str, Any]:
        value = self._read_optional(owner, directory, record_id)
        if value is None:
            raise KeyError(f"Unknown strategy-lab record: {record_id}")
        return value

    def _read_optional(
        self, owner: str, directory: str, record_id: str
    ) -> dict[str, Any] | None:
        path = self.owner_directory(owner) / directory / f"{record_id}.json"
        return _read_json(path) if path.exists() else None

    def _list_records(
        self, owner: str, directory: str, order_key: str, reverse: bool = False
    ) -> list[dict[str, Any]]:
        path = self.owner_directory(owner) / directory
        if not path.exists():
            return []
        records = [_read_json(item) for item in path.glob("*.json")]
        return sorted(
            records, key=lambda item: str(item.get(order_key, "")), reverse=reverse
        )

    @contextmanager
    def _owner_lock(self, owner: str) -> Iterator[None]:
        directory = self.owner_directory(owner)
        key = os.path.normcase(str(directory))
        with _LOCKS_GUARD:
            state = _LOCKS.setdefault(key, _OwnerLockState())
        with state.thread_lock:
            if state.depth:
                state.depth += 1
                try:
                    yield
                finally:
                    state.depth -= 1
                return
            with _file_lock(directory / ".owner.lock"):
                state.depth = 1
                try:
                    yield
                finally:
                    state.depth = 0


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _normalize_owner(owner: str) -> str:
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("Strategy-lab owner must be a non-empty string")
    normalized = owner.strip().casefold()
    if len(normalized) > 200:
        raise ValueError("Strategy-lab owner is too long")
    return normalized


def _valid_candidate_id(value: Any) -> str:
    if not isinstance(value, str) or not _CANDIDATE_ID.fullmatch(value):
        raise ValueError("Invalid strategy-lab candidate id")
    return value


def _valid_monitor_id(value: Any) -> str:
    if not isinstance(value, str) or not _MONITOR_ID.fullmatch(value):
        raise ValueError("Invalid strategy-lab monitor id")
    return value


def _read_json(path: Path) -> dict[str, Any]:
    max_bytes, allowed_fields, label = _record_policy(path)
    try:
        value = load_unique_json(path, max_bytes=max_bytes)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Invalid strategy-lab record: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError(f"Strategy-lab record must be an object: {path}")
    unsupported = sorted(set(value) - allowed_fields) if allowed_fields else []
    if unsupported:
        raise RuntimeError(
            f"{label} schema fields are invalid: " + ", ".join(unsupported)
        )
    return value


def _atomic_write_json(
    path: Path, value: dict[str, Any], *, replace_existing: bool = True
) -> None:
    if not isinstance(value, dict):
        raise ValueError("Strategy-lab records must be JSON objects")
    max_bytes, allowed_fields, label = _record_policy(path)
    unsupported = sorted(set(value) - allowed_fields) if allowed_fields else []
    if unsupported:
        raise ValueError(
            f"{label} schema fields are invalid: " + ", ".join(unsupported)
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        size = temporary.stat().st_size
        if size > max_bytes:
            raise ValueError(
                f"Strategy-lab record exceeds {max_bytes} bytes: {path}"
            )
        if replace_existing:
            os.replace(temporary, path)
        else:
            try:
                if os.name == "nt":
                    # Windows rename is atomic on one volume and refuses to replace.
                    os.rename(temporary, path)
                else:
                    os.link(temporary, path)
            except OSError as exc:
                if (
                    not isinstance(exc, FileExistsError)
                    and getattr(exc, "winerror", None) != 183
                ):
                    raise
                raise FileExistsError(
                    f"Immutable strategy-lab record already exists: {path.stem}"
                ) from exc
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _record_policy(
    path: Path,
) -> tuple[int, frozenset[str] | None, str]:
    """Return the byte limit and top-level schema for a strategy-lab file."""
    name = path.name
    directory = path.parent.name
    if name == "active.json":
        return MAX_ACTIVE_RECORD_BYTES, _ACTIVE_FIELDS, "Active strategy"
    if name == ".active-transition.json":
        return (
            MAX_TRANSACTION_RECORD_BYTES,
            _TRANSACTION_FIELDS,
            "Active strategy transaction",
        )
    policies = {
        "candidates": (
            MAX_CANDIDATE_RECORD_BYTES,
            _CANDIDATE_FIELDS,
            "Candidate",
        ),
        "validations": (
            MAX_VALIDATION_RECORD_BYTES,
            _VALIDATION_FIELDS,
            "Validation",
        ),
        "approvals": (MAX_APPROVAL_RECORD_BYTES, _APPROVAL_FIELDS, "Approval"),
        "monitors": (MAX_MONITOR_RECORD_BYTES, _MONITOR_FIELDS, "Monitor"),
        "events": (MAX_EVENT_RECORD_BYTES, _EVENT_FIELDS, "Event"),
        "paper_configs": (MAX_EXPORT_RECORD_BYTES, None, "Paper export"),
    }
    return policies.get(
        directory,
        (MAX_STRATEGY_LAB_RECORD_BYTES, None, "Strategy-lab record"),
    )
