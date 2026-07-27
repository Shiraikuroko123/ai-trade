"""Independent proof that unadjusted Tencent bars need no adjustment."""

from __future__ import annotations

import hashlib
import http.client
import math
import random
import re
import socket
import ssl
import time as time_module
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from ..json_utils import loads_unique_json
from ..models import Bar, Instrument


KLINE_ENDPOINT = (
    "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/"
    "CN_MarketData.getKLineData"
)
FACTOR_ENDPOINT = "https://finance.sina.com.cn/realstock/company"
MAX_KLINE_BYTES = 2 * 1024 * 1024
MAX_FACTOR_BYTES = 64 * 1024
MAX_ROWS = 5_000
START_TOLERANCE_DAYS = 10
LATEST_TOLERANCE_DAYS = 7
PRICE_TOLERANCE = 1e-9
VOLUME_ROUNDING_TOLERANCE_LOTS = 0.5000001
MAX_MISSING_VOLUME_SESSIONS = 5
MAX_MISSING_REFERENCE_SESSIONS = 15
MIN_REFERENCE_COVERAGE = 0.99
MAX_PRICE_MISMATCH_SESSIONS = 3
REQUEST_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Connection": "close",
    "Pragma": "no-cache",
    "Referer": "https://finance.sina.com.cn/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}
DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class SinaEquivalenceError(RuntimeError):
    """Sina cannot prove that raw bars equal forward-adjusted bars."""


def verify_forward_adjustment_identity(
    instrument: Instrument,
    bars: list[Bar],
    *,
    start: date,
    end: date,
    cutoff: date,
    timeout: int,
    proxy_mode: str,
    max_attempts: int,
    retry_base: float,
    retry_max: float,
    retry_jitter: float,
) -> dict[str, object]:
    """Require identity factors and full-history OHLCV agreement."""

    code = f"{instrument.market.lower()}{instrument.symbol}"
    kline_url = f"{KLINE_ENDPOINT}?{urllib.parse.urlencode(_kline_params(code))}"
    factor_url = f"{FACTOR_ENDPOINT}/{code}/qfq.js"
    kline_raw = _download(
        kline_url,
        maximum=MAX_KLINE_BYTES,
        timeout=timeout,
        proxy_mode=proxy_mode,
        max_attempts=max_attempts,
        retry_base=retry_base,
        retry_max=retry_max,
        retry_jitter=retry_jitter,
    )
    factor_raw = _download(
        factor_url,
        maximum=MAX_FACTOR_BYTES,
        timeout=timeout,
        proxy_mode=proxy_mode,
        max_attempts=max_attempts,
        retry_base=retry_base,
        retry_max=retry_max,
        retry_jitter=retry_jitter,
    )
    _validate_identity_factor(factor_raw, code)
    reference = _parse_klines(kline_raw, instrument, cutoff)
    selected_reference = {
        bar.date: bar for bar in reference if start <= bar.date <= end
    }
    selected_bars = {bar.date: bar for bar in bars if start <= bar.date <= end}
    if not selected_reference or not selected_bars:
        raise SinaEquivalenceError(
            f"Sina equivalence history is empty for {instrument.symbol}"
        )
    expected_start = max(start, instrument.listing_date or start)
    first_reference = min(selected_reference)
    latest_reference = max(selected_reference)
    if first_reference > expected_start + timedelta(days=START_TOLERANCE_DAYS):
        raise SinaEquivalenceError(
            f"Sina history starts too late for {instrument.symbol}: {first_reference}"
        )
    if cutoff - latest_reference > timedelta(days=LATEST_TOLERANCE_DAYS):
        raise SinaEquivalenceError(
            f"Sina history is stale for {instrument.symbol}: {latest_reference}"
        )
    missing_reference = sorted(set(selected_bars) - set(selected_reference))
    missing_tencent = sorted(set(selected_reference) - set(selected_bars))
    overlap = sorted(set(selected_bars) & set(selected_reference))
    coverage = len(overlap) / len(selected_bars)
    if (
        missing_tencent
        or len(missing_reference) > MAX_MISSING_REFERENCE_SESSIONS
        or coverage < MIN_REFERENCE_COVERAGE
    ):
        raise SinaEquivalenceError(
            f"Sina/Tencent session coverage differs for {instrument.symbol}: "
            f"missing_reference={len(missing_reference)}, "
            f"missing_tencent={len(missing_tencent)}"
        )
    missing_volume_sessions: list[str] = []
    price_mismatches: list[dict[str, object]] = []
    for on_date in overlap:
        left = selected_bars[on_date]
        right = selected_reference[on_date]
        price_differences = {
            field: abs(float(getattr(left, field)) - float(getattr(right, field)))
            for field in ("open", "close", "high", "low")
        }
        changed_fields = {
            field: difference
            for field, difference in price_differences.items()
            if difference > PRICE_TOLERANCE
        }
        if any(
            difference > instrument.tick_size + PRICE_TOLERANCE
            for difference in changed_fields.values()
        ):
            raise SinaEquivalenceError(
                f"Sina/Tencent OHLCV differs for {instrument.symbol} on {on_date}"
            )
        if changed_fields:
            price_mismatches.append(
                {
                    "session": on_date.isoformat(),
                    "differences": changed_fields,
                }
            )
        if right.volume == 0.0 and left.volume > 0.0:
            missing_volume_sessions.append(on_date.isoformat())
            continue
        if abs(left.volume - right.volume) > VOLUME_ROUNDING_TOLERANCE_LOTS:
            raise SinaEquivalenceError(
                f"Sina/Tencent OHLCV differs for {instrument.symbol} on {on_date}"
            )
    if len(missing_volume_sessions) > MAX_MISSING_VOLUME_SESSIONS:
        raise SinaEquivalenceError(
            f"Sina volume is missing on {len(missing_volume_sessions)} sessions "
            f"for {instrument.symbol}"
        )
    if len(price_mismatches) > MAX_PRICE_MISMATCH_SESSIONS:
        raise SinaEquivalenceError(
            f"Sina/Tencent prices differ on {len(price_mismatches)} sessions "
            f"for {instrument.symbol}"
        )
    return {
        "adjustment_equivalence": "sina_qfq_identity_factor",
        "adjustment_evidence_provider": "sina",
        "adjustment_evidence_scope": "full_history_ohlc_available_volume",
        "adjustment_evidence_rows": len(overlap),
        "adjustment_primary_rows": len(selected_bars),
        "adjustment_evidence_coverage": coverage,
        "adjustment_evidence_missing_reference_sessions": [
            value.isoformat() for value in missing_reference
        ],
        "adjustment_evidence_first_session": first_reference.isoformat(),
        "adjustment_evidence_latest_session": latest_reference.isoformat(),
        "adjustment_factor_sha256": hashlib.sha256(factor_raw).hexdigest(),
        "adjustment_reference_sha256": hashlib.sha256(kline_raw).hexdigest(),
        "adjustment_reference_volume_unit": "lots",
        "adjustment_reference_lot_size": instrument.lot_size,
        "adjustment_evidence_volume_rounding_lots": 0.5,
        "adjustment_evidence_missing_volume_sessions": missing_volume_sessions,
        "adjustment_evidence_price_tolerance": instrument.tick_size,
        "adjustment_evidence_price_mismatches": price_mismatches,
    }


