"""Bounded Tushare Pro daily bars for independent reconciliation.

The adapter is reference-only.  It reads its token from the process
environment, requests a short completed-session window, normalizes Tushare's
lot and thousand-CNY units, and never enters the strategy-visible snapshot
chain.
"""

from __future__ import annotations

import csv
import http.client
import json
import math
import os
import random
import socket
import ssl
import time as time_module
import urllib.error
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..config import AppConfig
from ..json_utils import loads_unique_json
from ..models import Bar, Instrument


ENDPOINT = "https://api.tushare.pro"
TOKEN_ENV = "AI_TRADE_TUSHARE_TOKEN"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_REFERENCE_RANGE_DAYS = 62
MAX_ROWS = 64
DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
REQUEST_HEADERS = {
    "Accept": "application/json",
    "Content-Type": "application/json; charset=utf-8",
    "User-Agent": "AI-Trade-Tushare-Reference/1",
}


class TushareDownloadError(RuntimeError):
    """A failed Tushare request with bounded transport classifications."""

    def __init__(self, message: str, attempt_errors: Sequence[Exception] = ()):
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
    """Download one short Tushare reference window and publish a valid CSV."""

    token = os.environ.get(TOKEN_ENV, "").strip()
    if not token:
        raise RuntimeError(f"{TOKEN_ENV} is required for the Tushare reference provider")
    adjustment = str(config.raw["data"].get("adjustment", "forward"))
    if adjustment not in {"none", "forward"}:
        raise RuntimeError("Tushare reference bars support only none or forward adjustment")
    start, end = _request_range(config, instrument, cutoff)
    ts_code = _ts_code(instrument)
    api_name, factor_api = _api_names(instrument)
    selected_proxy = _proxy_mode(config, proxy_mode)
    rows = _request_rows(
        config,
        token=token,
        api_name=api_name,
        ts_code=ts_code,
        start=start,
        end=end,
        proxy_mode=selected_proxy,
        fields=(
            "ts_code",
            "trade_date",
            "open",
            "high",
            "low",
            "close",
            "vol",
            "amount",
        ),
    )
    factors: dict[date, float] | None = None
    if adjustment == "forward":
        factor_rows = _request_rows(
            config,
            token=token,
            api_name=factor_api,
            ts_code=ts_code,
            start=start,
            end=end,
            proxy_mode=selected_proxy,
            fields=("ts_code", "trade_date", "adj_factor"),
        )
        factors = _factor_map(factor_rows, ts_code=ts_code, start=start, end=end)
    bars = _parse_rows(
        rows,
        ts_code=ts_code,
        start=start,
        end=end,
        adjustment=adjustment,
        factors=factors,
    )
    _write_bars(output_path, bars)
    if provider_metadata is not None:
        provider_metadata.update(
            {
                "source_provider": "tushare_pro",
                "source_mode": "credentialed_bounded_reference",
                "tushare_api": api_name,
                "tushare_factor_api": factor_api if adjustment == "forward" else None,
                "tushare_proxy_mode": selected_proxy,
                "token_source": TOKEN_ENV,
                "volume_unit": "lots_100_shares",
                "amount_unit": "cny_normalized_from_thousand_cny",
                "comparison_fields": [
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "amount",
                ],
            }
        )
    return output_path


def _request_range(
    config: AppConfig, instrument: Instrument, cutoff: date
) -> tuple[date, date]:
    configured_start = date.fromisoformat(config.raw["data"]["start"])
    configured_end = date.fromisoformat(config.raw["data"]["end"])
    start = max(configured_start, instrument.listing_date or configured_start)
    end = min(configured_end, cutoff, instrument.delisting_date or cutoff)
    if start > end:
        raise RuntimeError(
            f"Tushare reference range is empty for {instrument.symbol}: {start}..{end}"
        )
    if (end - start).days > MAX_REFERENCE_RANGE_DAYS:
        raise RuntimeError(
            "Tushare is reference-only and accepts at most "
            f"{MAX_REFERENCE_RANGE_DAYS + 1} calendar days per request"
        )
    return start, end


