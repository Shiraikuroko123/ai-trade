"""Auditable current quotes and stock-only historical valuation percentiles."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import math
from pathlib import Path
import re
import time as time_module
from typing import Any, Mapping, Sequence
import urllib.parse
import urllib.request
from uuid import uuid4

from ..config import AppConfig
from ..json_utils import load_unique_json, loads_unique_json
from ..models import Instrument
from .eastmoney import REQUEST_HEADERS, _open_request, _proxy_mode, completed_session_cutoff
from .evidence_io import atomic_create_json, evidence_store_lock


SCHEMA_VERSION = 1
DATASET = "valuation"
ENDPOINT = "https://push2.eastmoney.com/api/qt/stock/get"
HISTORY_ENDPOINT = "https://datacenter-web.eastmoney.com/api/data/v1/get"
HISTORY_REPORT_NAME = "RPT_VALUEANALYSIS_DET"
EASTMONEY_UT = "fa5fd1943c7b386f172d6893dbfba10b"
CHINA_TIMEZONE = timezone(timedelta(hours=8))
MAX_RESPONSE_BYTES = 256 * 1024
MAX_HISTORY_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_SYMBOLS = 500
HISTORY_PAGE_SIZE = 500
MAX_HISTORY_PAGES = 5
MAX_HISTORY_POINTS = HISTORY_PAGE_SIZE * MAX_HISTORY_PAGES
MIN_PERCENTILE_OBSERVATIONS = 120
MAX_REVISIONS_PER_DATE = 100
MAX_PERIODS = 5_000
MAX_REVISION_BYTES = 16 * 1024 * 1024
_SYMBOL = re.compile(r"\d{6}\Z")
_DATE_DIRECTORY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_REVISION_FILE = re.compile(r"revision_(\d{8})\.json\Z")
_REVISION_ID = re.compile(r"valuation_[0-9a-f]{32}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_AUTHORITY = {"research_only": True, "execution_authorized": False}
_FIELDS = (
    "f43",
    "f57",
    "f58",
    "f116",
    "f117",
    "f162",
    "f163",
    "f164",
    "f167",
    "f168",
    "f169",
    "f170",
    "f124",
)
_HISTORY_FIELDS = (
    "SECURITY_CODE",
    "SECURITY_NAME_ABBR",
    "TRADE_DATE",
    "CLOSE_PRICE",
    "PE_TTM",
    "PE_LAR",
    "PB_MRQ",
    "PCF_OCF_TTM",
    "PS_TTM",
)
_HISTORY_METRICS = {
    "pe_ttm": "PE_TTM",
    "pe_static": "PE_LAR",
    "pb": "PB_MRQ",
    "cash_flow": "PCF_OCF_TTM",
    "ps_ttm": "PS_TTM",
}


@dataclass(frozen=True)
class ValuationQuery:
    trade_date: date | None = None
    symbol: str | None = None
    limit: int = 200
    include_revisions: bool = False


class ValuationProviderError(RuntimeError):
    """Raised when a quote response cannot be trusted as evidence."""


def refresh_valuation(
    config: AppConfig,
    *,
    symbols: Sequence[str] | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Fetch current valuation fields for configured instruments and publish them."""

    instruments = {item.symbol: item for item in config.instruments}
    selected = list(symbols) if symbols is not None else list(instruments)
    if not selected or len(selected) > MAX_SYMBOLS:
        raise ValueError(f"symbols must contain between 1 and {MAX_SYMBOLS} items")
    if len(set(selected)) != len(selected):
        raise ValueError("symbols must be unique")
    selected_instruments: list[Instrument] = []
    for symbol in selected:
        if not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None:
            raise ValueError("symbol must be a six-digit security code")
        try:
            selected_instruments.append(instruments[symbol])
        except KeyError as exc:
            raise ValueError("symbol must be in the configured security master") from exc

    market_close = str(config.raw.get("data", {}).get("market_close_time", "15:30"))
    cutoff = completed_session_cutoff(as_of, market_close)
    china_now = _china_now(as_of)
    quote_date = cutoff
    provisional = china_now.date() > cutoff
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    response_hashes: list[str] = []
    history_responses: list[dict[str, Any]] = []
    for instrument in selected_instruments:
        try:
            payload, digest, response_bytes = _download(config, instrument)
            record = _parse_quote(payload, instrument)
            response_hashes.append(digest)
            record["response_sha256"] = digest
            record["response_bytes"] = response_bytes
            if instrument.instrument_type.strip().upper() == "STOCK":
                try:
                    rows, responses, total_count = _download_history(
                        config, instrument
                    )
                    history = _parse_history(
                        rows,
                        instrument,
                        cutoff=cutoff,
                        provider_total_count=total_count,
                    )
                    record["valuation_percentiles"] = history.pop("percentiles")
                    record["valuation_history"] = history
                    history_responses.extend(responses)
                except (ValuationProviderError, OSError, ValueError) as exc:
                    record["valuation_history"] = _unavailable_history(
                        "history_provider_error", str(exc)[:300]
                    )
                    errors.append(
                        {
                            "symbol": instrument.symbol,
                            "code": "valuation_history_provider_error",
                            "message": str(exc)[:300],
                        }
                    )
            else:
                record["valuation_history"] = _unavailable_history(
                    "instrument_type_not_supported",
                    "Historical valuation percentiles apply only to STOCK instruments.",
                )
            records.append(record)
        except (ValuationProviderError, OSError, ValueError) as exc:
            errors.append(
                {
                    "symbol": instrument.symbol,
                    "code": "valuation_provider_error",
                    "message": str(exc)[:300],
                }
            )
    if not records:
        return _unavailable(
            ValuationQuery(symbol=selected[0] if len(selected) == 1 else None),
            errors,
        )
    record = {
        "schema_version": SCHEMA_VERSION,
        "dataset": DATASET,
        "available": True,
        "status": "provisional" if provisional else ("partial" if errors else "current"),
        "trade_date": quote_date.isoformat(),
        "retrieved_at": _now(),
        "source": {
            "provider": "eastmoney",
            "endpoint": ENDPOINT,
            "fields": list(_FIELDS),
            "response_sha256": _fingerprint(sorted(response_hashes)),
            "history_endpoint": HISTORY_ENDPOINT,
            "history_report_name": HISTORY_REPORT_NAME,
            "history_fields": list(_HISTORY_FIELDS),
            "history_response_sha256": _fingerprint(history_responses),
            "history_response_count": len(history_responses),
            "certification": "not_exchange_certified",
            "scaling": {
                "price": "f43 / 100",
                "pe_ttm": "f162 / 100",
                "pe_static": "f163 / 100",
                "pe_dynamic": "f164 / 100",
                "pb": "f167 / 100",
                "market_cap": "f116 yuan",
                "float_market_cap": "f117 yuan",
                "change_pct": "f170 / 100",
            },
            "percentile_method": (
                "empirical CDF over positive finite completed-session observations; "
                "rank = count(value <= current) / count(values)"
            ),
        },
        "records": records,
        "summary": {
            "requested_count": len(selected_instruments),
            "returned_count": len(records),
            "error_count": len(errors),
            "valuation_metric_coverage": {
                name: sum(1 for item in records if item.get(name) is not None)
                for name in ("pe_ttm", "pe_static", "pe_dynamic", "pb")
            },
            "historical_percentile_supported_count": sum(
                item.get("valuation_history", {}).get("status")
                not in {"instrument_type_not_supported", "unavailable"}
                for item in records
            ),
            "historical_percentile_available_count": sum(
                bool(item.get("valuation_history", {}).get("available"))
                for item in records
            ),
            "historical_percentile_unsupported_count": sum(
                item.get("valuation_history", {}).get("status")
                == "instrument_type_not_supported"
                for item in records
            ),
        },
        "authority": dict(_AUTHORITY),
        "errors": errors,
        "warnings": [
            {
                "code": "not_exchange_certified",
                "message": "Eastmoney quote fields are third-party research evidence, not exchange-certified data.",
            },
            {
                "code": "stock_only_history",
                "message": "Historical PE/PB/cash-flow valuation percentiles are calculated only for STOCK instruments with sufficient positive observations.",
            },
        ],
    }
    if provisional:
        record["warnings"].append(
            {
                "code": "quote_before_completed_cutoff",
                "message": "The local clock is before the completed-session cutoff; this quote snapshot is provisional.",
            }
        )
    return ValuationStore(config).publish(record)