def _kline_params(code: str) -> dict[str, str]:
    return {
        "symbol": code,
        "scale": "240",
        "ma": "no",
        "datalen": str(MAX_ROWS),
    }


def _download(
    url: str,
    *,
    maximum: int,
    timeout: int,
    proxy_mode: str,
    max_attempts: int,
    retry_base: float,
    retry_max: float,
    retry_jitter: float,
) -> bytes:
    request = urllib.request.Request(url, headers=REQUEST_HEADERS)
    last_error: Exception | None = None
    for attempt in range(max(1, max_attempts)):
        try:
            with _open_request(request, timeout, proxy_mode) as response:
                raw = response.read(maximum + 1)
            if len(raw) > maximum:
                raise SinaEquivalenceError(
                    f"Sina response exceeds the {maximum}-byte limit"
                )
            return raw
        except (OSError, RuntimeError) as exc:
            last_error = exc
            if not _should_retry(exc) or attempt + 1 >= max_attempts:
                break
            exponential = min(retry_max, retry_base * (2**attempt))
            time_module.sleep(exponential + random.uniform(0.0, retry_jitter))
    raise SinaEquivalenceError(
        f"Sina equivalence request failed after {max(1, max_attempts)} attempt(s): "
        f"{last_error}"
    ) from last_error