def _request_rows(
    config: AppConfig,
    *,
    token: str,
    api_name: str,
    ts_code: str,
    start: date,
    end: date,
    proxy_mode: str,
    fields: Sequence[str],
) -> list[dict[str, Any]]:
    body = json.dumps(
        {
            "api_name": api_name,
            "token": token,
            "params": {
                "ts_code": ts_code,
                "start_date": start.strftime("%Y%m%d"),
                "end_date": end.strftime("%Y%m%d"),
            },
            "fields": ",".join(fields),
        },
        ensure_ascii=True,
        separators=(",", ":"),
    ).encode("utf-8")
    request = urllib.request.Request(
        ENDPOINT,
        data=body,
        headers=REQUEST_HEADERS,
        method="POST",
    )
    data_config = config.raw.get("data", {})
    timeout = int(data_config.get("timeout_seconds", 20))
    attempts = min(4, max(1, int(data_config.get("max_attempts", 4))))
    retry_base = float(data_config.get("retry_base_seconds", 1.0))
    retry_max = float(data_config.get("retry_max_seconds", 8.0))
    retry_jitter = float(data_config.get("retry_jitter_seconds", 0.5))
    errors: list[Exception] = []
    for attempt in range(attempts):
        try:
            with _open_request(request, timeout, proxy_mode) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError("Tushare response exceeds the size limit")
            payload = loads_unique_json(raw.decode("utf-8"))
            return _response_rows(payload, api_name=api_name, expected_fields=fields)
        except (UnicodeError, ValueError) as exc:
            raise RuntimeError(f"Tushare returned invalid JSON for {api_name}: {exc}") from exc
        except (OSError, RuntimeError) as exc:
            errors.append(exc)
            if not _retryable(exc) or attempt + 1 >= attempts:
                break
            delay = min(retry_max, retry_base * (2**attempt))
            time_module.sleep(delay + random.uniform(0.0, retry_jitter))
    detail = errors[-1] if errors else "unknown failure"
    raise TushareDownloadError(
        f"Tushare {api_name} failed after {len(errors)} attempt(s): {detail}",
        errors,
    ) from (errors[-1] if errors else None)


def _response_rows(
    payload: object, *, api_name: str, expected_fields: Sequence[str]
) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        raise RuntimeError("Tushare response envelope is invalid")
    code = payload.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        raise RuntimeError("Tushare response code is invalid")
    if code != 0:
        message = str(payload.get("msg") or "request rejected")[:300]
        raise RuntimeError(f"Tushare {api_name} returned code {code}: {message}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Tushare response data is invalid")
    fields = data.get("fields")
    items = data.get("items")
    if not isinstance(fields, list) or fields != list(expected_fields):
        raise RuntimeError("Tushare response fields do not match the request")
    if not isinstance(items, list) or not items or len(items) > MAX_ROWS:
        raise RuntimeError("Tushare response row count is invalid")
    result: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, list) or len(item) != len(fields):
            raise RuntimeError("Tushare response row shape is invalid")
        result.append(dict(zip(fields, item, strict=True)))
    return result


def _parse_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    ts_code: str,
    start: date,
    end: date,
    adjustment: str,
    factors: Mapping[date, float] | None,
) -> list[Bar]:
    parsed: list[tuple[date, Mapping[str, Any]]] = []
    for row in rows:
        if row.get("ts_code") != ts_code:
            raise RuntimeError("Tushare symbol identity does not match the request")
        on_date = _trade_date(row.get("trade_date"))
        if not start <= on_date <= end:
            raise RuntimeError("Tushare row date is outside the request")
        parsed.append((on_date, row))
    parsed.sort(key=lambda item: item[0])
    dates = [item[0] for item in parsed]
    if len(set(dates)) != len(dates):
        raise RuntimeError("Tushare returned duplicate trading dates")
    latest_factor = None
    if adjustment == "forward":
        if not factors or any(on_date not in factors for on_date in dates):
            raise RuntimeError("Tushare adjustment factors do not cover all bars")
        latest_factor = factors[dates[-1]]
        if latest_factor <= 0:
            raise RuntimeError("Tushare latest adjustment factor is invalid")
    bars: list[Bar] = []
    for index, (on_date, row) in enumerate(parsed):
        factor = (
            factors[on_date] / latest_factor
            if adjustment == "forward" and factors is not None and latest_factor is not None
            else 1.0
        )
        bar = Bar(
            date=on_date,
            open=_positive(row.get("open"), "open") * factor,
            close=_positive(row.get("close"), "close") * factor,
            high=_positive(row.get("high"), "high") * factor,
            low=_positive(row.get("low"), "low") * factor,
            volume=_nonnegative(row.get("vol"), "vol"),
            amount=_nonnegative(row.get("amount"), "amount") * 1000.0,
        )
        _validate_bar(bar, index)
        bars.append(bar)
    if not bars:
        raise RuntimeError("Tushare returned no completed daily bars")
    return bars


