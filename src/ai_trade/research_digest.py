"""Persistent, append-only daily and weekly research digests.

The digest store is deliberately independent from the archive projection and
from the web/CLI layers.  Callers provide an already validated projection
payload and a source manifest.  This module binds the payload to the owner,
paper-account epoch, configuration fingerprint, and an immutable revision
chain.  It has no broker, strategy, accounting, or provider capability.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
from threading import Lock, RLock
from typing import Any, Iterator, Mapping, Sequence
from uuid import uuid4

from .json_utils import load_unique_json


SCHEMA_VERSION = 1
DIGEST_KINDS = frozenset({"daily", "weekly"})
DIGEST_DEFAULT_LIMIT = 52
DIGEST_MAX_LIMIT = 200
MAX_REVISIONS_PER_CHAIN = 2_000
MAX_CHAINS_PER_ACCOUNT = 2_000
MAX_DIGEST_RECORD_BYTES = 1 * 1024 * 1024
MAX_PAYLOAD_BYTES = 768 * 1024
MAX_SOURCE_FINGERPRINTS = 366
MAX_DIGESTS_PER_BATCH = 104
MAX_ACTOR_LENGTH = 80
MAX_TRIGGER_LENGTH = 40
MAX_TEXT_LENGTH = 16_384
MAX_JSON_DEPTH = 12
MAX_JSON_NODES = 20_000


DIGEST_ID = re.compile(r"digest_[0-9a-f]{32}\Z")
FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
REVISION_FILE = re.compile(r"revision_[0-9]{8}\.json\Z")
CHAIN_DIRECTORY = re.compile(r"(?:daily|weekly)_\d{4}-\d{2}-\d{2}\Z")
ACTOR = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@+\-/ ]{0,79}\Z")
TRIGGER = re.compile(r"[a-z][a-z0-9_-]{0,39}\Z")


ARCHIVE_STATUSES = frozenset(
    {
        "current",
        "provisional",
        "partial",
        "empty",
        "unavailable",
        "missing_report",
        "unbound_report",
        "evidence_mismatch",
        "journal_only",
    }
)

_AUTHORITY = {
    "research_only": True,
    "execution_authorized": False,
    "strategy_changed": False,
    "paper_account_changed": False,
    "broker_permissions_changed": False,
}

_SOURCE_FIELDS = frozenset(
    {
        "fingerprint",
        "evidence_fingerprints",
        "calendar_fingerprint",
        "config_fingerprint",
        "account_fingerprint",
    }
)
_SENSITIVE_PAYLOAD_KEYS = frozenset(
    {"account_id", "owner", "owner_id", "account_identifier"}
)
_RECORD_FIELDS = frozenset(
    {
        "schema_version",
        "digest_id",
        "digest_fingerprint",
        "owner",
        "account_fingerprint",
        "config_fingerprint",
        "kind",
        "period_start",
        "period_end",
        "revision",
        "supersedes",
        "supersedes_fingerprint",
        "created_at",
        "actor",
        "trigger",
        "status",
        "payload",
        "payload_fingerprint",
        "source",
        "source_binding_fingerprint",
        "content_fingerprint",
        "authority",
    }
)

_LOCKS_GUARD = Lock()
_LOCKS: dict[str, "_AccountLockState"] = {}


@dataclass(frozen=True)
class ResearchDigestQuery:
    """Bounded query for persisted digest revisions."""

    kind: str = "all"
    period_start: date | None = None
    limit: int = DIGEST_DEFAULT_LIMIT
    include_revisions: bool = False


@dataclass(frozen=True)
class DigestWriteResult:
    """Outcome of an append attempt.

    ``digest`` is the same public record returned by :meth:`append`.  A
    repeated source/payload returns ``created=False`` and ``reused=True``.
    """

    digest: dict[str, Any]
    created: bool
    reused: bool


@dataclass(frozen=True)
class ResearchDigestDraft:
    """One validated-on-write digest request used by batch materialization."""

    kind: str
    period_start: date
    payload: Mapping[str, Any]
    source: Mapping[str, Any]
    config_fingerprint: str
    actor: str = "local-owner"
    trigger: str = "manual"
    now: datetime | None = None


class ResearchDigestCapacityError(RuntimeError):
    """The bounded immutable digest store cannot accept another revision."""


class ResearchDigestBatchError(RuntimeError):
    """A batch encountered an I/O failure after committing a visible prefix."""

    def __init__(self, message: str, results: Sequence[DigestWriteResult]):
        super().__init__(message)
        self.results = tuple(results)


class _ResearchDigestPublishedError(RuntimeError):
    """A revision became visible before its durability barrier failed."""


class _AccountLockState:
    def __init__(self) -> None:
        self.thread_lock = RLock()
        self.depth = 0


@dataclass(frozen=True)
class _PreparedDigest:
    kind: str
    period_start: date
    period_end: date
    payload: dict[str, Any]
    source: dict[str, Any]
    config_fingerprint: str
    actor: str
    trigger: str
    created_at: str
    content_fingerprint: str


class ResearchDigestStore:
    """Owner/account isolated immutable daily and weekly digest store."""

    def __init__(self, root: str | Path):
        raw_root = Path(root)
        if raw_root.is_symlink():
            raise RuntimeError("Research digest root must not be symbolic")
        self.root = raw_root.resolve()

    def owner_id(self, owner: str) -> str:
        return _owner_id(owner)

    def account_id(self, account_id: str) -> str:
        """Return the non-secret account epoch fingerprint used on disk."""

        return _account_id(account_id)

    def owner_directory(self, owner: str, account_id: str | None = None) -> Path:
        owner_hash = self.owner_id(owner)
        directory = self.root / "users" / owner_hash
        if account_id is not None:
            directory = directory / "accounts" / self.account_id(account_id)
        return directory

    def append(
        self,
        owner: str,
        account_id: str,
        *,
        kind: str,
        period_start: date,
        payload: Mapping[str, Any],
        source: Mapping[str, Any],
        config_fingerprint: str,
        actor: str = "local-owner",
        trigger: str = "manual",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Append or reuse a digest and return its public immutable record."""

        return self.append_with_result(
            owner,
            account_id,
            kind=kind,
            period_start=period_start,
            payload=payload,
            source=source,
            config_fingerprint=config_fingerprint,
            actor=actor,
            trigger=trigger,
            now=now,
        ).digest

    def append_with_result(
        self,
        owner: str,
        account_id: str,
        *,
        kind: str,
        period_start: date,
        payload: Mapping[str, Any],
        source: Mapping[str, Any],
        config_fingerprint: str,
        actor: str = "local-owner",
        trigger: str = "manual",
        now: datetime | None = None,
    ) -> DigestWriteResult:
        """Append one immutable revision, or return the exact latest revision.

        Idempotence is based only on the canonical period, source manifest and
        payload.  Volatile fields such as actor, trigger and creation time do
        not create a new revision when the evidence is unchanged.
        """

        return self.append_many_with_results(
            owner,
            account_id,
            [
                ResearchDigestDraft(
                    kind=kind,
                    period_start=period_start,
                    payload=payload,
                    source=source,
                    config_fingerprint=config_fingerprint,
                    actor=actor,
                    trigger=trigger,
                    now=now,
                )
            ],
        )[0]

    def append_many_with_results(
        self,
        owner: str,
        account_id: str,
        drafts: Sequence[ResearchDigestDraft],
    ) -> list[DigestWriteResult]:
        """Preflight and append a bounded owner/account batch.

        Schema, period, configuration and capacity checks complete before the
        first file is created. Unexpected filesystem failures can still leave
        a committed prefix; :class:`ResearchDigestBatchError` exposes that
        prefix so callers can report an explicit partial result.
        """

        if (
            isinstance(drafts, (str, bytes))
            or not isinstance(drafts, Sequence)
            or not 1 <= len(drafts) <= MAX_DIGESTS_PER_BATCH
        ):
            raise ValueError(
                "Research digest batch must contain between 1 and "
                f"{MAX_DIGESTS_PER_BATCH} drafts"
            )
        owner_hash = _owner_id(owner)
        account_hash = _account_id(account_id)
        prepared = [
            _prepare_digest(draft, account_fingerprint=account_hash)
            for draft in drafts
        ]
        keys = [(item.kind, item.period_start) for item in prepared]
        if len(set(keys)) != len(keys):
            raise ValueError("Research digest batch periods must be unique")
        requested_configs = {item.config_fingerprint for item in prepared}
        if len(requested_configs) != 1:
            raise ValueError(
                "Research digest batch must use one account configuration fingerprint"
            )
        requested_config = next(iter(requested_configs))

        with self._account_lock(owner_hash, account_hash):
            chains = self._all_chains_unlocked(owner_hash, account_hash)
            existing_configs = {
                record["config_fingerprint"]
                for chain in chains
                for record in chain
            }
            if existing_configs and existing_configs != {requested_config}:
                raise ValueError(
                    "Research digest configuration does not match the existing "
                    "paper-account epoch"
                )
            chain_map = {
                (chain[0]["kind"], date.fromisoformat(chain[0]["period_start"])): list(
                    chain
                )
                for chain in chains
            }
            plans: list[tuple[_PreparedDigest, dict[str, Any], bool]] = []
            chain_count = len(chain_map)
            for item in prepared:
                key = (item.kind, item.period_start)
                chain = chain_map.get(key, [])
                if (
                    chain
                    and chain[-1]["content_fingerprint"]
                    == item.content_fingerprint
                ):
                    plans.append((item, chain[-1], False))
                    continue
                if len(chain) >= MAX_REVISIONS_PER_CHAIN:
                    raise ResearchDigestCapacityError(
                        "Research digest revision limit reached "
                        f"({MAX_REVISIONS_PER_CHAIN})"
                    )
                if not chain:
                    if chain_count >= MAX_CHAINS_PER_ACCOUNT:
                        raise ResearchDigestCapacityError(
                            "Research digest chain limit reached "
                            f"({MAX_CHAINS_PER_ACCOUNT})"
                        )
                    chain_count += 1
                record = _digest_record(
                    item,
                    owner_hash=owner_hash,
                    account_hash=account_hash,
                    previous=chain[-1] if chain else None,
                    revision=len(chain) + 1,
                )
                chain_map[key] = [*chain, record]
                plans.append((item, record, True))

            results: list[DigestWriteResult] = []
            for item, record, created in plans:
                if not created:
                    results.append(
                        DigestWriteResult(
                            digest=_public_record(record),
                            created=False,
                            reused=True,
                        )
                    )
                    continue
                revision = int(record["revision"])
                path = self._chain_directory(
                    owner_hash,
                    account_hash,
                    item.kind,
                    item.period_start,
                ) / f"revision_{revision:08d}.json"
                published = False
                try:
                    _atomic_create_json(
                        path,
                        record,
                        staging_root=self.root / ".staging",
                    )
                    published = True
                    committed = _read_record(
                        path,
                        expected_owner=owner_hash,
                        expected_account=account_hash,
                        expected_kind=item.kind,
                        expected_period=item.period_start,
                        expected_revision=revision,
                    )
                except _ResearchDigestPublishedError as exc:
                    results.append(
                        DigestWriteResult(
                            digest=_public_record(record),
                            created=True,
                            reused=False,
                        )
                    )
                    raise ResearchDigestBatchError(
                        "Research digest batch stopped after "
                        f"{len(results)} of {len(plans)} results: {exc}",
                        results,
                    ) from exc
                except (OSError, RuntimeError, ValueError) as exc:
                    if published:
                        results.append(
                            DigestWriteResult(
                                digest=_public_record(record),
                                created=True,
                                reused=False,
                            )
                        )
                    raise ResearchDigestBatchError(
                        "Research digest batch stopped after "
                        f"{len(results)} of {len(plans)} results: {exc}",
                        results,
                    ) from exc
                results.append(
                    DigestWriteResult(
                        digest=_public_record(committed),
                        created=True,
                        reused=False,
                    )
                )
            return results

    def list(
        self,
        owner: str,
        account_id: str,
        query: ResearchDigestQuery | None = None,
    ) -> dict[str, Any]:
        """Return latest digests, or every immutable revision when requested."""

        query = query or ResearchDigestQuery()
        _validate_query(query)
        owner_hash = _owner_id(owner)
        account_hash = _account_id(account_id)
        with self._account_lock(owner_hash, account_hash):
            chains = self._all_chains_unlocked(owner_hash, account_hash)
        selected_chains = [
            chain
            for chain in chains
            if query.kind == "all" or chain[0]["kind"] == query.kind
        ]
        if query.period_start is not None:
            selected_chains = [
                chain
                for chain in selected_chains
                if date.fromisoformat(chain[0]["period_start"]) == query.period_start
            ]
        if query.include_revisions:
            records = [record for chain in selected_chains for record in chain]
            records.sort(
                key=lambda item: (
                    item["period_start"],
                    item["kind"],
                    int(item["revision"]),
                ),
                reverse=True,
            )
        else:
            records = [chain[-1] for chain in selected_chains]
            records.sort(
                key=lambda item: (item["period_start"], item["kind"]),
                reverse=True,
            )
        visible = records[: query.limit]
        return {
            "schema_version": SCHEMA_VERSION,
            "available": True,
            "status": _aggregate_ledger_status(selected_chains),
            "filters": _query_payload(query),
            "account_fingerprint": account_hash,
            "summary": {
                "total_revisions": sum(len(chain) for chain in selected_chains),
                "total_chains": len(selected_chains),
                "latest_count": len(selected_chains),
                "returned": len(visible),
                "truncated": len(records) > len(visible),
                "maximum_revisions_per_chain": MAX_REVISIONS_PER_CHAIN,
                "maximum_chains": MAX_CHAINS_PER_ACCOUNT,
            },
            "digests": [_public_record(item) for item in visible],
            "authority": dict(_AUTHORITY),
            "errors": [],
        }

    def get(
        self,
        owner: str,
        account_id: str,
        digest_id: str,
    ) -> dict[str, Any]:
        """Return one validated digest revision or raise ``KeyError``."""

        if not DIGEST_ID.fullmatch(digest_id):
            raise ValueError("digest_id is invalid")
        owner_hash = _owner_id(owner)
        account_hash = _account_id(account_id)
        with self._account_lock(owner_hash, account_hash):
            records = [
                record
                for chain in self._all_chains_unlocked(owner_hash, account_hash)
                for record in chain
                if record["digest_id"] == digest_id
            ]
        if not records:
            raise KeyError(digest_id)
        return _public_record(records[0])

    def verify(self, owner: str, account_id: str) -> None:
        """Validate every chain for an owner/account epoch."""

        owner_hash = _owner_id(owner)
        account_hash = _account_id(account_id)
        with self._account_lock(owner_hash, account_hash):
            self._verify_account_unlocked(owner_hash, account_hash)

    @contextmanager
    def _account_lock(self, owner_hash: str, account_hash: str) -> Iterator[None]:
        directory = self._account_directory(owner_hash, account_hash)
        _assert_safe_directory(self.root, "digest root")
        _assert_safe_directory(self.root / "users", "digest users")
        _assert_safe_directory(self.root / "users" / owner_hash, "digest owner")
        _assert_safe_directory(
            self.root / "users" / owner_hash / "accounts", "digest accounts"
        )
        _assert_safe_directory(directory, "digest account")
        key = os.path.normcase(str(directory))
        with _LOCKS_GUARD:
            state = _LOCKS.setdefault(key, _AccountLockState())
        with state.thread_lock:
            if state.depth:
                state.depth += 1
                try:
                    yield
                finally:
                    state.depth -= 1
                return
            with _file_lock(directory / ".account.lock"):
                state.depth = 1
                try:
                    yield
                finally:
                    state.depth = 0

    def _account_directory(self, owner_hash: str, account_hash: str) -> Path:
        _valid_hash(owner_hash, "owner")
        _valid_hash(account_hash, "account_fingerprint")
        return self.root / "users" / owner_hash / "accounts" / account_hash

    def _chain_directory(
        self,
        owner_hash: str,
        account_hash: str,
        kind: str,
        period_start: date,
    ) -> Path:
        return (
            self._account_directory(owner_hash, account_hash)
            / "digests"
            / kind
            / period_start.isoformat()
        )

    def _verify_account_unlocked(self, owner_hash: str, account_hash: str) -> None:
        account_directory = self._account_directory(owner_hash, account_hash)
        if account_directory.is_symlink():
            raise RuntimeError("Research digest account directory must not be symbolic")
        if not account_directory.exists():
            return
        if not account_directory.is_dir():
            raise RuntimeError("Research digest account path is not a directory")
        for path in account_directory.iterdir():
            if path.name == ".account.lock":
                if path.is_symlink() or not path.is_file():
                    raise RuntimeError("Research digest account lock is invalid")
                continue
            if path.name != "digests" or path.is_symlink() or not path.is_dir():
                raise RuntimeError("Unexpected research digest account member")
        entries = account_directory / "digests"
        if not entries.exists():
            return
        if entries.is_symlink() or not entries.is_dir():
            raise RuntimeError("Research digest entries path is invalid")
        kind_directories = {"daily", "weekly"}
        chain_count = 0
        seen_digest_ids: set[str] = set()
        seen_digest_fingerprints: set[str] = set()
        seen_config_fingerprints: set[str] = set()
        for path in entries.iterdir():
            if path.is_symlink() or not path.is_dir() or path.name not in kind_directories:
                raise RuntimeError("Unexpected research digest kind directory")
            for chain_path in path.iterdir():
                if (
                    chain_path.is_symlink()
                    or not chain_path.is_dir()
                    or not CHAIN_DIRECTORY.fullmatch(f"{path.name}_{chain_path.name}")
                ):
                    raise RuntimeError("Unexpected research digest chain directory")
                chain_count += 1
                if chain_count > MAX_CHAINS_PER_ACCOUNT:
                    raise ResearchDigestCapacityError(
                        "Research digest chain count exceeds the supported limit"
                    )
                period = _parse_chain_period(path.name, chain_path.name)
                chain = self._chain_unlocked(owner_hash, account_hash, path.name, period)
                for record in chain:
                    digest_id = record["digest_id"]
                    digest_fingerprint = record["digest_fingerprint"]
                    if digest_id in seen_digest_ids:
                        raise RuntimeError("Research digest IDs must be globally unique")
                    if digest_fingerprint in seen_digest_fingerprints:
                        raise RuntimeError(
                            "Research digest fingerprints must be globally unique"
                        )
                    seen_digest_ids.add(digest_id)
                    seen_digest_fingerprints.add(digest_fingerprint)
                    seen_config_fingerprints.add(record["config_fingerprint"])
                    if len(seen_config_fingerprints) > 1:
                        raise RuntimeError(
                            "Research digest account contains multiple configuration "
                            "fingerprints"
                        )

    def _all_chains_unlocked(
        self, owner_hash: str, account_hash: str
    ) -> list[list[dict[str, Any]]]:
        self._verify_account_unlocked(owner_hash, account_hash)
        account_directory = self._account_directory(owner_hash, account_hash)
        entries = account_directory / "digests"
        if not entries.exists():
            return []
        chains: list[list[dict[str, Any]]] = []
        for kind in ("daily", "weekly"):
            kind_directory = entries / kind
            if not kind_directory.exists():
                continue
            periods = sorted(
                (path for path in kind_directory.iterdir() if path.is_dir()),
                key=lambda path: path.name,
            )
            for path in periods:
                period = _parse_chain_period(kind, path.name)
                chains.append(self._chain_unlocked(owner_hash, account_hash, kind, period))
        return chains

    def _chain_unlocked(
        self,
        owner_hash: str,
        account_hash: str,
        kind: str,
        period_start: date,
    ) -> list[dict[str, Any]]:
        chain_directory = self._chain_directory(
            owner_hash, account_hash, kind, period_start
        )
        if chain_directory.is_symlink():
            raise RuntimeError("Research digest chain must not be symbolic")
        if not chain_directory.exists():
            return []
        if not chain_directory.is_dir():
            raise RuntimeError("Research digest chain path is not a directory")
        paths: list[Path] = []
        for path in chain_directory.iterdir():
            if path.is_symlink():
                raise RuntimeError("Research digest revision must not be symbolic")
            if not path.is_file() or not REVISION_FILE.fullmatch(path.name):
                # There is no recovery command for this store.  A temporary or
                # unknown member therefore remains ambiguous and fails closed.
                raise RuntimeError("Unexpected research digest chain member")
            paths.append(path)
        if not paths:
            raise RuntimeError("Research digest chain has no committed revisions")
        if len(paths) > MAX_REVISIONS_PER_CHAIN:
            raise ResearchDigestCapacityError(
                "Research digest revision count exceeds the supported limit"
            )
        paths.sort(key=lambda path: int(path.stem.removeprefix("revision_")))
        records: list[dict[str, Any]] = []
        previous: dict[str, Any] | None = None
        for expected_revision, path in enumerate(paths, start=1):
            revision = int(path.stem.removeprefix("revision_"))
            if revision != expected_revision:
                raise RuntimeError("Research digest revision chain has a gap")
            record = _read_record(
                path,
                expected_owner=owner_hash,
                expected_account=account_hash,
                expected_kind=kind,
                expected_period=period_start,
                expected_revision=revision,
            )
            if previous is None:
                if record["supersedes"] is not None:
                    raise RuntimeError("First research digest revision has a parent")
            elif (
                record["supersedes"] != previous["digest_id"]
                or record["supersedes_fingerprint"]
                != previous["digest_fingerprint"]
            ):
                raise RuntimeError("Research digest supersedes chain is invalid")
            records.append(record)
            previous = record
        return records


