"""Auditable, read-only market-intelligence evidence stores.

This module currently owns the Eastmoney daily dragon-tiger list. Network
responses are bounded and fully validated before an immutable local revision
can become visible. Persisted data is third-party research evidence; it is
not an exchange-certified disclosure and never grants execution authority.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import math
import os
from pathlib import Path
import re
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
DATASET = "dragon_tiger"
ENDPOINT = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT_NAME = "RPT_DAILYBILLBOARD_DETAILSNEW"
PAGE_SIZE = 200
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_RESPONSE_BYTES = 32 * 1024 * 1024
MAX_PAGES = 50
MAX_ROWS = 10_000
MAX_REVISION_BYTES = 16 * 1024 * 1024
MAX_REVISIONS_PER_DATE = 1_000
DEFAULT_QUERY_LIMIT = 200
MAX_QUERY_LIMIT = 500

EASTMONEY_COLUMNS = (
    "TRADE_DATE",
    "SECURITY_CODE",
    "SECUCODE",
    "SECURITY_NAME_ABBR",
    "CLOSE_PRICE",
    "CHANGE_RATE",
    "TURNOVERRATE",
    "BILLBOARD_DEAL_AMT",
    "BILLBOARD_BUY_AMT",
    "BILLBOARD_SELL_AMT",
    "BILLBOARD_NET_AMT",
    "DEAL_AMOUNT_RATIO",
    "DEAL_NET_RATIO",
    "EXPLANATION",
    "CHANGE_TYPE",
    "TRADE_ID",
    "TRADE_MARKET",
    "TRADE_MARKET_CODE",
)

_AUTHORITY = {"research_only": True, "execution_authorized": False}
_SOURCE_BASE = {
    "provider": "eastmoney",
    "endpoint": ENDPOINT,
    "report_name": REPORT_NAME,
    "certification": "not_exchange_certified",
    "exchange_certified": False,
    "classification": "third_party_market_activity",
}
_NOT_CERTIFIED_WARNING = {
    "code": "not_exchange_certified",
    "message": (
        "Eastmoney is a third-party research source and is not an "
        "exchange-certified disclosure feed."
    ),
    "recovery_action": "cross_check_exchange_disclosure",
}

_DATE_DIRECTORY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_REVISION_FILE = re.compile(r"revision_(\d{8})\.json\Z")
_REVISION_ID = re.compile(r"dragon_tiger_[0-9a-f]{32}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_SYMBOL = re.compile(r"\d{6}\Z")
_SECUCODE = re.compile(r"(\d{6})\.(SH|SZ|BJ)\Z")
_CODE = re.compile(r"[0-9A-Za-z._-]{1,40}\Z")
_ISO_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

_NORMALIZED_RECORD_FIELDS = frozenset(
    {
        "key",
        "trade_date",
        "trade_id",
        "symbol",
        "security_id",
        "name",
        "market",
        "close",
        "change_pct",
        "turnover_rate",
        "total_amount",
        "buy_amount",
        "sell_amount",
        "net_amount",
        "amount_ratio",
        "net_ratio",
        "reason",
        "change_type",
        "trade_market",
        "trade_market_code",
    }
)
_SOURCE_FIELDS = frozenset(
    {*_SOURCE_BASE, "response_version", "response_sha256"}
)
_COVERAGE_FIELDS = frozenset(
    {
        "page_size",
        "pages",
        "declared_count",
        "received_count",
        "complete",
        "response_bytes",
        "columns",
        "data_quality",
    }
)
_SUMMARY_FIELDS = frozenset(
    {
        "record_count",
        "security_count",
        "positive_net_count",
        "negative_net_count",
        "flat_net_count",
        "buy_amount",
        "sell_amount",
        "net_amount",
    }
)
_REVISION_SUMMARY_FIELDS = frozenset(
    {
        "revision_id",
        "revision",
        "trade_date",
        "retrieved_at",
        "status",
        "record_count",
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
        "records",
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
class DragonTigerQuery:
    """Bounded filters for one locally persisted dragon-tiger snapshot."""

    trade_date: date | None = None
    symbol: str | None = None
    market: str | None = None
    q: str | None = None
    limit: int = DEFAULT_QUERY_LIMIT
    include_revisions: bool = False


class DragonTigerStore:
    """Immutable date/revision store with no network capability on reads."""

    def __init__(self, config_or_root: AppConfig | str | Path):
        raw_root = (
            config_or_root.market_intelligence_dir
            if isinstance(config_or_root, AppConfig)
            else Path(config_or_root)
        )
        if raw_root.is_symlink():
            raise RuntimeError("Market-intelligence root must not be symbolic")
        self.root = raw_root.resolve()
        self.dataset_root = self.root / DATASET

    def list(
        self,
        query: DragonTigerQuery | None = None,
        completed_session_cutoff: date | None = None,
    ) -> dict[str, Any]:
        """Read and filter a validated local snapshot without using network I/O."""

        selected_query = query or DragonTigerQuery()
        _validate_query(selected_query)
        cutoff = _optional_date(
            completed_session_cutoff, "completed_session_cutoff"
        )
        with self._store_lock():
            periods = self._periods_unlocked()
            target = selected_query.trade_date
            if target is None:
                candidates = periods
                if cutoff is not None:
                    candidates = [item for item in candidates if item <= cutoff]
                target = max(candidates) if candidates else None
            if target is None or target not in periods:
                requested = selected_query.trade_date or cutoff
                return _unavailable(
                    requested,
                    query=selected_query,
                    code="dragon_tiger_not_refreshed",
                    message="No validated local dragon-tiger snapshot is available.",
                    recovery_action="refresh_dragon_tiger",
                    cutoff=cutoff,
                )
            chain = self._load_chain_unlocked(target)

        latest = _clone_json(chain[-1])
        filtered = _filter_records(latest["records"], selected_query)
        visible = filtered[: selected_query.limit]
        latest["records"] = visible
        latest["summary"] = {
            **latest["summary"],
            "matched_count": len(filtered),
            "returned_count": len(visible),
            "truncated": len(filtered) > len(visible),
        }
        latest["filters"] = _query_payload(selected_query)
        latest["revisions"] = [
            _revision_summary(item)
            for item in (chain if selected_query.include_revisions else chain[-1:])
        ]
        latest["reused"] = False
        latest["freshness"] = _freshness(target, cutoff)
        if cutoff is not None and target < cutoff:
            latest["status"] = "stale"
            latest["warnings"].append(
                {
                    "code": "dragon_tiger_stale",
                    "message": (
                        f"Latest local snapshot is {target.isoformat()}, before "
                        f"the completed-session cutoff {cutoff.isoformat()}."
                    ),
                    "recovery_action": "refresh_dragon_tiger",
                }
            )
        elif cutoff is not None and target > cutoff:
            latest["status"] = "provisional"
            latest["warnings"].append(
                {
                    "code": "after_completed_session_cutoff",
                    "message": "The selected date is after the completed-session cutoff.",
                    "recovery_action": "select_completed_session",
                }
            )
        return latest

    def _publish(
        self,
        *,
        trade_date: date,
        retrieved_at: str,
        source: Mapping[str, Any],
        coverage: Mapping[str, Any],
        records: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        normalized_records = [_clone_json(item) for item in records]
        evidence_fingerprint = _fingerprint(
            {
                "schema_version": SCHEMA_VERSION,
                "dataset": DATASET,
                "trade_date": trade_date.isoformat(),
                "columns": list(EASTMONEY_COLUMNS),
                "records": normalized_records,
            }
        )
        with self._store_lock():
            periods = self._periods_unlocked()
            chain = (
                self._load_chain_unlocked(trade_date)
                if trade_date in periods
                else []
            )
            if (
                chain
                and chain[-1]["evidence_fingerprint"] == evidence_fingerprint
            ):
                reused = _clone_json(chain[-1])
                reused["reused"] = True
                reused["filters"] = _query_payload(
                    DragonTigerQuery(trade_date=trade_date)
                )
                reused["freshness"] = _freshness(trade_date, None)
                reused["revisions"] = [_revision_summary(item) for item in chain]
                return reused
            if len(chain) >= MAX_REVISIONS_PER_DATE:
                raise RuntimeError(
                    "Dragon-tiger revision capacity reached for "
                    f"{trade_date.isoformat()}"
                )
            previous = chain[-1] if chain else None
            revision = len(chain) + 1
            revision_id = f"dragon_tiger_{uuid4().hex}"
            summary = _summary(normalized_records)
            record: dict[str, Any] = {
                "schema_version": SCHEMA_VERSION,
                "dataset": DATASET,
                "available": True,
                "status": "current" if normalized_records else "empty",
                "trade_date": trade_date.isoformat(),
                "retrieved_at": retrieved_at,
                "source": _clone_json(source),
                "coverage": _clone_json(coverage),
                "summary": summary,
                "records": normalized_records,
                "revisions": [],
                "authority": dict(_AUTHORITY),
                "errors": [],
                "warnings": _evidence_warnings(normalized_records),
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
            _validate_revision(
                record,
                expected_date=trade_date,
                expected_revision=revision,
            )
            self._atomic_create_unlocked(record)
            committed = self._load_chain_unlocked(trade_date)
            result = _clone_json(committed[-1])
            result["filters"] = _query_payload(
                DragonTigerQuery(trade_date=trade_date)
            )
            result["freshness"] = _freshness(trade_date, None)
            result["revisions"] = [_revision_summary(item) for item in committed]
            return result

    def _periods_unlocked(self) -> list[date]:
        _assert_directory(self.root, "Market-intelligence root", missing_ok=True)
        _assert_directory(
            self.dataset_root, "Dragon-tiger dataset", missing_ok=True
        )
        if not self.dataset_root.exists():
            return []
        periods: list[date] = []
        for path in self.dataset_root.iterdir():
            if path.is_symlink():
                raise RuntimeError("Dragon-tiger period must not be symbolic")
            if not path.is_dir() or _DATE_DIRECTORY.fullmatch(path.name) is None:
                raise RuntimeError(
                    f"Unexpected dragon-tiger dataset entry: {path.name}"
                )
            try:
                period = date.fromisoformat(path.name)
            except ValueError as exc:
                raise RuntimeError("Dragon-tiger period directory is invalid") from exc
            if period.isoformat() != path.name:
                raise RuntimeError("Dragon-tiger period directory is not canonical")
            periods.append(period)
            if len(periods) > MAX_ROWS:
                raise RuntimeError("Dragon-tiger store contains too many dates")
        if len(periods) != len(set(periods)):
            raise RuntimeError("Dragon-tiger store contains duplicate dates")
        return sorted(periods)

    def _load_chain_unlocked(self, trade_date: date) -> list[dict[str, Any]]:
        directory = self.dataset_root / trade_date.isoformat()
        _assert_directory(directory, "Dragon-tiger period")
        paths: list[tuple[int, Path]] = []
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("Dragon-tiger revision must be a regular file")
            match = _REVISION_FILE.fullmatch(path.name)
            if match is None:
                raise RuntimeError(f"Unexpected dragon-tiger revision: {path.name}")
            paths.append((int(match.group(1)), path))
        paths.sort()
        if not paths:
            raise RuntimeError("Dragon-tiger period has no revisions")
        if len(paths) > MAX_REVISIONS_PER_DATE:
            raise RuntimeError("Dragon-tiger period contains too many revisions")
        expected = list(range(1, len(paths) + 1))
        if [item[0] for item in paths] != expected:
            raise RuntimeError("Dragon-tiger revision sequence is not contiguous")
        chain: list[dict[str, Any]] = []
        previous: dict[str, Any] | None = None
        for revision, path in paths:
            item = _read_revision(
                path, expected_date=trade_date, expected_revision=revision
            )
            if previous is None:
                if (
                    item["supersedes"] is not None
                    or item["supersedes_fingerprint"] is not None
                ):
                    raise RuntimeError("First dragon-tiger revision has a parent")
            elif (
                item["supersedes"] != previous["revision_id"]
                or item["supersedes_fingerprint"]
                != previous["record_fingerprint"]
            ):
                raise RuntimeError("Dragon-tiger supersedes chain is invalid")
            expected_history = [
                *[_revision_summary(parent) for parent in chain],
                _revision_summary(item),
            ]
            if item["revisions"] != expected_history:
                raise RuntimeError("Dragon-tiger embedded revision history is invalid")
            chain.append(item)
            previous = item
        return chain

    def _atomic_create_unlocked(self, record: Mapping[str, Any]) -> None:
        trade_date = date.fromisoformat(str(record["trade_date"]))
        revision = int(record["revision"])
        final_directory = self.dataset_root / trade_date.isoformat()
        final_path = final_directory / f"revision_{revision:08d}.json"
        staging_root = self.root / ".staging"
        _assert_directory(staging_root, "Market-intelligence staging", missing_ok=True)
        staging_root.mkdir(parents=True, exist_ok=True)
        _assert_directory(staging_root, "Market-intelligence staging")
        stage_directory = staging_root / f"dragon-tiger-{uuid4().hex}"
        stage_directory.mkdir(mode=0o700)
        temporary = stage_directory / final_path.name
        published = False
        try:
            with temporary.open("x", encoding="utf-8", newline="\n") as handle:
                json.dump(
                    record,
                    handle,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                    allow_nan=False,
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            if temporary.stat().st_size > MAX_REVISION_BYTES:
                raise ValueError(
                    f"Dragon-tiger revision exceeds {MAX_REVISION_BYTES} bytes"
                )
            _read_revision(
                temporary,
                expected_date=trade_date,
                expected_revision=revision,
            )
            self.dataset_root.mkdir(parents=True, exist_ok=True)
            _assert_directory(self.dataset_root, "Dragon-tiger dataset")
            final_directory.mkdir(exist_ok=True)
            _assert_directory(final_directory, "Dragon-tiger period")
            if final_path.exists() or final_path.is_symlink():
                raise FileExistsError(
                    f"Immutable dragon-tiger revision already exists: {final_path.name}"
                )
            try:
                if os.name == "nt":
                    os.rename(temporary, final_path)
                else:
                    os.link(temporary, final_path)
                published = True
                _read_revision(
                    final_path,
                    expected_date=trade_date,
                    expected_revision=revision,
                )
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
            raise RuntimeError("Market-intelligence root must not be symbolic")
        key = os.path.normcase(str(self.root))
        with _LOCKS_GUARD:
            thread_lock = _LOCKS.setdefault(key, RLock())
        with thread_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            _assert_directory(self.root, "Market-intelligence root")
            with _file_lock(self.root / ".dragon-tiger.lock"):
                yield


def refresh_dragon_tiger(config: AppConfig, trade_date: date) -> dict[str, Any]:
    """Fetch, validate and atomically persist one Eastmoney trading date."""

    if not isinstance(config, AppConfig):
        raise TypeError("config must be an AppConfig")
    requested_date = _required_date(trade_date, "trade_date")
    retrieved_at = _now()
    query = DragonTigerQuery(trade_date=requested_date)
    try:
        source, coverage, records = _download_dragon_tiger(config, requested_date)
        return DragonTigerStore(config)._publish(
            trade_date=requested_date,
            retrieved_at=retrieved_at,
            source=source,
            coverage=coverage,
            records=records,
        )
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        return _unavailable(
            requested_date,
            query=query,
            code="dragon_tiger_refresh_failed",
            message=f"{type(exc).__name__}: {exc}"[:1_000],
            recovery_action="retry_refresh",
            cutoff=None,
            retrieved_at=retrieved_at,
        )


def _download_dragon_tiger(
    config: AppConfig, trade_date: date
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
    timeout = int(config.raw["data"].get("timeout_seconds", 20))
    proxy_mode = _proxy_mode(config)
    max_attempts = int(
        config.raw["data"].get(
            "eastmoney_max_attempts",
            config.raw["data"].get("max_attempts", 4),
        )
    )
    retry_base = float(config.raw["data"].get("retry_base_seconds", 1.0))
    retry_max = float(config.raw["data"].get("retry_max_seconds", 8.0))
    payload, raw_bytes, raw_sha256 = _request_page(
        trade_date,
        1,
        timeout=timeout,
        proxy_mode=proxy_mode,
        max_attempts=max_attempts,
        retry_base=retry_base,
        retry_max=retry_max,
    )
    envelope = _parse_page(payload, trade_date, page_number=1)
    if envelope["empty"]:
        source = {
            **_SOURCE_BASE,
            "response_version": None,
            "response_sha256": _ordered_response_sha256([(1, raw_sha256)]),
        }
        coverage = {
            "page_size": PAGE_SIZE,
            "pages": 0,
            "declared_count": 0,
            "received_count": 0,
            "complete": True,
            "response_bytes": raw_bytes,
            "columns": list(EASTMONEY_COLUMNS),
            "data_quality": _data_quality([]),
        }
        return source, coverage, []

    pages = envelope["pages"]
    count = envelope["count"]
    version = envelope["version"]
    raw_records = list(envelope["data"])
    total_bytes = raw_bytes
    response_digests = [(1, raw_sha256)]
    for page_number in range(2, pages + 1):
        payload, page_bytes, page_sha256 = _request_page(
            trade_date,
            page_number,
            timeout=timeout,
            proxy_mode=proxy_mode,
            max_attempts=max_attempts,
            retry_base=retry_base,
            retry_max=retry_max,
        )
        total_bytes += page_bytes
        response_digests.append((page_number, page_sha256))
        if total_bytes > MAX_TOTAL_RESPONSE_BYTES:
            raise ValueError(
                f"Eastmoney responses exceed {MAX_TOTAL_RESPONSE_BYTES} bytes"
            )
        page = _parse_page(payload, trade_date, page_number=page_number)
        if page["empty"]:
            raise ValueError("Eastmoney pagination became empty before completion")
        if (
            page["pages"] != pages
            or page["count"] != count
            or page["version"] != version
        ):
            raise ValueError("Eastmoney pagination metadata changed during refresh")
        raw_records.extend(page["data"])
        if len(raw_records) > MAX_ROWS:
            raise ValueError(f"Eastmoney response exceeds {MAX_ROWS} rows")

    if len(raw_records) != count:
        raise ValueError(
            "Eastmoney declared count does not match the complete paginated result"
        )
    records = [_normalize_eastmoney_row(item, trade_date) for item in raw_records]
    keys = [item["key"] for item in records]
    if len(keys) != len(set(keys)):
        raise ValueError("Eastmoney response contains duplicate composite keys")
    records.sort(key=lambda item: (item["trade_id"], item["symbol"], item["change_type"]))
    source = {
        **_SOURCE_BASE,
        "response_version": version,
        "response_sha256": _ordered_response_sha256(response_digests),
    }
    coverage = {
        "page_size": PAGE_SIZE,
        "pages": pages,
        "declared_count": count,
        "received_count": len(records),
        "complete": True,
        "response_bytes": total_bytes,
        "columns": list(EASTMONEY_COLUMNS),
        "data_quality": _data_quality(records),
    }
    _validate_source(source)
    _validate_coverage(coverage, records=records)
    return source, coverage, records


def _request_page(
    trade_date: date,
    page_number: int,
    *,
    timeout: int,
    proxy_mode: str,
    max_attempts: int,
    retry_base: float,
    retry_max: float,
) -> tuple[Any, int, str]:
    params = {
        "reportName": REPORT_NAME,
        "columns": ",".join(EASTMONEY_COLUMNS),
        "pageNumber": str(page_number),
        "pageSize": str(PAGE_SIZE),
        "sortColumns": "TRADE_ID,SECURITY_CODE,CHANGE_TYPE",
        "sortTypes": "1,1,1",
        "filter": f"(TRADE_DATE='{trade_date.isoformat()}')",
        "source": "WEB",
        "client": "WEB",
    }
    request = urllib.request.Request(
        f"{ENDPOINT}?{urllib.parse.urlencode(params)}",
        headers=REQUEST_HEADERS,
    )
    last_error: Exception | None = None
    for attempt in range(max(1, max_attempts)):
        try:
            with _open_request(request, timeout, proxy_mode) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError(
                    f"Eastmoney response exceeds {MAX_RESPONSE_BYTES} bytes"
                )
            return (
                loads_unique_json(raw.decode("utf-8")),
                len(raw),
                sha256(raw).hexdigest(),
            )
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            last_error = exc
            if not _should_retry_eastmoney(exc) or attempt + 1 >= max_attempts:
                raise
            time.sleep(min(retry_max, retry_base * (2**attempt)))
    raise RuntimeError(f"Eastmoney request failed: {last_error}")


def _parse_page(
    payload: Any, requested_date: date, *, page_number: int
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("Eastmoney response envelope must be an object")
    result = payload.get("result")
    if (
        page_number == 1
        and payload.get("success") is False
        and payload.get("code") == 9201
        and result is None
    ):
        return {
            "empty": True,
            "pages": 0,
            "count": 0,
            "version": None,
            "data": [],
        }
    if payload.get("success") is not True or payload.get("code") != 0:
        raise ValueError(
            f"Eastmoney returned an unsuccessful response for {requested_date}"
        )
    if not isinstance(result, dict):
        raise ValueError("Eastmoney result must be an object")
    pages = _strict_int(result.get("pages"), "result.pages", minimum=1)
    count = _strict_int(result.get("count"), "result.count", minimum=1)
    if pages > MAX_PAGES:
        raise ValueError(f"Eastmoney response exceeds {MAX_PAGES} pages")
    if count > MAX_ROWS:
        raise ValueError(f"Eastmoney response exceeds {MAX_ROWS} rows")
    expected_pages = math.ceil(count / PAGE_SIZE)
    if pages != expected_pages:
        raise ValueError("Eastmoney count/pages metadata is inconsistent")
    data = result.get("data")
    if not isinstance(data, list):
        raise ValueError("Eastmoney result.data must be an array")
    expected_rows = (
        PAGE_SIZE if page_number < pages else count - PAGE_SIZE * (pages - 1)
    )
    if page_number > pages or len(data) != expected_rows:
        raise ValueError("Eastmoney page row count is inconsistent")
    version = payload.get("version")
    if not isinstance(version, str) or not 1 <= len(version) <= 128:
        raise ValueError("Eastmoney response version is invalid")
    return {
        "empty": False,
        "pages": pages,
        "count": count,
        "version": version,
        "data": data,
    }


def _normalize_eastmoney_row(value: Any, requested_date: date) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(EASTMONEY_COLUMNS):
        raise ValueError("Eastmoney dragon-tiger row schema is invalid")
    raw_date = value["TRADE_DATE"]
    if not isinstance(raw_date, str):
        raise ValueError("TRADE_DATE must be text")
    try:
        observed = datetime.strptime(raw_date, "%Y-%m-%d %H:%M:%S")
    except ValueError as exc:
        raise ValueError("TRADE_DATE is invalid") from exc
    if observed.time().isoformat() != "00:00:00" or observed.date() != requested_date:
        raise ValueError("Eastmoney row date does not match the requested trade date")
    symbol = _bounded_pattern(value["SECURITY_CODE"], "SECURITY_CODE", _SYMBOL)
    security_id = _bounded_pattern(value["SECUCODE"], "SECUCODE", _SECUCODE)
    secucode_match = _SECUCODE.fullmatch(security_id)
    assert secucode_match is not None
    if secucode_match.group(1) != symbol:
        raise ValueError("SECUCODE does not match SECURITY_CODE")
    market = secucode_match.group(2)
    name = _bounded_text(value["SECURITY_NAME_ABBR"], "SECURITY_NAME_ABBR", 80)
    close = _finite_number(value["CLOSE_PRICE"], "CLOSE_PRICE", minimum=0, strict=True)
    change_pct = _finite_number(value["CHANGE_RATE"], "CHANGE_RATE")
    turnover_rate = _optional_finite_number(
        value["TURNOVERRATE"], "TURNOVERRATE", minimum=0
    )
    total_amount = _finite_number(
        value["BILLBOARD_DEAL_AMT"], "BILLBOARD_DEAL_AMT", minimum=0
    )
    buy_amount = _finite_number(
        value["BILLBOARD_BUY_AMT"], "BILLBOARD_BUY_AMT", minimum=0
    )
    sell_amount = _finite_number(
        value["BILLBOARD_SELL_AMT"], "BILLBOARD_SELL_AMT", minimum=0
    )
    net_amount = _finite_number(value["BILLBOARD_NET_AMT"], "BILLBOARD_NET_AMT")
    amount_ratio = _finite_number(value["DEAL_AMOUNT_RATIO"], "DEAL_AMOUNT_RATIO")
    net_ratio = _finite_number(value["DEAL_NET_RATIO"], "DEAL_NET_RATIO")
    if not _money_close(buy_amount + sell_amount, total_amount):
        raise ValueError("BILLBOARD_DEAL_AMT does not equal buy plus sell amount")
    if not _money_close(buy_amount - sell_amount, net_amount):
        raise ValueError("BILLBOARD_NET_AMT does not equal buy minus sell amount")
    reason = _bounded_text(value["EXPLANATION"], "EXPLANATION", 1_000)
    change_type = _bounded_pattern(value["CHANGE_TYPE"], "CHANGE_TYPE", _CODE)
    trade_id = _strict_int(value["TRADE_ID"], "TRADE_ID", minimum=1)
    trade_market = _bounded_text(value["TRADE_MARKET"], "TRADE_MARKET", 120)
    trade_market_code = _bounded_pattern(
        value["TRADE_MARKET_CODE"], "TRADE_MARKET_CODE", _CODE
    )
    key = f"{trade_id}|{symbol}|{change_type}"
    return {
        "key": key,
        "trade_date": requested_date.isoformat(),
        "trade_id": trade_id,
        "symbol": symbol,
        "security_id": security_id,
        "name": name,
        "market": market,
        "close": close,
        "change_pct": change_pct,
        "turnover_rate": turnover_rate,
        "total_amount": total_amount,
        "buy_amount": buy_amount,
        "sell_amount": sell_amount,
        "net_amount": net_amount,
        "amount_ratio": amount_ratio,
        "net_ratio": net_ratio,
        "reason": reason,
        "change_type": change_type,
        "trade_market": trade_market,
        "trade_market_code": trade_market_code,
    }


def _read_revision(
    path: Path, *, expected_date: date, expected_revision: int
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Dragon-tiger revision must be a regular file")
    try:
        value = load_unique_json(path, max_bytes=MAX_REVISION_BYTES)
        _validate_revision(
            value,
            expected_date=expected_date,
            expected_revision=expected_revision,
        )
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        raise RuntimeError(f"Dragon-tiger revision is invalid: {path.name}: {exc}") from exc
    return value


def _validate_revision(
    value: Any,
    *,
    expected_date: date,
    expected_revision: int,
) -> None:
    if not isinstance(value, dict) or set(value) != _STORED_FIELDS:
        raise ValueError("stored schema is invalid")
    if value["schema_version"] != SCHEMA_VERSION or value["dataset"] != DATASET:
        raise ValueError("dataset identity is invalid")
    if value["available"] is not True or value["status"] not in {"current", "empty"}:
        raise ValueError("stored availability status is invalid")
    if value["trade_date"] != expected_date.isoformat():
        raise ValueError("stored trade date is invalid")
    if not isinstance(value["retrieved_at"], str) or not _ISO_UTC.fullmatch(
        value["retrieved_at"]
    ):
        raise ValueError("retrieved_at is invalid")
    _validate_source(value["source"])
    if not isinstance(value["records"], list) or len(value["records"]) > MAX_ROWS:
        raise ValueError("records are invalid")
    normalized = [
        _validate_normalized_record(item, expected_date) for item in value["records"]
    ]
    if normalized != value["records"]:
        raise ValueError("records are not canonical")
    keys = [item["key"] for item in normalized]
    if len(keys) != len(set(keys)):
        raise ValueError("records contain duplicate composite keys")
    if normalized != sorted(
        normalized,
        key=lambda item: (item["trade_id"], item["symbol"], item["change_type"]),
    ):
        raise ValueError("records are not canonically ordered")
    _validate_coverage(value["coverage"], records=normalized)
    if value["summary"] != _summary(normalized):
        raise ValueError("summary does not match records")
    if value["status"] != ("current" if normalized else "empty"):
        raise ValueError("status does not match records")
    if value["authority"] != _AUTHORITY:
        raise ValueError("authority is invalid")
    if value["errors"] != [] or value["reused"] is not False:
        raise ValueError("stored outcome fields are invalid")
    if value["warnings"] != _evidence_warnings(normalized):
        raise ValueError("stored source warning is invalid")
    revision = _strict_int(value["revision"], "revision", minimum=1)
    if revision != expected_revision or revision > MAX_REVISIONS_PER_DATE:
        raise ValueError("revision sequence is invalid")
    if not isinstance(value["revision_id"], str) or not _REVISION_ID.fullmatch(
        value["revision_id"]
    ):
        raise ValueError("revision_id is invalid")
    _valid_fingerprint(value["evidence_fingerprint"], "evidence_fingerprint")
    expected_evidence = _fingerprint(
        {
            "schema_version": SCHEMA_VERSION,
            "dataset": DATASET,
            "trade_date": expected_date.isoformat(),
            "columns": list(EASTMONEY_COLUMNS),
            "records": normalized,
        }
    )
    if value["evidence_fingerprint"] != expected_evidence:
        raise ValueError("evidence_fingerprint does not match records")
    if revision == 1:
        if value["supersedes"] is not None or value["supersedes_fingerprint"] is not None:
            raise ValueError("first revision cannot supersede another revision")
    else:
        if not isinstance(value["supersedes"], str) or not _REVISION_ID.fullmatch(
            value["supersedes"]
        ):
            raise ValueError("supersedes is invalid")
        _valid_fingerprint(
            value["supersedes_fingerprint"], "supersedes_fingerprint"
        )
    if not isinstance(value["revisions"], list) or len(value["revisions"]) != revision:
        raise ValueError("revision history is invalid")
    for index, item in enumerate(value["revisions"], start=1):
        _validate_revision_summary(item, expected_date, index)
    latest_summary = value["revisions"][-1]
    if (
        latest_summary["revision_id"] != value["revision_id"]
        or latest_summary["evidence_fingerprint"] != value["evidence_fingerprint"]
        or latest_summary["record_count"] != len(normalized)
        or latest_summary["record_fingerprint"] != value["record_fingerprint"]
    ):
        raise ValueError("latest revision summary does not match the record")
    _valid_fingerprint(value["record_fingerprint"], "record_fingerprint")
    if value["record_fingerprint"] != _record_fingerprint(value):
        raise ValueError("record_fingerprint does not match the stored revision")


def _validate_normalized_record(value: Any, expected_date: date) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != _NORMALIZED_RECORD_FIELDS:
        raise ValueError("normalized record schema is invalid")
    symbol = _bounded_pattern(value["symbol"], "symbol", _SYMBOL)
    security_id = _bounded_pattern(value["security_id"], "security_id", _SECUCODE)
    match = _SECUCODE.fullmatch(security_id)
    assert match is not None
    if match.group(1) != symbol or value["market"] != match.group(2):
        raise ValueError("normalized market identity is invalid")
    trade_id = _strict_int(value["trade_id"], "trade_id", minimum=1)
    change_type = _bounded_pattern(value["change_type"], "change_type", _CODE)
    if value["key"] != f"{trade_id}|{symbol}|{change_type}":
        raise ValueError("normalized composite key is invalid")
    if value["trade_date"] != expected_date.isoformat():
        raise ValueError("normalized trade_date is invalid")
    _bounded_text(value["name"], "name", 80)
    _bounded_text(value["reason"], "reason", 1_000)
    _bounded_text(value["trade_market"], "trade_market", 120)
    _bounded_pattern(value["trade_market_code"], "trade_market_code", _CODE)
    close = _finite_number(value["close"], "close", minimum=0, strict=True)
    change_pct = _finite_number(value["change_pct"], "change_pct")
    turnover = _optional_finite_number(
        value["turnover_rate"], "turnover_rate", minimum=0
    )
    total = _finite_number(value["total_amount"], "total_amount", minimum=0)
    buy = _finite_number(value["buy_amount"], "buy_amount", minimum=0)
    sell = _finite_number(value["sell_amount"], "sell_amount", minimum=0)
    net = _finite_number(value["net_amount"], "net_amount")
    amount_ratio = _finite_number(value["amount_ratio"], "amount_ratio")
    net_ratio = _finite_number(value["net_ratio"], "net_ratio")
    if not _money_close(buy + sell, total) or not _money_close(buy - sell, net):
        raise ValueError("normalized amount relationship is invalid")
    return {
        **value,
        "close": close,
        "change_pct": change_pct,
        "turnover_rate": turnover,
        "total_amount": total,
        "buy_amount": buy,
        "sell_amount": sell,
        "net_amount": net,
        "amount_ratio": amount_ratio,
        "net_ratio": net_ratio,
    }


def _validate_source(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _SOURCE_FIELDS:
        raise ValueError("source schema is invalid")
    for key, expected in _SOURCE_BASE.items():
        if value[key] != expected:
            raise ValueError(f"source {key} is invalid")
    version = value["response_version"]
    if version is not None and (
        not isinstance(version, str) or not 1 <= len(version) <= 128
    ):
        raise ValueError("source response_version is invalid")
    _valid_fingerprint(value["response_sha256"], "source response_sha256")


def _validate_coverage(
    value: Any, *, records: Sequence[Mapping[str, Any]]
) -> None:
    if not isinstance(value, dict) or set(value) != _COVERAGE_FIELDS:
        raise ValueError("coverage schema is invalid")
    if value["page_size"] != PAGE_SIZE or value["columns"] != list(EASTMONEY_COLUMNS):
        raise ValueError("coverage request contract is invalid")
    pages = _strict_int(value["pages"], "coverage.pages", minimum=0)
    declared = _strict_int(value["declared_count"], "coverage.declared_count", minimum=0)
    received = _strict_int(value["received_count"], "coverage.received_count", minimum=0)
    response_bytes = _strict_int(value["response_bytes"], "coverage.response_bytes", minimum=1)
    if pages > MAX_PAGES or declared > MAX_ROWS or received > MAX_ROWS:
        raise ValueError("coverage exceeds supported bounds")
    if response_bytes > MAX_TOTAL_RESPONSE_BYTES:
        raise ValueError("coverage response size exceeds supported bounds")
    if value["complete"] is not True or declared != received or received != len(records):
        raise ValueError("coverage does not bind the complete record set")
    if pages != (math.ceil(declared / PAGE_SIZE) if declared else 0):
        raise ValueError("coverage count/pages relationship is invalid")
    if value["data_quality"] != _data_quality(records):
        raise ValueError("coverage data_quality does not match records")


def _summary(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    net_values = [float(item["net_amount"]) for item in records]
    return {
        "record_count": len(records),
        "security_count": len({str(item["symbol"]) for item in records}),
        "positive_net_count": sum(value > 0 for value in net_values),
        "negative_net_count": sum(value < 0 for value in net_values),
        "flat_net_count": sum(value == 0 for value in net_values),
        "buy_amount": math.fsum(float(item["buy_amount"]) for item in records),
        "sell_amount": math.fsum(float(item["sell_amount"]) for item in records),
        "net_amount": math.fsum(net_values),
    }


def _revision_summary(
    value: Mapping[str, Any], *, current_placeholder: bool = False
) -> dict[str, Any]:
    record_fingerprint = value.get("record_fingerprint")
    if current_placeholder:
        record_fingerprint = "0" * 64
    return {
        "revision_id": value["revision_id"],
        "revision": value["revision"],
        "trade_date": value["trade_date"],
        "retrieved_at": value["retrieved_at"],
        "status": value["status"],
        "record_count": value["summary"]["record_count"],
        "evidence_fingerprint": value["evidence_fingerprint"],
        "record_fingerprint": record_fingerprint,
        "supersedes": value["supersedes"],
    }


def _validate_revision_summary(value: Any, trade_date: date, revision: int) -> None:
    if not isinstance(value, dict) or set(value) != _REVISION_SUMMARY_FIELDS:
        raise ValueError("revision summary schema is invalid")
    if value["trade_date"] != trade_date.isoformat() or value["revision"] != revision:
        raise ValueError("revision summary sequence is invalid")
    if not isinstance(value["revision_id"], str) or not _REVISION_ID.fullmatch(
        value["revision_id"]
    ):
        raise ValueError("revision summary id is invalid")
    if not isinstance(value["retrieved_at"], str) or not _ISO_UTC.fullmatch(
        value["retrieved_at"]
    ):
        raise ValueError("revision summary retrieved_at is invalid")
    if value["status"] not in {"current", "empty"}:
        raise ValueError("revision summary status is invalid")
    _strict_int(value["record_count"], "revision record_count", minimum=0)
    _valid_fingerprint(value["evidence_fingerprint"], "revision evidence fingerprint")
    _valid_fingerprint(value["record_fingerprint"], "revision record fingerprint")
    if revision == 1:
        if value["supersedes"] is not None:
            raise ValueError("first revision summary cannot have a parent")
    elif not isinstance(value["supersedes"], str) or not _REVISION_ID.fullmatch(
        value["supersedes"]
    ):
        raise ValueError("revision summary parent is invalid")


def _filter_records(
    records: Sequence[Mapping[str, Any]], query: DragonTigerQuery
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    needle = query.q.casefold() if query.q is not None else None
    for item in records:
        if query.symbol is not None and item["symbol"] != query.symbol:
            continue
        if query.market is not None and item["market"] != query.market:
            continue
        if needle is not None:
            haystack = " ".join(
                str(item[key])
                for key in (
                    "symbol",
                    "security_id",
                    "name",
                    "reason",
                    "trade_market",
                    "change_type",
                )
            ).casefold()
            if needle not in haystack:
                continue
        selected.append(_clone_json(item))
    return selected


def _validate_query(query: DragonTigerQuery) -> None:
    if not isinstance(query, DragonTigerQuery):
        raise ValueError("DragonTigerQuery is invalid")
    _optional_date(query.trade_date, "trade_date")
    if query.symbol is not None:
        _bounded_pattern(query.symbol, "symbol", _SYMBOL)
    if query.market is not None and query.market not in {"SH", "SZ", "BJ"}:
        raise ValueError("market must be SH, SZ, or BJ")
    if query.q is not None:
        _bounded_text(query.q, "q", 100)
    if (
        isinstance(query.limit, bool)
        or not isinstance(query.limit, int)
        or not 1 <= query.limit <= MAX_QUERY_LIMIT
    ):
        raise ValueError(f"limit must be between 1 and {MAX_QUERY_LIMIT}")
    if not isinstance(query.include_revisions, bool):
        raise ValueError("include_revisions must be a boolean")


def _query_payload(query: DragonTigerQuery) -> dict[str, Any]:
    return {
        "trade_date": query.trade_date.isoformat() if query.trade_date else None,
        "symbol": query.symbol,
        "market": query.market,
        "q": query.q,
        "limit": query.limit,
        "include_revisions": query.include_revisions,
    }


def _freshness(trade_date: date | None, cutoff: date | None) -> dict[str, Any]:
    if trade_date is None or cutoff is None:
        status = "unknown"
        lag = None
    else:
        lag = (cutoff - trade_date).days
        status = "current" if lag == 0 else ("stale" if lag > 0 else "provisional")
    return {
        "status": status,
        "completed_session_cutoff": cutoff.isoformat() if cutoff else None,
        "lag_calendar_days": lag,
    }


def _unavailable(
    trade_date: date | None,
    *,
    query: DragonTigerQuery,
    code: str,
    message: str,
    recovery_action: str,
    cutoff: date | None,
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
            "response_version": None,
            "response_sha256": None,
        },
        "coverage": {
            "page_size": PAGE_SIZE,
            "pages": 0,
            "declared_count": 0,
            "received_count": 0,
            "complete": False,
            "response_bytes": 0,
            "columns": list(EASTMONEY_COLUMNS),
            "data_quality": {
                **_data_quality([]),
                "complete_identity_and_amounts": False,
            },
        },
        "summary": {
            **_summary([]),
            "matched_count": 0,
            "returned_count": 0,
            "truncated": False,
        },
        "records": [],
        "revisions": [],
        "authority": dict(_AUTHORITY),
        "errors": [
            {
                "code": code,
                "message": message,
                "recovery_action": recovery_action,
            }
        ],
        "warnings": [_clone_json(_NOT_CERTIFIED_WARNING)],
        "reused": False,
        "filters": _query_payload(query),
        "freshness": _freshness(trade_date, cutoff),
    }


def _record_fingerprint(value: Mapping[str, Any]) -> str:
    clone = _clone_json(value)
    clone["record_fingerprint"] = None
    # The latest summary mirrors this value for presentation. It must not make
    # the digest recursive.
    if clone.get("revisions"):
        clone["revisions"][-1]["record_fingerprint"] = None
    return _fingerprint(clone)


def _data_quality(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    missing_turnover = sum(item.get("turnover_rate") is None for item in records)
    return {
        "missing_optional_numeric_values": {"turnover_rate": missing_turnover},
        "rows_with_missing_optional_values": missing_turnover,
        "complete_identity_and_amounts": True,
    }


def _evidence_warnings(records: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    warnings = [_clone_json(_NOT_CERTIFIED_WARNING)]
    missing_turnover = sum(item.get("turnover_rate") is None for item in records)
    if missing_turnover:
        warnings.append(
            {
                "code": "optional_metric_missing",
                "message": (
                    f"TURNOVERRATE is unavailable for {missing_turnover} row(s); "
                    "the source null is preserved."
                ),
                "recovery_action": "review_data_quality",
            }
        )
    return warnings


def _fingerprint(value: Any) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


def _ordered_response_sha256(
    page_digests: Sequence[tuple[int, str]],
) -> str:
    normalized: list[dict[str, Any]] = []
    for expected_page, (page_number, digest) in enumerate(page_digests, start=1):
        if page_number != expected_page:
            raise ValueError("response page digests are not contiguous")
        normalized.append(
            {
                "page_number": page_number,
                "raw_response_sha256": _valid_fingerprint(
                    digest, "raw response sha256"
                ),
            }
        )
    if not normalized or len(normalized) > MAX_PAGES:
        raise ValueError("response page digest count is invalid")
    return _fingerprint(normalized)


def _canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")


def _clone_json(value: Any) -> Any:
    return json.loads(_canonical_bytes(value).decode("utf-8"))


def _finite_number(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
    strict: bool = False,
) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite")
    if minimum is not None and (parsed <= minimum if strict else parsed < minimum):
        comparison = "greater than" if strict else "at least"
        raise ValueError(f"{label} must be {comparison} {minimum}")
    return parsed


def _optional_finite_number(
    value: Any,
    label: str,
    *,
    minimum: float | None = None,
) -> float | None:
    if value is None:
        return None
    return _finite_number(value, label, minimum=minimum)


def _money_close(left: float, right: float) -> bool:
    return math.isclose(left, right, rel_tol=1e-10, abs_tol=0.02)


def _strict_int(value: Any, label: str, *, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise ValueError(f"{label} must be an integer of at least {minimum}")
    return value


def _bounded_text(value: Any, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
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
    if value is None:
        return None
    return _required_date(value, label)


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


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
        raise RuntimeError("Dragon-tiger lock must not be symbolic")
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


__all__ = ["DragonTigerQuery", "DragonTigerStore", "refresh_dragon_tiger"]
