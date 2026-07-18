from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import random
import re
import time as time_module
import urllib.parse
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..config import AppConfig
from ..json_utils import load_unique_json
from ..models import Bar, Instrument

ENDPOINT = "https://proxy.finance.qq.com/ifzqgtimg/appstock/app/newfqkline/get"
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
YEARLY_LIMIT = 320
LEGACY_START_TOLERANCE_DAYS = 3
REQUEST_HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Connection": "close",
    "Pragma": "no-cache",
    "Referer": "https://gu.qq.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}
DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
_ADJUSTMENTS = {
    "forward": ("qfq", "qfqday"),
    "backward": ("hfq", "hfqday"),
    "none": ("", "day"),
}


class TencentOverlapError(RuntimeError):
    """Cached and freshly downloaded Tencent bars cannot be safely merged."""


def download_instrument(
    config: AppConfig,
    instrument: Instrument,
    output_path: Path,
    *,
    cache_path: Path | None = None,
    cutoff: date,
    proxy_mode: str | None = None,
    provider_metadata: dict[str, object] | None = None,
) -> Path:
    """Download and atomically stage one instrument from Tencent Finance."""
    configured_start = date.fromisoformat(config.raw["data"]["start"])
    configured_end = date.fromisoformat(config.raw["data"]["end"])
    start = max(
        configured_start,
        instrument.listing_date or configured_start,
    )
    end = min(
        configured_end,
        cutoff,
        instrument.delisting_date or cutoff,
    )
    if start > end:
        raise RuntimeError(
            f"Tencent request range is empty for {instrument.symbol}: {start}..{end}"
        )

    selected_proxy_mode = _proxy_mode(config, proxy_mode)
    adjustment = config.raw["data"].get("adjustment", "forward")
    request_adjustment, response_key = _ADJUSTMENTS[adjustment]
    timeout = int(config.raw["data"].get("timeout_seconds", 20))
    max_attempts = int(config.raw["data"].get("max_attempts", 4))
    retry_base = float(config.raw["data"].get("retry_base_seconds", 1.0))
    retry_max = float(config.raw["data"].get("retry_max_seconds", 8.0))
    retry_jitter = float(config.raw["data"].get("retry_jitter_seconds", 0.5))
    request_interval = float(config.raw["data"].get("request_interval_seconds", 2.0))

    cache_provenance: dict[str, object] = {}
    cached = _recent_cached_bars(
        config,
        instrument,
        cache_path,
        start,
        end,
        adjustment,
        provenance=cache_provenance,
    )
    source_mode = "incremental" if cached else "full_history"
    try:
        bars, exact_override, pages, overlap_rows = _download_range(
            instrument,
            start,
            end,
            cached=cached,
            adjustment=request_adjustment,
            response_key=response_key,
            timeout=timeout,
            proxy_mode=selected_proxy_mode,
            max_attempts=max_attempts,
            retry_base=retry_base,
            retry_max=retry_max,
            retry_jitter=retry_jitter,
            request_interval=request_interval,
        )
    except TencentOverlapError:
        bars, exact_override, pages, overlap_rows = _download_range(
            instrument,
            start,
            end,
            cached=[],
            adjustment=request_adjustment,
            response_key=response_key,
            timeout=timeout,
            proxy_mode=selected_proxy_mode,
            max_attempts=max_attempts,
            retry_base=retry_base,
            retry_max=retry_max,
            retry_jitter=retry_jitter,
            request_interval=request_interval,
        )
        source_mode = "full_rebuild_after_overlap_mismatch"

    _write_bars(output_path, bars)
    if provider_metadata is not None:
        metadata: dict[str, object] = {
            "source_provider": "tencent_newfqkline",
            "source_mode": source_mode,
            "pages": pages,
            "overlap_rows": overlap_rows,
            "tencent_proxy_mode": selected_proxy_mode,
            "amount_quality": "provider_reported_rounded",
            "amount_resolution_cny": 100,
            "amount_max_rounding_error_cny": 50,
            "latest_amount_exact_override": exact_override,
        }
        if source_mode == "incremental":
            metadata.update(cache_provenance)
            metadata["retained_cached_rows"] = max(0, len(cached) - overlap_rows)
        provider_metadata.update(metadata)
    return output_path