class ValuationStore:
    """Immutable local date/revision store with read-only queries."""

    def __init__(self, config_or_root: AppConfig | str | Path):
        if isinstance(config_or_root, AppConfig) or hasattr(config_or_root, "valuation_dir"):
            raw_root = getattr(config_or_root, "valuation_dir", None)
            if raw_root is None:
                raw_root = config_or_root.resolve("state/valuation")
        else:
            raw_root = Path(config_or_root)
        self.root = Path(raw_root).resolve()

    def list(self, query: ValuationQuery | None = None) -> dict[str, Any]:
        selected = query or ValuationQuery()
        _validate_query(selected)
        periods = self._periods()
        target = selected.trade_date or (periods[-1] if periods else None)
        if target is None or target not in periods:
            return _unavailable(selected, [])
        chain = self._load_chain(target)
        latest = _clone(chain[-1])
        records = list(latest.get("records", []))
        if selected.symbol:
            records = [item for item in records if item.get("symbol") == selected.symbol]
        latest["records"] = records[: selected.limit]
        latest["summary"] = {
            **dict(latest.get("summary") or {}),
            "matched_count": len(records),
            "returned_count": len(latest["records"]),
            "truncated": len(records) > len(latest["records"]),
        }
        latest["filters"] = _query_payload(selected)
        latest["revisions"] = [_revision_summary(item) for item in chain]
        if not selected.include_revisions:
            latest["revisions"] = latest["revisions"][-1:]
        return latest

    def publish(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        with evidence_store_lock(self.root, "Valuation"):
            return self._publish_unlocked(draft)

    def _publish_unlocked(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        record = _clone(draft)
        if not isinstance(record, dict):
            raise ValueError("valuation record must be an object")
        _validate_draft(record)
        trade_date = date.fromisoformat(str(record["trade_date"]))
        chain = self._load_chain(trade_date, missing_ok=True)
        evidence = _fingerprint(
            {
                "schema_version": SCHEMA_VERSION,
                "dataset": DATASET,
                "trade_date": record["trade_date"],
                "source": record["source"],
                "records": record["records"],
                "errors": record["errors"],
            }
        )
        if chain and chain[-1].get("evidence_fingerprint") == evidence:
            result = _clone(chain[-1])
            result["reused"] = True
            result["revisions"] = [_revision_summary(item) for item in chain]
            return result
        if len(chain) >= MAX_REVISIONS_PER_DATE:
            raise RuntimeError("valuation revision capacity reached")
        previous = chain[-1] if chain else None
        record.update(
            {
                "revision_id": f"valuation_{uuid4().hex}",
                "revision": len(chain) + 1,
                "reused": False,
                "evidence_fingerprint": evidence,
                "supersedes": previous.get("revision_id") if previous else None,
                "supersedes_fingerprint": previous.get("record_fingerprint") if previous else None,
                "record_fingerprint": None,
            }
        )
        record["revisions"] = [*[_revision_summary(item) for item in chain], _revision_summary(record)]
        record["record_fingerprint"] = _record_fingerprint(record)
        record["revisions"][-1]["record_fingerprint"] = record["record_fingerprint"]
        _validate_record(record, expected_date=trade_date, expected_revision=record["revision"])
        self._atomic_create(record)
        committed = self._load_chain(trade_date)
        result = _clone(committed[-1])
        result["revisions"] = [_revision_summary(item) for item in committed]
        return result

    def _periods(self) -> list[date]:
        if not self.root.exists():
            return []
        if self.root.is_symlink() or not self.root.is_dir():
            raise RuntimeError("valuation root is invalid")
        result: list[date] = []
        for path in self.root.iterdir():
            if path.is_symlink() or not path.is_dir() or not _DATE_DIRECTORY.fullmatch(path.name):
                raise RuntimeError("valuation period directory is invalid")
            result.append(date.fromisoformat(path.name))
            if len(result) > MAX_PERIODS:
                raise RuntimeError("valuation store contains too many periods")
        return sorted(result)

    def _load_chain(self, trade_date: date, *, missing_ok: bool = False) -> list[dict[str, Any]]:
        directory = self.root / trade_date.isoformat()
        if not directory.exists():
            if missing_ok:
                return []
            raise RuntimeError("valuation period is missing")
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("valuation period is invalid")
        paths: list[tuple[int, Path]] = []
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("valuation revision must be a regular file")
            match = _REVISION_FILE.fullmatch(path.name)
            if match is None:
                raise RuntimeError("unexpected valuation revision file")
            paths.append((int(match.group(1)), path))
        paths.sort()
        if len(paths) > MAX_REVISIONS_PER_DATE:
            raise RuntimeError("valuation period contains too many revisions")
        if [number for number, _ in paths] != list(range(1, len(paths) + 1)):
            raise RuntimeError("valuation revision sequence is not contiguous")
        chain: list[dict[str, Any]] = []
        previous: dict[str, Any] | None = None
        for revision, path in paths:
            value = load_unique_json(path, max_bytes=16 * 1024 * 1024)
            if not isinstance(value, dict):
                raise RuntimeError("valuation revision must be an object")
            _validate_record(value, expected_date=trade_date, expected_revision=revision)
            if previous is None:
                if value.get("supersedes") is not None or value.get(
                    "supersedes_fingerprint"
                ) is not None:
                    raise RuntimeError("first valuation revision has a parent")
            elif (
                value.get("supersedes") != previous.get("revision_id")
                or value.get("supersedes_fingerprint") != previous.get("record_fingerprint")
            ):
                raise RuntimeError("valuation supersedes chain is invalid")
            expected_history = [*[_revision_summary(item) for item in chain], _revision_summary(value)]
            if value.get("revisions") != expected_history:
                raise RuntimeError("valuation embedded revision history is invalid")
            chain.append(value)
            previous = value
        if not chain and not missing_ok:
            raise RuntimeError("valuation period has no revisions")
        return chain

    def _atomic_create(self, record: Mapping[str, Any]) -> None:
        trade_date = date.fromisoformat(str(record["trade_date"]))
        directory = self.root / trade_date.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"revision_{int(record['revision']):08d}.json"
        if path.exists():
            existing = load_unique_json(path, max_bytes=16 * 1024 * 1024)
            if existing != record:
                raise RuntimeError("valuation revision already exists with different content")
            return
        atomic_create_json(
            self.root,
            path,
            record,
            label="valuation",
            maximum_bytes=MAX_REVISION_BYTES,
        )


def _download(config: AppConfig, instrument: Instrument) -> tuple[dict[str, Any], str, int]:
    params = {
        "secid": _secid(instrument),
        "fields": ",".join(_FIELDS),
        "ut": EASTMONEY_UT,
        "wbp2u": "|",
        "cb": "",
        "_": str(time_module.time_ns()),
    }
    request = urllib.request.Request(
        f"{ENDPOINT}?{urllib.parse.urlencode(params)}",
        headers=REQUEST_HEADERS,
        method="GET",
    )
    data_config = config.raw.get("data", {})
    timeout = int(data_config.get("timeout_seconds", 20))
    attempts = min(3, max(1, int(data_config.get("max_attempts", 3))))
    errors: list[str] = []
    for attempt in range(attempts):
        try:
            with _open_request(request, timeout, _proxy_mode(config)) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise ValuationProviderError("valuation response is too large")
            value = loads_unique_json(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValuationProviderError("valuation response is not an object")
            return value, sha256(raw).hexdigest(), len(raw)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt + 1 < attempts:
                time_module.sleep(min(2.0, 0.25 * (2**attempt)))
    raise ValuationProviderError(f"valuation download failed: {' | '.join(errors)}")


def _download_history(
    config: AppConfig, instrument: Instrument
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    rows: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    total_count: int | None = None
    for page in range(1, MAX_HISTORY_PAGES + 1):
        params = {
            "sortColumns": "TRADE_DATE",
            "sortTypes": "-1",
            "pageSize": str(HISTORY_PAGE_SIZE),
            "pageNumber": str(page),
            "reportName": HISTORY_REPORT_NAME,
            "columns": "ALL",
            "filter": f'(SECURITY_CODE="{instrument.symbol}")',
        }
        request = urllib.request.Request(
            f"{HISTORY_ENDPOINT}?{urllib.parse.urlencode(params)}",
            headers=REQUEST_HEADERS,
            method="GET",
        )
        payload, digest, response_bytes = _request_history_page(config, request)
        if payload.get("success") is not True or payload.get("code") != 0:
            raise ValuationProviderError(
                "valuation history provider rejected the request"
            )
        result = payload.get("result")
        if result is None and page == 1:
            raise ValuationProviderError("valuation history is unavailable")
        if not isinstance(result, dict) or not isinstance(result.get("data"), list):
            raise ValuationProviderError("valuation history result is invalid")
        raw_count = result.get("count", len(result["data"]))
        if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 0:
            raise ValuationProviderError("valuation history count is invalid")
        if total_count is None:
            total_count = raw_count
        elif raw_count != total_count:
            raise ValuationProviderError(
                "valuation history total changed between pages"
            )
        page_rows = result["data"]
        if len(page_rows) > HISTORY_PAGE_SIZE:
            raise ValuationProviderError("valuation history page exceeds its limit")
        if any(not isinstance(item, dict) for item in page_rows):
            raise ValuationProviderError("valuation history row is invalid")
        if total_count < len(rows) + len(page_rows):
            raise ValuationProviderError(
                "valuation history total is smaller than returned rows"
            )
        rows.extend(page_rows)
        responses.append(
            {
                "symbol": instrument.symbol,
                "page": page,
                "response_sha256": digest,
                "response_bytes": response_bytes,
            }
        )
        expected_rows = min(total_count, MAX_HISTORY_POINTS)
        if not page_rows and len(rows) < expected_rows:
            raise ValuationProviderError(
                "valuation history ended before the declared total"
            )
        if (
            page_rows
            and len(page_rows) < HISTORY_PAGE_SIZE
            and len(rows) < expected_rows
        ):
            raise ValuationProviderError(
                "valuation history page is incomplete before the declared total"
            )
        if not page_rows or len(rows) >= expected_rows:
            break
    if total_count is None or len(rows) < min(total_count, MAX_HISTORY_POINTS):
        raise ValuationProviderError("valuation history response is incomplete")
    return rows[:MAX_HISTORY_POINTS], responses, total_count


def _request_history_page(
    config: AppConfig, request: urllib.request.Request
) -> tuple[dict[str, Any], str, int]:
    data_config = config.raw.get("data", {})
    timeout = int(data_config.get("timeout_seconds", 20))
    attempts = min(3, max(1, int(data_config.get("max_attempts", 3))))
    errors: list[str] = []
    for attempt in range(attempts):
        try:
            with _open_request(request, timeout, _proxy_mode(config)) as response:
                raw = response.read(MAX_HISTORY_RESPONSE_BYTES + 1)
            if len(raw) > MAX_HISTORY_RESPONSE_BYTES:
                raise ValuationProviderError(
                    "valuation history response is too large"
                )
            value = loads_unique_json(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise ValuationProviderError(
                    "valuation history response is not an object"
                )
            return value, sha256(raw).hexdigest(), len(raw)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt + 1 < attempts:
                time_module.sleep(min(2.0, 0.25 * (2**attempt)))
    raise ValuationProviderError(
        "valuation history download failed: " + " | ".join(errors)
    )


def _parse_quote(payload: Mapping[str, Any], instrument: Instrument) -> dict[str, Any]:
    if payload.get("rc") not in (None, 0):
        raise ValuationProviderError(f"Eastmoney returned rc={payload.get('rc')}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise ValuationProviderError("valuation response data is missing")
    if str(data.get("f57")) != instrument.symbol:
        raise ValuationProviderError("valuation response symbol does not match request")
    name = data.get("f58")
    if not isinstance(name, str) or not name.strip() or len(name) > 100:
        raise ValuationProviderError("valuation response name is invalid")
    return {
        "symbol": instrument.symbol,
        "name": name.strip(),
        "market": instrument.market,
        "price": _scaled_positive(data.get("f43"), 100, "f43"),
        "pe_ttm": _scaled_optional(data.get("f162"), 100),
        "pe_static": _scaled_optional(data.get("f163"), 100),
        "pe_dynamic": _scaled_optional(data.get("f164"), 100),
        "pb": _scaled_optional(data.get("f167"), 100),
        "market_cap": _optional_nonnegative(data.get("f116")),
        "float_market_cap": _optional_nonnegative(data.get("f117")),
        "change_pct": _scaled_optional(data.get("f170"), 100),
        "valuation_percentiles": {
            "pe_ttm": None,
            "pe_static": None,
            "pe_dynamic": None,
            "pb": None,
            "cash_flow": None,
            "ps_ttm": None,
        },
        "provider_fields": {key: data.get(key) for key in _FIELDS},
    }


def _parse_history(
    rows: Sequence[Mapping[str, Any]],
    instrument: Instrument,
    *,
    cutoff: date,
    provider_total_count: int,
) -> dict[str, Any]:
    if not rows:
        raise ValuationProviderError("valuation history contains no rows")
    by_date: dict[date, Mapping[str, Any]] = {}
    for row in rows:
        if str(row.get("SECURITY_CODE")) != instrument.symbol:
            raise ValuationProviderError(
                "valuation history symbol does not match request"
            )
        trade_date = _history_date(row.get("TRADE_DATE"))
        if trade_date > cutoff:
            continue
        if trade_date in by_date:
            raise ValuationProviderError("valuation history dates are duplicated")
        by_date[trade_date] = row
    if not by_date:
        raise ValuationProviderError(
            "valuation history has no completed-session observations"
        )
    ordered = sorted(by_date.items())
    latest_date, latest = ordered[-1]
    metric_results: dict[str, dict[str, Any]] = {}
    percentiles: dict[str, float | None] = {}
    for metric, source_field in _HISTORY_METRICS.items():
        current = _positive_history_number(latest.get(source_field))
        values = [
            number
            for _, row in ordered
            if (number := _positive_history_number(row.get(source_field)))
            is not None
        ]
        unavailable_reason = None
        percentile = None
        if current is None:
            unavailable_reason = "latest_value_not_positive"
        elif len(values) < MIN_PERCENTILE_OBSERVATIONS:
            unavailable_reason = "insufficient_positive_observations"
        else:
            percentile = round(
                100.0 * sum(value <= current for value in values) / len(values),
                2,
            )
        metric_results[metric] = {
            "source_field": source_field,
            "current": current,
            "observation_count": len(values),
            "percentile": percentile,
            "unavailable_reason": unavailable_reason,
        }
        percentiles[metric] = percentile
    percentiles["pe_dynamic"] = None
    metric_results["pe_dynamic"] = {
        "source_field": None,
        "current": None,
        "observation_count": 0,
        "percentile": None,
        "unavailable_reason": "historical_source_field_unavailable",
    }
    available = any(value is not None for value in percentiles.values())
    return {
        "available": available,
        "status": "current" if available else "insufficient_history",
        "as_of_date": latest_date.isoformat(),
        "sample_start": ordered[0][0].isoformat(),
        "sample_end": latest_date.isoformat(),
        "sample_count": len(ordered),
        "provider_total_count": provider_total_count,
        "truncated": provider_total_count > len(rows),
        "minimum_observations": MIN_PERCENTILE_OBSERVATIONS,
        "method": "empirical_cdf_positive_observations_lte_current",
        "metrics": metric_results,
        "percentiles": percentiles,
    }


def _unavailable_history(code: str, message: str) -> dict[str, Any]:
    status = (
        "instrument_type_not_supported"
        if code == "instrument_type_not_supported"
        else "unavailable"
    )
    return {
        "available": False,
        "status": status,
        "as_of_date": None,
        "sample_start": None,
        "sample_end": None,
        "sample_count": 0,
        "provider_total_count": 0,
        "truncated": False,
        "minimum_observations": MIN_PERCENTILE_OBSERVATIONS,
        "method": "empirical_cdf_positive_observations_lte_current",
        "metrics": {},
        "unavailable_reason": code,
        "message": message,
    }


def _history_date(value: Any) -> date:
    if not isinstance(value, str) or len(value) < 10:
        raise ValuationProviderError("valuation history trade date is invalid")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise ValuationProviderError(
            "valuation history trade date is invalid"
        ) from exc


def _positive_history_number(value: Any) -> float | None:
    result = _optional_number(value)
    return result if result is not None and result > 0 else None


def _validate_draft(value: Mapping[str, Any]) -> None:
    if value.get("schema_version") != SCHEMA_VERSION or value.get("dataset") != DATASET:
        raise RuntimeError("valuation record schema is invalid")
    try:
        date.fromisoformat(str(value["trade_date"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("valuation trade_date is invalid") from exc
    records = value.get("records")
    if not isinstance(records, list) or not records or len(records) > MAX_SYMBOLS:
        raise RuntimeError("valuation records are invalid")
    seen: set[str] = set()
    for item in records:
        if not isinstance(item, dict) or not _SYMBOL.fullmatch(str(item.get("symbol", ""))):
            raise RuntimeError("valuation record symbol is invalid")
        if item["symbol"] in seen:
            raise RuntimeError("valuation symbols are duplicated")
        seen.add(item["symbol"])
        if not isinstance(item.get("name"), str) or not item["name"].strip():
            raise RuntimeError("valuation record name is invalid")
        if not isinstance(item.get("price"), (int, float)) or not math.isfinite(float(item["price"])) or float(item["price"]) <= 0:
            raise RuntimeError("valuation price is invalid")
        for key in ("pe_ttm", "pe_static", "pe_dynamic", "pb", "market_cap", "float_market_cap", "change_pct"):
            value_item = item.get(key)
            if value_item is not None and (
                not isinstance(value_item, (int, float))
                or isinstance(value_item, bool)
                or not math.isfinite(float(value_item))
            ):
                raise RuntimeError(f"valuation field {key} is invalid")
        percentiles = item.get("valuation_percentiles")
        if not isinstance(percentiles, dict):
            raise RuntimeError("valuation percentiles are invalid")
        for metric, percentile in percentiles.items():
            if metric not in {*_HISTORY_METRICS, "pe_dynamic"}:
                raise RuntimeError("valuation percentile metric is invalid")
            if percentile is not None and (
                isinstance(percentile, bool)
                or not isinstance(percentile, (int, float))
                or not math.isfinite(float(percentile))
                or not 0 <= float(percentile) <= 100
            ):
                raise RuntimeError("valuation percentile value is invalid")
        history = item.get("valuation_history")
        if history is not None:
            _validate_history_summary(history)


def _validate_history_summary(value: Any) -> None:
    if not isinstance(value, dict) or not isinstance(value.get("available"), bool):
        raise RuntimeError("valuation history summary is invalid")
    if value.get("status") not in {
        "current",
        "insufficient_history",
        "instrument_type_not_supported",
        "unavailable",
    }:
        raise RuntimeError("valuation history status is invalid")
    for field in (
        "sample_count",
        "provider_total_count",
        "minimum_observations",
    ):
        number = value.get(field)
        if isinstance(number, bool) or not isinstance(number, int) or number < 0:
            raise RuntimeError(f"valuation history {field} is invalid")
    if not isinstance(value.get("truncated"), bool):
        raise RuntimeError("valuation history truncated flag is invalid")
    for field in ("as_of_date", "sample_start", "sample_end"):
        raw = value.get(field)
        if raw is not None:
            try:
                date.fromisoformat(str(raw))
            except ValueError as exc:
                raise RuntimeError(
                    f"valuation history {field} is invalid"
                ) from exc
    metrics = value.get("metrics")
    if not isinstance(metrics, dict):
        raise RuntimeError("valuation history metrics are invalid")
    for metric, detail in metrics.items():
        if metric not in {*_HISTORY_METRICS, "pe_dynamic"} or not isinstance(
            detail, dict
        ):
            raise RuntimeError("valuation history metric detail is invalid")
        count = detail.get("observation_count")
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise RuntimeError("valuation history observation count is invalid")
        percentile = detail.get("percentile")
        if percentile is not None and (
            isinstance(percentile, bool)
            or not isinstance(percentile, (int, float))
            or not math.isfinite(float(percentile))
            or not 0 <= float(percentile) <= 100
        ):
            raise RuntimeError("valuation history metric percentile is invalid")


def _validate_record(value: Mapping[str, Any], *, expected_date: date, expected_revision: int) -> None:
    _validate_draft(value)
    if value.get("trade_date") != expected_date.isoformat():
        raise RuntimeError("valuation trade_date does not match directory")
    if not _REVISION_ID.fullmatch(str(value.get("revision_id", ""))):
        raise RuntimeError("valuation revision id is invalid")
    if value.get("revision") != expected_revision:
        raise RuntimeError("valuation revision number is invalid")
    for key in ("evidence_fingerprint", "record_fingerprint"):
        if not _FINGERPRINT.fullmatch(str(value.get(key, ""))):
            raise RuntimeError(f"valuation {key} is invalid")
    if value.get("record_fingerprint") != _record_fingerprint(value):
        raise RuntimeError("valuation record fingerprint does not match content")


def _scaled_positive(value: Any, divisor: float, label: str) -> float:
    result = _finite(value, label) / divisor
    if result <= 0:
        raise ValuationProviderError(f"{label} is not positive")
    return result


def _scaled_optional(value: Any, divisor: float) -> float | None:
    result = _optional_number(value)
    return None if result is None or result == 0 else result / divisor


def _optional_nonnegative(value: Any) -> float | None:
    result = _optional_number(value)
    if result is None:
        return None
    if result < 0:
        raise ValuationProviderError("negative market-cap field")
    return result


def _optional_number(value: Any) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValuationProviderError("quote field is not numeric") from exc
    if not math.isfinite(result):
        raise ValuationProviderError("quote field is not finite")
    return result


def _finite(value: Any, label: str) -> float:
    result = _optional_number(value)
    if result is None:
        raise ValuationProviderError(f"{label} is unavailable")
    return result


def _secid(instrument: Instrument) -> str:
    market = {"SH": "1", "SZ": "0", "BJ": "0"}.get(instrument.market)
    if market is None:
        raise ValueError(f"Unsupported market for valuation: {instrument.market}")
    return f"{market}.{instrument.symbol}"


def _validate_query(query: ValuationQuery) -> None:
    if query.symbol is not None and _SYMBOL.fullmatch(query.symbol) is None:
        raise ValueError("symbol must be a six-digit security code")
    if query.trade_date is not None and (
        not isinstance(query.trade_date, date) or isinstance(query.trade_date, datetime)
    ):
        raise ValueError("trade_date must be a date")
    if isinstance(query.limit, bool) or not isinstance(query.limit, int) or not 1 <= query.limit <= MAX_SYMBOLS:
        raise ValueError(f"limit must be between 1 and {MAX_SYMBOLS}")


def _query_payload(query: ValuationQuery) -> dict[str, Any]:
    return {
        "trade_date": query.trade_date.isoformat() if query.trade_date else None,
        "symbol": query.symbol,
        "limit": query.limit,
        "include_revisions": query.include_revisions,
    }


def _revision_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "revision_id": value.get("revision_id"),
        "revision": value.get("revision"),
        "trade_date": value.get("trade_date"),
        "retrieved_at": value.get("retrieved_at"),
        "status": value.get("status"),
        "evidence_fingerprint": value.get("evidence_fingerprint"),
        "record_fingerprint": value.get("record_fingerprint"),
        "supersedes": value.get("supersedes"),
    }


def _unavailable(query: ValuationQuery, errors: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": DATASET,
        "available": False,
        "status": "unavailable",
        "trade_date": query.trade_date.isoformat() if query.trade_date else None,
        "records": [],
        "summary": {"requested_count": 0, "returned_count": 0},
        "filters": _query_payload(query),
        "authority": dict(_AUTHORITY),
        "errors": list(errors) or [{"code": "valuation_not_refreshed", "message": "No validated local valuation snapshot is available."}],
        "warnings": [],
        "revisions": [],
    }


def _record_fingerprint(value: Mapping[str, Any]) -> str:
    payload = _clone(value)
    if isinstance(payload, dict):
        payload.pop("record_fingerprint", None)
        history = payload.get("revisions")
        if isinstance(history, list) and history and isinstance(history[-1], dict):
            history[-1]["record_fingerprint"] = None
    return _fingerprint(payload)


def _fingerprint(value: Any) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    ).hexdigest()


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True))


def _china_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(CHINA_TIMEZONE)
    if value.tzinfo is None:
        return value.replace(tzinfo=CHINA_TIMEZONE)
    return value.astimezone(CHINA_TIMEZONE)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
