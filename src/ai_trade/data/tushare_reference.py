"""Bounded Tushare Pro reference datasets for research-only reconciliation.

These helpers never publish strategy-visible market data.  They normalize
independent financial, valuation and news responses so the owning evidence
store can bind them into its own immutable revision.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
import os
import time as time_module
from typing import Any, Mapping, Sequence
import urllib.request

from ..config import AppConfig
from ..json_utils import loads_unique_json
from ..models import Instrument
from .eastmoney import _open_request, _proxy_mode
from .tushare import ENDPOINT, TOKEN_ENV


MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_REFERENCE_ROWS = 200
CHINA_TIMEZONE = timezone(timedelta(hours=8))
_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
    "User-Agent": "ai-trade/0.17 research-only reference check",
}


class TushareReferenceError(RuntimeError):
    """Raised when an optional Tushare reference response is unusable."""


def token_configured() -> bool:
    return bool(os.getenv(TOKEN_ENV, "").strip())


def fetch_fundamental_reference(
    config: AppConfig,
    instrument: Instrument,
    *,
    cutoff: date,
    limit: int = 20,
) -> dict[str, Any]:
    """Fetch point-in-time financial fields without publishing primary data."""

    if instrument.instrument_type.strip().upper() != "STOCK":
        raise TushareReferenceError("fundamental reference supports STOCK only")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
        raise ValueError("fundamental reference limit must be between 1 and 20")
    ts_code = _ts_code(instrument)
    start = cutoff - timedelta(days=1_500)
    common_params = {
        "ts_code": ts_code,
        "start_date": start.strftime("%Y%m%d"),
        "end_date": cutoff.strftime("%Y%m%d"),
        "limit": limit,
    }
    indicator_rows, indicator_evidence = request_table(
        config,
        "fina_indicator",
        common_params,
        (
            "ts_code",
            "ann_date",
            "end_date",
            "eps",
            "roe",
            "grossprofit_margin",
            "bps",
            "ocfps",
        ),
        maximum_rows=limit,
    )
    income_rows, income_evidence = request_table(
        config,
        "income",
        {**common_params, "report_type": "1"},
        (
            "ts_code",
            "ann_date",
            "f_ann_date",
            "end_date",
            "revenue",
            "n_income_attr_p",
        ),
        maximum_rows=limit,
    )
    indicators = _latest_financial_rows(
        indicator_rows, ts_code=ts_code, cutoff=cutoff, announcement_fields=("ann_date",)
    )
    income = _latest_financial_rows(
        income_rows,
        ts_code=ts_code,
        cutoff=cutoff,
        announcement_fields=("f_ann_date", "ann_date"),
    )
    periods: list[dict[str, Any]] = []
    for period in sorted(set(indicators) | set(income), reverse=True)[:limit]:
        indicator = indicators.get(period, {})
        income_row = income.get(period, {})
        notice_dates = [
            value
            for value in (
                _compact_date_or_none(indicator.get("ann_date")),
                _compact_date_or_none(income_row.get("f_ann_date")),
                _compact_date_or_none(income_row.get("ann_date")),
            )
            if value is not None
        ]
        periods.append(
            {
                "report_date": period.isoformat(),
                "notice_date": max(notice_dates).isoformat() if notice_dates else None,
                "basic_eps": _optional_number(indicator.get("eps")),
                "revenue": _optional_number(income_row.get("revenue")),
                "parent_net_profit": _optional_number(income_row.get("n_income_attr_p")),
                "weighted_roe_pct": _optional_number(indicator.get("roe")),
                "book_value_per_share": _optional_number(indicator.get("bps")),
                "operating_cash_flow_per_share": _optional_number(indicator.get("ocfps")),
                "gross_margin_pct": _optional_number(
                    indicator.get("grossprofit_margin")
                ),
            }
        )
    return {
        "provider": "tushare",
        "dataset": "fina_indicator+income",
        "periods": periods,
        "responses": [indicator_evidence, income_evidence],
        "response_sha256": _fingerprint([indicator_evidence, income_evidence]),
    }


def fetch_valuation_reference(
    config: AppConfig,
    instrument: Instrument,
    *,
    trade_date: date,
) -> dict[str, Any]:
    """Fetch one exact-date Tushare daily_basic valuation row."""

    if instrument.instrument_type.strip().upper() != "STOCK":
        raise TushareReferenceError("valuation reference supports STOCK only")
    ts_code = _ts_code(instrument)
    compact_date = trade_date.strftime("%Y%m%d")
    rows, evidence = request_table(
        config,
        "daily_basic",
        {"ts_code": ts_code, "trade_date": compact_date, "limit": 2},
        ("ts_code", "trade_date", "pe_ttm", "pb", "ps_ttm"),
        maximum_rows=2,
    )
    matching = [
        row
        for row in rows
        if row.get("ts_code") == ts_code
        and _compact_date(row.get("trade_date"), "trade_date") == trade_date
    ]
    if len(matching) != 1:
        raise TushareReferenceError(
            "daily_basic must return exactly one row for the completed session"
        )
    row = matching[0]
    return {
        "provider": "tushare",
        "dataset": "daily_basic",
        "trade_date": trade_date.isoformat(),
        "pe_ttm": _optional_number(row.get("pe_ttm")),
        "pb": _optional_number(row.get("pb")),
        "ps_ttm": _optional_number(row.get("ps_ttm")),
        "responses": [evidence],
        "response_sha256": _fingerprint([evidence]),
    }


def fetch_news_reference(
    config: AppConfig,
    *,
    snapshot_date: date,
    sources: Sequence[str],
    limit_per_source: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, str]]]:
    """Fetch bounded editorial feeds exposed by Tushare's news endpoint."""

    records: list[dict[str, Any]] = []
    responses: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    for source in sources:
        try:
            rows, evidence = request_table(
                config,
                "news",
                {
                    "src": source,
                    "start_date": f"{snapshot_date.isoformat()} 00:00:00",
                    "end_date": f"{snapshot_date.isoformat()} 23:59:59",
                    "limit": limit_per_source,
                },
                ("datetime", "content", "title", "channels"),
                maximum_rows=limit_per_source,
            )
            evidence["editorial_source"] = source
            responses.append(evidence)
            for index, row in enumerate(rows):
                published = _news_timestamp(row.get("datetime"))
                if published.date() != snapshot_date:
                    raise TushareReferenceError(
                        "Tushare news row falls outside the requested date"
                    )
                title = _bounded_text(row.get("title"), "news title", 2_000)
                content = _bounded_text(
                    row.get("content") or title, "news content", 20_000
                )
                records.append(
                    {
                        "source_id": (
                            f"tushare:{source}:"
                            + sha256(
                                f"{published.isoformat()}\n{title}\n{index}".encode(
                                    "utf-8"
                                )
                            ).hexdigest()[:32]
                        ),
                        "title": title,
                        "summary": content[:2_000],
                        "published_at": published.isoformat(),
                        "editorial_source": source,
                        "channels": _bounded_optional_text(row.get("channels"), 500),
                    }
                )
        except (OSError, TushareReferenceError, TypeError, ValueError) as exc:
            errors.append(
                {
                    "source": f"tushare_news:{source}",
                    "code": "tushare_news_provider_error",
                    "message": str(exc)[:300],
                }
            )
    return records, responses, errors