def _download_range(
    instrument: Instrument,
    start: date,
    end: date,
    *,
    cached: list[Bar],
    adjustment: str,
    response_key: str,
    timeout: int,
    proxy_mode: str,
    max_attempts: int,
    retry_base: float,
    retry_max: float,
    retry_jitter: float,
    request_interval: float,
) -> tuple[list[Bar], bool, int, int]:
    overlap = cached[-min(YEARLY_LIMIT, len(cached)) :] if cached else []
    request_start = overlap[0].date if overlap else start
    ranges = [
        (max(request_start, date(year, 1, 1), start), min(end, date(year, 12, 31)))
        for year in range(request_start.year, end.year + 1)
    ]
    ranges = [value for value in ranges if value[0] <= value[1]]

    merged = {bar.date: bar for bar in cached}
    expected_overlap = {bar.date for bar in overlap}
    matched_overlap: set[date] = set()
    latest_exact_override = False
    downloaded_dates: set[date] = set()
    for index, (year_start, year_end) in enumerate(ranges):
        rows, exact_override_date = _download_year(
            instrument,
            year_start,
            year_end,
            cutoff=end,
            adjustment=adjustment,
            response_key=response_key,
            timeout=timeout,
            proxy_mode=proxy_mode,
            max_attempts=max_attempts,
            retry_base=retry_base,
            retry_max=retry_max,
            retry_jitter=retry_jitter,
        )
        for bar in rows:
            if bar.date in downloaded_dates:
                raise RuntimeError(
                    f"Tencent returned duplicate pages for {instrument.symbol} on "
                    f"{bar.date}"
                )
            downloaded_dates.add(bar.date)
            existing = merged.get(bar.date)
            if existing is not None:
                _validate_overlap(existing, bar, instrument.symbol)
                if bar.date == exact_override_date:
                    merged[bar.date] = bar
                    latest_exact_override = True
                if bar.date in expected_overlap:
                    matched_overlap.add(bar.date)
                continue
            merged[bar.date] = bar
            if bar.date == exact_override_date:
                latest_exact_override = True
        if index + 1 < len(ranges) and request_interval > 0:
            time_module.sleep(request_interval)

    if matched_overlap != expected_overlap:
        missing = len(expected_overlap - matched_overlap)
        raise TencentOverlapError(
            f"Tencent overlap coverage is missing {missing} cached bars for "
            f"{instrument.symbol}; a full refresh is required"
        )
    bars = [merged[value] for value in sorted(merged) if start <= value <= end]
    if not bars:
        raise RuntimeError(
            f"Tencent returned no completed bars for {instrument.symbol}"
        )
    return bars, latest_exact_override, len(ranges), len(matched_overlap)


