"""Official disclosure metadata from SSE and the CNINFO designated platform."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import html
import math
from pathlib import Path
import re
import time as time_module
from typing import Any, Mapping, Sequence
import urllib.parse
import urllib.request

from ..config import AppConfig
from ..json_utils import loads_unique_json
from ..models import Instrument
from .eastmoney import _open_request, _proxy_mode, completed_session_cutoff
from .evidence_io import DateRevisionSpec, ImmutableDateRevisionStore


SCHEMA_VERSION = 1
DATASET = "official_disclosures"
SSE_ENDPOINT = (
    "https://query.sse.com.cn/security/stock/queryCompanyBulletin.do"
)
SSE_STATIC_ROOT = "https://static.sse.com.cn"
CNINFO_QUERY_ENDPOINT = "https://www.cninfo.com.cn/new/hisAnnouncement/query"
CNINFO_MASTER_ROOT = "https://www.cninfo.com.cn/new/data"
CNINFO_STATIC_ROOT = "https://static.cninfo.com.cn"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SYMBOLS = 100
MAX_RECORDS = 5_000
MAX_QUERY_LIMIT = 500
MAX_TEXT = 500
CHINA_TIMEZONE = timezone(timedelta(hours=8))
_SYMBOL = re.compile(r"\d{6}\Z")
_DISCLOSURE_ID = re.compile(r"(?:sse|cninfo)_[a-zA-Z0-9]{1,80}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_AUTHORITY = {"research_only": True, "execution_authorized": False}


@dataclass(frozen=True)
class DisclosureQuery:
    trade_date: date | None = None
    symbol: str | None = None
    provider: str = "all"
    q: str | None = None
    limit: int = 200
    include_revisions: bool = False


class DisclosureProviderError(RuntimeError):
    """Raised when official disclosure metadata fails its evidence contract."""


def refresh_disclosures(
    config: AppConfig,
    *,
    symbols: Sequence[str] | None = None,
    as_of: datetime | None = None,
    lookback_days: int = 30,
    limit_per_symbol: int = 50,
) -> dict[str, Any]:
    if (
        isinstance(lookback_days, bool)
        or not isinstance(lookback_days, int)
        or not 1 <= lookback_days <= 365
    ):
        raise ValueError("lookback_days must be between 1 and 365")
    if (
        isinstance(limit_per_symbol, bool)
        or not isinstance(limit_per_symbol, int)
        or not 1 <= limit_per_symbol <= 100
    ):
        raise ValueError("limit_per_symbol must be between 1 and 100")
    instruments = {item.symbol: item for item in config.instruments}
    selected = list(symbols) if symbols is not None else list(instruments)
    if not selected or len(selected) > MAX_SYMBOLS or len(set(selected)) != len(selected):
        raise ValueError(f"symbols must contain 1 to {MAX_SYMBOLS} unique items")
    resolved: list[Instrument] = []
    for symbol in selected:
        if not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None:
            raise ValueError("symbol must be a six-digit security code")
        try:
            resolved.append(instruments[symbol])
        except KeyError as exc:
            raise ValueError("symbol must be in the configured security master") from exc

    market_close = str(config.raw.get("data", {}).get("market_close_time", "15:30"))
    cutoff = completed_session_cutoff(as_of, market_close)
    start = cutoff - timedelta(days=lookback_days)
    records: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    coverage: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    master_cache: dict[str, dict[str, str]] = {}

    for instrument in resolved:
        try:
            provider = _provider_for(instrument)
            if provider is None:
                coverage.append(
                    _coverage(
                        instrument,
                        None,
                        "unavailable",
                        "official_market_coverage_unavailable",
                        "Official disclosure coverage is not validated for this market and instrument type.",
                    )
                )
                continue
            if provider == "sse":
                payload, evidence = _download_sse(
                    config,
                    instrument,
                    start=start,
                    end=cutoff,
                    page_size=limit_per_symbol,
                )
                items, total = _parse_sse(
                    payload, instrument, start=start, cutoff=cutoff
                )
            else:
                master_name, plate = _cninfo_master_for(instrument)
                if master_name not in master_cache:
                    master, evidence_item = _download_cninfo_master(
                        config, master_name
                    )
                    master_cache[master_name] = master
                    responses.append(evidence_item)
                org_id = master_cache[master_name].get(instrument.symbol)
                if org_id is None:
                    coverage.append(
                        _coverage(
                            instrument,
                            "cninfo",
                            "unavailable",
                            "cninfo_security_not_registered",
                            "The security is absent from the selected CNINFO official master.",
                        )
                    )
                    continue
                payload, evidence = _download_cninfo_query(
                    config,
                    instrument,
                    org_id=org_id,
                    plate=plate,
                    start=start,
                    end=cutoff,
                    page_size=limit_per_symbol,
                )
                items, total = _parse_cninfo(
                    payload, instrument, start=start, cutoff=cutoff
                )
            records.extend(items)
            responses.append(evidence)
            coverage.append(
                _coverage(
                    instrument,
                    provider,
                    "current" if items else "current_no_records",
                    None,
                    None,
                    returned_count=len(items),
                    provider_total_count=total,
                    truncated=total > len(items),
                )
            )
        except (DisclosureProviderError, OSError, ValueError) as exc:
            errors.append(
                {
                    "symbol": instrument.symbol,
                    "code": "official_disclosure_provider_error",
                    "message": str(exc)[:300],
                }
            )
            coverage.append(
                _coverage(
                    instrument,
                    _provider_for(instrument),
                    "error",
                    "official_disclosure_provider_error",
                    str(exc)[:300],
                )
            )

    if not responses and not records:
        return _unavailable(
            DisclosureQuery(symbol=selected[0] if len(selected) == 1 else None),
            errors
            or [
                {
                    "code": "official_coverage_unavailable",
                    "message": "No selected security has validated official disclosure coverage.",
                }
            ],
            coverage=coverage,
        )
    records.sort(key=lambda item: (item["published_at"], item["disclosure_id"]), reverse=True)
    responses.sort(
        key=lambda item: (
            str(item.get("kind")),
            str(item.get("master_name") or ""),
            str(item.get("symbol") or ""),
        )
    )
    has_gap = bool(errors) or any(
        item["status"] in {"unavailable", "error"} for item in coverage
    )
    draft = {
        "schema_version": SCHEMA_VERSION,
        "dataset": DATASET,
        "available": True,
        "status": "partial" if has_gap else "current",
        "trade_date": cutoff.isoformat(),
        "retrieved_at": _now(),
        "window": {"start": start.isoformat(), "end": cutoff.isoformat()},
        "source": {
            "providers": ["sse", "cninfo"],
            "sse_endpoint": SSE_ENDPOINT,
            "cninfo_query_endpoint": CNINFO_QUERY_ENDPOINT,
            "cninfo_master_root": CNINFO_MASTER_ROOT,
            "certification": "official_exchange_or_designated_disclosure_platform",
            "document_archived": False,
            "responses": responses,
            "response_count": len(responses),
            "response_sha256": _fingerprint(responses),
        },
        "records": records,
        "coverage": coverage,
        "summary": _summary(records, coverage, errors),
        "errors": errors,
        "warnings": [
            {
                "code": "metadata_only",
                "message": "Official metadata and document links are archived; PDF content is not downloaded or WORM-stored.",
            },
            {
                "code": "no_sentiment_inference",
                "message": "Official disclosures remain source evidence and are not converted into a sentiment score.",
            },
            {
                "code": "bounded_window",
                "message": "Each refresh is bounded by the configured date window and per-security result limit.",
            },
        ],
        "authority": dict(_AUTHORITY),
    }
    return DisclosureStore(config).publish(draft)


class DisclosureStore:
    def __init__(self, config_or_root: AppConfig | str | Path):
        if isinstance(config_or_root, AppConfig) or hasattr(
            config_or_root, "disclosures_dir"
        ):
            root = getattr(config_or_root, "disclosures_dir", None)
            if root is None:
                root = config_or_root.resolve("state/disclosures")
        else:
            root = Path(config_or_root)
        self._store = ImmutableDateRevisionStore(
            Path(root),
            DateRevisionSpec(DATASET, "Official disclosures", "disclosures"),
            _validate_payload,
        )

    def publish(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        return self._store.publish(draft)

    def list(self, query: DisclosureQuery | None = None) -> dict[str, Any]:
        selected = query or DisclosureQuery()
        _validate_query(selected)
        latest = self._store.latest(
            selected.trade_date, include_revisions=selected.include_revisions
        )
        if latest is None:
            return _unavailable(selected, [], coverage=[])
        matched = [item for item in latest["records"] if _matches(item, selected)]
        latest["records"] = matched[: selected.limit]
        latest["summary"] = {
            **latest["summary"],
            "matched_count": len(matched),
            "returned_count": len(latest["records"]),
            "truncated": len(matched) > len(latest["records"]),
        }
        latest["filters"] = _query_payload(selected)
        return latest


def _provider_for(instrument: Instrument) -> str | None:
    market = instrument.market.strip().upper()
    instrument_type = instrument.instrument_type.strip().upper()
    if market == "SH" and instrument_type == "STOCK":
        return "sse"
    if market == "SZ" and instrument_type in {"STOCK", "ETF"}:
        return "cninfo"
    return None


def _cninfo_master_for(instrument: Instrument) -> tuple[str, str]:
    if instrument.instrument_type.strip().upper() == "STOCK":
        return "szse_stock.json", "sz"
    return "fund_stock.json", "fund"


def _download_sse(
    config: AppConfig,
    instrument: Instrument,
    *,
    start: date,
    end: date,
    page_size: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    params = {
        "isPagination": "true",
        "productId": instrument.symbol,
        "keyWord": "",
        "securityType": "0101,120100,020100,020200,120200",
        "reportType2": "",
        "reportType": "ALL",
        "beginDate": start.isoformat(),
        "endDate": end.isoformat(),
        "pageHelp.pageSize": str(page_size),
        "pageHelp.pageNo": "1",
        "pageHelp.beginPage": "1",
        "pageHelp.cacheSize": "1",
        "pageHelp.endPage": "1",
    }
    request = urllib.request.Request(
        f"{SSE_ENDPOINT}?{urllib.parse.urlencode(params)}",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.sse.com.cn/",
            "Accept": "application/json, text/plain, */*",
        },
        method="GET",
    )
    payload, digest, response_bytes = _request_json(config, request, "SSE")
    return payload, _response_evidence(
        "query", "sse", digest, response_bytes, symbol=instrument.symbol
    )


def _download_cninfo_master(
    config: AppConfig, master_name: str
) -> tuple[dict[str, str], dict[str, Any]]:
    request = urllib.request.Request(
        f"{CNINFO_MASTER_ROOT}/{master_name}",
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.cninfo.com.cn/",
            "Accept": "application/json, text/plain, */*",
        },
        method="GET",
    )
    payload, digest, response_bytes = _request_json(config, request, "CNINFO master")
    rows = payload.get("stockList")
    if not isinstance(rows, list) or len(rows) > 20_000:
        raise DisclosureProviderError("CNINFO master stockList is invalid")
    result: dict[str, str] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise DisclosureProviderError("CNINFO master row is invalid")
        symbol = str(row.get("code", ""))
        org_id = row.get("orgId")
        if _SYMBOL.fullmatch(symbol) is None or not isinstance(org_id, str):
            raise DisclosureProviderError("CNINFO master identity is invalid")
        if symbol in result or not org_id.strip() or len(org_id) > 100:
            raise DisclosureProviderError("CNINFO master identity is duplicated")
        result[symbol] = org_id.strip()
    evidence = _response_evidence(
        "master", "cninfo", digest, response_bytes, master_name=master_name
    )
    return result, evidence


def _download_cninfo_query(
    config: AppConfig,
    instrument: Instrument,
    *,
    org_id: str,
    plate: str,
    start: date,
    end: date,
    page_size: int,
) -> tuple[dict[str, Any], dict[str, Any]]:
    form = urllib.parse.urlencode(
        {
            "pageNum": "1",
            "pageSize": str(page_size),
            "column": "szse",
            "tabName": "fulltext",
            "plate": plate,
            "stock": f"{instrument.symbol},{org_id}",
            "searchkey": "",
            "secid": "",
            "category": "",
            "trade": "",
            "seDate": f"{start.isoformat()}~{end.isoformat()}",
            "sortName": "",
            "sortType": "",
            "isHLtitle": "true",
        }
    ).encode("ascii")
    request = urllib.request.Request(
        CNINFO_QUERY_ENDPOINT,
        data=form,
        headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.cninfo.com.cn/",
            "Origin": "https://www.cninfo.com.cn",
            "X-Requested-With": "XMLHttpRequest",
            "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
            "Accept": "application/json, text/plain, */*",
        },
        method="POST",
    )
    payload, digest, response_bytes = _request_json(config, request, "CNINFO query")
    return payload, _response_evidence(
        "query", "cninfo", digest, response_bytes, symbol=instrument.symbol
    )


def _request_json(
    config: AppConfig, request: urllib.request.Request, label: str
) -> tuple[dict[str, Any], str, int]:
    data_config = config.raw.get("data", {})
    timeout = int(data_config.get("timeout_seconds", 20))
    attempts = min(3, max(1, int(data_config.get("max_attempts", 3))))
    errors: list[str] = []
    for attempt in range(attempts):
        try:
            with _open_request(request, timeout, _proxy_mode(config)) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise DisclosureProviderError(f"{label} response is too large")
            value = loads_unique_json(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise DisclosureProviderError(f"{label} response is not an object")
            return value, sha256(raw).hexdigest(), len(raw)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt + 1 < attempts:
                time_module.sleep(min(2.0, 0.25 * (2**attempt)))
    raise DisclosureProviderError(f"{label} request failed: {' | '.join(errors)}")


def _parse_sse(
    payload: Mapping[str, Any], instrument: Instrument, *, start: date, cutoff: date
) -> tuple[list[dict[str, Any]], int]:
    page = payload.get("pageHelp")
    if not isinstance(page, dict) or not isinstance(page.get("data"), list):
        raise DisclosureProviderError("SSE pageHelp.data is invalid")
    total = _nonnegative_int(page.get("total", len(page["data"])), "SSE total")
    if total < len(page["data"]):
        raise DisclosureProviderError("SSE total is smaller than the returned page")
    result: list[dict[str, Any]] = []
    for row in page["data"]:
        if not isinstance(row, dict) or str(row.get("SECURITY_CODE")) != instrument.symbol:
            raise DisclosureProviderError("SSE disclosure identity is invalid")
        published = _sse_timestamp(row.get("ADDDATE") or row.get("SSEDATE"))
        relative_url = row.get("URL")
        if not isinstance(relative_url, str) or not relative_url.startswith("/"):
            raise DisclosureProviderError("SSE document URL is invalid")
        document_url = _official_url(SSE_STATIC_ROOT + relative_url)
        effective = _date_value(row.get("SSEDATE"), published.date())
        if published.date() > cutoff or not start <= effective <= cutoff:
            raise DisclosureProviderError("SSE disclosure is outside the requested window")
        result.append(
            {
                "disclosure_id": "sse_" + sha256(document_url.encode()).hexdigest()[:32],
                "symbol": instrument.symbol,
                "name": _text(row.get("SECURITY_NAME") or instrument.name, "name"),
                "market": instrument.market,
                "instrument_type": instrument.instrument_type,
                "title": _text(row.get("TITLE"), "title"),
                "published_at": published.isoformat(),
                "effective_date": effective.isoformat(),
                "category": _optional_text(
                    row.get("BULLETIN_TYPE") or row.get("BULLETIN_HEADING")
                ),
                "document_url": document_url,
                "document_format": "PDF",
                "source_provider": "sse",
                "source_authority": "Shanghai Stock Exchange",
            }
        )
    return result, total


def _parse_cninfo(
    payload: Mapping[str, Any], instrument: Instrument, *, start: date, cutoff: date
) -> tuple[list[dict[str, Any]], int]:
    rows = payload.get("announcements")
    if rows is None:
        rows = []
    if not isinstance(rows, list):
        raise DisclosureProviderError("CNINFO announcements are invalid")
    total = _nonnegative_int(
        payload.get("totalRecordNum", payload.get("totalAnnouncement", len(rows))),
        "CNINFO total",
    )
    if total < len(rows):
        raise DisclosureProviderError("CNINFO total is smaller than the returned page")
    result: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or str(row.get("secCode")) != instrument.symbol:
            raise DisclosureProviderError("CNINFO disclosure identity is invalid")
        announcement_id = str(row.get("announcementId", ""))
        if not announcement_id.isdigit() or len(announcement_id) > 80:
            raise DisclosureProviderError("CNINFO announcement id is invalid")
        timestamp = row.get("announcementTime")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            raise DisclosureProviderError("CNINFO announcement time is invalid")
        if not math.isfinite(float(timestamp)):
            raise DisclosureProviderError("CNINFO announcement time is invalid")
        try:
            published = datetime.fromtimestamp(
                float(timestamp) / 1000, timezone.utc
            ).astimezone(CHINA_TIMEZONE)
        except (OSError, OverflowError, ValueError) as exc:
            raise DisclosureProviderError(
                "CNINFO announcement time is invalid"
            ) from exc
        if not start <= published.date() <= cutoff:
            raise DisclosureProviderError(
                "CNINFO disclosure is outside the requested window"
            )
        adjunct = row.get("adjunctUrl")
        if not isinstance(adjunct, str) or adjunct.startswith(("/", "http")):
            raise DisclosureProviderError("CNINFO document URL is invalid")
        document_url = _official_url(f"{CNINFO_STATIC_ROOT}/{adjunct}")
        title = html.unescape(
            re.sub(r"</?em>", "", str(row.get("announcementTitle") or ""), flags=re.I)
        )
        result.append(
            {
                "disclosure_id": f"cninfo_{announcement_id}",
                "symbol": instrument.symbol,
                "name": _text(row.get("secName") or instrument.name, "name"),
                "market": instrument.market,
                "instrument_type": instrument.instrument_type,
                "title": _text(title, "title"),
                "published_at": published.isoformat(),
                "effective_date": published.date().isoformat(),
                "category": _optional_text(row.get("announcementTypeName")),
                "document_url": document_url,
                "document_format": str(row.get("adjunctType") or "PDF")[:20].upper(),
                "source_provider": "cninfo",
                "source_authority": "CNINFO designated disclosure platform",
            }
        )
    return result, total


def _coverage(
    instrument: Instrument,
    provider: str | None,
    status: str,
    reason: str | None,
    message: str | None,
    *,
    returned_count: int = 0,
    provider_total_count: int = 0,
    truncated: bool = False,
) -> dict[str, Any]:
    return {
        "symbol": instrument.symbol,
        "name": instrument.name,
        "market": instrument.market,
        "instrument_type": instrument.instrument_type,
        "provider": provider,
        "status": status,
        "reason": reason,
        "message": message,
        "returned_count": returned_count,
        "provider_total_count": provider_total_count,
        "truncated": truncated,
    }


def _response_evidence(
    kind: str,
    provider: str,
    digest: str,
    response_bytes: int,
    *,
    symbol: str | None = None,
    master_name: str | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "provider": provider,
        "symbol": symbol,
        "master_name": master_name,
        "response_sha256": digest,
        "response_bytes": response_bytes,
    }


def _validate_payload(value: Mapping[str, Any]) -> None:
    if value.get("schema_version") != SCHEMA_VERSION or value.get("dataset") != DATASET:
        raise RuntimeError("official disclosure schema is invalid")
    try:
        trade_date = date.fromisoformat(str(value["trade_date"]))
        window = value["window"]
        start = date.fromisoformat(str(window["start"]))
        end = date.fromisoformat(str(window["end"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("official disclosure window is invalid") from exc
    if end != trade_date or start > end:
        raise RuntimeError("official disclosure window does not match trade_date")
    records = value.get("records")
    coverage = value.get("coverage")
    if not isinstance(records, list) or len(records) > MAX_RECORDS:
        raise RuntimeError("official disclosure records are invalid")
    if not isinstance(coverage, list) or len(coverage) > MAX_SYMBOLS:
        raise RuntimeError("official disclosure coverage is invalid")
    seen: set[str] = set()
    for item in records:
        if not isinstance(item, dict):
            raise RuntimeError("official disclosure record is invalid")
        disclosure_id = str(item.get("disclosure_id", ""))
        if _DISCLOSURE_ID.fullmatch(disclosure_id) is None or disclosure_id in seen:
            raise RuntimeError("official disclosure id is invalid or duplicated")
        seen.add(disclosure_id)
        if _SYMBOL.fullmatch(str(item.get("symbol", ""))) is None:
            raise RuntimeError("official disclosure symbol is invalid")
        _text(item.get("title"), "title")
        _text(item.get("name"), "name")
        published = _timestamp(item.get("published_at")).astimezone(CHINA_TIMEZONE)
        effective = _date_value(item.get("effective_date"), None)
        if published.date() > end or not start <= effective <= end:
            raise RuntimeError("official disclosure record is outside its window")
        _official_url(item.get("document_url"))
        if item.get("source_provider") not in {"sse", "cninfo"}:
            raise RuntimeError("official disclosure source is invalid")
    coverage_symbols: set[str] = set()
    for item in coverage:
        if not isinstance(item, dict) or _SYMBOL.fullmatch(
            str(item.get("symbol", ""))
        ) is None:
            raise RuntimeError("official disclosure coverage row is invalid")
        if item["symbol"] in coverage_symbols:
            raise RuntimeError("official disclosure coverage is duplicated")
        coverage_symbols.add(item["symbol"])
        if item.get("status") not in {
            "current",
            "current_no_records",
            "unavailable",
            "error",
        }:
            raise RuntimeError("official disclosure coverage status is invalid")
    source = value.get("source")
    if not isinstance(source, dict) or source.get("certification") != (
        "official_exchange_or_designated_disclosure_platform"
    ):
        raise RuntimeError("official disclosure source metadata is invalid")
    responses = source.get("responses")
    if not isinstance(responses, list) or source.get("response_count") != len(responses):
        raise RuntimeError("official disclosure responses are invalid")
    if source.get("response_sha256") != _fingerprint(responses):
        raise RuntimeError("official disclosure response fingerprint is invalid")
    for item in responses:
        if not isinstance(item, dict) or _FINGERPRINT.fullmatch(
            str(item.get("response_sha256", ""))
        ) is None:
            raise RuntimeError("official disclosure response evidence is invalid")
        size = item.get("response_bytes")
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_RESPONSE_BYTES:
            raise RuntimeError("official disclosure response size is invalid")
    if value.get("authority") != _AUTHORITY:
        raise RuntimeError("official disclosure authority is invalid")


def _validate_query(query: DisclosureQuery) -> None:
    if query.symbol is not None and _SYMBOL.fullmatch(query.symbol) is None:
        raise ValueError("symbol must be a six-digit security code")
    if query.provider not in {"all", "sse", "cninfo"}:
        raise ValueError("provider must be all, sse, or cninfo")
    if query.q is not None and (not isinstance(query.q, str) or len(query.q) > 200):
        raise ValueError("q is too long")
    if isinstance(query.limit, bool) or not isinstance(query.limit, int) or not 1 <= query.limit <= MAX_QUERY_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_QUERY_LIMIT}")


def _matches(item: Mapping[str, Any], query: DisclosureQuery) -> bool:
    if query.symbol is not None and item.get("symbol") != query.symbol:
        return False
    if query.provider != "all" and item.get("source_provider") != query.provider:
        return False
    if query.q:
        needle = query.q.casefold()
        return needle in str(item.get("title", "")).casefold()
    return True


def _summary(
    records: Sequence[Mapping[str, Any]],
    coverage: Sequence[Mapping[str, Any]],
    errors: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "record_count": len(records),
        "symbol_count": len({item["symbol"] for item in records}),
        "sse_count": sum(item.get("source_provider") == "sse" for item in records),
        "cninfo_count": sum(
            item.get("source_provider") == "cninfo" for item in records
        ),
        "covered_symbol_count": sum(
            item.get("status") in {"current", "current_no_records"}
            for item in coverage
        ),
        "coverage_gap_count": sum(
            item.get("status") in {"unavailable", "error"} for item in coverage
        ),
        "error_count": len(errors),
    }


def _unavailable(
    query: DisclosureQuery,
    errors: Sequence[Mapping[str, Any]],
    *,
    coverage: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": DATASET,
        "available": False,
        "status": "unavailable",
        "trade_date": query.trade_date.isoformat() if query.trade_date else None,
        "records": [],
        "coverage": list(coverage),
        "summary": {"record_count": 0, "returned_count": 0},
        "errors": list(errors)
        or [
            {
                "code": "official_disclosures_not_refreshed",
                "message": "No validated local official disclosure snapshot is available.",
            }
        ],
        "warnings": [],
        "authority": dict(_AUTHORITY),
        "filters": _query_payload(query),
        "revisions": [],
    }


def _query_payload(query: DisclosureQuery) -> dict[str, Any]:
    return {
        "trade_date": query.trade_date.isoformat() if query.trade_date else None,
        "symbol": query.symbol,
        "provider": query.provider,
        "q": query.q,
        "limit": query.limit,
    }


def _sse_timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise DisclosureProviderError("SSE disclosure time is invalid")
    for pattern in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return datetime.strptime(value[:19], pattern).replace(tzinfo=CHINA_TIMEZONE)
        except ValueError:
            continue
    raise DisclosureProviderError("SSE disclosure time is invalid")


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError("official disclosure timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError("official disclosure timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise RuntimeError("official disclosure timestamp lacks a timezone")
    return parsed


def _date_value(value: Any, fallback: date | None) -> date:
    if value in {None, ""} and fallback is not None:
        return fallback
    if not isinstance(value, str) or len(value) < 10:
        raise DisclosureProviderError("official disclosure date is invalid")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise DisclosureProviderError("official disclosure date is invalid") from exc


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise DisclosureProviderError(f"official disclosure {label} is invalid")
    result = value.strip()
    if not result or len(result) > MAX_TEXT or any(ord(char) < 32 for char in result):
        raise DisclosureProviderError(f"official disclosure {label} is invalid")
    return result


def _optional_text(value: Any) -> str:
    if value in {None, ""}:
        return ""
    return _text(str(value), "category")


def _official_url(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 2_048:
        raise DisclosureProviderError("official disclosure URL is invalid")
    parsed = urllib.parse.urlsplit(value)
    host = (parsed.hostname or "").lower().rstrip(".")
    if parsed.scheme != "https" or host not in {
        "static.sse.com.cn",
        "static.cninfo.com.cn",
    }:
        raise DisclosureProviderError("official disclosure URL is not allowlisted")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise DisclosureProviderError(f"{label} is invalid")
    number = int(value)
    if number < 0 or not math.isfinite(float(value)) or number != value:
        raise DisclosureProviderError(f"{label} is invalid")
    return number


def _fingerprint(value: Any) -> str:
    import json

    return sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "DisclosureProviderError",
    "DisclosureQuery",
    "DisclosureStore",
    "refresh_disclosures",
]