def _validate_identity_factor(raw: bytes, code: str) -> None:
    try:
        text = raw.decode("utf-8")
    except UnicodeError as exc:
        raise SinaEquivalenceError("Sina qfq factor is not UTF-8") from exc
    match = re.fullmatch(
        rf"var {re.escape(code)}qfq=(\{{.*\}})\s*/\*[A-Za-z0-9+/=\s]+\*/\s*",
        text,
        flags=re.DOTALL,
    )
    if match is None:
        raise SinaEquivalenceError(
            f"Sina qfq factor envelope is invalid for {code}"
        )
    try:
        payload = loads_unique_json(match.group(1))
    except ValueError as exc:
        raise SinaEquivalenceError(
            f"Sina qfq factor JSON is invalid for {code}"
        ) from exc
    if not isinstance(payload, dict) or payload.get("total") != 1:
        raise SinaEquivalenceError(
            f"Sina qfq factor is not an identity series for {code}"
        )
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        raise SinaEquivalenceError(
            f"Sina qfq factor rows are invalid for {code}"
        )
    row = data[0]
    if row.get("d") != "1900-01-01" or not (
        _decimal_equals(row.get("f"), Decimal("1"))
        and _decimal_equals(row.get("s"), Decimal("1"))
        and _decimal_equals(row.get("u"), Decimal("0"))
    ):
        raise SinaEquivalenceError(
            f"Sina qfq factor is not identity for {code}"
        )


def _parse_klines(raw: bytes, instrument: Instrument, cutoff: date) -> list[Bar]:
    symbol = instrument.symbol
    if instrument.lot_size < 1:
        raise SinaEquivalenceError(f"Sina lot size is invalid for {symbol}")
    try:
        payload = loads_unique_json(raw.decode("utf-8"))
    except (UnicodeError, ValueError) as exc:
        raise SinaEquivalenceError(
            f"Sina kline JSON is invalid for {symbol}"
        ) from exc
    if not isinstance(payload, list) or not payload or len(payload) > MAX_ROWS:
        raise SinaEquivalenceError(
            f"Sina kline row count is invalid for {symbol}"
        )
    bars: list[Bar] = []
    previous: date | None = None
    for index, row in enumerate(payload):
        if not isinstance(row, dict):
            raise SinaEquivalenceError(
                f"Sina kline row is invalid for {symbol} at {index}"
            )
        raw_date = row.get("day")
        if not isinstance(raw_date, str):
            raise SinaEquivalenceError(
                f"Sina kline date is invalid for {symbol} at {index}"
            )
        try:
            on_date = date.fromisoformat(raw_date)
        except ValueError as exc:
            raise SinaEquivalenceError(
                f"Sina kline date is invalid for {symbol} at {index}"
            ) from exc
        if previous is not None and on_date <= previous:
            raise SinaEquivalenceError(
                f"Sina kline dates are not increasing for {symbol}"
            )
        if on_date > cutoff:
            raise SinaEquivalenceError(
                f"Sina kline exceeds completed cutoff for {symbol}: {on_date}"
            )
        previous = on_date
        bar = Bar(
            date=on_date,
            open=_number(row.get("open"), "open", symbol),
            close=_number(row.get("close"), "close", symbol),
            high=_number(row.get("high"), "high", symbol),
            low=_number(row.get("low"), "low", symbol),
            volume=(
                _number(row.get("volume"), "volume", symbol)
                / instrument.lot_size
            ),
            amount=0.0,
        )
        if (
            min(bar.open, bar.close, bar.high, bar.low) <= 0
            or bar.high < max(bar.open, bar.close, bar.low)
            or bar.low > min(bar.open, bar.close, bar.high)
            or bar.volume < 0
        ):
            raise SinaEquivalenceError(
                f"Sina kline values are invalid for {symbol} on {on_date}"
            )
        bars.append(bar)
    return bars


def _decimal_equals(value: object, expected: Decimal) -> bool:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return False
    try:
        return Decimal(str(value)) == expected
    except InvalidOperation:
        return False


def _number(value: object, field: str, symbol: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise SinaEquivalenceError(f"Sina {field} is invalid for {symbol}")
    try:
        parsed = float(value)
    except ValueError as exc:
        raise SinaEquivalenceError(
            f"Sina {field} is invalid for {symbol}"
        ) from exc
    if not math.isfinite(parsed):
        raise SinaEquivalenceError(f"Sina {field} is non-finite for {symbol}")
    return parsed


def _should_retry(error: Exception) -> bool:
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


def _open_request(
    request: urllib.request.Request, timeout: int, proxy_mode: str
):
    if proxy_mode == "direct":
        return DIRECT_OPENER.open(request, timeout=timeout)
    if proxy_mode == "system":
        return urllib.request.urlopen(request, timeout=timeout)
    raise ValueError(f"Unsupported Sina proxy mode: {proxy_mode!r}")


__all__ = [
    "DIRECT_OPENER",
    "SinaEquivalenceError",
    "verify_forward_adjustment_identity",
]
