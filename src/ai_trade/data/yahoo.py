"""Bounded Yahoo Finance daily bars for independent snapshot reconciliation.

Yahoo's chart response has no provider-reported turnover amount.  This adapter
therefore cannot enter the strategy-visible snapshot chain: it writes a schema-
compatible estimated amount only so the common CSV validator can read the
temporary file, while the provider descriptor excludes amount from comparison.
"""

from __future__ import annotations

import csv
import http.client
import math
import os
import random
import socket
import ssl
import time as time_module
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..json_utils import loads_unique_json
from ..models import Bar, Instrument


ENDPOINT = "https://query1.finance.yahoo.com/v8/finance/chart"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REFERENCE_RANGE_DAYS = 62
MAX_ROWS = 64
SHARES_PER_LOT = 100.0
CHINA_TIMEZONE = timezone(timedelta(hours=8))
REQUEST_HEADERS = {
    "Accept": "application/json",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Connection": "close",
    "Pragma": "no-cache",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}
DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class YahooDownloadError(RuntimeError):
    """A failed Yahoo request with bounded attempt classifications."""

    def __init__(self, message: str, attempt_errors: list[Exception]):
        super().__init__(message)
        self.attempt_errors = tuple(attempt_errors)


def download_instrument(
    config: AppConfig,
    instrument: Instrument,
    output_path: Path,
    *,
    cutoff: date,
    proxy_mode: str | None = None,
    provider_metadata: dict[str, object] | None = None,
) -> Path:
    """Download a short reference window and stage one validated CSV."""

    adjustment = str(config.raw["data"].get("adjustment", "forward"))
    if adjustment not in {"none", "forward"}:
        raise RuntimeError(
            "Yahoo reference bars support only none or forward adjustment"
        )
    configured_start = date.fromisoformat(config.raw["data"]["start"])
    configured_end = date.fromisoformat(config.raw["data"]["end"])
    start = max(configured_start, instrument.listing_date or configured_start)
    end = min(
        configured_end,
        cutoff,
        instrument.delisting_date or cutoff,
    )
    if start > end:
        raise RuntimeError(
            f"Yahoo reference range is empty for {instrument.symbol}: {start}..{end}"
        )
    if (end - start).days > MAX_REFERENCE_RANGE_DAYS:
        raise RuntimeError(
            "Yahoo is reference-only and accepts at most "
            f"{MAX_REFERENCE_RANGE_DAYS + 1} calendar days per request"
        )

    ticker = _ticker(instrument)
    selected_proxy_mode = _proxy_mode(config, proxy_mode)
    timeout = int(config.raw["data"].get("timeout_seconds", 20))
    max_attempts = int(config.raw["data"].get("max_attempts", 4))
    retry_base = float(config.raw["data"].get("retry_base_seconds", 1.0))
    retry_max = float(config.raw["data"].get("retry_max_seconds", 8.0))
    retry_jitter = float(config.raw["data"].get("retry_jitter_seconds", 0.5))
    payload = _download_payload(
        ticker,
        start,
        end,
        timeout=timeout,
        proxy_mode=selected_proxy_mode,
        max_attempts=max_attempts,
        retry_base=retry_base,
        retry_max=retry_max,
        retry_jitter=retry_jitter,
    )
    bars = _parse_payload(
        payload,
        instrument,
        ticker=ticker,
        adjustment=adjustment,
        start=start,
        end=end,
        cutoff=cutoff,
    )
    _write_bars(output_path, bars)
    if provider_metadata is not None:
        provider_metadata.update(
            {
                "source_provider": "yahoo_chart",
                "source_mode": "bounded_reference",
                "yahoo_proxy_mode": selected_proxy_mode,
                "amount_quality": "locally_estimated_not_compared",
                "volume_unit": "lots_100_shares",
                "comparison_fields": ["open", "high", "low", "close", "volume"],
            }
        )
    return output_path


def _download_payload(
    ticker: str,
    start: date,
    end: date,
    *,
    timeout: int,
    proxy_mode: str,
    max_attempts: int,
    retry_base: float,
    retry_max: float,
    retry_jitter: float,
) -> object:
    params = {
        "period1": _unix_seconds(start),
        "period2": _unix_seconds(end + timedelta(days=1)),
        "interval": "1d",
        "events": "div,splits,capitalGains",
        "includeAdjustedClose": "true",
    }
    request = urllib.request.Request(
        f"{ENDPOINT}/{urllib.parse.quote(ticker)}?{urllib.parse.urlencode(params)}",
        headers=REQUEST_HEADERS,
    )
    attempt_errors: list[Exception] = []
    last_error: Exception | None = None
    for attempt in range(max(1, max_attempts)):
        try:
            with _open_request(request, timeout, proxy_mode) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError(
                    f"Yahoo response exceeds {MAX_RESPONSE_BYTES} bytes for {ticker}"
                )
        except (OSError, RuntimeError) as exc:
            last_error = exc
            attempt_errors.append(exc)
            if not _should_retry(exc) or attempt + 1 >= max_attempts:
                break
            exponential = min(retry_max, retry_base * (2**attempt))
            time_module.sleep(exponential + random.uniform(0.0, retry_jitter))
            continue
        try:
            return loads_unique_json(raw.decode("utf-8"))
        except (UnicodeError, ValueError) as exc:
            raise RuntimeError(f"Yahoo returned invalid JSON for {ticker}: {exc}") from exc
    raise YahooDownloadError(
        f"Yahoo failed to download {ticker} after {len(attempt_errors)} attempt(s): "
        f"{last_error}",
        attempt_errors,
    ) from last_error


