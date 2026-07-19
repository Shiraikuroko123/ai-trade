"""Auditable daily provider-defined board capital-flow evidence.

The adapter is deliberately read-only. It validates every Eastmoney board
page for one completed quote date and publishes an immutable local revision.
The reported order-size buckets are third-party research evidence, not an
exchange-certified market statistic or an execution signal.
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
    EASTMONEY_UT,
    REQUEST_HEADERS,
    _open_request,
    _proxy_mode,
    _should_retry_eastmoney,
)


SCHEMA_VERSION = 1
DATASET = "capital_flow"
ENDPOINT = "https://push2.eastmoney.com/api/qt/clist/get"
BOARD_FILTER = "m:90+t:2"
PAGE_SIZE = 100
MAX_PAGES = 20
MAX_ROWS = 2_000
MAX_PERIODS = 5_000
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_RESPONSE_BYTES = 24 * 1024 * 1024
MAX_REVISION_BYTES = 16 * 1024 * 1024
MAX_REVISIONS_PER_DATE = 1_000
DEFAULT_QUERY_LIMIT = 200
MAX_QUERY_LIMIT = 500
CHINA_TIMEZONE = timezone(timedelta(hours=8))

FLOW_COLUMNS = (
    "f12",
    "f13",
    "f14",
    "f2",
    "f3",
    "f62",
    "f184",
    "f66",
    "f69",
    "f72",
    "f75",
    "f78",
    "f81",
    "f84",
    "f87",
    "f124",
)
FLOW_METRICS = (
    "main_net_inflow",
    "main_net_inflow_pct",
    "super_large_net_inflow",
    "super_large_net_inflow_pct",
    "large_net_inflow",
    "large_net_inflow_pct",
    "medium_net_inflow",
    "medium_net_inflow_pct",
    "small_net_inflow",
    "small_net_inflow_pct",
)
AMOUNT_METRICS = (
    "main_net_inflow",
    "super_large_net_inflow",
    "large_net_inflow",
    "medium_net_inflow",
    "small_net_inflow",
)
PERCENT_METRICS = (
    "main_net_inflow_pct",
    "super_large_net_inflow_pct",
    "large_net_inflow_pct",
    "medium_net_inflow_pct",
    "small_net_inflow_pct",
)
SORT_FIELDS = frozenset({"name", "change_pct", *FLOW_METRICS})

_SOURCE_BASE = {
    "provider": "eastmoney",
    "endpoint": ENDPOINT,
    "board_filter": BOARD_FILTER,
    "provider_sort_field": "f62",
    "amount_unit": "CNY_yuan",
    "methodology": "provider_reported_net_order_size_flow",
    "certification": "not_exchange_certified",
    "exchange_certified": False,
    "classification": "third_party_provider_defined_board_flow",
}
_AUTHORITY = {"research_only": True, "execution_authorized": False}
_NOT_CERTIFIED_WARNING = {
    "code": "not_exchange_certified",
    "message": (
        "Eastmoney is a third-party research source and is not an "
        "exchange-certified capital-flow feed."
    ),
    "recovery_action": "cross_check_exchange_disclosure",
}
_PROVIDER_SCOPE_WARNING = {
    "code": "provider_flow_scope",
    "message": (
        "Capital-flow rows use Eastmoney's provider-defined m:90+t:2 board "
        "universe; boards may overlap and row sums are not whole-market flow."
    ),
    "recovery_action": "review_provider_scope",
}
_METHODOLOGY_WARNING = {
    "code": "provider_flow_methodology",
    "message": (
        "Order-size buckets and percentages are provider-reported methodology "
        "without an independent cross-source or exchange definition check."
    ),
    "recovery_action": "review_provider_methodology",
}

_DATE_DIRECTORY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_REVISION_FILE = re.compile(r"revision_(\d{8})\.json\Z")
_REVISION_ID = re.compile(r"capital_flow_[0-9a-f]{32}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_BOARD_CODE = re.compile(r"BK[0-9]{4}\Z")
_ISO_UTC = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\Z")

_FLOW_FIELDS = frozenset(
    {
        "code",
        "name",
        "market",
        "close",
        "change_pct",
        *FLOW_METRICS,
        "quote_timestamp",
        "quote_date",
    }
)
_SOURCE_FIELDS = frozenset({*(_SOURCE_BASE.keys()), "response_sha256"})
_COVERAGE_FIELDS = frozenset(
    {
        "page_size",
        "pages",
        "declared_count",
        "received_count",
        "complete",
        "response_bytes",
        "data_quality",
    }
)
_SUMMARY_FIELDS = frozenset(
    {
        "flow_count",
        "main_metric_count",
        "positive_main_count",
        "negative_main_count",
        "flat_main_count",
        "missing_main_count",
        "positive_main_share",
        "median_main_net_inflow",
        "top_inflow_code",
        "top_inflow_name",
        "top_inflow_value",
        "top_outflow_code",
        "top_outflow_name",
        "top_outflow_value",
        "total_main_net_inflow",
        "super_large_net_inflow_sum",
        "large_net_inflow_sum",
        "medium_net_inflow_sum",
        "small_net_inflow_sum",
    }
)
_REVISION_SUMMARY_FIELDS = frozenset(
    {
        "revision_id",
        "revision",
        "trade_date",
        "retrieved_at",
        "status",
        "flow_count",
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
        "flows",
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
class CapitalFlowQuery:
    """Bounded read filters for one local capital-flow snapshot."""

    trade_date: date | None = None
    q: str | None = None
    sort: str = "main_net_inflow"
    direction: str = "desc"
    limit: int = DEFAULT_QUERY_LIMIT
    include_revisions: bool = False


class CapitalFlowStore:
    """Immutable date/revision store with network-free reads."""

    def __init__(self, config_or_root: AppConfig | str | Path):
        raw_root = (
            config_or_root.market_intelligence_dir
            if isinstance(config_or_root, AppConfig)
            else Path(config_or_root)
        )
        if raw_root.is_symlink():
            raise RuntimeError("Capital-flow root must not be symbolic")
        self.root = raw_root.resolve()
        self.dataset_root = self.root / DATASET

    def list(
        self,
        query: CapitalFlowQuery | None = None,
        completed_session_cutoff: date | None = None,
    ) -> dict[str, Any]:
        selected = query or CapitalFlowQuery()
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
                    code="capital_flow_not_refreshed",
                    message="No validated local capital-flow snapshot is available.",
                    recovery_action="refresh_capital_flow",
                )
            chain = self._load_chain_unlocked(target)

        latest = _clone_json(chain[-1])
        filtered = _filter_flows(latest["flows"], selected)
        visible = filtered[: selected.limit]
        latest["flows"] = visible
        latest["summary"] = {
            **latest["summary"],
            "matched_flow_count": len(filtered),
            "returned_flow_count": len(visible),
            "flows_truncated": len(filtered) > len(visible),
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
        flows: Sequence[Mapping[str, Any]],
    ) -> dict[str, Any]:
        normalized_flows = [_clone_json(item) for item in flows]
        evidence_fingerprint = _fingerprint(
            {
                "schema_version": SCHEMA_VERSION,
                "dataset": DATASET,
                "trade_date": trade_date.isoformat(),
                "flows": normalized_flows,
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
                    CapitalFlowQuery(trade_date=trade_date)
                )
                result["revisions"] = [_revision_summary(item) for item in chain]
                result["freshness"] = _freshness(trade_date, None)
                return result
            if len(chain) >= MAX_REVISIONS_PER_DATE:
                raise RuntimeError(
                    f"Capital-flow revision capacity reached for {trade_date.isoformat()}"
                )
            previous = chain[-1] if chain else None
            revision = len(chain) + 1
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
                "flows": normalized_flows,
                "revisions": [],
                "authority": dict(_AUTHORITY),
                "errors": [],
                "warnings": _warnings(coverage),
                "reused": False,
                "revision_id": f"capital_flow_{uuid4().hex}",
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
                CapitalFlowQuery(trade_date=trade_date)
            )
            result["revisions"] = [_revision_summary(item) for item in committed]
            result["freshness"] = _freshness(trade_date, None)
            return result

    def _periods_unlocked(self) -> list[date]:
        _assert_directory(self.root, "Capital-flow root", missing_ok=True)
        _assert_directory(
            self.dataset_root, "Capital-flow dataset", missing_ok=True
        )
        if not self.dataset_root.exists():
            return []
        periods: list[date] = []
        for path in self.dataset_root.iterdir():
            if path.is_symlink() or not path.is_dir():
                raise RuntimeError("Capital-flow dataset contains an invalid entry")
            if _DATE_DIRECTORY.fullmatch(path.name) is None:
                raise RuntimeError(f"Unexpected capital-flow period: {path.name}")
            try:
                period = date.fromisoformat(path.name)
            except ValueError as exc:
                raise RuntimeError("Capital-flow period is invalid") from exc
            if period.isoformat() != path.name:
                raise RuntimeError("Capital-flow period is not canonical")
            periods.append(period)
            if len(periods) > MAX_PERIODS:
                raise RuntimeError("Capital-flow store contains too many dates")
        if len(periods) != len(set(periods)):
            raise RuntimeError("Capital-flow store contains duplicate dates")
        return sorted(periods)

    def _load_chain_unlocked(self, trade_date: date) -> list[dict[str, Any]]:
        directory = self.dataset_root / trade_date.isoformat()
        _assert_directory(directory, "Capital-flow period")
        paths: list[tuple[int, Path]] = []
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("Capital-flow revision must be a regular file")
            match = _REVISION_FILE.fullmatch(path.name)
            if match is None:
                raise RuntimeError(f"Unexpected capital-flow revision: {path.name}")
            paths.append((int(match.group(1)), path))
        paths.sort()
        if not paths:
            raise RuntimeError("Capital-flow period has no revisions")
        if len(paths) > MAX_REVISIONS_PER_DATE:
            raise RuntimeError("Capital-flow period contains too many revisions")
        if [item[0] for item in paths] != list(range(1, len(paths) + 1)):
            raise RuntimeError("Capital-flow revision sequence is not contiguous")
        chain: list[dict[str, Any]] = []
        previous: dict[str, Any] | None = None
        for revision, path in paths:
            item = _read_revision(path, trade_date, revision)
            if previous is None:
                if (
                    item["supersedes"] is not None
                    or item["supersedes_fingerprint"] is not None
                ):
                    raise RuntimeError("First capital-flow revision has a parent")
            elif (
                item["supersedes"] != previous["revision_id"]
                or item["supersedes_fingerprint"]
                != previous["record_fingerprint"]
            ):
                raise RuntimeError("Capital-flow supersedes chain is invalid")
            expected_history = [
                *[_revision_summary(parent) for parent in chain],
                _revision_summary(item),
            ]
            if item["revisions"] != expected_history:
                raise RuntimeError(
                    "Capital-flow embedded revision history is invalid"
                )
            chain.append(item)
            previous = item
        return chain

    def _atomic_create_unlocked(self, record: Mapping[str, Any]) -> None:
        trade_date = date.fromisoformat(str(record["trade_date"]))
        revision = int(record["revision"])
        final_directory = self.dataset_root / trade_date.isoformat()
        final_path = final_directory / f"revision_{revision:08d}.json"
        staging_root = self.root / ".staging"
        _assert_directory(staging_root, "Capital-flow staging", missing_ok=True)
        staging_root.mkdir(parents=True, exist_ok=True)
        _assert_directory(staging_root, "Capital-flow staging")
        stage_directory = staging_root / f"capital-flow-{uuid4().hex}"
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
                raise ValueError("Capital-flow revision exceeds the supported size")
            _read_revision(temporary, trade_date, revision)
            self.dataset_root.mkdir(parents=True, exist_ok=True)
            final_directory.mkdir(exist_ok=True)
            if final_path.exists() or final_path.is_symlink():
                raise FileExistsError(
                    f"Immutable capital-flow revision already exists: {final_path.name}"
                )
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
            raise RuntimeError("Capital-flow root must not be symbolic")
        key = os.path.normcase(str(self.root))
        with _LOCKS_GUARD:
            thread_lock = _LOCKS.setdefault(key, RLock())
        with thread_lock:
            self.root.mkdir(parents=True, exist_ok=True)
            _assert_directory(self.root, "Capital-flow root")
            with _file_lock(self.root / ".capital-flow.lock"):
                yield


def refresh_capital_flow(config: AppConfig, trade_date: date) -> dict[str, Any]:
    """Fetch and persist one completed-date board capital-flow snapshot."""

    if not isinstance(config, AppConfig):
        raise TypeError("config must be an AppConfig")
    requested = _required_date(trade_date, "trade_date")
    retrieved_at = _now()
    query = CapitalFlowQuery(trade_date=requested)
    try:
        source, coverage, summary, flows = _download_capital_flow(
            config, requested
        )
        return CapitalFlowStore(config)._publish(
            trade_date=requested,
            retrieved_at=retrieved_at,
            source=source,
            coverage=coverage,
            summary=summary,
            flows=flows,
        )
    except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
        return _unavailable(
            requested,
            query=query,
            cutoff=None,
            code="capital_flow_refresh_failed",
            message=f"{type(exc).__name__}: {exc}"[:1_000],
            recovery_action="retry_refresh",
            retrieved_at=retrieved_at,
        )


def _download_capital_flow(
    config: AppConfig, trade_date: date
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], list[dict[str, Any]]]:
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
    page_digests: list[tuple[int, str]] = []
    first_payload, response_bytes, response_hash = _request_json(
        _request_params(1),
        timeout=timeout,
        proxy_mode=proxy_mode,
        max_attempts=max_attempts,
        retry_base=retry_base,
        retry_max=retry_max,
    )
    total_bytes += response_bytes
    page_digests.append((1, response_hash))
    first = _parse_page(first_payload, 1)
    declared = first["total"]
    page_count = math.ceil(declared / PAGE_SIZE)
    raw_rows = list(first["rows"])
    for page_number in range(2, page_count + 1):
        payload, response_bytes, response_hash = _request_json(
            _request_params(page_number),
            timeout=timeout,
            proxy_mode=proxy_mode,
            max_attempts=max_attempts,
            retry_base=retry_base,
            retry_max=retry_max,
        )
        total_bytes += response_bytes
        page_digests.append((page_number, response_hash))
        page = _parse_page(payload, page_number)
        if page["total"] != declared:
            raise ValueError("Eastmoney capital-flow count changed during pagination")
        raw_rows.extend(page["rows"])
    if len(raw_rows) != declared:
        raise ValueError("Eastmoney capital-flow count does not match complete pages")
    if total_bytes > MAX_TOTAL_RESPONSE_BYTES:
        raise ValueError("Eastmoney capital-flow responses exceed the size bound")
    flows = [_normalize_flow(item, trade_date) for item in raw_rows]
    flows.sort(key=lambda item: item["code"])
    if len({item["code"] for item in flows}) != len(flows):
        raise ValueError("Eastmoney capital-flow response contains duplicate codes")
    quality = _data_quality(flows)
    if quality["main_metric_available_rows"] <= 0:
        raise ValueError("Eastmoney capital-flow response has no main-flow values")
    source = {
        **_SOURCE_BASE,
        "response_sha256": _ordered_response_sha256(page_digests),
    }
    coverage = {
        "page_size": PAGE_SIZE,
        "pages": page_count,
        "declared_count": declared,
        "received_count": len(flows),
        "complete": True,
        "response_bytes": total_bytes,
        "data_quality": quality,
    }
    summary = _summary(flows)
    _validate_source(source)
    _validate_coverage(coverage, flows)
    _validate_summary(summary, flows)
    return source, coverage, summary, flows


def _request_params(page_number: int) -> dict[str, str]:
    return {
        "pn": str(page_number),
        "pz": str(PAGE_SIZE),
        "po": "1",
        "np": "1",
        "ut": EASTMONEY_UT,
        "fltt": "2",
        "invt": "2",
        "fid": "f62",
        "fs": BOARD_FILTER,
        "fields": ",".join(FLOW_COLUMNS),
    }


def _request_json(
    params: Mapping[str, str],
    *,
    timeout: int,
    proxy_mode: str,
    max_attempts: int,
    retry_base: float,
    retry_max: float,
) -> tuple[Any, int, str]:
    request = urllib.request.Request(
        f"{ENDPOINT}?{urllib.parse.urlencode(params)}", headers=REQUEST_HEADERS
    )
    last_error: Exception | None = None
    for attempt in range(max(1, max_attempts)):
        try:
            with _open_request(request, timeout, proxy_mode) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValueError("Eastmoney capital-flow response exceeds size bound")
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
    raise RuntimeError(f"Eastmoney capital-flow request failed: {last_error}")


def _parse_page(payload: Any, page_number: int) -> dict[str, Any]:
    if not isinstance(payload, dict) or payload.get("rc") != 0:
        raise ValueError("Eastmoney capital-flow response was unsuccessful")
    data = payload.get("data")
    if not isinstance(data, dict) or set(data) != {"total", "diff"}:
        raise ValueError("Eastmoney capital-flow response envelope is invalid")
    total = _strict_int(data.get("total"), "capital-flow total", minimum=1)
    if total > MAX_ROWS:
        raise ValueError("Eastmoney capital-flow count exceeds supported bound")
    page_count = math.ceil(total / PAGE_SIZE)
    if page_count > MAX_PAGES:
        raise ValueError("Eastmoney capital-flow page count exceeds supported bound")
    rows = data.get("diff")
    if not isinstance(rows, list):
        raise ValueError("Eastmoney capital-flow diff must be an array")
    expected = (
        PAGE_SIZE
        if page_number < page_count
        else total - PAGE_SIZE * (page_count - 1)
    )
    if page_number > page_count or len(rows) != expected:
        raise ValueError("Eastmoney capital-flow page row count is inconsistent")
    return {"total": total, "rows": rows}


def _normalize_flow(value: Any, trade_date: date) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != set(FLOW_COLUMNS):
        raise ValueError("Eastmoney capital-flow row schema is invalid")
    code = _bounded_pattern(value["f12"], "board code", _BOARD_CODE)
    market_code = _strict_int(value["f13"], "board market code", minimum=0)
    if market_code != 90:
        raise ValueError("Eastmoney capital-flow board market code is invalid")
    timestamp, quote_date = _quote_timestamp(value["f124"], trade_date)
    return {
        "code": code,
        "name": _bounded_text(value["f14"], "board name", 80),
        "market": "BOARD",
        "close": _optional_bounded_number(
            value["f2"], "board close", minimum=0.0, maximum=1_000_000_000.0,
            strict_minimum=True,
        ),
        "change_pct": _optional_bounded_number(
            value["f3"], "board change_pct", minimum=-100_000.0, maximum=100_000.0
        ),
        "main_net_inflow": _flow_amount(value["f62"], "main net inflow"),
        "main_net_inflow_pct": _flow_percent(
            value["f184"], "main net inflow percent"
        ),
        "super_large_net_inflow": _flow_amount(
            value["f66"], "super-large net inflow"
        ),
        "super_large_net_inflow_pct": _flow_percent(
            value["f69"], "super-large net inflow percent"
        ),
        "large_net_inflow": _flow_amount(value["f72"], "large net inflow"),
        "large_net_inflow_pct": _flow_percent(
            value["f75"], "large net inflow percent"
        ),
        "medium_net_inflow": _flow_amount(value["f78"], "medium net inflow"),
        "medium_net_inflow_pct": _flow_percent(
            value["f81"], "medium net inflow percent"
        ),
        "small_net_inflow": _flow_amount(value["f84"], "small net inflow"),
        "small_net_inflow_pct": _flow_percent(
            value["f87"], "small net inflow percent"
        ),
        "quote_timestamp": timestamp,
        "quote_date": quote_date.isoformat(),
    }


def _summary(flows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    main_rows = [item for item in flows if item.get("main_net_inflow") is not None]
    main_values = [float(item["main_net_inflow"]) for item in main_rows]
    top = (
        max(main_rows, key=lambda item: (float(item["main_net_inflow"]), item["code"]))
        if main_rows
        else None
    )
    bottom = (
        min(main_rows, key=lambda item: (float(item["main_net_inflow"]), item["code"]))
        if main_rows
        else None
    )
    return {
        "flow_count": len(flows),
        "main_metric_count": len(main_rows),
        "positive_main_count": sum(value > 0 for value in main_values),
        "negative_main_count": sum(value < 0 for value in main_values),
        "flat_main_count": sum(value == 0 for value in main_values),
        "missing_main_count": len(flows) - len(main_rows),
        "positive_main_share": (
            sum(value > 0 for value in main_values) / len(main_values)
            if main_values
            else None
        ),
        "median_main_net_inflow": (
            float(median(main_values)) if main_values else None
        ),
        "top_inflow_code": top["code"] if top else None,
        "top_inflow_name": top["name"] if top else None,
        "top_inflow_value": top["main_net_inflow"] if top else None,
        "top_outflow_code": bottom["code"] if bottom else None,
        "top_outflow_name": bottom["name"] if bottom else None,
        "top_outflow_value": bottom["main_net_inflow"] if bottom else None,
        "total_main_net_inflow": _complete_sum(flows, "main_net_inflow"),
        "super_large_net_inflow_sum": _complete_sum(
            flows, "super_large_net_inflow"
        ),
        "large_net_inflow_sum": _complete_sum(flows, "large_net_inflow"),
        "medium_net_inflow_sum": _complete_sum(flows, "medium_net_inflow"),
        "small_net_inflow_sum": _complete_sum(flows, "small_net_inflow"),
    }


def _complete_sum(flows: Sequence[Mapping[str, Any]], field: str) -> float | None:
    values = [item.get(field) for item in flows]
    if not values or any(value is None for value in values):
        return None
    return float(sum(float(value) for value in values))


def _data_quality(flows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    optional_fields = ("close", "change_pct", *FLOW_METRICS)
    missing = {
        name: sum(item.get(name) is None for item in flows)
        for name in optional_fields
    }
    return {
        "rows_with_missing_optional_values": sum(
            any(item.get(name) is None for name in optional_fields)
            for item in flows
        ),
        "missing_optional_numeric_values": missing,
        "main_metric_available_rows": sum(
            item.get("main_net_inflow") is not None for item in flows
        ),
        "complete_order_size_rows": sum(
            all(item.get(name) is not None for name in AMOUNT_METRICS)
            for item in flows
        ),
        "all_quote_dates_match": all(item.get("quote_date") for item in flows),
    }


def _warnings(coverage: Mapping[str, Any]) -> list[dict[str, str]]:
    warnings = [
        _clone_json(_NOT_CERTIFIED_WARNING),
        _clone_json(_PROVIDER_SCOPE_WARNING),
        _clone_json(_METHODOLOGY_WARNING),
    ]
    missing = (
        coverage.get("data_quality", {}).get("missing_optional_numeric_values", {})
    )
    total_missing = sum(int(value) for value in missing.values())
    if total_missing:
        warnings.append(
            {
                "code": "optional_metric_missing",
                "message": (
                    f"{total_missing} optional capital-flow metric value(s) were "
                    "unavailable; source nulls are preserved."
                ),
                "recovery_action": "review_data_quality",
            }
        )
    return warnings


def _filter_flows(
    flows: Sequence[Mapping[str, Any]], query: CapitalFlowQuery
) -> list[dict[str, Any]]:
    needle = query.q.casefold() if query.q is not None else None
    selected = [
        _clone_json(item)
        for item in flows
        if needle is None
        or needle in f"{item['code']} {item['name']}".casefold()
    ]
    if query.sort == "name":
        selected.sort(
            key=lambda item: (item["name"].casefold(), item["code"]),
            reverse=query.direction == "desc",
        )
        return selected
    present = [item for item in selected if item.get(query.sort) is not None]
    missing = [item for item in selected if item.get(query.sort) is None]
    present.sort(
        key=lambda item: (float(item[query.sort]), item["code"]),
        reverse=query.direction == "desc",
    )
    missing.sort(key=lambda item: item["code"])
    return [*present, *missing]


def _validate_query(query: CapitalFlowQuery) -> None:
    if not isinstance(query, CapitalFlowQuery):
        raise ValueError("CapitalFlowQuery is invalid")
    _optional_date(query.trade_date, "trade_date")
    if query.q is not None:
        _bounded_text(query.q, "q", 100)
    if query.sort not in SORT_FIELDS:
        raise ValueError("unsupported capital-flow sort field")
    if query.direction not in {"asc", "desc"}:
        raise ValueError("direction must be asc or desc")
    if (
        isinstance(query.limit, bool)
        or not isinstance(query.limit, int)
        or not 1 <= query.limit <= MAX_QUERY_LIMIT
    ):
        raise ValueError(f"limit must be between 1 and {MAX_QUERY_LIMIT}")
    if not isinstance(query.include_revisions, bool):
        raise ValueError("include_revisions must be a boolean")


def _query_payload(query: CapitalFlowQuery) -> dict[str, Any]:
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


def _apply_freshness(
    payload: dict[str, Any], trade_date: date, cutoff: date | None
) -> None:
    if cutoff is not None and trade_date < cutoff:
        payload["status"] = "stale"
        payload["warnings"].append(
            {
                "code": "capital_flow_stale",
                "message": (
                    f"Local capital-flow snapshot is {trade_date.isoformat()}, "
                    f"before completed-session cutoff {cutoff.isoformat()}."
                ),
                "recovery_action": "refresh_capital_flow",
            }
        )
    elif cutoff is not None and trade_date > cutoff:
        payload["status"] = "provisional"
        payload["warnings"].append(
            {
                "code": "after_completed_session_cutoff",
                "message": (
                    "Selected capital-flow snapshot is after the completed-session "
                    "cutoff."
                ),
                "recovery_action": "select_completed_session",
            }
        )


def _unavailable(
    trade_date: date | None,
    *,
    query: CapitalFlowQuery,
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
        "source": {**_SOURCE_BASE, "response_sha256": None},
        "coverage": {
            "page_size": PAGE_SIZE,
            "pages": 0,
            "declared_count": 0,
            "received_count": 0,
            "complete": False,
            "response_bytes": 0,
            "data_quality": _data_quality([]),
        },
        "summary": {
            **_summary([]),
            "matched_flow_count": 0,
            "returned_flow_count": 0,
            "flows_truncated": False,
        },
        "flows": [],
        "revisions": [],
        "authority": dict(_AUTHORITY),
        "errors": [
            {
                "code": code,
                "message": message,
                "recovery_action": recovery_action,
            }
        ],
        "warnings": [
            _clone_json(_NOT_CERTIFIED_WARNING),
            _clone_json(_PROVIDER_SCOPE_WARNING),
            _clone_json(_METHODOLOGY_WARNING),
        ],
        "reused": False,
        "filters": _query_payload(query),
        "freshness": _freshness(trade_date, cutoff),
    }


def _validate_revision(
    value: Any, expected_date: date, expected_revision: int
) -> None:
    if not isinstance(value, dict) or set(value) != _STORED_FIELDS:
        raise ValueError("capital-flow revision schema is invalid")
    if value["schema_version"] != SCHEMA_VERSION or value["dataset"] != DATASET:
        raise ValueError("capital-flow revision identity is invalid")
    if value["available"] is not True or value["status"] != "current":
        raise ValueError("capital-flow revision status is invalid")
    if value["trade_date"] != expected_date.isoformat():
        raise ValueError("capital-flow revision date is invalid")
    if (
        not isinstance(value["retrieved_at"], str)
        or _ISO_UTC.fullmatch(value["retrieved_at"]) is None
    ):
        raise ValueError("capital-flow retrieved_at is invalid")
    if value["revision"] != expected_revision:
        raise ValueError("capital-flow revision number is invalid")
    if (
        not isinstance(value["revision_id"], str)
        or _REVISION_ID.fullmatch(value["revision_id"]) is None
    ):
        raise ValueError("capital-flow revision id is invalid")
    _validate_source(value["source"])
    _validate_flows(value["flows"], expected_date)
    _validate_coverage(value["coverage"], value["flows"])
    _validate_summary(value["summary"], value["flows"])
    if (
        value["authority"] != _AUTHORITY
        or value["errors"] != []
        or value["warnings"] != _warnings(value["coverage"])
    ):
        raise ValueError("capital-flow authority or warning contract is invalid")
    _valid_fingerprint(value["evidence_fingerprint"], "evidence_fingerprint")
    _valid_fingerprint(value["record_fingerprint"], "record_fingerprint")
    if expected_revision == 1:
        if (
            value["supersedes"] is not None
            or value["supersedes_fingerprint"] is not None
        ):
            raise ValueError("first capital-flow revision cannot have a parent")
    else:
        if (
            not isinstance(value["supersedes"], str)
            or _REVISION_ID.fullmatch(value["supersedes"]) is None
        ):
            raise ValueError("capital-flow parent revision id is invalid")
        _valid_fingerprint(
            value["supersedes_fingerprint"], "supersedes_fingerprint"
        )
    revisions = value["revisions"]
    if not isinstance(revisions, list) or len(revisions) != expected_revision:
        raise ValueError("capital-flow revision history is invalid")
    for number, item in enumerate(revisions, start=1):
        _validate_revision_summary(item, expected_date, number)
    latest = revisions[-1]
    if (
        latest["revision_id"] != value["revision_id"]
        or latest["evidence_fingerprint"] != value["evidence_fingerprint"]
        or latest["record_fingerprint"] != value["record_fingerprint"]
    ):
        raise ValueError("capital-flow revision history does not match record")
    expected_evidence = _fingerprint(
        {
            "schema_version": SCHEMA_VERSION,
            "dataset": DATASET,
            "trade_date": value["trade_date"],
            "flows": value["flows"],
        }
    )
    if value["evidence_fingerprint"] != expected_evidence:
        raise ValueError("capital-flow evidence fingerprint does not match records")
    if value["record_fingerprint"] != _record_fingerprint(value):
        raise ValueError("capital-flow record fingerprint does not match record")


def _validate_flows(flows: Any, expected_date: date) -> None:
    if not isinstance(flows, list) or not flows or len(flows) > MAX_ROWS:
        raise ValueError("capital-flow records are invalid")
    codes: set[str] = set()
    for item in flows:
        if not isinstance(item, dict) or set(item) != _FLOW_FIELDS:
            raise ValueError("capital-flow record schema is invalid")
        code = _bounded_pattern(item["code"], "board code", _BOARD_CODE)
        if code in codes or item["market"] != "BOARD":
            raise ValueError("capital-flow board identity is invalid")
        codes.add(code)
        _bounded_text(item["name"], "board name", 80)
        _optional_bounded_number(
            item["close"], "board close", minimum=0.0, maximum=1_000_000_000.0,
            strict_minimum=True,
        )
        _optional_bounded_number(
            item["change_pct"], "board change_pct", minimum=-100_000.0,
            maximum=100_000.0,
        )
        for field in AMOUNT_METRICS:
            _flow_amount(item[field], field)
        for field in PERCENT_METRICS:
            _flow_percent(item[field], field)
        timestamp, quote_date = _quote_timestamp(
            item["quote_timestamp"], expected_date
        )
        if (
            item["quote_timestamp"] != timestamp
            or item["quote_date"] != quote_date.isoformat()
        ):
            raise ValueError("capital-flow quote time is invalid")
    if [item["code"] for item in flows] != sorted(codes):
        raise ValueError("capital-flow records are not deterministically ordered")
    if not any(item["main_net_inflow"] is not None for item in flows):
        raise ValueError("capital-flow records contain no main-flow values")


def _validate_summary(value: Any, flows: Sequence[Mapping[str, Any]]) -> None:
    if not isinstance(value, dict) or set(value) != _SUMMARY_FIELDS:
        raise ValueError("capital-flow summary schema is invalid")
    expected = _summary(flows)
    for key, expected_value in expected.items():
        if value.get(key) != expected_value:
            raise ValueError(f"capital-flow summary field {key} is inconsistent")


def _validate_source(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != _SOURCE_FIELDS:
        raise ValueError("capital-flow source schema is invalid")
    for key, expected in _SOURCE_BASE.items():
        if value.get(key) != expected:
            raise ValueError(f"capital-flow source {key} is invalid")
    _valid_fingerprint(value.get("response_sha256"), "response_sha256")


def _validate_coverage(
    value: Any, flows: Sequence[Mapping[str, Any]]
) -> None:
    if not isinstance(value, dict) or set(value) != _COVERAGE_FIELDS:
        raise ValueError("capital-flow coverage schema is invalid")
    if (
        value["page_size"] != PAGE_SIZE
        or value["declared_count"] != len(flows)
        or value["received_count"] != len(flows)
        or value["complete"] is not True
    ):
        raise ValueError("capital-flow coverage count is invalid")
    pages = _strict_int(value["pages"], "capital-flow pages", minimum=1)
    if pages != math.ceil(len(flows) / PAGE_SIZE) or pages > MAX_PAGES:
        raise ValueError("capital-flow page count is invalid")
    response_bytes = _strict_int(
        value["response_bytes"], "capital-flow response bytes", minimum=1
    )
    if response_bytes > MAX_TOTAL_RESPONSE_BYTES:
        raise ValueError("capital-flow response bytes exceed bound")
    if value["data_quality"] != _data_quality(flows):
        raise ValueError("capital-flow data quality is inconsistent")


def _revision_summary(
    value: Mapping[str, Any], *, current_placeholder: bool = False
) -> dict[str, Any]:
    return {
        "revision_id": value["revision_id"],
        "revision": value["revision"],
        "trade_date": value["trade_date"],
        "retrieved_at": value["retrieved_at"],
        "status": value["status"],
        "flow_count": value["summary"]["flow_count"],
        "evidence_fingerprint": value["evidence_fingerprint"],
        "record_fingerprint": (
            "0" * 64 if current_placeholder else value["record_fingerprint"]
        ),
        "supersedes": value["supersedes"],
    }


def _validate_revision_summary(
    value: Any, expected_date: date, revision: int
) -> None:
    if not isinstance(value, dict) or set(value) != _REVISION_SUMMARY_FIELDS:
        raise ValueError("capital-flow revision summary schema is invalid")
    if (
        value["trade_date"] != expected_date.isoformat()
        or value["revision"] != revision
        or value["status"] != "current"
    ):
        raise ValueError("capital-flow revision summary sequence is invalid")
    if (
        not isinstance(value["revision_id"], str)
        or _REVISION_ID.fullmatch(value["revision_id"]) is None
    ):
        raise ValueError("capital-flow revision summary id is invalid")
    if (
        not isinstance(value["retrieved_at"], str)
        or _ISO_UTC.fullmatch(value["retrieved_at"]) is None
    ):
        raise ValueError("capital-flow revision summary time is invalid")
    _strict_int(value["flow_count"], "revision flow count", minimum=1)
    _valid_fingerprint(
        value["evidence_fingerprint"], "revision evidence fingerprint"
    )
    _valid_fingerprint(value["record_fingerprint"], "revision record fingerprint")
    if revision == 1:
        if value["supersedes"] is not None:
            raise ValueError("first capital-flow revision summary has a parent")
    elif (
        not isinstance(value["supersedes"], str)
        or _REVISION_ID.fullmatch(value["supersedes"]) is None
    ):
        raise ValueError("capital-flow revision summary parent is invalid")


def _read_revision(
    path: Path, expected_date: date, expected_revision: int
) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Capital-flow revision must be a regular file")
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
    observed = datetime.fromtimestamp(timestamp, timezone.utc).astimezone(
        CHINA_TIMEZONE
    )
    if observed.date() != expected_date:
        raise ValueError("quote timestamp date does not match requested trade date")
    return timestamp, observed.date()


def _flow_amount(value: Any, label: str) -> float | None:
    return _optional_bounded_number(
        value, label, minimum=-1_000_000_000_000_000.0,
        maximum=1_000_000_000_000_000.0,
    )


def _flow_percent(value: Any, label: str) -> float | None:
    return _optional_bounded_number(
        value, label, minimum=-100_000.0, maximum=100_000.0
    )


def _optional_bounded_number(
    value: Any,
    label: str,
    *,
    minimum: float,
    maximum: float,
    strict_minimum: bool = False,
) -> float | None:
    if value == "-" or value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} must be numeric or unavailable")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite")
    if (parsed <= minimum if strict_minimum else parsed < minimum) or parsed > maximum:
        raise ValueError(f"{label} is outside the supported range")
    return parsed


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
    return None if value is None else _required_date(value, label)


def _now() -> str:
    return (
        datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def _fingerprint(value: Any) -> str:
    return sha256(_canonical_bytes(value)).hexdigest()


def _ordered_response_sha256(
    page_digests: Sequence[tuple[int, str]],
) -> str:
    normalized: list[dict[str, Any]] = []
    for expected, (page, digest) in enumerate(page_digests, start=1):
        if page != expected:
            raise ValueError("response page digests are not contiguous")
        normalized.append(
            {
                "page": page,
                "sha256": _valid_fingerprint(digest, "response sha256"),
            }
        )
    if not normalized:
        raise ValueError("response page digest list is empty")
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


def _assert_directory(
    path: Path, label: str, *, missing_ok: bool = False
) -> None:
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
        raise RuntimeError("Capital-flow lock must not be symbolic")
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


__all__ = ["CapitalFlowQuery", "CapitalFlowStore", "refresh_capital_flow"]