def _factor_map(
    rows: Sequence[Mapping[str, Any]], *, ts_code: str, start: date, end: date
) -> dict[date, float]:
    result: dict[date, float] = {}
    for row in rows:
        if row.get("ts_code") != ts_code:
            raise RuntimeError("Tushare factor symbol does not match the request")
        on_date = _trade_date(row.get("trade_date"))
        if not start <= on_date <= end or on_date in result:
            raise RuntimeError("Tushare adjustment factor date is invalid")
        result[on_date] = _positive(row.get("adj_factor"), "adj_factor")
    return result


def _write_bars(path: Path, bars: Sequence[Bar]) -> None:
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
                        _format(bar.open),
                        _format(bar.close),
                        _format(bar.high),
                        _format(bar.low),
                        _format(bar.volume),
                        _format(bar.amount),
                        "",
                    ]
                )
        from .eastmoney import load_cached_bars

        load_cached_bars(temporary)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _api_names(instrument: Instrument) -> tuple[str, str]:
    kind = instrument.instrument_type.strip().upper()
    if kind == "ETF":
        return "fund_daily", "fund_adj"
    if kind == "STOCK":
        return "daily", "adj_factor"
    raise RuntimeError(
        f"Tushare reference bars do not support instrument type {instrument.instrument_type!r}"
    )


def _ts_code(instrument: Instrument) -> str:
    market = instrument.market.strip().upper()
    if market not in {"SH", "SZ", "BJ"}:
        raise RuntimeError(f"Tushare has no code mapping for market {instrument.market!r}")
    return f"{instrument.symbol}.{market}"


def _trade_date(value: object) -> date:
    if not isinstance(value, str) or len(value) != 8 or not value.isdigit():
        raise RuntimeError("Tushare trade_date is invalid")
    return date(int(value[:4]), int(value[4:6]), int(value[6:]))


def _positive(value: object, label: str) -> float:
    result = _number(value, label)
    if result <= 0:
        raise RuntimeError(f"Tushare {label} is not positive")
    return result


def _nonnegative(value: object, label: str) -> float:
    result = _number(value, label)
    if result < 0:
        raise RuntimeError(f"Tushare {label} is negative")
    return result


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"Tushare {label} is invalid")
    result = float(value)
    if not math.isfinite(result):
        raise RuntimeError(f"Tushare {label} is not finite")
    return result


def _validate_bar(bar: Bar, index: int) -> None:
    if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(
        bar.open, bar.close, bar.high
    ):
        raise RuntimeError(f"Tushare OHLC relationship is invalid at row {index}")
    if bar.volume < 0 or bar.amount < 0:
        raise RuntimeError(f"Tushare volume or amount is invalid at row {index}")


def _format(value: float) -> str:
    return format(value, ".15g")


def _proxy_mode(config: AppConfig, explicit: str | None) -> str:
    value: object = explicit if explicit is not None else config.raw["data"].get("proxy_mode", "system")
    if not isinstance(value, str) or value.strip().lower() not in {"system", "direct"}:
        raise ValueError("Tushare proxy mode must be system or direct")
    return value.strip().lower()


def _open_request(request: urllib.request.Request, timeout: int, proxy_mode: str):
    if proxy_mode == "direct":
        return DIRECT_OPENER.open(request, timeout=timeout)
    if proxy_mode == "system":
        return urllib.request.urlopen(request, timeout=timeout)
    raise ValueError(f"Unsupported Tushare proxy mode: {proxy_mode!r}")


def is_transport_failure(error: Exception) -> bool:
    attempts = error.attempt_errors if isinstance(error, TushareDownloadError) else (error,)
    return bool(attempts) and all(_transport(item) for item in attempts)


def _retryable(error: Exception) -> bool:
    return _transport(error)


def _transport(error: BaseException) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code in {408, 425, 429} or 500 <= error.code <= 599
    if isinstance(error, urllib.error.URLError):
        return True
    return isinstance(
        error,
        (
            http.client.BadStatusLine,
            http.client.IncompleteRead,
            ConnectionError,
            TimeoutError,
            socket.gaierror,
            ssl.SSLError,
        ),
    )


__all__ = [
    "MAX_REFERENCE_RANGE_DAYS",
    "MAX_RESPONSE_BYTES",
    "TOKEN_ENV",
    "TushareDownloadError",
    "download_instrument",
    "is_transport_failure",
]