def _parse_payload(
    payload: object,
    instrument: Instrument,
    *,
    ticker: str,
    adjustment: str,
    start: date,
    end: date,
    cutoff: date,
) -> list[Bar]:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Yahoo response envelope is invalid for {ticker}")
    chart = payload.get("chart")
    if not isinstance(chart, dict):
        raise RuntimeError(f"Yahoo response has no chart object for {ticker}")
    if chart.get("error") is not None:
        raise RuntimeError(f"Yahoo returned a chart error for {ticker}")
    results = chart.get("result")
    if not isinstance(results, list) or len(results) != 1:
        raise RuntimeError(f"Yahoo response has no unique result for {ticker}")
    result = results[0]
    if not isinstance(result, dict):
        raise RuntimeError(f"Yahoo chart result is invalid for {ticker}")
    meta = result.get("meta")
    if not isinstance(meta, dict) or meta.get("symbol") != ticker:
        raise RuntimeError(f"Yahoo symbol identity does not match {ticker}")
    if meta.get("exchangeTimezoneName") != "Asia/Shanghai" or meta.get(
        "gmtoffset"
    ) != 28_800:
        raise RuntimeError(f"Yahoo exchange timezone is invalid for {ticker}")

    timestamps = result.get("timestamp")
    indicators = result.get("indicators")
    if not isinstance(timestamps, list) or not timestamps or len(timestamps) > MAX_ROWS:
        raise RuntimeError(f"Yahoo timestamp count is invalid for {ticker}")
    if not isinstance(indicators, dict):
        raise RuntimeError(f"Yahoo indicators are invalid for {ticker}")
    quote = _single_object(indicators.get("quote"), "quote", ticker)
    arrays = {
        field: quote.get(field) for field in ("open", "high", "low", "close", "volume")
    }
    if any(not isinstance(value, list) for value in arrays.values()):
        raise RuntimeError(f"Yahoo quote arrays are invalid for {ticker}")
    if any(len(value) != len(timestamps) for value in arrays.values()):
        raise RuntimeError(f"Yahoo quote array lengths differ for {ticker}")

    adjusted_values: list[object] | None = None
    if adjustment == "forward":
        adjusted = _single_object(indicators.get("adjclose"), "adjclose", ticker)
        candidate = adjusted.get("adjclose")
        if not isinstance(candidate, list) or len(candidate) != len(timestamps):
            raise RuntimeError(f"Yahoo adjusted-close array is invalid for {ticker}")
        adjusted_values = candidate

    bars: list[Bar] = []
    previous_timestamp: int | None = None
    previous_date: date | None = None
    for index, raw_timestamp in enumerate(timestamps):
        if isinstance(raw_timestamp, bool) or not isinstance(raw_timestamp, int):
            raise RuntimeError(f"Yahoo timestamp is invalid for {ticker} at row {index}")
        if previous_timestamp is not None and raw_timestamp <= previous_timestamp:
            raise RuntimeError(f"Yahoo timestamps are not increasing for {ticker}")
        previous_timestamp = raw_timestamp
        raw_values = [arrays[field][index] for field in arrays]
        if all(value is None for value in raw_values):
            continue
        if any(value is None for value in raw_values):
            raise RuntimeError(f"Yahoo quote row is incomplete for {ticker} at row {index}")
        on_date = datetime.fromtimestamp(raw_timestamp, timezone.utc).astimezone(
            CHINA_TIMEZONE
        ).date()
        if not start <= on_date <= end or on_date > cutoff:
            raise RuntimeError(f"Yahoo row date is outside the request for {ticker}")
        if previous_date is not None and on_date <= previous_date:
            raise RuntimeError(f"Yahoo trading dates are not increasing for {ticker}")
        previous_date = on_date

        raw_open = _finite_number(arrays["open"][index], "open", ticker)
        raw_high = _finite_number(arrays["high"][index], "high", ticker)
        raw_low = _finite_number(arrays["low"][index], "low", ticker)
        raw_close = _finite_number(arrays["close"][index], "close", ticker)
        raw_volume = _finite_number(arrays["volume"][index], "volume", ticker)
        if adjustment == "forward":
            adjusted_close = _finite_number(
                adjusted_values[index] if adjusted_values is not None else None,
                "adjusted close",
                ticker,
            )
            if raw_close <= 0 or adjusted_close <= 0:
                raise RuntimeError(f"Yahoo adjustment factor is invalid for {ticker}")
            factor = adjusted_close / raw_close
        else:
            factor = 1.0
        bar = Bar(
            date=on_date,
            open=raw_open * factor,
            close=raw_close * factor,
            high=raw_high * factor,
            low=raw_low * factor,
            volume=raw_volume / SHARES_PER_LOT,
            amount=((raw_high + raw_low + raw_close) / 3.0) * raw_volume,
        )
        _validate_bar(bar, ticker, index)
        bars.append(bar)
    if not bars:
        raise RuntimeError(f"Yahoo returned no completed daily bars for {ticker}")
    return bars