def _prepare_digest(
    draft: ResearchDigestDraft,
    *,
    account_fingerprint: str,
) -> _PreparedDigest:
    if not isinstance(draft, ResearchDigestDraft):
        raise ValueError("Research digest batch member is invalid")
    kind = _valid_kind(draft.kind)
    period_start, period_end = _period_bounds(kind, draft.period_start)
    config_fingerprint = _valid_fingerprint(
        draft.config_fingerprint, "config_fingerprint"
    )
    payload = _normalize_payload(draft.payload, kind, period_start, period_end)
    source = _normalize_source(
        draft.source,
        account_fingerprint=account_fingerprint,
        config_fingerprint=config_fingerprint,
    )
    _validate_payload_source_binding(payload, kind, source["fingerprint"])
    content_fingerprint = _content_fingerprint(
        kind=kind,
        period_start=period_start,
        period_end=period_end,
        account_fingerprint=account_fingerprint,
        config_fingerprint=config_fingerprint,
        payload=payload,
        source=source,
    )
    return _PreparedDigest(
        kind=kind,
        period_start=period_start,
        period_end=period_end,
        payload=payload,
        source=source,
        config_fingerprint=config_fingerprint,
        actor=_valid_actor(draft.actor),
        trigger=_valid_trigger(draft.trigger),
        created_at=_normalize_now(draft.now),
        content_fingerprint=content_fingerprint,
    )


