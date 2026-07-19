"""Auditable daily sector rankings and market breadth evidence.

The provider is intentionally small and read-only.  It captures the closing
quote timestamp exposed by Eastmoney, validates that timestamp against the
requested completed session, and publishes one immutable local revision only
after the breadth and sector responses are complete.
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
from statistics import median
from threading import Lock, RLock
import time
from typing import Any, Iterator, Mapping, Sequence
import urllib.parse
import urllib.request
from uuid import uuid4

from ..config import AppConfig
from ..json_utils import load_unique_json, loads_unique_json
from .eastmoney import (
    REQUEST_HEADERS,
    _open_request,
    _proxy_mode,
    _should_retry_eastmoney,
)


SCHEMA_VERSION = 1
DATASET = "sector_breadth"
SECTOR_ENDPOINT = "https://push2.eastmoney.com/api/qt/clist/get"
BREADTH_ENDPOINT = "https://push2.eastmoney.com/api/qt/ulist.np/get"
SECTOR_FILTER = "m:90+t:2"
SECTOR_PAGE_SIZE = 100
MAX_SECTOR_PAGES = 20
MAX_SECTORS = 2_000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_RESPONSE_BYTES = 24 * 1024 * 1024
MAX_REVISION_BYTES = 16 * 1024 * 1024
MAX_REVISIONS_PER_DATE = 1_000
DEFAULT_QUERY_LIMIT = 200
MAX_QUERY_LIMIT = 500
CHINA_TIMEZONE = timezone(timedelta(hours=8))

SECTOR_COLUMNS = (
    "f12",
    "f13",
    "f14",
    "f2",
    "f3",
    "f4",
    "f8",
    "f10",
    "f20",
    "f104",
    "f105",
    "f106",
    "f124",
)
BREADTH_COLUMNS = (
    "f12",
    "f13",
    "f14",
    "f2",
    "f3",
    "f104",
    "f105",
    "f106",
    "f124",
)
BREADTH_SECIDS = ("1.000001", "0.399001", "0.899050")
BREADTH_IDENTITIES = {
    ("000001", 1): ("SH", "上证指数"),
    ("399001", 0): ("SZ", "深证成指"),
    ("899050", 0): ("BJ", "北证50"),
}
EXCHANGE_ORDER = ("SH", "SZ", "BJ")
_SOURCE_BASE = {
    "provider": "eastmoney",
    "sector_endpoint": SECTOR_ENDPOINT,
    "breadth_endpoint": BREADTH_ENDPOINT,
    "sector_filter": SECTOR_FILTER,
    "breadth_secids": list(BREADTH_SECIDS),
    "certification": "not_exchange_certified",
    "exchange_certified": False,
    "classification": "third_party_closing_market_evidence",
}
_AUTHORITY = {"research_only": True, "execution_authorized": False}
_NOT_CERTIFIED_WARNING = {
    "code": "not_exchange_certified",
    "message": (
        "Eastmoney is a third-party research source and is not an "
        "exchange-certified market breadth feed."
    ),
    "recovery_action": "cross_check_exchange_disclosure",
}
_BREADTH_SCOPE_WARNING = {
    "code": "provider_breadth_scope",
    "message": (
        "Advance/decline counts use the three benchmark responses exposed by "
        "Eastmoney; they are a provider-defined closing breadth scope."
    ),
    "recovery_action": "review_provider_scope",
}

_DATE_DIRECTORY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_REVISION_FILE = re.compile(r"revision_(\d{8})\.json\Z")
_REVISION_ID = re.compile(r"sector_breadth_[0-9a-f]{32}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_SECTOR_CODE = re.compile(r"BK[0-9]{4}\Z")
_EXCHANGE = re.compile(r"(SH|SZ|BJ)\Z")
_ISO_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

_SECTOR_FIELDS = frozenset(
    {
        "code",
        "name",
        "market",
        "close",
        "change_pct",
        "change_amount",
        "turnover_rate",
        "volume_ratio",
        "market_cap",
        "advancers",
        "decliners",
        "unchanged",
        "constituent_count",
        "advance_share",
        "net_advances",
        "quote_timestamp",
        "quote_date",
    }
)
_BREADTH_FIELDS = frozenset(
    {
        "exchange",
        "benchmark_code",
        "benchmark_name",
        "close",
        "change_pct",
        "advancers",
        "decliners",
        "unchanged",
        "total_count",
        "advance_share",
        "net_advances",
        "quote_timestamp",
        "quote_date",
    }
)
_SOURCE_FIELDS = frozenset(
    {
        *(_SOURCE_BASE.keys()),
        "sector_response_sha256",
        "breadth_response_sha256",
        "response_sha256",
    }
)
_COVERAGE_FIELDS = frozenset(
    {
        "sector_page_size",
        "sector_pages",
        "sector_declared_count",
        "sector_received_count",
        "sector_complete",
        "breadth_declared_count",
        "breadth_received_count",
        "breadth_complete",
        "response_bytes",
        "data_quality",
    }
)
_SUMMARY_FIELDS = frozenset(
    {
        "sector_count",
        "positive_sector_count",
        "negative_sector_count",
        "flat_sector_count",
        "median_sector_change_pct",
        "best_sector_code",
        "best_sector_name",
        "best_sector_change_pct",
        "worst_sector_code",
        "worst_sector_name",
        "worst_sector_change_pct",
        "exchange_count",
        "advancers",
        "decliners",
        "unchanged",
        "breadth_total_count",
        "advance_share",
        "advance_decline_ratio",
        "net_advances",
    }
)
_REVISION_SUMMARY_FIELDS = frozenset(
    {
        "revision_id",
        "revision",
        "trade_date",
        "retrieved_at",
        "status",
        "sector_count",
        "breadth_total_count",
        "evidence_fingerprint",
        "record_fingerprint",
        "supersedes",
    }
)
_STORED_FIELDS = frozenset(
    {
        "schema_version",
        "dataset",
        "available",
        "status",
        "trade_date",
        "retrieved_at",
        "source",
        "coverage",
        "summary",
        "breadth",
        "sectors",
        "revisions",
        "authority",
        "errors",
        "warnings",
        "reused",
        "revision_id",
        "revision",
        "evidence_fingerprint",
        "supersedes",
        "supersedes_fingerprint",
        "record_fingerprint",
    }
)

_LOCKS_GUARD = Lock()
_LOCKS: dict[str, RLock] = {}


@dataclass(frozen=True)
class MarketBreadthQuery:
    """Bounded read filters for one local sector/breadth snapshot."""

    trade_date: date | None = None
    q: str | None = None
    sort: str = "change_pct"
    direction: str = "desc"
    limit: int = DEFAULT_QUERY_LIMIT
    include_revisions: bool = False


class MarketBreadthStore:
    """Immutable date/revision store with network-free reads."""

    def __init__(self, config_or_root: AppConfig | str | Path):
        raw_root = (
            config_or_root.market_intelligence_dir
            if isinstance(config_or_root, AppConfig)
            else Path(config_or_root)
        )
        if raw_root.is_symlink():
            raise RuntimeError("Market-breadth root must not be symbolic")
        self.root = raw_root.resolve()
        self.dataset_root = self.root / DATASET

    def list(
        self,
        query: MarketBreadthQuery | None = None,
        completed_session_cutoff: date | None = None,
    ) -> dict[str, Any]:
        selected = query or MarketBreadthQuery()
        _validate_query(selected)
        cutoff = _optional_date(completed_session_cutoff, "completed_session_cutoff")
        with self._store_lock():
            periods = self._periods_unlocked()
            target = selected.trade_date
            if target is None:
                candidates = periods
                if cutoff is not None:
                    candidates = [item for item in candidates if item <= cutoff]
                target = max(candidates) if candidates else None
            if target is None or target not in periods:
                return _unavailable(
                    selected.trade_date or cutoff,
                    query=selected,
                    cutoff=cutoff,
                    code="market_breadth_not_refreshed",
                    message="No validated local sector/breadth snapshot is available.",
                    recovery_action="refresh_market_breadth",
                )
            chain = self._load_chain_unlocked(target)

        latest = _clone_json(chain[-1])
        filtered = _filter_sectors(latest["sectors"], selected)
        visible = filtered[: selected.limit]
        latest["sectors"] = visible
        latest["summary"] = {
            **latest["summary"],
            "matched_sector_count": len(filtered),
            "returned_sector_count": len(visible),
            "sectors_truncated": len(filtered) > len(visible),
        }
        latest["filters"] = _query_payload(selected)
        latest["revisions"] = [
            _revision_summary(item)
            for item in (chain if selected.include_revisions else chain[-1:])
        ]
        latest["reused"] = False
        latest["freshness"] = _freshness(target, cutoff)
        _apply_freshness(latest, target, cutoff)
        return latest

    def _publish(
        self,
        *,
        trade_date: date,
        retrieved_at: str,
        source: Mapping[str, Any],
        coverage: Mapping[str, Any],
        summary: Mapping[str, Any],
        breadth: Sequence[Mapping[str, Any]],
        sectors: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        normalized_breadth = [_clone_json(item) for item in breadth]
        normalized_sectors = [_clone_json(item) for item in sectors]
        evidence_fingerprint = _fingerprint(
            {
                "schema_version": SCHEMA_VERSION,
                "dataset": DATASET,
                "trade_date": trade_date.isoformat(),
                "breadth": normalized_breadth,
                "sectors": normalized_sectors,
            }
        )
        with self._store_lock():
            periods = self._periods_unlocked()
            chain = (
                self._load_chain_unlocked(trade_date)
                if trade_date in periods
                else []
            )
            if chain and chain[-1]["evidence_fingerprint"] == evidence_fingerprint:
                result = _clone_json(chain[-1])
                result["reused"] = True
                result["filters"] = _query_payload(
                    MarketBreadthQuery(trade_date=trade_date)
                )
                result["revisions"] = [_revision_summary(item) for item in chain]
                result["freshness"] = _freshness(trade_date, None)
                return result
            if len(chain) >= MAX_REVISIONS_PER_DATE:
                raise RuntimeError(
                    f"Market-breadth revision capacity reached for {trade_date.isoformat()}"
                )
            previous = chain[-1] if chain else None
            revision = len(chain) + 1
            revision_id = f"sector_breadth_{uuid4().hex}"
            record: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "dataset": DATASET,
                "available": True,
                "status": "current",
                "trade_date": trade_date.isoformat(),
                "retrieved_at": retrieved_at,
                "source": _clone_json(source),
                "coverage": _clone_json(coverage),
                "summary": _clone_json(summary),
                "breadth": normalized_breadth,
                "sectors": normalized_sectors,
                "revisions": [],
                "authority": dict(_AUTHORITY),
                "errors": [],
                "warnings": _warnings(coverage),
                "reused": False,
                "revision_id": revision_id,
                "revision": revision,
                "evidence_fingerprint": evidence_fingerprint,
                "supersedes": previous["revision_id"] if previous else None,
                "supersedes_fingerprint": (
                    previous["record_fingerprint"] if previous else None
                ),
                "record_fingerprint": None,
            }
            record["revisions"] = [
                *[_revision_summary(item) for item in chain],
                _revision_summary(record, current_placeholder=True),
            ]
            record["record_fingerprint"] = _record_fingerprint(record)
            record["revisions"][-1]["record_fingerprint"] = record[
                "record_fingerprint"
            ]
            _validate_revision(record, trade_date, revision)
            self._atomic_create_unlocked(record)
            committed = self._load_chain_unlocked(trade_date)
            result = _clone_json(committed[-1])
            result["filters"] = _query_payload(
                MarketBreadthQuery(trade_date=trade_date)
            )
            result["revisions"] = [_revision_summary(item) for item in committed]
            result["freshness"] = _freshness(trade_date, None)
            return result

    def _periods_unlocked(self) -> list[date]:
        _assert_directory(self.root, "Market-breadth root", missing_ok=True)
        _assert_directory(self.dataset_root, "Market-breadth dataset", missing_ok=True)
        if not self.dataset_root.exists():
            return []
        periods: list[date] = []
        for path in self.dataset_root.iterdir():
            if path.is_symlink() or not path.is_dir():
                raise RuntimeError("Market-breadth dataset contains an invalid entry")
            if _DATE_DIRECTORY.fullmatch(path.name) is None:
                raise RuntimeError(f"Unexpected market-breadth period: {path.name}")
            try:
                period = date.fromisoformat(path.name)
            except ValueError as exc:
                raise RuntimeError("Market-breadth period is invalid") from exc
            if period.isoformat() != path.name:
                raise RuntimeError("Market-breadth period is not canonical")
            periods.append(period)
            if len(periods) > MAX_SECTORS:
                raise RuntimeError("Market-breadth store contains too many dates")
        if len(periods) != len(set(periods)):
            raise RuntimeError("Market-breadth store contains duplicate dates")
        return sorted(periods)

    def _load_chain_unlocked(self, trade_date: date) -> list[dict[str, Any]]:
        directory = self.dataset_root / trade_date.isoformat()
        _assert_directory(directory, "Market-breadth period")
        paths: list[tuple[int, Path]] = []
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("Market-breadth revision must be a regular file")
            match = _REVISION_FILE.fullmatch(path.name)
            if match is None:
                raise RuntimeError(f"Unexpected market-breadth revision: {path.name}")
            paths.append((int(match.group(1)), path))
        paths.sort()
        if not paths:
            raise RuntimeError("Market-breadth period has no revisions")
        if len(paths) > MAX_REVISIONS_PER_DATE:
            raise RuntimeError("Market-breadth period contains too many revisions")
        if [item[0] for item in paths] != list(range(1, len(paths) + 1)):
            raise RuntimeError("Market-breadth revision sequence is not contiguous")
        chain: list[dict[str, Any]] = []
        previous: dict[str, Any] | None = None
        for revision, path in paths:
            item = _read_revision(path, trade_date, revision)
            if previous is None:
                if item["supersedes"] is not None or item["supersedes_fingerprint"] is not None:
                    raise RuntimeError("First market-breadth revision has a parent")
            elif (
                item["supersedes"] != previous["revision_id"]
                or item["supersedes_fingerprint"] != previous["record_fingerprint"]
            ):
                raise RuntimeError("Market-breadth supersedes chain is invalid")
            expected_history = [
                *[_revision_summary(parent) for parent in chain],
                _revision_summary(item),
            ]
            if item["revisions"] != expected_history:
                raise RuntimeError("Market-breadth embedded revision history is invalid")
            chain.append(item)
            previous = item
        return chain

    def _atomic_create_unlocked(self, record: Mapping[str, Any]) -> None:
        trade_date = date.fromisoformat(str(record["trade_date"]))
        revision = int(record["revision"])
        final_directory = self.dataset_root / trade_date.isoformat()
        final_path = final_directory / f"revision_{revision:08d}.json"
        staging_root = self.root / ".staging"
        _assert_directory(staging_root, "Market-breadth staging", missing_ok=True)
        staging_root.mkdir(parents=True, exist_ok=True)
        _assert_directory(staging_root, "Market-breadth staging")
        stage_directory = staging_root / f"sector-breadth-{uuid4().hex}"
        stage_directory.mkdir(mode=0o700)
        temporary = stage_directory / final_path.name
        published = False
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(record, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if temporary.stat().st_size > MAX_REVISION_BYTES:
                raise ValueError("Market-breadth revision exceeds the supported size")
            _read_revision(temporary, trade_date, revision)
            self.dataset_root.mkdir(parents=True, exist_ok=True)
            final_directory.mkdir(exist_ok=True)
            if final_path.exists() or final_path.is_symlink():
                raise FileExistsError(f"Immutable market-breadth revision already exists: {final_path.name}")
            try:
                if os.name == "nt":
                    os.rename(temporary, final_path)
                else:
                    os.link(temporary, final_path)
                published = True
                _read_revision(final_path, trade_date, revision)
                _fsync_directory(final_directory)
            except Exception:
                if published:
                    final_path.unlink(missing_ok=True)
                raise
        finally:
            temporary.unlink(missing_ok=True)
            try:
                stage_directory.rmdir()
            except OSError:
                pass
            if final_directory.exists():
                try:
                    final_directory.rmdir()
                except OSError:
                    pass

    @contextmanager
    def _store_lock(self) -> Iterator[None]:
        if self.root.is_symlink():
            raise RuntimeError("Market-breadth root must not be symbolic")
        key = os.path.normcase(str(self.root))
        with _LOCKS_GUARD:
            thread_lock = _LOCKS.setdefault(key, RLock())
        with thread_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            _assert_directory(self.root, "Market-breadth root")
            with _file_lock(self.root / ".sector-breadth.lock"):
                yield


def refresh_market_breadth(config: AppConfig, trade_date: date) -> dict[str, Any]:
    """Fetch and persist one completed-date sector/breadth snapshot."""

    if not isinstance(config, AppConfig):
        raise TypeError("config must be an AppConfig")
    requested = _required_date(trade_date, "trade_date")
    retrieved_at = _now()
    query = MarketBreadthQuery(trade_date=requested)
    try:
        source, coverage, summary, breadth, sectors = _download_market_breadth(
            config, requested
        )
        return MarketBreadthStore(config)._publish(
            trade_date=requested,
            retrieved_at=retrieved_at,
            source=source,
            coverage=coverage,
            summary=summary,
            breadth=breadth,
            sectors=sectors,
        )
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        return _unavailable(
            requested,
            query=query,
            cutoff=None,
            code="market_breadth_refresh_failed",
            message=f"{type(exc).__name__}: {exc}"[:1_000],
            recovery_action="retry_refresh",
            retrieved_at=retrieved_at,
        )


def _download_market_breadth(
    config: AppConfig, trade_date: date
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    timeout = int(config.raw["data"].get("timeout_seconds", 20))
    proxy_mode = _proxy_mode(config)
    max_attempts = int(
        config.raw["data"].get(
            "eastmoney_max_attempts", config.raw["data"].get("max_attempts", 4)
        )
    )
    retry_base = float(config.raw["data"].get("retry_base_seconds", 1.0))
    retry_max = float(config.raw["data"].get("retry_max_seconds", 8.0))
    total_bytes = 0
    sector_pages: list[tuple[int, str]] = []
    first_payload, first_bytes, first_hash = _request_json(
        SECTOR_ENDPOINT,
        {
            "pn": "1",
            "pz": str(SECTOR_PAGE_SIZE),
            "po": "1",
            "np": "1",
            "fltt": "2",
            "invt": "2",
            "fid": "f3",
            "fs": SECTOR_FILTER,
            "fields": ",".join(SECTOR_COLUMNS),
        },
        timeout=timeout,
        proxy_mode=proxy_mode,
        max_attempts=max_attempts,
        retry_base=retry_base,
        retry_max=retry_max,
    )
    total_bytes += first_bytes
    sector_pages.append((1, first_hash))
    first = _parse_sector_page(first_payload, 1)
    sector_total = first["total"]
    sector_page_count = math.ceil(sector_total / SECTOR_PAGE_SIZE)
    raw_sectors = list(first["rows"])
    for page_number in range(2, sector_page_count + 1):
        payload, response_bytes, response_hash = _request_json(
            SECTOR_ENDPOINT,
            {
                "pn": str(page_number),
                "pz": str(SECTOR_PAGE_SIZE),
                "po": "1",
                "np": "1",
                "fltt": "2",
                "invt": "2",
                "fid": "f3",
                "fs": SECTOR_FILTER,
                "fields": ",".join(SECTOR_COLUMNS),
            },
            timeout=timeout,
            proxy_mode=proxy_mode,
            max_attempts=max_attempts,
            retry_base=retry_base,
            retry_max=retry_max,
        )
        total_bytes += response_bytes
        sector_pages.append((page_number, response_hash))
        page = _parse_sector_page(payload, page_number)
        if page["total"] != sector_total:
            raise ValueError("Eastmoney sector count changed during pagination")
        raw_sectors.extend(page["rows"])
    if len(raw_sectors) != sector_total:
        raise ValueError("Eastmoney sector count does not match complete pages")
    sectors = [_normalize_sector(item, trade_date) for item in raw_sectors]
    sectors.sort(key=lambda item: item["code"])
    if len({item["code"] for item in sectors}) != len(sectors):
        raise ValueError("Eastmoney sector response contains duplicate codes")

    breadth_payload, breadth_bytes, breadth_hash = _request_json(
        BREADTH_ENDPOINT,
        {
            "fltt": "2",
            "invt": "2",
            "fields": ",".join(BREADTH_COLUMNS),
            "secids": ",".join(BREADTH_SECIDS),
        },
        timeout=timeout,
        proxy_mode=proxy_mode,
        max_attempts=max_attempts,
        retry_base=retry_base,
        retry_max=retry_max,
    )
    total_bytes += breadth_bytes
    breadth = _parse_breadth(breadth_payload, trade_date)
    breadth.sort(key=lambda item: EXCHANGE_ORDER.index(item["exchange"]))
    if total_bytes > MAX_TOTAL_RESPONSE_BYTES:
        raise ValueError("Eastmoney market-breadth responses exceed the size bound")
    source = {
        **_SOURCE_BASE,
        "sector_response_sha256": _ordered_response_sha256(sector_pages),
        "breadth_response_sha256": _valid_fingerprint(breadth_hash, "breadth response sha256"),
        "response_sha256": _fingerprint(
            {
                "sector": _ordered_response_sha256(sector_pages),
                "breadth": breadth_hash,
            }
        ),
    }
    coverage = {
        "sector_page_size": SECTOR_PAGE_SIZE,
        "sector_pages": sector_page_count,
        "sector_declared_count": sector_total,
        "sector_received_count": len(sectors),
        "sector_complete": True,
        "breadth_declared_count": len(BREADTH_SECIDS),
        "breadth_received_count": len(breadth),
        "breadth_complete": True,
        "response_bytes": total_bytes,
        "data_quality": _data_quality(sectors, breadth),
    }
    summary = _summary(sectors, breadth)
    _validate_source(source)
    _validate_coverage(coverage, sectors, breadth)
    _validate_summary(summary, sectors, breadth)
    return source, coverage, summary, breadth, sectors


def _request_json(
    endpoint: str,
    params: Mapping[str, str],
    *,
    timeout: int,
    proxy_mode: str,
    max_attempts: int,
    retry_base: float,
    retry_max: float,
) -> tuple[Any, int, str]:
    request = urllib.request.Request(
        f"{endpoint}?{urllib.parse.urlencode(params)}", headers=REQUEST_HEADERS
    )
    last_error: Exception | None = None
    for attempt in range(max(1, max_attempts)):
        try:
            with _open_request(request, timeout, proxy_mode) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("Eastmoney market-breadth response exceeds size bound")
            return loads_unique_json(raw.decode("utf-8")), len(raw), sha256(raw).hexdigest()
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            last_error = exc
            if not _should_retry_eastmoney(exc) or attempt + 1 >= max_attempts:
                raise
            time.sleep(min(retry_max, retry_base * (2**attempt)))
    raise RuntimeError(f"Eastmoney market-breadth request failed: {last_error}")


def _parse_sector_page(payload: Any, page_number: int) -> dict[str, Any]:
    data = _parse_quote_envelope(payload, "sector")
    total = _strict_int(data.get("total"), "sector total", minimum=1)
    if total > MAX_SECTORS:
        raise ValueError("Eastmoney sector count exceeds supported bound")
    rows = data.get("diff")
    if not isinstance(rows, list):
        raise ValueError("Eastmoney sector diff must be an array")
    expected = (
        SECTOR_PAGE_SIZE
        if page_number < math.ceil(total / SECTOR_PAGE_SIZE)
        else total - SECTOR_PAGE_SIZE * (math.ceil(total / SECTOR_PAGE_SIZE) - 1)
    )
    if page_number > math.ceil(total / SECTOR_PAGE_SIZE) or len(rows) != expected:
        raise ValueError("Eastmoney sector page row count is inconsistent")
    return {"total": total, "rows": rows}


def _parse_breadth(payload: Any, trade_date: date) -> list[dict[str, Any]]:
    data = _parse_quote_envelope(payload, "breadth")
    total = _strict_int(data.get("total"), "breadth total", minimum=1)
    if total != len(BREADTH_SECIDS):
        raise ValueError("Eastmoney breadth total does not match requested benchmarks")
    rows = data.get("diff")
    if not isinstance(rows, list) or len(rows) != total:
        raise ValueError("Eastmoney breadth response is incomplete")
    result = [_normalize_breadth(row, trade_date) for row in rows]
    if {item["exchange"] for item in result} != set(EXCHANGE_ORDER):
        raise ValueError("Eastmoney breadth response is missing an exchange")
    return result


def _parse_quote_envelope(payload: Any, label: str) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("rc") != 0:
        raise ValueError(f"Eastmoney {label} response was unsuccessful")
    data = payload.get("data")
    if not isinstance(data, dict) or set(data) != {"total", "diff"}:
        raise ValueError(f"Eastmoney {label} response envelope is invalid")
    return data


def _normalize_sector(value: Any, trade_date: date) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(SECTOR_COLUMNS):
        raise ValueError("Eastmoney sector row schema is invalid")
    code = _bounded_pattern(value["f12"], "sector code", _SECTOR_CODE)
    market_code = _strict_int(value["f13"], "sector market code", minimum=0)
    if market_code != 90:
        raise ValueError("Eastmoney sector market code is invalid")
    name = _bounded_text(value["f14"], "sector name", 80)
    timestamp, quote_date = _quote_timestamp(value["f124"], trade_date)
    close = _finite_number(value["f2"], "sector close", minimum=0, strict=True)
    change_pct = _finite_number(value["f3"], "sector change_pct")
    change_amount = _optional_number(value["f4"], "sector change_amount")
    turnover = _optional_number(value["f8"], "sector turnover_rate", minimum=0)
    volume_ratio = _optional_number(value["f10"], "sector volume_ratio", minimum=0)
    market_cap = _optional_number(value["f20"], "sector market_cap", minimum=0)
    advancers = _count(value["f104"], "sector advancers")
    decliners = _count(value["f105"], "sector decliners")
    unchanged = _count(value["f106"], "sector unchanged")
    constituent_count = advancers + decliners + unchanged
    if constituent_count <= 0:
        raise ValueError("sector constituent count must be positive")
    return {
        "code": code,
        "name": name,
        "market": "BOARD",
        "close": close,
        "change_pct": change_pct,
        "change_amount": change_amount,
        "turnover_rate": turnover,
        "volume_ratio": volume_ratio,
        "market_cap": market_cap,
        "advancers": advancers,
        "decliners": decliners,
        "unchanged": unchanged,
        "constituent_count": constituent_count,
        "advance_share": advancers / constituent_count,
        "net_advances": advancers - decliners,
        "quote_timestamp": timestamp,
        "quote_date": quote_date.isoformat(),
    }


def _normalize_breadth(value: Any, trade_date: date) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(BREADTH_COLUMNS):
        raise ValueError("Eastmoney breadth row schema is invalid")
    code = _bounded_text(value["f12"], "benchmark code", 12)
    market_code = _strict_int(value["f13"], "benchmark market code", minimum=0)
    market_key = (code, market_code)
    identity = BREADTH_IDENTITIES.get(market_key)
    if identity is None:
        raise ValueError("Eastmoney breadth benchmark identity is invalid")
    exchange, expected_name = identity
    name = _bounded_text(value["f14"], "benchmark name", 80)
    if name != expected_name:
        raise ValueError("Eastmoney breadth benchmark name is invalid")
    timestamp, quote_date = _quote_timestamp(value["f124"], trade_date)
    close = _finite_number(value["f2"], "benchmark close", minimum=0, strict=True)
    change_pct = _finite_number(value["f3"], "benchmark change_pct")
    advancers = _count(value["f104"], "benchmark advancers")
    decliners = _count(value["f105"], "benchmark decliners")
    unchanged = _count(value["f106"], "benchmark unchanged")
    total_count = advancers + decliners + unchanged
    if total_count <= 0:
        raise ValueError("benchmark breadth count must be positive")
    return {
        "exchange": exchange,
        "benchmark_code": code,
        "benchmark_name": name,
        "close": close,
        "change_pct": change_pct,
        "advancers": advancers,
        "decliners": decliners,
        "unchanged": unchanged,
        "total_count": total_count,
        "advance_share": advancers / total_count,
        "net_advances": advancers - decliners,
        "quote_timestamp": timestamp,
        "quote_date": quote_date.isoformat(),
    }


def _summary(
    sectors: Sequence[Mapping[str, Any]], breadth: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    changes = [float(item["change_pct"]) for item in sectors]
    best = max(sectors, key=lambda item: (float(item["change_pct"]), item["code"]))
    worst = min(sectors, key=lambda item: (float(item["change_pct"]), item["code"]))
    advancers = sum(int(item["advancers"]) for item in breadth)
    decliners = sum(int(item["decliners"]) for item in breadth)
    unchanged = sum(int(item["unchanged"]) for item in breadth)
    total = advancers + decliners + unchanged
    ratio = advancers / decliners if decliners else None
    return {
        "sector_count": len(sectors),
        "positive_sector_count": sum(value > 0 for value in changes),
        "negative_sector_count": sum(value < 0 for value in changes),
        "flat_sector_count": sum(value == 0 for value in changes),
        "median_sector_change_pct": float(median(changes)) if changes else None,
        "best_sector_code": best["code"] if sectors else None,
        "best_sector_name": best["name"] if sectors else None,
        "best_sector_change_pct": best["change_pct"] if sectors else None,
        "worst_sector_code": worst["code"] if sectors else None,
        "worst_sector_name": worst["name"] if sectors else None,
        "worst_sector_change_pct": worst["change_pct"] if sectors else None,
        "exchange_count": len(breadth),
        "advancers": advancers,
        "decliners": decliners,
        "unchanged": unchanged,
        "breadth_total_count": total,
        "advance_share": advancers / total if total else None,
        "advance_decline_ratio": ratio,
        "net_advances": advancers - decliners,
    }


def _data_quality(
    sectors: Sequence[Mapping[str, Any]], breadth: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    optional_names = ("change_amount", "turnover_rate", "volume_ratio", "market_cap")
    missing = {
        name: sum(item.get(name) is None for item in sectors) for name in optional_names
    }
    return {
        "sector_rows_with_missing_optional_values": sum(
            any(item.get(name) is None for name in optional_names)
            for item in sectors
        ),
        "missing_optional_numeric_values": missing,
        "all_sector_quote_dates_match": all(item["quote_date"] for item in sectors),
        "all_breadth_quote_dates_match": all(item["quote_date"] for item in breadth),
        "breadth_count_relationships_valid": all(
            item["total_count"]
            == item["advancers"] + item["decliners"] + item["unchanged"]
            for item in breadth
        ),
    }


def _warnings(coverage: Mapping[str, Any]) -> list[dict[str, str]]:
    warnings = [
        _clone_json(_NOT_CERTIFIED_WARNING),
        _clone_json(_BREADTH_SCOPE_WARNING),
    ]
    quality = coverage.get("data_quality", {})
    missing = quality.get("missing_optional_numeric_values", {})
    total_missing = sum(int(value) for value in missing.values())
    if total_missing:
        warnings.append(
            {
                "code": "optional_metric_missing",
                "message": f"{total_missing} optional sector metric value(s) were unavailable; source nulls are preserved.",
                "recovery_action": "review_data_quality",
            }
        )
    return warnings


def _filter_sectors(
    sectors: Sequence[Mapping[str, Any]], query: MarketBreadthQuery
) -> list[dict[str, Any]]:
    needle = query.q.casefold() if query.q is not None else None
    selected: list[dict[str, Any]] = []
    for item in sectors:
        if needle is not None and needle not in f"{item['code']} {item['name']}".casefold():
            continue
        selected.append(_clone_json(item))
    if query.sort == "name":
        selected.sort(key=lambda item: (item["name"].casefold(), item["code"]), reverse=query.direction == "desc")
        return selected
    missing: list[dict[str, Any]] = []
    present: list[dict[str, Any]] = []
    for item in selected:
        if item.get(query.sort) is None:
            missing.append(item)
        else:
            present.append(item)
    present.sort(
        key=lambda item: (float(item[query.sort]), item["code"]),
        reverse=query.direction == "desc",
    )
    missing.sort(key=lambda item: item["code"])
    return [*present, *missing]


def _validate_query(query: MarketBreadthQuery) -> None:
    if not isinstance(query, MarketBreadthQuery):
        raise ValueError("MarketBreadthQuery is invalid")
    _optional_date(query.trade_date, "trade_date")
    if query.q is not None:
        _bounded_text(query.q, "q", 100)
    if query.sort not in {
        "change_pct",
        "advance_share",
        "turnover_rate",
        "volume_ratio",
        "market_cap",
        "constituent_count",
        "name",
    }:
        raise ValueError("unsupported market-breadth sort field")
    if query.direction not in {"asc", "desc"}:
        raise ValueError("direction must be asc or desc")
    if isinstance(query.limit, bool) or not isinstance(query.limit, int) or not 1 <= query.limit <= MAX_QUERY_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_QUERY_LIMIT}")
    if not isinstance(query.include_revisions, bool):
        raise ValueError("include_revisions must be a boolean")


def _query_payload(query: MarketBreadthQuery) -> dict[str, Any]:
    return {
        "trade_date": query.trade_date.isoformat() if query.trade_date else None,
        "q": query.q,
        "sort": query.sort,
        "direction": query.direction,
        "limit": query.limit,
        "include_revisions": query.include_revisions,
    }


def _freshness(trade_date: date | None, cutoff: date | None) -> dict[str, Any]:
    if trade_date is None or cutoff is None:
        status, lag = "unknown", None
    else:
        lag = (cutoff - trade_date).days
        status = "current" if lag == 0 else "stale" if lag > 0 else "provisional"
    return {
        "status": status,
        "completed_session_cutoff": cutoff.isoformat() if cutoff else None,
        "lag_calendar_days": lag,
    }


def _apply_freshness(payload: dict[str, Any], trade_date: date, cutoff: date | None) -> None:
    if cutoff is not None and trade_date < cutoff:
        payload["status"] = "stale"
        payload["warnings"].append(
            {
                "code": "market_breadth_stale",
                "message": f"Local market-breadth snapshot is {trade_date.isoformat()}, before completed-session cutoff {cutoff.isoformat()}.",
                "recovery_action": "refresh_market_breadth",
            }
        )
    elif cutoff is not None and trade_date > cutoff:
        payload["status"] = "provisional"
        payload["warnings"].append(
            {
                "code": "after_completed_session_cutoff",
                "message": "Selected market-breadth snapshot is after the completed-session cutoff.",
                "recovery_action": "select_completed_session",
            }
        )


def _unavailable(
    trade_date: date | None,
    *,
    query: MarketBreadthQuery,
    cutoff: date | None,
    code: str,
    message: str,
    recovery_action: str,
    retrieved_at: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": DATASET,
        "available": False,
        "status": "unavailable",
        "trade_date": trade_date.isoformat() if trade_date else None,
        "retrieved_at": retrieved_at,
        "source": {
            **_SOURCE_BASE,
            "sector_response_sha256": None,
            "breadth_response_sha256": None,
            "response_sha256": None,
        },
        "coverage": {
            "sector_page_size": SECTOR_PAGE_SIZE,
            "sector_pages": 0,
            "sector_declared_count": 0,
            "sector_received_count": 0,
            "sector_complete": False,
            "breadth_declared_count": len(BREADTH_SECIDS),
            "breadth_received_count": 0,
            "breadth_complete": False,
            "response_bytes": 0,
            "data_quality": _data_quality([], []),
        },
        "summary": {
            "sector_count": 0,
            "positive_sector_count": 0,
            "negative_sector_count": 0,
            "flat_sector_count": 0,
            "median_sector_change_pct": None,
            "best_sector_code": None,
            "best_sector_name": None,
            "best_sector_change_pct": None,
            "worst_sector_code": None,
            "worst_sector_name": None,
            "worst_sector_change_pct": None,
            "exchange_count": 0,
            "advancers": 0,
            "decliners": 0,
            "unchanged": 0,
            "breadth_total_count": 0,
            "advance_share": None,
            "advance_decline_ratio": None,
            "net_advances": 0,
            "matched_sector_count": 0,
            "returned_sector_count": 0,
            "sectors_truncated": False,
        },
        "breadth": [],
        "sectors": [],
        "revisions": [],
        "authority": dict(_AUTHORITY),
        "errors": [{"code": code, "message": message, "recovery_action": recovery_action}],
        "warnings": [
            _clone_json(_NOT_CERTIFIED_WARNING),
            _clone_json(_BREADTH_SCOPE_WARNING),
        ],
        "reused": False,
        "filters": _query_payload(query),
        "freshness": _freshness(trade_date, cutoff),
    }


def _validate_revision(value: Any, expected_date: date, expected_revision: int) -> None:
    if not isinstance(value, dict) or set(value) != _STORED_FIELDS:
        raise ValueError("market-breadth revision schema is invalid")
    if value["schema_version"] != SCHEMA_VERSION or value["dataset"] != DATASET:
        raise ValueError("market-breadth revision identity is invalid")
    if value["available"] is not True or value["status"] != "current":
        raise ValueError("market-breadth revision status is invalid")
    if value["trade_date"] != expected_date.isoformat():
        raise ValueError("market-breadth revision date is invalid")
    if not isinstance(value["retrieved_at"], str) or not _ISO_UTC.fullmatch(value["retrieved_at"]):
        raise ValueError("market-breadth retrieved_at is invalid")
    if value["revision"] != expected_revision:
        raise ValueError("market-breadth revision number is invalid")
    if not isinstance(value["revision_id"], str) or not _REVISION_ID.fullmatch(value["revision_id"]):
        raise ValueError("market-breadth revision id is invalid")
    _validate_source(value["source"])
    _validate_coverage(value["coverage"], value["sectors"], value["breadth"])
    _validate_normalized_records(value["sectors"], value["breadth"], expected_date)
    _validate_summary(value["summary"], value["sectors"], value["breadth"])
    if value["authority"] != _AUTHORITY or value["errors"] != [] or value["warnings"] != _warnings(value["coverage"]):
        raise ValueError("market-breadth authority or warning contract is invalid")
    _valid_fingerprint(value["evidence_fingerprint"], "evidence_fingerprint")
    _valid_fingerprint(value["record_fingerprint"], "record_fingerprint")
    if expected_revision == 1:
        if value["supersedes"] is not None or value["supersedes_fingerprint"] is not None:
            raise ValueError("first market-breadth revision cannot have a parent")
    else:
        if not isinstance(value["supersedes"], str) or not _REVISION_ID.fullmatch(value["supersedes"]):
            raise ValueError("market-breadth parent revision id is invalid")
        _valid_fingerprint(value["supersedes_fingerprint"], "supersedes_fingerprint")
    revisions = value["revisions"]
    if not isinstance(revisions, list) or len(revisions) != expected_revision:
        raise ValueError("market-breadth revision history is invalid")
    for number, item in enumerate(revisions, start=1):
        _validate_revision_summary(item, expected_date, number)
    latest = revisions[-1]
    if latest["revision_id"] != value["revision_id"] or latest["evidence_fingerprint"] != value["evidence_fingerprint"] or latest["record_fingerprint"] != value["record_fingerprint"]:
        raise ValueError("market-breadth revision history does not match current record")
    if value["evidence_fingerprint"] != _fingerprint({"schema_version": SCHEMA_VERSION, "dataset": DATASET, "trade_date": value["trade_date"], "breadth": value["breadth"], "sectors": value["sectors"]}):
        raise ValueError("market-breadth evidence fingerprint does not match records")
    if value["record_fingerprint"] != _record_fingerprint(value):
        raise ValueError("market-breadth record fingerprint does not match record")


def _validate_normalized_records(sectors: Any, breadth: Any, expected_date: date) -> None:
    if not isinstance(sectors, list) or not sectors or len(sectors) > MAX_SECTORS:
        raise ValueError("market-breadth sectors are invalid")
    if not isinstance(breadth, list) or len(breadth) != len(BREADTH_SECIDS):
        raise ValueError("market-breadth breadth records are invalid")
    sector_codes: set[str] = set()
    for item in sectors:
        if not isinstance(item, dict) or set(item) != _SECTOR_FIELDS:
            raise ValueError("market-breadth sector schema is invalid")
        code = _bounded_pattern(item["code"], "sector code", _SECTOR_CODE)
        if code in sector_codes or item["market"] != "BOARD":
            raise ValueError("market-breadth sector identity is invalid")
        sector_codes.add(code)
        _bounded_text(item["name"], "sector name", 80)
        _finite_number(item["close"], "sector close", minimum=0, strict=True)
        _finite_number(item["change_pct"], "sector change_pct")
        _optional_number(item["change_amount"], "sector change_amount")
        _optional_number(item["turnover_rate"], "sector turnover_rate", minimum=0)
        _optional_number(item["volume_ratio"], "sector volume_ratio", minimum=0)
        _optional_number(item["market_cap"], "sector market_cap", minimum=0)
        adv = _count(item["advancers"], "sector advancers")
        dec = _count(item["decliners"], "sector decliners")
        flat = _count(item["unchanged"], "sector unchanged")
        total = adv + dec + flat
        if item["constituent_count"] != total or item["net_advances"] != adv - dec or not math.isclose(item["advance_share"], adv / total, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("market-breadth sector count relationship is invalid")
        timestamp, quote_date = _quote_timestamp(item["quote_timestamp"], expected_date)
        if item["quote_date"] != quote_date.isoformat() or item["quote_timestamp"] != timestamp:
            raise ValueError("market-breadth sector quote time is invalid")
    if [item["code"] for item in sectors] != sorted(sector_codes):
        raise ValueError("market-breadth sectors are not deterministically ordered")
    expected_exchanges = set(EXCHANGE_ORDER)
    seen_exchanges: set[str] = set()
    for item in breadth:
        if not isinstance(item, dict) or set(item) != _BREADTH_FIELDS:
            raise ValueError("market-breadth exchange schema is invalid")
        exchange = _bounded_pattern(item["exchange"], "exchange", _EXCHANGE)
        if exchange in seen_exchanges or exchange not in expected_exchanges:
            raise ValueError("market-breadth exchange identity is invalid")
        seen_exchanges.add(exchange)
        benchmark_code = _bounded_text(item["benchmark_code"], "benchmark code", 12)
        expected_code = {"SH": "000001", "SZ": "399001", "BJ": "899050"}[exchange]
        if benchmark_code != expected_code:
            raise ValueError("market-breadth benchmark code is invalid")
        benchmark_name = _bounded_text(
            item["benchmark_name"], "benchmark name", 80
        )
        expected_name = {"SH": "上证指数", "SZ": "深证成指", "BJ": "北证50"}[
            exchange
        ]
        if benchmark_name != expected_name:
            raise ValueError("market-breadth benchmark name is invalid")
        _finite_number(item["close"], "benchmark close", minimum=0, strict=True)
        _finite_number(item["change_pct"], "benchmark change_pct")
        adv = _count(item["advancers"], "benchmark advancers")
        dec = _count(item["decliners"], "benchmark decliners")
        flat = _count(item["unchanged"], "benchmark unchanged")
        total = adv + dec + flat
        if item["total_count"] != total or item["net_advances"] != adv - dec or not math.isclose(item["advance_share"], adv / total, rel_tol=1e-12, abs_tol=1e-12):
            raise ValueError("market-breadth exchange count relationship is invalid")
        timestamp, quote_date = _quote_timestamp(item["quote_timestamp"], expected_date)
        if item["quote_date"] != quote_date.isoformat() or item["quote_timestamp"] != timestamp:
            raise ValueError("market-breadth exchange quote time is invalid")
    if seen_exchanges != expected_exchanges:
        raise ValueError("market-breadth exchange coverage is incomplete")
    if [item["exchange"] for item in breadth] != list(EXCHANGE_ORDER):
        raise ValueError("market-breadth exchanges are not deterministically ordered")


def _validate_summary(value: Any, sectors: Sequence[Mapping[str, Any]], breadth: Sequence[Mapping[str, Any]]) -> None:
    if not isinstance(value, dict) or set(value) != _SUMMARY_FIELDS:
        raise ValueError("market-breadth summary schema is invalid")
    expected = _summary(sectors, breadth)
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise ValueError(f"market-breadth summary field {key} is inconsistent")


def _validate_source(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _SOURCE_FIELDS:
        raise ValueError("market-breadth source schema is invalid")
    for key, expected in _SOURCE_BASE.items():
        if value.get(key) != expected:
            raise ValueError(f"market-breadth source {key} is invalid")
    for key in ("sector_response_sha256", "breadth_response_sha256", "response_sha256"):
        _valid_fingerprint(value.get(key), key)


def _validate_coverage(value: Any, sectors: Sequence[Mapping[str, Any]], breadth: Sequence[Mapping[str, Any]]) -> None:
    if not isinstance(value, dict) or set(value) != _COVERAGE_FIELDS:
        raise ValueError("market-breadth coverage schema is invalid")
    if value["sector_page_size"] != SECTOR_PAGE_SIZE or value["sector_declared_count"] != len(sectors) or value["sector_received_count"] != len(sectors) or value["sector_complete"] is not True:
        raise ValueError("market-breadth sector coverage is invalid")
    pages = _strict_int(value["sector_pages"], "sector pages", minimum=1)
    if pages != math.ceil(len(sectors) / SECTOR_PAGE_SIZE) or pages > MAX_SECTOR_PAGES:
        raise ValueError("market-breadth sector page count is invalid")
    if value["breadth_declared_count"] != len(BREADTH_SECIDS) or value["breadth_received_count"] != len(breadth) or value["breadth_complete"] is not True:
        raise ValueError("market-breadth breadth coverage is invalid")
    response_bytes = _strict_int(value["response_bytes"], "response bytes", minimum=1)
    if response_bytes > MAX_TOTAL_RESPONSE_BYTES:
        raise ValueError("market-breadth response bytes exceed bound")
    if value["data_quality"] != _data_quality(sectors, breadth):
        raise ValueError("market-breadth data quality is inconsistent")


def _revision_summary(value: Mapping[str, Any], *, current_placeholder: bool = False) -> dict[str, Any]:
    return {
        "revision_id": value["revision_id"],
        "revision": value["revision"],
        "trade_date": value["trade_date"],
        "retrieved_at": value["retrieved_at"],
        "status": value["status"],
        "sector_count": value["summary"]["sector_count"],
        "breadth_total_count": value["summary"]["breadth_total_count"],
        "evidence_fingerprint": value["evidence_fingerprint"],
        "record_fingerprint": "0" * 64 if current_placeholder else value["record_fingerprint"],
        "supersedes": value["supersedes"],
    }


def _validate_revision_summary(value: Any, expected_date: date, revision: int) -> None:
    if not isinstance(value, dict) or set(value) != _REVISION_SUMMARY_FIELDS:
        raise ValueError("market-breadth revision summary schema is invalid")
    if value["trade_date"] != expected_date.isoformat() or value["revision"] != revision or value["status"] != "current":
        raise ValueError("market-breadth revision summary sequence is invalid")
    if not isinstance(value["revision_id"], str) or not _REVISION_ID.fullmatch(value["revision_id"]):
        raise ValueError("market-breadth revision summary id is invalid")
    if not isinstance(value["retrieved_at"], str) or not _ISO_UTC.fullmatch(value["retrieved_at"]):
        raise ValueError("market-breadth revision summary time is invalid")
    _strict_int(value["sector_count"], "revision sector count", minimum=1)
    _strict_int(value["breadth_total_count"], "revision breadth count", minimum=1)
    _valid_fingerprint(value["evidence_fingerprint"], "revision evidence fingerprint")
    _valid_fingerprint(value["record_fingerprint"], "revision record fingerprint")
    if revision == 1:
        if value["supersedes"] is not None:
            raise ValueError("first market-breadth revision summary has a parent")
    elif not isinstance(value["supersedes"], str) or not _REVISION_ID.fullmatch(value["supersedes"]):
        raise ValueError("market-breadth revision summary parent is invalid")


def _read_revision(path: Path, expected_date: date, expected_revision: int) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Market-breadth revision must be a regular file")
    value = load_unique_json(path, max_bytes=MAX_REVISION_BYTES)
    _validate_revision(value, expected_date, expected_revision)
    return value


def _record_fingerprint(value: Mapping[str, Any]) -> str:
    clone = _clone_json(value)
    clone["record_fingerprint"] = None
    if clone.get("revisions"):
        clone["revisions"][-1]["record_fingerprint"] = None
    return _fingerprint(clone)


def _quote_timestamp(value: Any, expected_date: date) -> tuple[int, date]:
    timestamp = _strict_int(value, "quote_timestamp", minimum=946684800)
    observed = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(CHINA_TIMEZONE)
    if observed.date() != expected_date:
        raise ValueError("quote timestamp date does not match requested trade date")
    return timestamp, observed.date()


def _count(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a nonnegative integer")
    return value


def _finite_number(value: Any, label: str, *, minimum: float | None = None, strict: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite")
    if minimum is not None and (parsed <= minimum if strict else parsed < minimum):
        raise ValueError(f"{label} is outside the supported range")
    return parsed


def _optional_number(value: Any, label: str, *, minimum: float | None = None) -> float | None:
    if value == "-" or value is None:
        return None
    return _finite_number(value, label, minimum=minimum)


def _strict_int(value: Any, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer of at least {minimum}")
    return value


def _bounded_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > maximum or any(ord(character) < 32 for character in value):
        raise ValueError(f"{label} must be bounded non-empty text")
    return value


def _bounded_pattern(value: Any, label: str, pattern: re.Pattern[str]) -> str:
    if not isinstance(value, str) or pattern.fullmatch(value) is None:
        raise ValueError(f"{label} has an invalid format")
    return value


def _valid_fingerprint(value: Any, label: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise ValueError(f"{label} is invalid")
    return value


def _required_date(value: Any, label: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise ValueError(f"{label} must be a calendar date")
    return value


def _optional_date(value: Any, label: str) -> date | None:
    return None if value is None else _required_date(value, label)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _fingerprint(value: Any) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


def _ordered_response_sha256(page_digests: Sequence[tuple[int, str]]) -> str:
    normalized: list[dict[str, Any]] = []
    for expected, (page, digest) in enumerate(page_digests, start=1):
        if page != expected:
            raise ValueError("response page digests are not contiguous")
        normalized.append({"page": page, "sha256": _valid_fingerprint(digest, "response sha256")})
    if not normalized:
        raise ValueError("response page digest list is empty")
    return _fingerprint(normalized)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")


def _clone_json(value: Any) -> Any:
    return json.loads(_canonical_bytes(value).decode("utf-8"))


def _assert_directory(path: Path, label: str, *, missing_ok: bool = False) -> None:
    if path.is_symlink():
        raise RuntimeError(f"{label} must not be symbolic")
    if not path.exists():
        if missing_ok:
            return
        raise RuntimeError(f"{label} does not exist")
    if not path.is_dir():
        raise RuntimeError(f"{label} must be a directory")


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    if path.is_symlink():
        raise RuntimeError("Market-breadth lock must not be symbolic")
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


__all__ = ["MarketBreadthQuery", "MarketBreadthStore", "refresh_market_breadth"]