def request_table(
    config: AppConfig,
    api_name: str,
    params: Mapping[str, Any],
    fields: Sequence[str],
    *,
    maximum_rows: int = MAX_REFERENCE_ROWS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """POST one bounded Tushare table request and validate its row shape."""

    token = os.getenv(TOKEN_ENV, "").strip()
    if not token:
        raise TushareReferenceError(f"{TOKEN_ENV} is not configured")
    if not api_name.isascii() or not api_name.replace("_", "").isalnum():
        raise ValueError("Tushare api_name is invalid")
    requested_fields = list(fields)
    if (
        not requested_fields
        or len(set(requested_fields)) != len(requested_fields)
        or any(not isinstance(item, str) or not item for item in requested_fields)
    ):
        raise ValueError("Tushare fields are invalid")
    if (
        isinstance(maximum_rows, bool)
        or not isinstance(maximum_rows, int)
        or not 1 <= maximum_rows <= MAX_REFERENCE_ROWS
    ):
        raise ValueError("Tushare maximum_rows is invalid")
    body = json.dumps(
        {
            "api_name": api_name,
            "token": token,
            "params": dict(params),
            "fields": ",".join(requested_fields),
        },
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    request = urllib.request.Request(ENDPOINT, data=body, headers=_HEADERS, method="POST")
    data_config = config.raw.get("data", {})
    timeout = min(60, max(1, int(data_config.get("timeout_seconds", 20))))
    attempts = min(3, max(1, int(data_config.get("max_attempts", 3))))
    errors: list[str] = []
    for attempt in range(attempts):
        try:
            with _open_request(request, timeout, _proxy_mode(config)) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise TushareReferenceError("Tushare reference response is too large")
            payload = loads_unique_json(raw.decode("utf-8"))
            rows = _table_rows(
                payload,
                requested_fields=requested_fields,
                maximum_rows=maximum_rows,
            )
            return rows, {
                "provider": "tushare",
                "api_name": api_name,
                "response_sha256": sha256(raw).hexdigest(),
                "response_bytes": len(raw),
                "row_count": len(rows),
            }
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt + 1 < attempts:
                time_module.sleep(min(2.0, 0.25 * (2**attempt)))
    raise TushareReferenceError(
        "Tushare reference request failed: " + " | ".join(errors)
    )


def _table_rows(
    payload: Any, *, requested_fields: Sequence[str], maximum_rows: int
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise TushareReferenceError("Tushare response is not an object")
    code = payload.get("code")
    if isinstance(code, bool) or code != 0:
        raise TushareReferenceError(
            f"Tushare rejected the reference request with code={code!r}"
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise TushareReferenceError("Tushare response data is invalid")
    response_fields = data.get("fields")
    items = data.get("items")
    if (
        not isinstance(response_fields, list)
        or not all(isinstance(item, str) for item in response_fields)
        or len(response_fields) != len(set(response_fields))
        or not set(requested_fields).issubset(response_fields)
        or not isinstance(items, list)
        or len(items) > maximum_rows
    ):
        raise TushareReferenceError("Tushare response table shape is invalid")
    rows: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, list) or len(item) != len(response_fields):
            raise TushareReferenceError("Tushare response row shape is invalid")
        rows.append(dict(zip(response_fields, item, strict=True)))
    return rows


def _latest_financial_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    ts_code: str,
    cutoff: date,
    announcement_fields: Sequence[str],
) -> dict[date, dict[str, Any]]:
    result: dict[date, tuple[date, dict[str, Any]]] = {}
    for source in rows:
        row = dict(source)
        if row.get("ts_code") != ts_code:
            raise TushareReferenceError("Tushare financial symbol does not match")
        period = _compact_date(row.get("end_date"), "end_date")
        notices = [
            parsed
            for field in announcement_fields
            if (parsed := _compact_date_or_none(row.get(field))) is not None
        ]
        if not notices:
            raise TushareReferenceError("Tushare financial announcement date is missing")
        notice = max(notices)
        if period > cutoff or notice > cutoff:
            continue
        prior = result.get(period)
        if prior is None or notice > prior[0]:
            result[period] = (notice, row)
    return {key: value[1] for key, value in result.items()}


def _ts_code(instrument: Instrument) -> str:
    market = instrument.market.strip().upper()
    if market not in {"SH", "SZ", "BJ"}:
        raise TushareReferenceError("Tushare reference market is unsupported")
    return f"{instrument.symbol}.{market}"


def _compact_date(value: Any, label: str) -> date:
    if not isinstance(value, str) or len(value) != 8 or not value.isascii() or not value.isdigit():
        raise TushareReferenceError(f"Tushare {label} is invalid")
    try:
        return datetime.strptime(value, "%Y%m%d").date()
    except ValueError as exc:
        raise TushareReferenceError(f"Tushare {label} is invalid") from exc


def _compact_date_or_none(value: Any) -> date | None:
    if value in {None, ""}:
        return None
    return _compact_date(value, "announcement date")


def _news_timestamp(value: Any) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise TushareReferenceError("Tushare news datetime is invalid")
    text = value.strip().replace("Z", "+00:00")
    parsed: datetime | None = None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for pattern in ("%Y-%m-%d %H:%M:%S", "%Y%m%d %H:%M:%S"):
            try:
                parsed = datetime.strptime(text, pattern)
                break
            except ValueError:
                continue
    if parsed is None:
        raise TushareReferenceError("Tushare news datetime is invalid")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=CHINA_TIMEZONE)
    return parsed.astimezone(CHINA_TIMEZONE)


def _optional_number(value: Any) -> float | None:
    if value in {None, "", "-"}:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TushareReferenceError("Tushare numeric field is invalid")
    number = float(value)
    if not (-1.7976931348623157e308 <= number <= 1.7976931348623157e308):
        raise TushareReferenceError("Tushare numeric field is not finite")
    return number


def _bounded_text(value: Any, label: str, maximum: int) -> str:
    if not isinstance(value, str):
        raise TushareReferenceError(f"Tushare {label} is invalid")
    text = value.strip()
    if not text or len(text) > maximum or any(ord(char) < 32 and char not in "\n\r\t" for char in text):
        raise TushareReferenceError(f"Tushare {label} is invalid")
    return " ".join(text.split())


def _bounded_optional_text(value: Any, maximum: int) -> str:
    if value in {None, ""}:
        return ""
    return _bounded_text(str(value), "optional text", maximum)


def _fingerprint(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("ascii")
    ).hexdigest()


__all__ = [
    "TushareReferenceError",
    "fetch_fundamental_reference",
    "fetch_news_reference",
    "fetch_valuation_reference",
    "request_table",
    "token_configured",
]