def _digest_record(
    item: _PreparedDigest,
    *,
    owner_hash: str,
    account_hash: str,
    previous: Mapping[str, Any] | None,
    revision: int,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "digest_id": f"digest_{uuid4().hex}",
        "digest_fingerprint": None,
        "owner": owner_hash,
        "account_fingerprint": account_hash,
        "config_fingerprint": item.config_fingerprint,
        "kind": item.kind,
        "period_start": item.period_start.isoformat(),
        "period_end": item.period_end.isoformat(),
        "revision": revision,
        "supersedes": previous["digest_id"] if previous else None,
        "supersedes_fingerprint": (
            previous["digest_fingerprint"] if previous else None
        ),
        "created_at": item.created_at,
        "actor": item.actor,
        "trigger": item.trigger,
        "status": item.payload["status"],
        "payload": item.payload,
        "payload_fingerprint": _fingerprint(item.payload),
        "source": item.source,
        "source_binding_fingerprint": _fingerprint(item.source),
        "content_fingerprint": item.content_fingerprint,
        "authority": dict(_AUTHORITY),
    }
    record["digest_fingerprint"] = _fingerprint(
        {key: value for key, value in record.items() if key != "digest_fingerprint"}
    )
    return record


def _public_record(record: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: _clone_json(value)
        for key, value in record.items()
        if key != "owner"
    }


