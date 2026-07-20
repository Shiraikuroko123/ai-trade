"""Point-in-time company fundamental evidence from Eastmoney Data Center."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
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
from .eastmoney import REQUEST_HEADERS, _open_request, _proxy_mode, completed_session_cutoff
from .evidence_io import DateRevisionSpec, ImmutableDateRevisionStore


SCHEMA_VERSION = 1
DATASET = "fundamentals"
ENDPOINT = "https://datacenter-web.eastmoney.com/api/data/v1/get"
REPORT_NAME = "RPT_LICO_FN_CPD"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_SYMBOLS = 100
MAX_PERIODS_PER_SYMBOL = 20
MAX_QUERY_LIMIT = 500
CHINA_TIMEZONE = timezone(timedelta(hours=8))
_SYMBOL = re.compile(r"\d{6}\Z")
_AUTHORITY = {"research_only": True, "execution_authorized": False}
_SOURCE_FIELDS = (
    "SECURITY_CODE",
    "SECURITY_NAME_ABBR",
    "REPORTDATE",
    "NOTICE_DATE",
    "UPDATE_DATE",
    "DATATYPE",
    "BASIC_EPS",
    "TOTAL_OPERATE_INCOME",
    "PARENT_NETPROFIT",
    "WEIGHTAVG_ROE",
    "YSTZ",
    "SJLTZ",
    "BPS",
    "MGJYXJJE",
    "XSMLL",
)


@dataclass(frozen=True)
class FundamentalQuery:
    trade_date: date | None = None
    symbol: str | None = None
    limit: int = 100
    include_revisions: bool = False


class FundamentalProviderError(RuntimeError):
    """Raised when a fundamental response fails its evidence contract."""


def refresh_fundamentals(
    config: AppConfig,
    *,
    symbols: Sequence[str] | None = None,
    as_of: datetime | None = None,
    periods_per_symbol: int = 8,
) -> dict[str, Any]:
    if (
        isinstance(periods_per_symbol, bool)
        or not isinstance(periods_per_symbol, int)
        or not 1 <= periods_per_symbol <= MAX_PERIODS_PER_SYMBOL
    ):
        raise ValueError(
            f"periods_per_symbol must be between 1 and {MAX_PERIODS_PER_SYMBOL}"
        )
    instruments = {item.symbol: item for item in config.instruments}
    selected = list(symbols) if symbols is not None else list(instruments)
    if not selected or len(selected) > MAX_SYMBOLS or len(set(selected)) != len(selected):
        raise ValueError(f"symbols must contain 1 to {MAX_SYMBOLS} unique items")
    market_close = str(config.raw.get("data", {}).get("market_close_time", "15:30"))
    cutoff = completed_session_cutoff(as_of, market_close)
    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    responses: list[dict[str, Any]] = []
    for symbol in selected:
        if not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None:
            raise ValueError("symbol must be a six-digit security code")
        try:
            instrument = instruments[symbol]
        except KeyError as exc:
            raise ValueError("symbol must be in the configured security master") from exc
        if instrument.instrument_type.strip().upper() != "STOCK":
            errors.append(
                {
                    "symbol": symbol,
                    "code": "instrument_type_not_supported",
                    "message": "Company fundamentals apply only to STOCK instruments.",
                }
            )
            continue
        try:
            payload, digest, response_bytes = _download(
                config, instrument, periods_per_symbol
            )
            periods = _parse_payload(
                payload,
                instrument,
                cutoff=cutoff,
                limit=periods_per_symbol,
            )
            if not periods:
                raise FundamentalProviderError(
                    "no disclosed periods were available by the completed-session cutoff"
                )
            records.append(
                {
                    "symbol": symbol,
                    "name": instrument.name,
                    "market": instrument.market,
                    "instrument_type": instrument.instrument_type,
                    "latest_report_date": periods[0]["report_date"],
                    "latest_notice_date": periods[0]["notice_date"],
                    "periods": periods,
                    "response_sha256": digest,
                    "response_bytes": response_bytes,
                }
            )
            responses.append(
                {
                    "symbol": symbol,
                    "response_sha256": digest,
                    "response_bytes": response_bytes,
                }
            )
        except (FundamentalProviderError, OSError, ValueError) as exc:
            errors.append(
                {
                    "symbol": symbol,
                    "code": "fundamental_provider_error",
                    "message": str(exc)[:300],
                }
            )
    query = FundamentalQuery(symbol=selected[0] if len(selected) == 1 else None)
    if not records:
        return _unavailable(query, errors)
    records.sort(key=lambda item: item["symbol"])
    record = {
        "schema_version": SCHEMA_VERSION,
        "dataset": DATASET,
        "available": True,
        "status": "partial" if errors else "current",
        "trade_date": cutoff.isoformat(),
        "retrieved_at": _now(),
        "source": {
            "provider": "eastmoney",
            "endpoint": ENDPOINT,
            "report_name": REPORT_NAME,
            "fields": list(_SOURCE_FIELDS),
            "response_sha256": _fingerprint(responses),
            "response_count": len(responses),
            "certification": "third_party_not_exchange_certified",
            "point_in_time_filter": "NOTICE_DATE and UPDATE_DATE <= trade_date",
        },
        "records": records,
        "summary": {
            "requested_count": len(selected),
            "returned_count": len(records),
            "unsupported_count": sum(
                item["code"] == "instrument_type_not_supported" for item in errors
            ),
            "error_count": len(errors),
            "period_count": sum(len(item["periods"]) for item in records),
        },
        "errors": errors,
        "warnings": [
            {
                "code": "single_third_party_source",
                "message": "Financial fields are third-party normalized disclosures and have no independent source reconciliation.",
            },
            {
                "code": "stock_only",
                "message": "Company fundamentals are not inferred for ETFs, indexes, bonds, or commodities.",
            },
        ],
        "authority": dict(_AUTHORITY),
    }
    return FundamentalStore(config).publish(record)


class FundamentalStore:
    def __init__(self, config_or_root: AppConfig | str | Path):
        if isinstance(config_or_root, AppConfig) or hasattr(
            config_or_root, "fundamentals_dir"
        ):
            root = getattr(config_or_root, "fundamentals_dir", None)
            if root is None:
                root = config_or_root.resolve("state/fundamentals")
        else:
            root = Path(config_or_root)
        self._store = ImmutableDateRevisionStore(
            Path(root),
            DateRevisionSpec(DATASET, "Fundamentals", "fundamentals"),
            _validate_payload,
        )

    def publish(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        return self._store.publish(draft)

    def list(self, query: FundamentalQuery | None = None) -> dict[str, Any]:
        selected = query or FundamentalQuery()
        _validate_query(selected)
        latest = self._store.latest(
            selected.trade_date, include_revisions=selected.include_revisions
        )
        if latest is None:
            return _unavailable(selected, [])
        records = list(latest["records"])
        if selected.symbol:
            records = [item for item in records if item["symbol"] == selected.symbol]
        latest["records"] = records[: selected.limit]
        latest["summary"] = {
            **latest["summary"],
            "matched_count": len(records),
            "returned_count": len(latest["records"]),
            "truncated": len(records) > len(latest["records"]),
        }
        latest["filters"] = {
            "trade_date": selected.trade_date.isoformat()
            if selected.trade_date
            else None,
            "symbol": selected.symbol,
            "limit": selected.limit,
        }
        return latest


def _download(
    config: AppConfig, instrument: Instrument, page_size: int
) -> tuple[dict[str, Any], str, int]:
    params = {
        "sortColumns": "REPORTDATE",
        "sortTypes": "-1",
        "pageSize": str(page_size),
        "pageNumber": "1",
        "reportName": REPORT_NAME,
        "columns": "ALL",
        "filter": f'(SECURITY_CODE="{instrument.symbol}")',
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
                raise FundamentalProviderError("fundamental response is too large")
            value = loads_unique_json(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise FundamentalProviderError("fundamental response is not an object")
            return value, sha256(raw).hexdigest(), len(raw)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt + 1 < attempts:
                time_module.sleep(min(2.0, 0.25 * (2**attempt)))
    raise FundamentalProviderError(
        "fundamental download failed: " + " | ".join(errors)
    )


def _parse_payload(
    payload: Mapping[str, Any],
    instrument: Instrument,
    *,
    cutoff: date,
    limit: int,
) -> list[dict[str, Any]]:
    if payload.get("success") is not True or payload.get("code") != 0:
        raise FundamentalProviderError(
            f"fundamental provider rejected the request: {payload.get('message')}"
        )
    result = payload.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("data"), list):
        raise FundamentalProviderError("fundamental result data is invalid")
    rows = result["data"]
    if len(rows) > MAX_PERIODS_PER_SYMBOL:
        raise FundamentalProviderError("fundamental response exceeds the row limit")
    by_period: dict[date, tuple[date, dict[str, Any]]] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("SECURITY_CODE") != instrument.symbol:
            raise FundamentalProviderError("fundamental row identity is invalid")
        report_date = _date_field(row.get("REPORTDATE"), "REPORTDATE")
        notice_date = _date_field(row.get("NOTICE_DATE"), "NOTICE_DATE")
        update_date = _date_field(
            row.get("UPDATE_DATE") or row.get("NOTICE_DATE"), "UPDATE_DATE"
        )
        if report_date > cutoff or notice_date > cutoff or update_date > cutoff:
            continue
        normalized = {
            "report_date": report_date.isoformat(),
            "notice_date": notice_date.isoformat(),
            "update_date": update_date.isoformat(),
            "report_type": _text(row.get("DATATYPE"), "DATATYPE", 100),
            "basic_eps": _optional_number(row.get("BASIC_EPS")),
            "revenue": _optional_number(row.get("TOTAL_OPERATE_INCOME")),
            "parent_net_profit": _optional_number(row.get("PARENT_NETPROFIT")),
            "weighted_roe_pct": _optional_number(row.get("WEIGHTAVG_ROE")),
            "revenue_yoy_pct": _optional_number(row.get("YSTZ")),
            "net_profit_yoy_pct": _optional_number(row.get("SJLTZ")),
            "book_value_per_share": _optional_number(row.get("BPS")),
            "operating_cash_flow_per_share": _optional_number(row.get("MGJYXJJE")),
            "gross_margin_pct": _optional_number(row.get("XSMLL")),
        }
        previous = by_period.get(report_date)
        if previous is None or update_date > previous[0]:
            by_period[report_date] = (update_date, normalized)
    return [
        value[1]
        for _, value in sorted(by_period.items(), reverse=True)[:limit]
    ]


def _validate_payload(value: Mapping[str, Any]) -> None:
    if value.get("schema_version") != SCHEMA_VERSION or value.get("dataset") != DATASET:
        raise RuntimeError("fundamental record schema is invalid")
    try:
        trade_date = date.fromisoformat(str(value["trade_date"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("fundamental trade_date is invalid") from exc
    records = value.get("records")
    if not isinstance(records, list) or not records or len(records) > MAX_SYMBOLS:
        raise RuntimeError("fundamental records are invalid")
    seen: set[str] = set()
    for item in records:
        if not isinstance(item, dict) or _SYMBOL.fullmatch(str(item.get("symbol", ""))) is None:
            raise RuntimeError("fundamental symbol is invalid")
        if item["symbol"] in seen or item.get("instrument_type") != "STOCK":
            raise RuntimeError("fundamental symbol identity is invalid")
        seen.add(item["symbol"])
        periods = item.get("periods")
        if not isinstance(periods, list) or not periods or len(periods) > MAX_PERIODS_PER_SYMBOL:
            raise RuntimeError("fundamental periods are invalid")
        dates = [date.fromisoformat(str(period["report_date"])) for period in periods]
        if dates != sorted(dates, reverse=True) or len(dates) != len(set(dates)):
            raise RuntimeError("fundamental report dates are invalid")
        for report_date, period in zip(dates, periods, strict=True):
            notice_date = date.fromisoformat(str(period["notice_date"]))
            update_date = date.fromisoformat(str(period["update_date"]))
            if max(report_date, notice_date, update_date) > trade_date:
                raise RuntimeError("fundamental period exceeds the completed cutoff")
            for field in (
                "basic_eps",
                "revenue",
                "parent_net_profit",
                "weighted_roe_pct",
                "revenue_yoy_pct",
                "net_profit_yoy_pct",
                "book_value_per_share",
                "operating_cash_flow_per_share",
                "gross_margin_pct",
            ):
                number = period.get(field)
                if number is not None and (
                    isinstance(number, bool)
                    or not isinstance(number, (int, float))
                    or not math.isfinite(float(number))
                ):
                    raise RuntimeError(f"fundamental field {field} is invalid")
    source = value.get("source")
    if not isinstance(source, dict) or source.get("report_name") != REPORT_NAME:
        raise RuntimeError("fundamental source metadata is invalid")
    responses = [
        {
            "symbol": item["symbol"],
            "response_sha256": item.get("response_sha256"),
            "response_bytes": item.get("response_bytes"),
        }
        for item in records
    ]
    for response in responses:
        if not re.fullmatch(r"[0-9a-f]{64}", str(response["response_sha256"])):
            raise RuntimeError("fundamental response fingerprint is invalid")
        size = response["response_bytes"]
        if isinstance(size, bool) or not isinstance(size, int) or not 0 <= size <= MAX_RESPONSE_BYTES:
            raise RuntimeError("fundamental response size is invalid")
    if source.get("response_count") != len(responses) or source.get(
        "response_sha256"
    ) != _fingerprint(responses):
        raise RuntimeError("fundamental response evidence is invalid")
    if value.get("authority") != _AUTHORITY:
        raise RuntimeError("fundamental authority is invalid")


def _validate_query(query: FundamentalQuery) -> None:
    if query.trade_date is not None and (
        not isinstance(query.trade_date, date)
        or isinstance(query.trade_date, datetime)
    ):
        raise ValueError("trade_date must be a date")
    if query.symbol is not None and _SYMBOL.fullmatch(query.symbol) is None:
        raise ValueError("symbol must be a six-digit security code")
    if isinstance(query.limit, bool) or not 1 <= query.limit <= MAX_QUERY_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_QUERY_LIMIT}")


def _date_field(value: object, label: str) -> date:
    if not isinstance(value, str) or len(value) < 10:
        raise FundamentalProviderError(f"{label} is invalid")
    try:
        return date.fromisoformat(value[:10])
    except ValueError as exc:
        raise FundamentalProviderError(f"{label} is invalid") from exc


def _optional_number(value: object) -> float | None:
    if value is None or value == "" or value == "-":
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FundamentalProviderError("fundamental numeric field is invalid")
    result = float(value)
    if not math.isfinite(result):
        raise FundamentalProviderError("fundamental numeric field is not finite")
    return result


def _text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > maximum:
        raise FundamentalProviderError(f"{label} is invalid")
    return value.strip()


def _unavailable(
    query: FundamentalQuery, errors: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": DATASET,
        "available": False,
        "status": "unavailable",
        "trade_date": query.trade_date.isoformat() if query.trade_date else None,
        "records": [],
        "summary": {"returned_count": 0, "error_count": len(errors)},
        "errors": list(errors)
        or [
            {
                "code": "fundamentals_not_refreshed",
                "message": "No validated local fundamental snapshot is available.",
            }
        ],
        "warnings": [],
        "authority": dict(_AUTHORITY),
        "filters": {
            "trade_date": query.trade_date.isoformat() if query.trade_date else None,
            "symbol": query.symbol,
            "limit": query.limit,
        },
        "revisions": [],
    }


def _fingerprint(value: Any) -> str:
    import json

    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "FundamentalProviderError",
    "FundamentalQuery",
    "FundamentalStore",
    "refresh_fundamentals",
]
