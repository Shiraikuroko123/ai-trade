from __future__ import annotations

from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import tempfile
from threading import Lock, RLock
from typing import Any, Iterator, Mapping
from uuid import uuid4

from .json_utils import load_unique_json


SCHEMA_VERSION = 1
JOURNAL_CATEGORIES = (
    "observation",
    "decision",
    "trade_review",
    "risk",
    "strategy",
    "weekly_review",
)
JOURNAL_DECISIONS = (
    "not_recorded",
    "watch",
    "consider_increase",
    "hold",
    "consider_reduce",
    "avoid",
)
JOURNAL_DEFAULT_LIMIT = 100
JOURNAL_MAX_LIMIT = 200
MAX_ENTRIES_PER_OWNER = 2_000
MAX_JOURNAL_RECORD_BYTES = 64 * 1024
MAX_JOURNAL_QUERY_LENGTH = 80
MAX_JOURNAL_TITLE_LENGTH = 80
MAX_JOURNAL_NOTE_LENGTH = 4_000
MAX_JOURNAL_ACTOR_LENGTH = 80

_ENTRY_ID = re.compile(r"journal_[0-9a-f]{32}\Z")
_SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:=+-]{0,63}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_ENTRY_FIELDS = frozenset(
    {
        "schema_version",
        "entry_id",
        "entry_fingerprint",
        "owner",
        "created_at",
        "research_date",
        "week_start",
        "category",
        "symbol",
        "title",
        "note",
        "decision",
        "confidence",
        "correction_of",
        "actor",
        "evidence",
        "authority",
    }
)
_MARKET_EVIDENCE_FIELDS = frozenset(
    {"available", "date", "fingerprint"}
)
_STRATEGY_EVIDENCE_FIELDS = frozenset(
    {"available", "candidate_id", "fingerprint", "lifecycle_state"}
)
_AUTHORITY = {
    "research_only": True,
    "execution_authorized": False,
    "strategy_changed": False,
    "paper_account_changed": False,
    "broker_permissions_changed": False,
}
_LOCKS_GUARD = Lock()
_LOCKS: dict[str, "_OwnerLockState"] = {}


@dataclass(frozen=True)
class JournalQuery:
    category: str | None = None
    symbol: str | None = None
    query: str | None = None
    limit: int = JOURNAL_DEFAULT_LIMIT


@dataclass(frozen=True)
class JournalDraft:
    research_date: date
    category: str
    symbol: str | None
    title: str
    note: str
    decision: str
    confidence: int | None
    correction_of: str | None = None


class ResearchJournalCapacityError(RuntimeError):
    pass


class _OwnerLockState:
    def __init__(self) -> None:
        self.thread_lock = RLock()


class ResearchJournalStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def owner_id(self, owner: str) -> str:
        normalized = _normalize_owner(owner)
        return sha256(normalized.encode("utf-8")).hexdigest()

    def owner_directory(self, owner: str) -> Path:
        return self.root / "users" / self.owner_id(owner)

    def append(
        self,
        owner: str,
        draft: JournalDraft,
        *,
        actor: str,
        market_evidence: Mapping[str, Any],
        strategy_evidence: Mapping[str, Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        _validate_draft(draft)
        actor = _bounded_text(actor, "actor", MAX_JOURNAL_ACTOR_LENGTH)
        market = _validated_market_evidence(market_evidence)
        strategy = _validated_strategy_evidence(strategy_evidence)
        created_at = now or datetime.now(timezone.utc)
        if created_at.tzinfo is None:
            raise ValueError("Journal creation time must include a timezone")
        created_at = created_at.astimezone(timezone.utc).replace(microsecond=0)
        entry_id = f"journal_{uuid4().hex}"
        owner_hash = self.owner_id(owner)
        week_start = draft.research_date - timedelta(
            days=draft.research_date.weekday()
        )
        record = {
            "schema_version": SCHEMA_VERSION,
            "entry_id": entry_id,
            "owner": owner_hash,
            "created_at": created_at.isoformat().replace("+00:00", "Z"),
            "research_date": draft.research_date.isoformat(),
            "week_start": week_start.isoformat(),
            "category": draft.category,
            "symbol": draft.symbol,
            "title": draft.title,
            "note": draft.note,
            "decision": draft.decision,
            "confidence": draft.confidence,
            "correction_of": draft.correction_of,
            "actor": actor,
            "evidence": {
                "market_snapshot": market,
                "strategy": strategy,
            },
            "authority": dict(_AUTHORITY),
        }
        record["entry_fingerprint"] = _record_fingerprint(record)
        directory = self.owner_directory(owner) / "entries"
        path = directory / f"{entry_id}.json"
        with self._owner_lock(owner):
            if draft.correction_of is not None:
                correction_path = directory / f"{draft.correction_of}.json"
                if not correction_path.is_file():
                    raise KeyError(
                        f"Unknown research journal entry: {draft.correction_of}"
                    )
                _read_record(correction_path, owner_hash)
            count = sum(1 for item in directory.glob("journal_*.json") if item.is_file())
            if count >= MAX_ENTRIES_PER_OWNER:
                raise ResearchJournalCapacityError(
                    "Research journal entry limit reached "
                    f"({MAX_ENTRIES_PER_OWNER}); archive the owner directory before "
                    "recording more entries"
                )
            _atomic_create_json(path, record)
        return _public_record(record)

    def list(
        self,
        owner: str,
        query: JournalQuery | None = None,
    ) -> dict[str, Any]:
        query = query or JournalQuery()
        _validate_query(query)
        owner_hash = self.owner_id(owner)
        directory = self.owner_directory(owner) / "entries"
        if not directory.exists():
            records: list[dict[str, Any]] = []
        else:
            paths = [
                path
                for path in directory.glob("journal_*.json")
                if path.is_file()
            ]
            if len(paths) > MAX_ENTRIES_PER_OWNER:
                raise RuntimeError(
                    "Research journal contains more records than the supported "
                    f"per-owner limit ({MAX_ENTRIES_PER_OWNER})"
                )
            records = [_read_record(path, owner_hash) for path in paths]
        records.sort(
            key=lambda item: (
                item["research_date"],
                item["created_at"],
                item["entry_id"],
            ),
            reverse=True,
        )
        by_category = Counter(record["category"] for record in records)
        matched = [record for record in records if _matches(record, query)]
        by_week = Counter(record["week_start"] for record in matched)
        visible = matched[: query.limit]
        return {
            "schema_version": SCHEMA_VERSION,
            "available": True,
            "filters": {
                "category": query.category,
                "symbol": query.symbol,
                "query": query.query,
                "limit": query.limit,
            },
            "summary": {
                "total": len(records),
                "matched": len(matched),
                "returned": len(visible),
                "truncated": len(matched) > len(visible),
                "maximum": MAX_ENTRIES_PER_OWNER,
                "by_category": {
                    category: by_category.get(category, 0)
                    for category in JOURNAL_CATEGORIES
                },
                "by_week": [
                    {"week_start": week, "count": count}
                    for week, count in sorted(by_week.items(), reverse=True)
                ],
            },
            "entries": [_public_record(record) for record in visible],
            "authority": dict(_AUTHORITY),
            "errors": [],
        }

    def entries_between(
        self,
        owner: str,
        start: date,
        end: date,
    ) -> list[dict[str, Any]]:
        """Return validated immutable entries for an inclusive date range.

        This internal projection boundary is intentionally separate from the
        paginated browser query. Archive generation must not silently build a
        weekly report from only the first 200 visible entries.
        """
        if not isinstance(start, date) or isinstance(start, datetime):
            raise ValueError("Research journal range start is invalid")
        if not isinstance(end, date) or isinstance(end, datetime) or end < start:
            raise ValueError("Research journal range end is invalid")
        owner_hash = self.owner_id(owner)
        directory = self.owner_directory(owner) / "entries"
        if directory.is_symlink():
            raise RuntimeError("Research journal entries must not be symbolic links")
        if not directory.exists():
            return []
        if not directory.is_dir():
            raise RuntimeError("Research journal entries path must be a directory")
        paths: list[Path] = []
        for path in directory.glob("journal_*.json"):
            if path.is_symlink():
                raise RuntimeError(
                    "Research journal entries must not be symbolic links"
                )
            if path.is_file():
                if len(paths) >= MAX_ENTRIES_PER_OWNER:
                    raise RuntimeError(
                        "Research journal contains more records than the supported "
                        f"per-owner limit ({MAX_ENTRIES_PER_OWNER})"
                    )
                paths.append(path)
        records = [_read_record(path, owner_hash) for path in paths]
        records = [
            record
            for record in records
            if start <= date.fromisoformat(record["research_date"]) <= end
        ]
        records.sort(
            key=lambda item: (
                item["research_date"],
                item["created_at"],
                item["entry_id"],
            )
        )
        return [_public_record(record) for record in records]

    @contextmanager
    def _owner_lock(self, owner: str) -> Iterator[None]:
        directory = self.owner_directory(owner)
        key = os.path.normcase(str(directory))
        with _LOCKS_GUARD:
            state = _LOCKS.setdefault(key, _OwnerLockState())
        with state.thread_lock:
            with _file_lock(directory / ".owner.lock"):
                yield


def unavailable_journal(message: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "available": False,
        "filters": {
            "category": None,
            "symbol": None,
            "query": None,
            "limit": JOURNAL_DEFAULT_LIMIT,
        },
        "summary": {
            "total": 0,
            "matched": 0,
            "returned": 0,
            "truncated": False,
            "maximum": MAX_ENTRIES_PER_OWNER,
            "by_category": {category: 0 for category in JOURNAL_CATEGORIES},
            "by_week": [],
        },
        "entries": [],
        "authority": dict(_AUTHORITY),
        "errors": [
            {
                "code": "research_journal_unavailable",
                "message": message,
                "recovery_action": "retry",
            }
        ],
    }


def _validate_draft(draft: JournalDraft) -> None:
    if not isinstance(draft, JournalDraft):
        raise ValueError("Research journal draft is invalid")
    if not isinstance(draft.research_date, date) or isinstance(
        draft.research_date, datetime
    ):
        raise ValueError("research_date must be an ISO calendar date")
    if draft.category not in JOURNAL_CATEGORIES:
        raise ValueError("Unsupported research journal category")
    if draft.symbol is not None and (
        not isinstance(draft.symbol, str) or not _SYMBOL.fullmatch(draft.symbol)
    ):
        raise ValueError("symbol must be a valid instrument identifier")
    _bounded_text(draft.title, "title", MAX_JOURNAL_TITLE_LENGTH)
    _bounded_text(draft.note, "note", MAX_JOURNAL_NOTE_LENGTH)
    if draft.decision not in JOURNAL_DECISIONS:
        raise ValueError("Unsupported research journal decision")
    if draft.decision == "not_recorded":
        if draft.confidence is not None:
            raise ValueError(
                "confidence must be omitted when no research decision is recorded"
            )
    elif (
        isinstance(draft.confidence, bool)
        or not isinstance(draft.confidence, int)
        or not 0 <= draft.confidence <= 100
    ):
        raise ValueError(
            "confidence must be an integer between 0 and 100 for a research decision"
        )
    if draft.correction_of is not None and (
        not isinstance(draft.correction_of, str)
        or not _ENTRY_ID.fullmatch(draft.correction_of)
    ):
        raise ValueError("correction_of must be a valid research journal entry id")


def _validate_query(query: JournalQuery) -> None:
    if not isinstance(query, JournalQuery):
        raise ValueError("Research journal query is invalid")
    if query.category is not None and query.category not in JOURNAL_CATEGORIES:
        raise ValueError("Unsupported research journal category")
    if query.symbol is not None and (
        not isinstance(query.symbol, str) or not _SYMBOL.fullmatch(query.symbol)
    ):
        raise ValueError("symbol must be a valid instrument identifier")
    if query.query is not None:
        _bounded_text(query.query, "query", MAX_JOURNAL_QUERY_LENGTH)
    if (
        isinstance(query.limit, bool)
        or not isinstance(query.limit, int)
        or not 1 <= query.limit <= JOURNAL_MAX_LIMIT
    ):
        raise ValueError(
            f"limit must be an integer between 1 and {JOURNAL_MAX_LIMIT}"
        )


def _matches(record: Mapping[str, Any], query: JournalQuery) -> bool:
    if query.category is not None and record["category"] != query.category:
        return False
    if query.symbol is not None and record["symbol"] != query.symbol:
        return False
    if query.query is None:
        return True
    needle = query.query.casefold()
    haystack = "\n".join(
        str(record.get(field) or "")
        for field in (
            "title",
            "note",
            "symbol",
            "category",
            "decision",
            "actor",
            "entry_id",
        )
    ).casefold()
    return needle in haystack


def _read_record(path: Path, expected_owner: str) -> dict[str, Any]:
    try:
        record = load_unique_json(path, max_bytes=MAX_JOURNAL_RECORD_BYTES)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Invalid research journal record: {path}: {exc}") from exc
    if not isinstance(record, dict):
        raise RuntimeError(f"Research journal record must be an object: {path}")
    unsupported = sorted(set(record) - _ENTRY_FIELDS)
    missing = sorted(_ENTRY_FIELDS - set(record))
    if unsupported or missing:
        details = []
        if unsupported:
            details.append("unsupported fields: " + ", ".join(unsupported))
        if missing:
            details.append("missing fields: " + ", ".join(missing))
        raise RuntimeError("Research journal schema is invalid: " + "; ".join(details))
    try:
        entry_id = record["entry_id"]
        if not isinstance(entry_id, str) or not _ENTRY_ID.fullmatch(entry_id):
            raise ValueError("entry_id is invalid")
        if path.stem != entry_id:
            raise ValueError("entry filename does not match entry_id")
        if record["schema_version"] != SCHEMA_VERSION:
            raise ValueError("schema_version is unsupported")
        fingerprint = record["entry_fingerprint"]
        if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(
            fingerprint
        ):
            raise ValueError("entry_fingerprint is invalid")
        unsigned = {
            key: value for key, value in record.items() if key != "entry_fingerprint"
        }
        if _record_fingerprint(unsigned) != fingerprint:
            raise ValueError("entry_fingerprint does not match the record content")
        if record["owner"] != expected_owner:
            raise ValueError("owner binding does not match its directory")
        created_at = datetime.fromisoformat(
            str(record["created_at"]).replace("Z", "+00:00")
        )
        if created_at.tzinfo is None:
            raise ValueError("created_at must include a timezone")
        research_date = date.fromisoformat(str(record["research_date"]))
        week_start = research_date - timedelta(days=research_date.weekday())
        if record["week_start"] != week_start.isoformat():
            raise ValueError("week_start does not match research_date")
        draft = JournalDraft(
            research_date=research_date,
            category=record["category"],
            symbol=record["symbol"],
            title=record["title"],
            note=record["note"],
            decision=record["decision"],
            confidence=record["confidence"],
            correction_of=record["correction_of"],
        )
        _validate_draft(draft)
        _bounded_text(record["actor"], "actor", MAX_JOURNAL_ACTOR_LENGTH)
        evidence = record["evidence"]
        if not isinstance(evidence, dict) or set(evidence) != {
            "market_snapshot",
            "strategy",
        }:
            raise ValueError("evidence is invalid")
        _validated_market_evidence(evidence["market_snapshot"])
        _validated_strategy_evidence(evidence["strategy"])
        if record["authority"] != _AUTHORITY:
            raise ValueError("authority boundary is invalid")
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid research journal record: {path}: {exc}") from exc
    return record


def _validated_market_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _MARKET_EVIDENCE_FIELDS:
        raise ValueError("market snapshot evidence is invalid")
    available = value.get("available")
    if not isinstance(available, bool):
        raise ValueError("market snapshot availability must be boolean")
    selected_date = value.get("date")
    fingerprint = value.get("fingerprint")
    if available:
        if not isinstance(selected_date, str):
            raise ValueError("market snapshot date is required")
        date.fromisoformat(selected_date)
        if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(
            fingerprint
        ):
            raise ValueError("market snapshot fingerprint is invalid")
    elif selected_date is not None or fingerprint is not None:
        raise ValueError("unavailable market evidence must not contain values")
    return {
        "available": available,
        "date": selected_date,
        "fingerprint": fingerprint,
    }


def _validated_strategy_evidence(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _STRATEGY_EVIDENCE_FIELDS:
        raise ValueError("strategy evidence is invalid")
    available = value.get("available")
    if not isinstance(available, bool):
        raise ValueError("strategy evidence availability must be boolean")
    candidate_id = value.get("candidate_id")
    fingerprint = value.get("fingerprint")
    lifecycle_state = value.get("lifecycle_state")
    if available:
        if candidate_id is not None and (
            not isinstance(candidate_id, str)
            or not re.fullmatch(r"cand_[0-9a-f]{32}", candidate_id)
        ):
            raise ValueError("strategy candidate id is invalid")
        if not isinstance(fingerprint, str) or not _FINGERPRINT.fullmatch(
            fingerprint
        ):
            raise ValueError("strategy fingerprint is invalid")
        if not isinstance(lifecycle_state, str) or not lifecycle_state:
            raise ValueError("strategy lifecycle state is required")
    elif any(item is not None for item in (candidate_id, fingerprint, lifecycle_state)):
        raise ValueError("unavailable strategy evidence must not contain values")
    return {
        "available": available,
        "candidate_id": candidate_id,
        "fingerprint": fingerprint,
        "lifecycle_state": lifecycle_state,
    }


def _public_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in record.items() if key != "owner"}


def _record_fingerprint(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return sha256(encoded).hexdigest()


def _normalize_owner(owner: str) -> str:
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("Research journal owner must be a non-empty string")
    normalized = owner.strip().casefold()
    if len(normalized) > 200:
        raise ValueError("Research journal owner is too long")
    return normalized


def _bounded_text(value: object, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > maximum
        or "\x00" in value
    ):
        raise ValueError(
            f"{field} must contain between 1 and {maximum} trimmed characters"
        )
    return value


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


def _atomic_create_json(path: Path, value: Mapping[str, Any]) -> None:
    if set(value) != _ENTRY_FIELDS:
        raise ValueError("Research journal record schema is invalid")
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
        if temporary.stat().st_size > MAX_JOURNAL_RECORD_BYTES:
            raise ValueError(
                f"Research journal record exceeds {MAX_JOURNAL_RECORD_BYTES} bytes"
            )
        try:
            if os.name == "nt":
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
                f"Immutable research journal record already exists: {path.stem}"
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