def _query_payload(query: ResearchDigestQuery) -> dict[str, Any]:
    return {
        "kind": query.kind,
        "period_start": (
            query.period_start.isoformat() if query.period_start is not None else None
        ),
        "limit": query.limit,
        "include_revisions": query.include_revisions,
    }


def _aggregate_ledger_status(chains: Sequence[Sequence[Mapping[str, Any]]]) -> str:
    """Summarize the newest revision of every selected period chain."""

    if not chains:
        return "empty"
    statuses = {str(chain[-1]["status"]) for chain in chains}
    if "unavailable" in statuses:
        return "unavailable"
    incomplete = {
        "partial",
        "missing_report",
        "unbound_report",
        "evidence_mismatch",
        "journal_only",
    }
    if statuses & incomplete:
        return "partial"
    if "provisional" in statuses:
        return "provisional"
    if "empty" in statuses:
        return "empty" if statuses == {"empty"} else "partial"
    return "current"


def _validate_query(query: ResearchDigestQuery) -> None:
    if not isinstance(query, ResearchDigestQuery):
        raise ValueError("Research digest query is invalid")
    if query.kind not in {"all", *DIGEST_KINDS}:
        raise ValueError("Research digest kind must be all, daily, or weekly")
    if query.period_start is not None:
        if not isinstance(query.period_start, date) or isinstance(
            query.period_start, datetime
        ):
            raise ValueError("Research digest period_start is invalid")
        if query.kind == "weekly" and query.period_start.weekday() != 0:
            raise ValueError("weekly period_start must be an ISO Monday")
    if (
        isinstance(query.limit, bool)
        or not isinstance(query.limit, int)
        or not 1 <= query.limit <= DIGEST_MAX_LIMIT
    ):
        raise ValueError(
            f"Research digest limit must be between 1 and {DIGEST_MAX_LIMIT}"
        )
    if not isinstance(query.include_revisions, bool):
        raise ValueError("include_revisions must be a boolean")