def _download_year(
    instrument: Instrument,
    start: date,
    end: date,
    *,
    cutoff: date,
    adjustment: str,
    response_key: str,
    timeout: int,
    proxy_mode: str,
    max_attempts: int,
    retry_base: float,
    retry_max: float,
    retry_jitter: float,
) -> tuple[list[Bar], date | None]:
    code = f"{instrument.market.lower()}{instrument.symbol}"
    callback = f"ai_trade_{code}_{start.year}"
    last_error: Exception | None = None
    for attempt in range(max_attempts):
        params = {
            "_var": callback,
            "param": (
                f"{code},day,{start.isoformat()},{end.isoformat()},"
                f"{YEARLY_LIMIT},{adjustment}"
            ),
            "_": str((time_module.time_ns() // 1_000_000) * 100 + attempt),
        }
        request = urllib.request.Request(
            f"{ENDPOINT}?{urllib.parse.urlencode(params)}",
            headers=REQUEST_HEADERS,
        )
        try:
            with _open_request(request, timeout, proxy_mode) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise RuntimeError(
                    f"Tencent response is too large for {instrument.symbol}"
                )
            text = raw.decode("utf-8")
            payload = _parse_jsonp(text, callback, instrument.symbol)
            return _parse_payload(
                payload,
                instrument,
                code,
                response_key,
                adjustment,
                start,
                end,
                cutoff,
            )
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            last_error = exc
            if attempt + 1 < max_attempts:
                exponential = min(retry_max, retry_base * (2**attempt))
                time_module.sleep(exponential + random.uniform(0.0, retry_jitter))
    raise RuntimeError(
        f"Tencent failed to download {instrument.symbol} year {start.year} "
        f"after {max_attempts} attempts: {last_error}"
    ) from last_error


def _parse_jsonp(text: str, callback: str, symbol: str) -> object:
    match = re.fullmatch(
        rf"{re.escape(callback)}=(\{{.*\}});?",
        text,
        flags=re.DOTALL,
    )
    if match is None:
        raise RuntimeError(f"Invalid Tencent JSONP envelope for {symbol}")
    try:
        return json.loads(match.group(1), object_pairs_hook=_unique_object)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid Tencent JSON payload for {symbol}: {exc}") from exc


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    value: dict[str, Any] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError(f"duplicate object key {key!r}")
        value[key] = item
    return value


def _parse_payload(
    payload: object,
    instrument: Instrument,
    code: str,
    response_key: str,
    adjustment: str,
    start: date,
    end: date,
    cutoff: date,
) -> tuple[list[Bar], date | None]:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid Tencent response envelope for {instrument.symbol}")
    response_code = payload.get("code")
    if (
        isinstance(response_code, bool)
        or not isinstance(response_code, int)
        or response_code != 0
    ):
        raise RuntimeError(
            f"Tencent returned code={response_code!r} for {instrument.symbol}"
        )
    data = payload.get("data")
    if not isinstance(data, dict):
        raise RuntimeError(f"Tencent response has no data for {instrument.symbol}")
    item = data.get(code)
    if not isinstance(item, dict):
        raise RuntimeError(f"Tencent response omitted {instrument.symbol}")
    raw_rows = item.get(response_key)
    if not isinstance(raw_rows, list) or not raw_rows:
        raise RuntimeError(
            f"Tencent response has no {response_key} rows for {instrument.symbol}"
        )
    if len(raw_rows) > YEARLY_LIMIT:
        raise RuntimeError(
            f"Tencent page exceeds the {YEARLY_LIMIT}-row limit for {instrument.symbol}"
        )

    rows: dict[date, Bar] = {}
    previous_date: date | None = None
    for index, raw_row in enumerate(raw_rows):
        bar = _parse_row(raw_row, instrument.symbol, index)
        if previous_date is not None and bar.date <= previous_date:
            raise RuntimeError(
                f"Tencent page dates are not strictly increasing for "
                f"{instrument.symbol} at row {index}"
            )
        previous_date = bar.date
        if bar.date > end or bar.date > cutoff:
            raise RuntimeError(
                f"Tencent kline exceeds the completed cutoff for "
                f"{instrument.symbol}: {bar.date}"
            )
        if start <= bar.date:
            rows[bar.date] = bar

    exact_override_date: date | None = None
    quote = _parse_quote(item.get("qt"), code, instrument.symbol)
    if (
        quote is not None
        and adjustment in {"qfq", ""}
        and start <= quote[0] <= end
        and quote[0] <= cutoff
    ):
        (
            row_date,
            quote_open,
            quote_close,
            quote_high,
            quote_low,
            quote_volume,
            quote_amount,
        ) = quote
        if not rows or row_date != max(rows):
            raise RuntimeError(
                f"Tencent qt date does not match the latest kline for {instrument.symbol}"
            )
        current = rows[row_date]
        if (
            quote_open,
            quote_close,
            quote_high,
            quote_low,
            quote_volume,
        ) != (
            current.open,
            current.close,
            current.high,
            current.low,
            current.volume,
        ):
            raise RuntimeError(
                f"Tencent qt OHLCV does not match the latest kline for "
                f"{instrument.symbol}"
            )
        corrected = Bar(
            date=current.date,
            open=current.open,
            close=current.close,
            high=current.high,
            low=current.low,
            volume=current.volume,
            amount=quote_amount,
        )
        _validate_bar(corrected, f"Tencent quote for {instrument.symbol}")
        rows[row_date] = corrected
        exact_override_date = row_date

    return [rows[value] for value in sorted(rows)], exact_override_date


def _parse_row(raw: object, symbol: str, index: int) -> Bar:
    if not isinstance(raw, list) or len(raw) < 9:
        raise RuntimeError(f"Malformed Tencent kline for {symbol} at row {index}")
    row_date = _parse_date(raw[0], f"Tencent kline for {symbol} at row {index}")
    bar = Bar(
        date=row_date,
        open=_finite_number(raw[1], "open", symbol),
        close=_finite_number(raw[2], "close", symbol),
        high=_finite_number(raw[3], "high", symbol),
        low=_finite_number(raw[4], "low", symbol),
        volume=_finite_number(raw[5], "volume", symbol),
        amount=_finite_number(raw[8], "amount", symbol) * 10_000.0,
    )
    _validate_bar(bar, f"Tencent kline for {symbol} at row {index}")
    return bar


def _parse_quote(
    raw_qt: object, code: str, symbol: str
) -> tuple[date, float, float, float, float, float, float] | None:
    if raw_qt is None:
        return None
    if not isinstance(raw_qt, dict):
        raise RuntimeError(f"Malformed Tencent qt envelope for {symbol}")
    quote = raw_qt.get(code)
    if not isinstance(quote, list) or len(quote) <= 35:
        raise RuntimeError(f"Malformed Tencent qt row for {symbol}")
    if quote[2] != symbol:
        raise RuntimeError(f"Tencent qt code does not match {symbol}")
    raw_timestamp = quote[30]
    if (
        not isinstance(raw_timestamp, str)
        or len(raw_timestamp) not in {8, 14}
        or not raw_timestamp.isdigit()
    ):
        raise RuntimeError(f"Malformed Tencent qt date for {symbol}")
    quote_date = _parse_date(
        f"{raw_timestamp[:4]}-{raw_timestamp[4:6]}-{raw_timestamp[6:8]}",
        f"Tencent qt for {symbol}",
    )
    raw_values = quote[35]
    if not isinstance(raw_values, str) or len(raw_values.split("/")) != 3:
        raise RuntimeError(f"Malformed Tencent qt values for {symbol}")
    precise_close, precise_volume, precise_amount = raw_values.split("/")
    open_price = _finite_number(quote[5], "qt open", symbol)
    close = _finite_number(quote[3], "qt close", symbol)
    high = _finite_number(quote[33], "qt high", symbol)
    low = _finite_number(quote[34], "qt low", symbol)
    volume = _finite_number(quote[6], "qt volume", symbol)
    if close != _finite_number(precise_close, "qt precise close", symbol):
        raise RuntimeError(f"Tencent qt close fields disagree for {symbol}")
    if volume != _finite_number(precise_volume, "qt precise volume", symbol):
        raise RuntimeError(f"Tencent qt volume fields disagree for {symbol}")
    return (
        quote_date,
        open_price,
        close,
        high,
        low,
        volume,
        _finite_number(precise_amount, "qt amount", symbol),
    )


def _validate_overlap(cached: Bar, downloaded: Bar, symbol: str) -> None:
    if (
        cached.open,
        cached.close,
        cached.high,
        cached.low,
        cached.volume,
    ) != (
        downloaded.open,
        downloaded.close,
        downloaded.high,
        downloaded.low,
        downloaded.volume,
    ):
        raise TencentOverlapError(
            f"Tencent overlap OHLCV mismatch for {symbol} on {cached.date}; "
            "a full refresh is required"
        )
    if abs(cached.amount - downloaded.amount) > 50.000001:
        raise TencentOverlapError(
            f"Tencent overlap amount mismatch exceeds CNY 50 for {symbol} on "
            f"{cached.date}"
        )


def _parse_date(value: object, source: str) -> date:
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        raise RuntimeError(f"Malformed date in {source}")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError(f"Malformed date in {source}") from exc


def _finite_number(value: object, field: str, symbol: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise RuntimeError(f"Malformed Tencent {field} for {symbol}")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise RuntimeError(f"Malformed Tencent {field} for {symbol}") from exc
    if not math.isfinite(parsed):
        raise RuntimeError(f"Non-finite Tencent {field} for {symbol}")
    return parsed


def _validate_bar(bar: Bar, source: str) -> None:
    if min(bar.open, bar.close, bar.high, bar.low) <= 0:
        raise RuntimeError(f"Non-positive price in {source}")
    if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(
        bar.open, bar.close, bar.high
    ):
        raise RuntimeError(f"Invalid OHLC relationship in {source}")
    if bar.volume < 0 or bar.amount < 0:
        raise RuntimeError(f"Negative volume or amount in {source}")


def _recent_cached_bars(
    config: AppConfig,
    instrument: Instrument,
    cache_path: Path | None,
    start: date,
    cutoff: date,
    adjustment: str,
    *,
    provenance: dict[str, object] | None = None,
) -> list[Bar]:
    if cache_path is None or not cache_path.exists():
        return []
    try:
        active_cache_dir = config.cache_dir.resolve()
        if (
            cache_path.resolve().parent != active_cache_dir
            or cache_path.name != f"{instrument.symbol}.csv"
        ):
            return []
        manifest_path = active_cache_dir / "manifest.json"
        if (
            not manifest_path.is_file()
            or manifest_path.stat().st_size > MAX_MANIFEST_BYTES
        ):
            return []
        manifest = load_unique_json(
            manifest_path,
            max_bytes=MAX_MANIFEST_BYTES,
        )
        if not isinstance(manifest, dict):
            return []
        if manifest.get("provider") != config.raw["data"].get("provider"):
            return []
        if manifest.get("adjustment") != adjustment:
            return []
        files = manifest.get("files")
        if not isinstance(files, dict):
            return []
        file_metadata = files.get(instrument.symbol)
        if not isinstance(file_metadata, dict):
            return []

        requested_from = file_metadata.get(
            "requested_from", manifest.get("requested_from")
        )
        requested_start = _parse_date(
            requested_from,
            f"cache manifest requested_from for {instrument.symbol}",
        )
        if requested_start > start:
            return []

        expected_sha256 = file_metadata.get("sha256")
        if (
            not isinstance(expected_sha256, str)
            or re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256) is None
        ):
            return []

        from .eastmoney import load_cached_bars

        all_bars = load_cached_bars(cache_path)
        if _file_sha256(cache_path) != expected_sha256.lower():
            return []
        expected_rows = file_metadata.get("rows")
        if (
            isinstance(expected_rows, bool)
            or not isinstance(expected_rows, int)
            or expected_rows != len(all_bars)
        ):
            return []
        source = file_metadata.get("source")
        if not isinstance(source, str) or source not in {
            "network",
            "eastmoney_network_fallback",
            "tencent_network_fallback",
            "validated_local_fallback",
        }:
            return []
        latest_session = _parse_date(
            file_metadata.get("latest_session"),
            f"cache manifest latest_session for {instrument.symbol}",
        )
        if latest_session != all_bars[-1].date:
            return []
        if all_bars[0].date > start + timedelta(days=LEGACY_START_TOLERANCE_DAYS):
            return []
        bars = [bar for bar in all_bars if start <= bar.date <= cutoff]
        if provenance is not None:
            provenance.update(
                {
                    "cached_seed_source": source,
                    "cached_seed_sha256": expected_sha256.lower(),
                    "cached_seed_rows": expected_rows,
                }
            )
    except (OSError, UnicodeError, ValueError, RuntimeError):
        return []
    if not bars or cutoff - bars[-1].date > timedelta(days=7):
        return []
    return bars


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _proxy_mode(config: AppConfig, explicit: str | None) -> str:
    if explicit is not None:
        value: object = explicit
        source = "proxy_mode"
    else:
        value = os.environ.get("AI_TRADE_TENCENT_PROXY_MODE")
        source = "AI_TRADE_TENCENT_PROXY_MODE"
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
    raise ValueError(f"Unsupported Tencent proxy mode: {proxy_mode!r}")


def _write_bars(path: Path, bars: list[Bar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                [
                    "date",
                    "open",
                    "close",
                    "high",
                    "low",
                    "volume",
                    "amount",
                    "amplitude",
                ]
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
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def _format_number(value: float) -> str:
    return format(value, ".15g")