def _single_object(value: object, field: str, ticker: str) -> dict[str, Any]:
    if not isinstance(value, list) or len(value) != 1 or not isinstance(value[0], dict):
        raise RuntimeError(f"Yahoo {field} envelope is invalid for {ticker}")
    return value[0]


def _finite_number(value: object, field: str, ticker: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"Yahoo {field} is invalid for {ticker}")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise RuntimeError(f"Yahoo {field} is not finite for {ticker}")
    return parsed


def _validate_bar(bar: Bar, ticker: str, index: int) -> None:
    if min(bar.open, bar.close, bar.high, bar.low) <= 0:
        raise RuntimeError(f"Yahoo price is non-positive for {ticker} at row {index}")
    if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(
        bar.open, bar.close, bar.high
    ):
        raise RuntimeError(f"Yahoo OHLC relationship is invalid for {ticker} at row {index}")
    if bar.volume < 0 or bar.amount < 0:
        raise RuntimeError(f"Yahoo volume is negative for {ticker} at row {index}")


def _write_bars(path: Path, bars: list[Bar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["date", "open", "close", "high", "low", "volume", "amount", "amplitude"]
            )
            for bar in bars:
                writer.writerow(
                    [
                        bar.date.isoformat(),
                        _format_number(bar.open),
                        _format_number(bar.close),
                        _format_number(bar.high),
                        _format_number(bar.low),
                        _format_number(bar.volume),
                        _format_number(bar.amount),
                        "",
                    ]
                )
        from .eastmoney import load_cached_bars

        load_cached_bars(temporary)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _format_number(value: float) -> str:
    return format(value, ".15g")


def _ticker(instrument: Instrument) -> str:
    suffixes = {"SH": "SS", "SZ": "SZ"}
    try:
        suffix = suffixes[instrument.market.upper()]
    except KeyError as exc:
        raise RuntimeError(
            f"Yahoo has no A-share ticker mapping for market {instrument.market!r}"
        ) from exc
    return f"{instrument.symbol}.{suffix}"


def _unix_seconds(value: date) -> int:
    return int(datetime.combine(value, time.min, tzinfo=timezone.utc).timestamp())


def _proxy_mode(config: AppConfig, explicit: str | None) -> str:
    if explicit is not None:
        value: object = explicit
        source = "proxy_mode"
    else:
        value = os.environ.get("AI_TRADE_YAHOO_PROXY_MODE")
        source = "AI_TRADE_YAHOO_PROXY_MODE"
        if value is None:
            value = config.raw["data"].get("proxy_mode", "system")
            source = "data.proxy_mode"
    if not isinstance(value, str):
        raise ValueError(f"{source} must be system or direct")
    mode = value.strip().lower()
    if mode not in {"system", "direct"}:
        raise ValueError(f"{source} must be system or direct")
    return mode


def _open_request(request: urllib.request.Request, timeout: int, proxy_mode: str):
    if proxy_mode == "direct":
        return DIRECT_OPENER.open(request, timeout=timeout)
    if proxy_mode == "system":
        return urllib.request.urlopen(request, timeout=timeout)
    raise ValueError(f"Unsupported Yahoo proxy mode: {proxy_mode!r}")


def is_transport_failure(error: Exception) -> bool:
    if isinstance(error, YahooDownloadError):
        return bool(error.attempt_errors) and all(
            _provider_wide_failure(attempt) is True for attempt in error.attempt_errors
        )
    current: BaseException | None = error
    seen: set[int] = set()
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        classification = _provider_wide_failure(current)
        if classification is not None:
            return classification
        current = current.__cause__ or current.__context__
    return False


def _should_retry(error: Exception) -> bool:
    return _provider_wide_failure(error) is True


def _provider_wide_failure(error: BaseException) -> bool | None:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in {408, 425, 429} or 500 <= error.code <= 599
    if isinstance(error, urllib.error.URLError):
        reason = error.reason
        if isinstance(reason, BaseException):
            nested = _provider_wide_failure(reason)
            return True if nested is None else nested
        return True
    if isinstance(
        error,
        (
            http.client.BadStatusLine,
            http.client.IncompleteRead,
            ConnectionError,
            TimeoutError,
            socket.gaierror,
            ssl.SSLError,
        ),
    ):
        return True
    if isinstance(error, OSError):
        return False
    return None


__all__ = [
    "MAX_REFERENCE_RANGE_DAYS",
    "MAX_RESPONSE_BYTES",
    "YahooDownloadError",
    "download_instrument",
    "is_transport_failure",
]