def _valid_kind(value: object) -> str:
    if not isinstance(value, str) or value not in DIGEST_KINDS:
        raise ValueError("Research digest kind must be daily or weekly")
    return value


def _period_bounds(kind: str, period_start: object) -> tuple[date, date]:
    if not isinstance(period_start, date) or isinstance(period_start, datetime):
        raise ValueError("Research digest period_start must be a calendar date")
    if kind == "daily":
        return period_start, period_start
    if period_start.weekday() != 0:
        raise ValueError("weekly period_start must be an ISO Monday")
    return period_start, period_start + timedelta(days=6)


def _parse_chain_period(kind: str, value: str) -> date:
    if not isinstance(value, str):
        raise RuntimeError("Research digest chain period is invalid")
    try:
        period = date.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError("Research digest chain period is invalid") from exc
    if period.isoformat() != value:
        raise RuntimeError("Research digest chain period is not canonical")
    _period_bounds(kind, period)
    return period


def _normalize_payload(
    value: Mapping[str, Any],
    kind: str,
    period_start: date,
    period_end: date,
) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Research digest payload must be an object")
    normalized = _clone_json(value)
    if not isinstance(normalized, dict):
        raise ValueError("Research digest payload must be an object")
    _reject_sensitive_payload_keys(normalized)
    status = normalized.get("status")
    if not isinstance(status, str) or status not in ARCHIVE_STATUSES:
        raise ValueError("Research digest payload status is invalid")
    if kind == "daily":
        if normalized.get("as_of_date") != period_start.isoformat():
            raise ValueError("daily payload date does not match period_start")
    else:
        if normalized.get("week_start") != period_start.isoformat():
            raise ValueError("weekly payload week_start does not match period_start")
        if normalized.get("week_end") != period_end.isoformat():
            raise ValueError("weekly payload week_end does not match period_end")
    if "generated_at" in normalized:
        raise ValueError("volatile generated_at is not allowed in a digest payload")
    authority = normalized.get("authority")
    if authority is not None and authority != _AUTHORITY:
        raise ValueError("Research digest payload authority is invalid")
    source = normalized.get("source")
    if source is not None and not isinstance(source, Mapping):
        raise ValueError("Research digest payload source is invalid")
    encoded = _canonical_bytes(normalized)
    if len(encoded) > MAX_PAYLOAD_BYTES:
        raise ValueError(
            f"Research digest payload exceeds {MAX_PAYLOAD_BYTES} bytes"
        )
    return normalized


def _normalize_source(
    value: Mapping[str, Any],
    *,
    account_fingerprint: str,
    config_fingerprint: str,
) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _SOURCE_FIELDS:
        raise ValueError("Research digest source schema is invalid")
    fingerprint = _valid_fingerprint(value.get("fingerprint"), "source fingerprint")
    evidence = value.get("evidence_fingerprints")
    if not isinstance(evidence, list) or len(evidence) > MAX_SOURCE_FINGERPRINTS:
        raise ValueError("source evidence_fingerprints is invalid")
    normalized_evidence: list[str] = []
    for item in evidence:
        normalized_evidence.append(
            _valid_fingerprint(item, "source evidence fingerprint")
        )
    if len(set(normalized_evidence)) != len(normalized_evidence):
        raise ValueError("source evidence_fingerprints must be unique")
    calendar = value.get("calendar_fingerprint")
    if calendar is not None:
        calendar = _valid_fingerprint(calendar, "calendar_fingerprint")
    source_config = _valid_fingerprint(
        value.get("config_fingerprint"), "source config_fingerprint"
    )
    source_account = _valid_fingerprint(
        value.get("account_fingerprint"), "source account_fingerprint"
    )
    if source_config != config_fingerprint:
        raise ValueError("source config_fingerprint does not match the digest")
    if source_account != account_fingerprint:
        raise ValueError("source account_fingerprint does not match the digest")
    return {
        "fingerprint": fingerprint,
        "evidence_fingerprints": normalized_evidence,
        "calendar_fingerprint": calendar,
        "config_fingerprint": source_config,
        "account_fingerprint": source_account,
    }


def _content_fingerprint(
    *,
    kind: str,
    period_start: date,
    period_end: date,
    account_fingerprint: str,
    config_fingerprint: str,
    payload: Mapping[str, Any],
    source: Mapping[str, Any],
) -> str:
    return _fingerprint(
        {
            "kind": kind,
            "period_start": period_start.isoformat(),
            "period_end": period_end.isoformat(),
            "account_fingerprint": account_fingerprint,
            "config_fingerprint": config_fingerprint,
            "payload": payload,
            "source": source,
        }
    )


def _read_record(
    path: Path,
    *,
    expected_owner: str,
    expected_account: str,
    expected_kind: str,
    expected_period: date,
    expected_revision: int,
) -> dict[str, Any]:
    """Read one record and normalize all persisted corruption to RuntimeError."""

    try:
        return _read_record_checked(
            path,
            expected_owner=expected_owner,
            expected_account=expected_account,
            expected_kind=expected_kind,
            expected_period=expected_period,
            expected_revision=expected_revision,
        )
    except RuntimeError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, OSError) as exc:
        raise RuntimeError(f"Research digest revision is invalid: {exc}") from exc


def _read_record_checked(
    path: Path,
    *,
    expected_owner: str,
    expected_account: str,
    expected_kind: str,
    expected_period: date,
    expected_revision: int,
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Research digest revision is not a regular file")
    if path.stat().st_size > MAX_DIGEST_RECORD_BYTES:
        raise RuntimeError("Research digest revision exceeds the supported size")
    try:
        record = load_unique_json(path, max_bytes=MAX_DIGEST_RECORD_BYTES)
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise RuntimeError(f"Research digest revision cannot be read: {exc}") from exc
    if not isinstance(record, dict) or set(record) != _RECORD_FIELDS:
        raise RuntimeError("Research digest revision schema is invalid")
    if record["schema_version"] != SCHEMA_VERSION:
        raise RuntimeError("Research digest schema version is unsupported")
    if record["owner"] != expected_owner:
        raise RuntimeError("Research digest owner binding is invalid")
    if record["account_fingerprint"] != expected_account:
        raise RuntimeError("Research digest account binding is invalid")
    kind = _valid_kind(record["kind"])
    if kind != expected_kind:
        raise RuntimeError("Research digest kind binding is invalid")
    period_start, period_end = _period_bounds(kind, _parse_date(record["period_start"]))
    if period_start != expected_period or record["period_end"] != period_end.isoformat():
        raise RuntimeError("Research digest period binding is invalid")
    revision = record["revision"]
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision != expected_revision
    ):
        raise RuntimeError("Research digest revision number is invalid")
    digest_id = record["digest_id"]
    if not isinstance(digest_id, str) or not DIGEST_ID.fullmatch(digest_id):
        raise RuntimeError("Research digest ID is invalid")
    _valid_fingerprint(record["config_fingerprint"], "config_fingerprint")
    _valid_fingerprint(record["digest_fingerprint"], "digest_fingerprint")
    _valid_timestamp(record["created_at"], "created_at")
    _valid_actor(record["actor"])
    _valid_trigger(record["trigger"])
    if record["status"] not in ARCHIVE_STATUSES:
        raise RuntimeError("Research digest status is invalid")
    payload = _normalize_payload(record["payload"], kind, period_start, period_end)
    if payload != record["payload"]:
        raise RuntimeError("Research digest payload is not canonical")
    source = _normalize_source(
        record["source"],
        account_fingerprint=expected_account,
        config_fingerprint=record["config_fingerprint"],
    )
    if source != record["source"]:
        raise RuntimeError("Research digest source is not canonical")
    if record["status"] != payload["status"]:
        raise RuntimeError("Research digest status binding is invalid")
    try:
        _validate_payload_source_binding(payload, kind, source["fingerprint"])
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if record["payload_fingerprint"] != _fingerprint(payload):
        raise RuntimeError("Research digest payload fingerprint is invalid")
    if record["source_binding_fingerprint"] != _fingerprint(source):
        raise RuntimeError("Research digest source fingerprint is invalid")
    expected_content = _content_fingerprint(
        kind=kind,
        period_start=period_start,
        period_end=period_end,
        account_fingerprint=expected_account,
        config_fingerprint=record["config_fingerprint"],
        payload=payload,
        source=source,
    )
    if record["content_fingerprint"] != expected_content:
        raise RuntimeError("Research digest content fingerprint is invalid")
    if record["authority"] != _AUTHORITY:
        raise RuntimeError("Research digest authority is invalid")
    expected_digest = _fingerprint(
        {key: value for key, value in record.items() if key != "digest_fingerprint"}
    )
    if record["digest_fingerprint"] != expected_digest:
        raise RuntimeError("Research digest fingerprint is invalid")
    supersedes = record["supersedes"]
    supersedes_fingerprint = record["supersedes_fingerprint"]
    if supersedes is None:
        if supersedes_fingerprint is not None:
            raise RuntimeError("Research digest parent fingerprint is invalid")
    else:
        if not isinstance(supersedes, str) or not DIGEST_ID.fullmatch(supersedes):
            raise RuntimeError("Research digest parent ID is invalid")
        _valid_fingerprint(supersedes_fingerprint, "supersedes_fingerprint")
    return record


def _parse_date(value: object) -> date:
    if not isinstance(value, str):
        raise RuntimeError("Research digest date is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError("Research digest date is invalid") from exc
    if parsed.isoformat() != value:
        raise RuntimeError("Research digest date is not canonical")
    return parsed


def _validate_payload_source_binding(
    payload: Mapping[str, Any], kind: str, source_fingerprint: str
) -> None:
    """Cross-check the projection's nested source marker when it is present."""

    nested = payload.get("source")
    if nested is None:
        return
    if not isinstance(nested, Mapping):
        raise ValueError("Research digest payload source is invalid")
    key = "evidence_fingerprint" if kind == "daily" else "weekly_fingerprint"
    value = nested.get(key)
    if value is None:
        return
    _valid_fingerprint(value, f"payload source {key}")
    if value != source_fingerprint:
        raise ValueError("payload source fingerprint does not match the digest source")


def _reject_sensitive_payload_keys(value: Any) -> None:
    """Prevent accidental persistence of raw owner/account identifiers."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _SENSITIVE_PAYLOAD_KEYS:
                raise ValueError(f"Research digest payload field {key!r} is forbidden")
            _reject_sensitive_payload_keys(item)
    elif isinstance(value, list):
        for item in value:
            _reject_sensitive_payload_keys(item)


def _normalize_now(value: datetime | None) -> str:
    current = value or datetime.now(timezone.utc)
    if not isinstance(current, datetime) or current.tzinfo is None:
        raise ValueError("Research digest creation time must include a timezone")
    current = current.astimezone(timezone.utc).replace(microsecond=0)
    return current.isoformat().replace("+00:00", "Z")


def _valid_timestamp(value: object, field: str) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise RuntimeError(f"Research digest {field} is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"Research digest {field} is invalid") from exc
    if parsed.tzinfo is None:
        raise RuntimeError(f"Research digest {field} must include a timezone")
    canonical = parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )
    if canonical != value:
        raise RuntimeError(f"Research digest {field} is not canonical")
    return value


def _valid_actor(value: object) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not value
        or len(value) > MAX_ACTOR_LENGTH
        or not ACTOR.fullmatch(value)
    ):
        raise ValueError("Research digest actor is invalid")
    return value


def _valid_trigger(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > MAX_TRIGGER_LENGTH
        or not TRIGGER.fullmatch(value)
    ):
        raise ValueError("Research digest trigger is invalid")
    return value


def _valid_fingerprint(value: object, field: str) -> str:
    if not isinstance(value, str) or not FINGERPRINT.fullmatch(value):
        raise ValueError(f"{field} must be a lowercase SHA-256 fingerprint")
    return value


def _valid_hash(value: object, field: str) -> str:
    try:
        return _valid_fingerprint(value, field)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc


def _owner_id(owner: str) -> str:
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("Research digest owner must be a non-empty string")
    normalized = owner.strip().casefold()
    if len(normalized) > 200:
        raise ValueError("Research digest owner is too long")
    return sha256(normalized.encode("utf-8")).hexdigest()


def _account_id(account_id: str) -> str:
    if (
        not isinstance(account_id, str)
        or not account_id
        or account_id != account_id.strip()
        or len(account_id) > 256
        or "\x00" in account_id
    ):
        raise ValueError("Research digest account_id is invalid")
    return sha256(account_id.encode("utf-8")).hexdigest()


def _fingerprint(value: Any) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


def _canonical_bytes(value: Any) -> bytes:
    _validate_json_tree(value)
    try:
        return json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("Research digest value is not canonical JSON") from exc


def _validate_json_tree(value: Any, *, depth: int = 0, nodes: list[int] | None = None) -> None:
    if nodes is None:
        nodes = [0]
    nodes[0] += 1
    if nodes[0] > MAX_JSON_NODES:
        raise ValueError("Research digest JSON node limit exceeded")
    if depth > MAX_JSON_DEPTH:
        raise ValueError("Research digest JSON nesting is too deep")
    if value is None or isinstance(value, (bool, int)):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("Research digest JSON numbers must be finite")
        return
    if isinstance(value, str):
        if len(value) > MAX_TEXT_LENGTH or "\x00" in value:
            raise ValueError("Research digest text value is too long or contains NUL")
        return
    if isinstance(value, Mapping):
        if len(value) > MAX_JSON_NODES:
            raise ValueError("Research digest object is too large")
        for key, item in value.items():
            if not isinstance(key, str) or not key or len(key) > 256 or "\x00" in key:
                raise ValueError("Research digest object key is invalid")
            _validate_json_tree(item, depth=depth + 1, nodes=nodes)
        return
    if isinstance(value, list):
        if len(value) > MAX_JSON_NODES:
            raise ValueError("Research digest array is too large")
        for item in value:
            _validate_json_tree(item, depth=depth + 1, nodes=nodes)
        return
    raise ValueError("Research digest value must contain only JSON types")


def _clone_json(value: Any) -> Any:
    encoded = _canonical_bytes(value)
    try:
        return json.loads(encoded.decode("ascii"))
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("Research digest value cannot be cloned") from exc


def _assert_safe_directory(path: Path, label: str) -> None:
    if path.is_symlink():
        raise RuntimeError(f"{label} must not be a symbolic link")
    if path.exists() and not path.is_dir():
        raise RuntimeError(f"{label} path is not a directory")


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError("Research digest lock must not be a symbolic link")
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


def _atomic_create_json(
    path: Path,
    value: Mapping[str, Any],
    *,
    staging_root: Path,
) -> None:
    if set(value) != _RECORD_FIELDS:
        raise ValueError("Research digest record schema is invalid")
    if path.is_symlink():
        raise RuntimeError("Research digest revision must not be a symbolic link")
    _assert_safe_directory(staging_root, "Research digest staging")
    staging_root.mkdir(parents=True, exist_ok=True)
    _assert_safe_directory(staging_root, "Research digest staging")
    stage_directory = staging_root / f"digest-{uuid4().hex}"
    stage_directory.mkdir(mode=0o700)
    temporary = stage_directory / path.name
    new_chain = not path.parent.exists()
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
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
        if temporary.stat().st_size > MAX_DIGEST_RECORD_BYTES:
            raise ValueError(
                f"Research digest record exceeds {MAX_DIGEST_RECORD_BYTES} bytes"
            )
        if new_chain:
            kind_directory = path.parent.parent
            _assert_safe_directory(kind_directory.parent, "Research digest entries")
            _assert_safe_directory(kind_directory, "Research digest kind")
            kind_directory.mkdir(parents=True, exist_ok=True)
            _assert_safe_directory(kind_directory, "Research digest kind")
            if path.parent.exists():
                raise FileExistsError(
                    f"Immutable research digest chain already exists: {path.parent.name}"
                )
            os.rename(stage_directory, path.parent)
        else:
            _assert_safe_directory(path.parent, "Research digest chain")
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
                    f"Immutable research digest revision already exists: {path.name}"
                ) from exc
        try:
            _fsync_directory(path.parent)
            if new_chain:
                _fsync_directory(path.parent.parent)
        except OSError as exc:
            raise _ResearchDigestPublishedError(
                "Research digest revision was published but its directory "
                f"durability barrier failed: {exc}"
            ) from exc
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        try:
            stage_directory.rmdir()
        except OSError:
            pass


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


def unavailable_research_digests(
    message: str,
    *,
    code: str = "research_digests_unavailable",
    recovery_action: str | None = None,
    query: ResearchDigestQuery | None = None,
) -> dict[str, Any]:
    """Return a stable, non-throwing response for unavailable archive state."""

    query = query or ResearchDigestQuery()
    return {
        "schema_version": SCHEMA_VERSION,
        "available": False,
        "status": "unavailable",
        "filters": _query_payload(query),
        "summary": {
            "total_revisions": 0,
            "total_chains": 0,
            "latest_count": 0,
            "returned": 0,
            "truncated": False,
        },
        "digests": [],
        "authority": dict(_AUTHORITY),
        "errors": [
            {
                "code": code,
                "message": str(message),
                **({"recovery_action": recovery_action} if recovery_action else {}),
            }
        ],
    }
