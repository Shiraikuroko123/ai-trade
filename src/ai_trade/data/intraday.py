"""Bounded, read-only intraday evidence from Eastmoney.

The ordinary market snapshot deliberately remains daily and completed-session
only. This module is a separate research dataset for historical minute bars.
It validates Eastmoney's f52-f55 OHLC fields and stores immutable local
revisions. Nothing in this module is consumed by strategy, paper accounting, or
broker code.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
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
from .eastmoney import (
    REQUEST_HEADERS,
    _open_request,
    _proxy_mode,
    completed_session_cutoff,
)
from .evidence_io import atomic_create_json, evidence_store_lock


SCHEMA_VERSION = 1
DATASET = "intraday"
ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/trends2/get"
EASTMONEY_UT = "fa5fd1943c7b386f172d6893dbfba10b"
CHINA_TIMEZONE = timezone(timedelta(hours=8))
SUPPORTED_INTERVALS = frozenset({1, 5, 15, 30, 60})
DEFAULT_INTERVAL = 1
DEFAULT_LIMIT = 480
MAX_LIMIT = 1_500
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
MAX_ROWS = 5_000
MAX_REVISIONS_PER_PERIOD = 100
MAX_REVISION_BYTES = 16 * 1024 * 1024
MAX_TEXT = 256
_SYMBOL = re.compile(r"\d{6}\Z")
_DATE_DIRECTORY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_REVISION_FILE = re.compile(r"revision_(\d{8})\.json\Z")
_REVISION_ID = re.compile(r"intraday_[0-9a-f]{32}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_AUTHORITY = {"research_only": True, "execution_authorized": False}


@dataclass(frozen=True)
class IntradayQuery:
    """Bounded local query for one symbol/date/aggregation interval."""

    symbol: str
    trade_date: date | None = None
    interval: int = DEFAULT_INTERVAL
    limit: int = DEFAULT_LIMIT
    include_revisions: bool = False


class IntradayProviderError(RuntimeError):
    """Raised when the provider response cannot become research evidence."""


def refresh_intraday(
    config: AppConfig,
    symbol: str,
    *,
    trade_date: date | None = None,
    interval: int = DEFAULT_INTERVAL,
    limit: int = DEFAULT_LIMIT,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Download and publish one bounded historical intraday snapshot."""

    instrument = _instrument(config, symbol)
    selected_interval, selected_limit = _validate_query_values(interval, limit)
    market_close = str(config.raw.get("data", {}).get("market_close_time", "15:30"))
    cutoff = completed_session_cutoff(as_of, market_close)
    selected_date = trade_date or cutoff
    if not isinstance(selected_date, date) or isinstance(selected_date, datetime):
        raise ValueError("trade_date must be a date")
    if selected_date > cutoff:
        raise ValueError(
            "intraday trade_date must not be after the completed-session cutoff"
        )
    payload, raw_sha256, response_bytes = _download(
        config,
        instrument,
        selected_date,
        cutoff=cutoff,
    )
    bars, provider_meta = _parse_payload(
        payload,
        instrument,
        selected_date,
        interval=selected_interval,
        limit=selected_limit,
    )
    source = {
        "provider": "eastmoney",
        "endpoint": ENDPOINT,
        "secid": _secid(instrument),
        "fields": ["f51", "f52", "f53", "f54", "f55", "f56", "f57", "f58"],
        "response_sha256": raw_sha256,
        "response_bytes": response_bytes,
        "provider_trends_total": int(provider_meta["trends_total"]),
        "raw_bar_count": int(provider_meta["raw_bar_count"]),
        "open_method": "provider_reported_f52",
        "average_method": "provider_cumulative_f58",
        "volume_unit": "provider_native_lots",
        "certification": "not_exchange_certified",
    }
    record = {
        "schema_version": SCHEMA_VERSION,
        "dataset": DATASET,
        "available": True,
        "status": "current",
        "symbol": instrument.symbol,
        "name": instrument.name,
        "market": instrument.market,
        "trade_date": selected_date.isoformat(),
        "interval_minutes": selected_interval,
        "retrieved_at": _now(),
        "source": source,
        "bars": bars,
        "summary": _summary(bars),
        "authority": dict(_AUTHORITY),
        "warnings": [
            {
                "code": "not_exchange_certified",
                "message": (
                    "Eastmoney intraday data is a third-party research feed; "
                    "it is not exchange-certified and must not authorize orders."
                ),
            },
            {
                "code": "provider_minute_methodology",
                "message": (
                    "Minute OHLC uses Eastmoney fields f52-f55. Wider intervals "
                    "are deterministic local aggregations; f58 remains the "
                    "provider cumulative average at the end of each window."
                ),
            },
        ],
    }
    return IntradayStore(config).publish(record)


class IntradayStore:
    """Immutable local revision store; reads never contact the provider."""

    def __init__(self, config_or_root: AppConfig | str | Path):
        if isinstance(config_or_root, AppConfig) or hasattr(config_or_root, "intraday_dir"):
            raw_root = getattr(config_or_root, "intraday_dir", None)
            if raw_root is None:
                raw_root = config_or_root.resolve("state/intraday")
        else:
            raw_root = Path(config_or_root)
        self.root = Path(raw_root).resolve()

    def list(self, query: IntradayQuery) -> dict[str, Any]:
        _validate_query(query)
        periods = self._periods(query.symbol, query.interval)
        base_interval = 1
        base_periods = (
            self._periods(query.symbol, base_interval)
            if query.interval != base_interval
            else []
        )
        available_periods = sorted(set(periods) | set(base_periods))
        target = query.trade_date or (
            available_periods[-1] if available_periods else None
        )
        if target is None or target not in available_periods:
            return _unavailable(query, "intraday_not_refreshed")
        derived_from_base = target not in periods and target in base_periods
        chain = self._load_chain(
            query.symbol,
            target,
            base_interval if derived_from_base else query.interval,
        )
        latest = _clone(chain[-1])
        bars = list(latest.get("bars", []))
        if derived_from_base:
            bars = _aggregate(bars, query.interval)
            latest["interval_minutes"] = query.interval
            latest["base_interval_minutes"] = base_interval
            latest["derived_view"] = True
            latest["warnings"] = [
                *list(latest.get("warnings") or []),
                {
                    "code": "locally_aggregated_view",
                    "message": (
                        "No separately published interval revision was found; "
                        "this view is deterministically aggregated from the "
                        "validated one-minute revision."
                    ),
                },
            ]
        latest["bars"] = bars[-query.limit :]
        latest["summary"] = {
            **_summary(bars),
            "returned_count": len(latest["bars"]),
            "truncated": len(bars) > len(latest["bars"]),
        }
        latest["filters"] = _query_payload(query)
        latest["revisions"] = [_revision_summary(item) for item in chain]
        latest["freshness"] = {
            "status": "local",
            "trade_date": target.isoformat(),
        }
        if not query.include_revisions:
            latest["revisions"] = latest["revisions"][-1:]
        return latest

    def publish(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        with evidence_store_lock(self.root, "Intraday"):
            return self._publish_unlocked(draft)

    def _publish_unlocked(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        record = _prepare_record(draft)
        symbol = str(record["symbol"])
        trade_date = date.fromisoformat(str(record["trade_date"]))
        interval = int(record["interval_minutes"])
        chain = self._load_chain(symbol, trade_date, interval, missing_ok=True)
        evidence = _fingerprint(
            {
                "schema_version": SCHEMA_VERSION,
                "dataset": DATASET,
                "symbol": symbol,
                "trade_date": trade_date.isoformat(),
                "interval_minutes": interval,
                "source": record["source"],
                "bars": record["bars"],
            }
        )
        if chain and chain[-1].get("evidence_fingerprint") == evidence:
            result = _clone(chain[-1])
            result["reused"] = True
            result["revisions"] = [_revision_summary(item) for item in chain]
            return result
        if len(chain) >= MAX_REVISIONS_PER_PERIOD:
            raise RuntimeError("intraday revision capacity reached")
        revision = len(chain) + 1
        previous = chain[-1] if chain else None
        record = {
            **record,
            "revision_id": f"intraday_{uuid4().hex}",
            "revision": revision,
            "reused": False,
            "evidence_fingerprint": evidence,
            "supersedes": previous.get("revision_id") if previous else None,
            "supersedes_fingerprint": (
                previous.get("record_fingerprint") if previous else None
            ),
            "record_fingerprint": None,
        }
        record["revisions"] = [
            *[_revision_summary(item) for item in chain],
            _revision_summary(record),
        ]
        record["record_fingerprint"] = _record_fingerprint(record)
        record["revisions"][-1]["record_fingerprint"] = record["record_fingerprint"]
        _validate_record(record, symbol=symbol, trade_date=trade_date, interval=interval)
        self._atomic_create(record)
        committed = self._load_chain(symbol, trade_date, interval)
        result = _clone(committed[-1])
        result["revisions"] = [_revision_summary(item) for item in committed]
        return result

    def _periods(self, symbol: str, interval: int) -> list[date]:
        root = self.root / symbol / f"interval-{interval}"
        if not root.exists():
            return []
        if root.is_symlink() or not root.is_dir():
            raise RuntimeError("intraday period root is invalid")
        periods: list[date] = []
        for path in root.iterdir():
            if path.is_symlink() or not path.is_dir() or not _DATE_DIRECTORY.fullmatch(path.name):
                raise RuntimeError("intraday period directory is invalid")
            periods.append(date.fromisoformat(path.name))
            if len(periods) > MAX_ROWS:
                raise RuntimeError("intraday store contains too many periods")
        return sorted(periods)

    def _load_chain(
        self,
        symbol: str,
        trade_date: date,
        interval: int,
        *,
        missing_ok: bool = False,
    ) -> list[dict[str, Any]]:
        directory = self.root / symbol / f"interval-{interval}" / trade_date.isoformat()
        if not directory.exists():
            if missing_ok:
                return []
            raise RuntimeError("intraday period is missing")
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("intraday period is invalid")
        paths: list[tuple[int, Path]] = []
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("intraday revision must be a regular file")
            match = _REVISION_FILE.fullmatch(path.name)
            if match is None:
                raise RuntimeError("unexpected intraday revision file")
            paths.append((int(match.group(1)), path))
        paths.sort()
        if not paths:
            raise RuntimeError("intraday period has no revisions")
        if len(paths) > MAX_REVISIONS_PER_PERIOD:
            raise RuntimeError("intraday period contains too many revisions")
        if [number for number, _ in paths] != list(range(1, len(paths) + 1)):
            raise RuntimeError("intraday revision sequence is not contiguous")
        chain: list[dict[str, Any]] = []
        previous: dict[str, Any] | None = None
        for revision, path in paths:
            value = load_unique_json(path, max_bytes=16 * 1024 * 1024)
            if not isinstance(value, dict):
                raise RuntimeError("intraday revision must be an object")
            _validate_record(
                value,
                symbol=symbol,
                trade_date=trade_date,
                interval=interval,
                expected_revision=revision,
            )
            if previous is None:
                if value.get("supersedes") is not None or value.get(
                    "supersedes_fingerprint"
                ) is not None:
                    raise RuntimeError("first intraday revision has a parent")
            elif (
                value.get("supersedes") != previous.get("revision_id")
                or value.get("supersedes_fingerprint")
                != previous.get("record_fingerprint")
            ):
                raise RuntimeError("intraday supersedes chain is invalid")
            expected_history = [*[_revision_summary(item) for item in chain], _revision_summary(value)]
            if value.get("revisions") != expected_history:
                raise RuntimeError("intraday embedded revision history is invalid")
            chain.append(value)
            previous = value
        return chain

    def _atomic_create(self, record: Mapping[str, Any]) -> None:
        symbol = str(record["symbol"])
        trade_date = date.fromisoformat(str(record["trade_date"]))
        interval = int(record["interval_minutes"])
        directory = self.root / symbol / f"interval-{interval}" / trade_date.isoformat()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"revision_{int(record['revision']):08d}.json"
        if path.exists():
            existing = load_unique_json(path, max_bytes=16 * 1024 * 1024)
            if existing != record:
                raise RuntimeError("intraday revision already exists with different content")
            return
        atomic_create_json(
            self.root,
            path,
            record,
            label="intraday",
            maximum_bytes=MAX_REVISION_BYTES,
        )


def _download(
    config: AppConfig,
    instrument: Instrument,
    trade_date: date,
    *,
    cutoff: date,
) -> tuple[dict[str, Any], str, int]:
    params = {
        "secid": _secid(instrument),
        "fields1": "f1,f2,f3,f4,f5,f6,f7,f8,f9,f10,f11,f12,f13",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58",
        "iscr": "0",
        "ndays": "5",
        "ut": EASTMONEY_UT,
        "cb": "",
        "_": str(time_module.time_ns()),
    }
    url = f"{ENDPOINT}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers=REQUEST_HEADERS, method="GET")
    data_config = config.raw.get("data", {})
    timeout = int(data_config.get("timeout_seconds", 20))
    proxy_mode = _proxy_mode(config)
    attempts = min(3, max(1, int(data_config.get("max_attempts", 3))))
    errors: list[str] = []
    for attempt in range(attempts):
        try:
            with _open_request(request, timeout, proxy_mode) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise IntradayProviderError("intraday response is too large")
            value = loads_unique_json(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise IntradayProviderError("intraday response is not an object")
            return value, sha256(raw).hexdigest(), len(raw)
        except Exception as exc:  # provider failures are recorded, never hidden
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt + 1 < attempts:
                time_module.sleep(min(2.0, 0.25 * (2**attempt)))
    raise IntradayProviderError(
        f"intraday download failed for {instrument.symbol} {trade_date.isoformat()}: "
        + " | ".join(errors)
    )


def _parse_payload(
    payload: Mapping[str, Any],
    instrument: Instrument,
    trade_date: date,
    *,
    interval: int,
    limit: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    if payload.get("rc") not in (None, 0):
        raise IntradayProviderError(f"Eastmoney returned rc={payload.get('rc')}")
    data = payload.get("data")
    if not isinstance(data, dict):
        raise IntradayProviderError("intraday response data is missing")
    trends = data.get("trends")
    if not isinstance(trends, list):
        raise IntradayProviderError("intraday trends are missing")
    _positive_number(data.get("prePrice"), "prePrice")
    parsed: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, raw_line in enumerate(trends):
        if not isinstance(raw_line, str):
            raise IntradayProviderError(f"intraday row {index} is not text")
        fields = raw_line.split(",")
        if len(fields) < 8:
            raise IntradayProviderError(f"intraday row {index} has too few fields")
        try:
            stamp = datetime.strptime(fields[0], "%Y-%m-%d %H:%M").replace(
                tzinfo=CHINA_TIMEZONE
            )
        except ValueError as exc:
            raise IntradayProviderError(f"intraday row {index} timestamp is invalid") from exc
        if stamp.date() != trade_date:
            continue
        key = stamp.isoformat()
        if key in seen:
            raise IntradayProviderError("intraday timestamps are duplicated")
        seen.add(key)
        open_price = _positive_number(fields[1], f"open row {index}")
        close = _positive_number(fields[2], f"close row {index}")
        high = _positive_number(fields[3], f"high row {index}")
        low = _positive_number(fields[4], f"low row {index}")
        volume = _nonnegative_number(fields[5], f"volume row {index}")
        amount = _nonnegative_number(fields[6], f"amount row {index}")
        average = _positive_number(fields[7], f"average row {index}")
        if high < max(open_price, close) or low > min(open_price, close):
            raise IntradayProviderError(f"intraday OHLC relationship is invalid at row {index}")
        if not _session_time(stamp.time()):
            raise IntradayProviderError(f"intraday timestamp is outside the session at row {index}")
        parsed.append(
            {
                "timestamp": stamp.isoformat(),
                "time": stamp.strftime("%H:%M"),
                "open": open_price,
                "close": close,
                "high": high,
                "low": low,
                "volume": volume,
                "amount": amount,
                "average": average,
                "open_derived": False,
            }
        )
    if not parsed:
        raise IntradayProviderError(
            f"no minute bars returned for {instrument.symbol} {trade_date.isoformat()}"
        )
    parsed.sort(key=lambda item: item["timestamp"])
    aggregated = _aggregate(parsed, interval)
    return aggregated[-limit:], {
        "trends_total": _bounded_int(data.get("trendsTotal"), "trendsTotal"),
        "raw_bar_count": len(parsed),
    }


def _aggregate(rows: Sequence[Mapping[str, Any]], interval: int) -> list[dict[str, Any]]:
    if interval == 1:
        return [dict(row) for row in rows]
    groups: list[list[Mapping[str, Any]]] = []
    keys: list[tuple[date, int]] = []
    for row in rows:
        stamp = datetime.fromisoformat(str(row["timestamp"]))
        start_minutes = 570 if stamp.hour < 12 else 780
        minute = stamp.hour * 60 + stamp.minute
        key = (stamp.date(), start_minutes + ((minute - start_minutes) // interval) * interval)
        if not groups or key != keys[-1]:
            groups.append([])
            keys.append(key)
        groups[-1].append(row)
    result: list[dict[str, Any]] = []
    for key, group in zip(keys, groups):
        first = group[0]
        last = group[-1]
        start = datetime.combine(key[0], time.min, tzinfo=CHINA_TIMEZONE) + timedelta(minutes=key[1])
        volume = sum(float(item["volume"]) for item in group)
        amount = sum(float(item["amount"]) for item in group)
        result.append(
            {
                "timestamp": start.isoformat(),
                "time": start.strftime("%H:%M"),
                "open": float(first["open"]),
                "close": float(last["close"]),
                "high": max(float(item["high"]) for item in group),
                "low": min(float(item["low"]) for item in group),
                "volume": volume,
                "amount": amount,
                "average": float(last["average"]),
                "open_derived": False,
            }
        )
    return result


def _summary(rows: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    return {
        "bar_count": len(rows),
        "start": rows[0]["timestamp"] if rows else None,
        "end": rows[-1]["timestamp"] if rows else None,
        "total_volume": sum(float(item["volume"]) for item in rows),
        "total_amount": sum(float(item["amount"]) for item in rows),
    }


def _prepare_record(value: Mapping[str, Any]) -> dict[str, Any]:
    record = _clone(value)
    if not isinstance(record, dict):
        raise ValueError("intraday record must be an object")
    try:
        trade_date = date.fromisoformat(str(record.get("trade_date")))
        interval = int(record.get("interval_minutes", 0))
    except (TypeError, ValueError) as exc:
        raise ValueError("intraday draft identity is invalid") from exc
    _validate_draft(
        record,
        symbol=str(record.get("symbol", "")),
        trade_date=trade_date,
        interval=interval,
    )
    return record


def _validate_draft(
    value: Mapping[str, Any],
    *,
    symbol: str,
    trade_date: date,
    interval: int,
) -> None:
    _instrument_like_symbol(symbol)
    if value.get("schema_version") != SCHEMA_VERSION or value.get("dataset") != DATASET:
        raise RuntimeError("intraday record schema is invalid")
    if value.get("symbol") != symbol or value.get("trade_date") != trade_date.isoformat():
        raise RuntimeError("intraday record identity is invalid")
    if value.get("interval_minutes") != interval or interval not in SUPPORTED_INTERVALS:
        raise RuntimeError("intraday interval is invalid")
    bars = value.get("bars")
    if not isinstance(bars, list) or not bars or len(bars) > MAX_LIMIT:
        raise RuntimeError("intraday bars are invalid")
    _validate_bars(bars, trade_date)


def _validate_record(
    value: Mapping[str, Any],
    *,
    symbol: str,
    trade_date: date,
    interval: int,
    expected_revision: int | None = None,
) -> None:
    _validate_draft(value, symbol=symbol, trade_date=trade_date, interval=interval)
    if not _REVISION_ID.fullmatch(str(value.get("revision_id", ""))):
        raise RuntimeError("intraday revision id is invalid")
    revision = value.get("revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 1:
        raise RuntimeError("intraday revision number is invalid")
    if expected_revision is not None and revision != expected_revision:
        raise RuntimeError("intraday revision number is not expected")
    if not _FINGERPRINT.fullmatch(str(value.get("evidence_fingerprint", ""))):
        raise RuntimeError("intraday evidence fingerprint is invalid")
    if not _FINGERPRINT.fullmatch(str(value.get("record_fingerprint", ""))):
        raise RuntimeError("intraday record fingerprint is invalid")
    expected_fingerprint = _record_fingerprint(value)
    if value.get("record_fingerprint") != expected_fingerprint:
        raise RuntimeError("intraday record fingerprint does not match content")


def _validate_bars(bars: Sequence[Mapping[str, Any]], trade_date: date) -> None:
    previous: datetime | None = None
    for row in bars:
        if not isinstance(row, dict):
            raise RuntimeError("intraday bar is invalid")
        try:
            stamp = datetime.fromisoformat(str(row["timestamp"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("intraday bar timestamp is invalid") from exc
        if (
            stamp.tzinfo is None
            or stamp.utcoffset() != timedelta(hours=8)
            or stamp.date() != trade_date
            or (previous is not None and stamp <= previous)
        ):
            raise RuntimeError("intraday bar timestamps are not ordered")
        if row.get("time") != stamp.strftime("%H:%M"):
            raise RuntimeError("intraday bar time does not match timestamp")
        if not _session_time(stamp.timetz()):
            raise RuntimeError("intraday bar timestamp is outside the session")
        if not isinstance(row.get("open_derived"), bool):
            raise RuntimeError("intraday open provenance is invalid")
        previous = stamp
        numbers = [row.get(name) for name in ("open", "close", "high", "low", "volume", "amount", "average")]
        if not all(isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(float(item)) for item in numbers):
            raise RuntimeError("intraday bar contains a non-finite number")
        open_price, close, high, low, volume, amount, _ = map(float, numbers)
        if min(open_price, close, high, low) <= 0 or volume < 0 or amount < 0:
            raise RuntimeError("intraday bar value is out of range")
        if high < max(open_price, close) or low > min(open_price, close):
            raise RuntimeError("intraday bar OHLC relationship is invalid")


def _record_fingerprint(value: Mapping[str, Any]) -> str:
    payload = _clone(value)
    if isinstance(payload, dict):
        payload.pop("record_fingerprint", None)
        history = payload.get("revisions")
        if isinstance(history, list) and history:
            tail = history[-1]
            if isinstance(tail, dict):
                tail["record_fingerprint"] = None
    return _fingerprint(payload)


def _instrument(config: AppConfig, symbol: str) -> Instrument:
    if not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol.strip()) is None:
        raise ValueError("symbol must be a six-digit security code")
    selected = symbol.strip()
    for item in config.instruments:
        if item.symbol == selected:
            return item
    raise ValueError("symbol must be in the configured security master")


def _secid(instrument: Instrument) -> str:
    market = {"SH": "1", "SZ": "0", "BJ": "0"}.get(instrument.market)
    if market is None:
        raise ValueError(f"Unsupported market for intraday data: {instrument.market}")
    return f"{market}.{instrument.symbol}"


def _validate_query_values(interval: int, limit: int) -> tuple[int, int]:
    if isinstance(interval, bool) or not isinstance(interval, int) or interval not in SUPPORTED_INTERVALS:
        raise ValueError("interval must be one of 1, 5, 15, 30, or 60 minutes")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_LIMIT}")
    return interval, limit


def _validate_query(query: IntradayQuery) -> None:
    _instrument_like_symbol(query.symbol)
    _validate_query_values(query.interval, query.limit)
    if query.trade_date is not None and (
        not isinstance(query.trade_date, date) or isinstance(query.trade_date, datetime)
    ):
        raise ValueError("trade_date must be a date")


def _instrument_like_symbol(value: Any) -> None:
    if not isinstance(value, str) or _SYMBOL.fullmatch(value) is None:
        raise ValueError("symbol must be a six-digit security code")


def _session_time(value: time) -> bool:
    minutes = value.hour * 60 + value.minute
    return 570 <= minutes <= 690 or 780 <= minutes <= 900


def _positive_number(value: Any, label: str) -> float:
    result = _finite_number(value, label)
    if result <= 0:
        raise IntradayProviderError(f"{label} must be positive")
    return result


def _nonnegative_number(value: Any, label: str) -> float:
    result = _finite_number(value, label)
    if result < 0:
        raise IntradayProviderError(f"{label} must not be negative")
    return result


def _finite_number(value: Any, label: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise IntradayProviderError(f"{label} is not numeric") from exc
    if not math.isfinite(result):
        raise IntradayProviderError(f"{label} is not finite")
    return result


def _bounded_int(value: Any, label: str) -> int:
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise IntradayProviderError(f"{label} is invalid") from exc
    if result < 0 or result > MAX_ROWS:
        raise IntradayProviderError(f"{label} is out of range")
    return result


def _query_payload(query: IntradayQuery) -> dict[str, Any]:
    return {
        "symbol": query.symbol,
        "trade_date": query.trade_date.isoformat() if query.trade_date else None,
        "interval": query.interval,
        "limit": query.limit,
        "include_revisions": query.include_revisions,
    }


def _revision_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "revision_id": value.get("revision_id"),
        "revision": value.get("revision"),
        "trade_date": value.get("trade_date"),
        "interval_minutes": value.get("interval_minutes"),
        "retrieved_at": value.get("retrieved_at"),
        "evidence_fingerprint": value.get("evidence_fingerprint"),
        "record_fingerprint": value.get("record_fingerprint"),
        "supersedes": value.get("supersedes"),
    }


def _unavailable(query: IntradayQuery, code: str) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": DATASET,
        "available": False,
        "status": "unavailable",
        "symbol": query.symbol,
        "trade_date": query.trade_date.isoformat() if query.trade_date else None,
        "interval_minutes": query.interval,
        "bars": [],
        "summary": {"bar_count": 0, "returned_count": 0, "truncated": False},
        "filters": _query_payload(query),
        "authority": dict(_AUTHORITY),
        "errors": [{"code": code, "message": "No validated local intraday snapshot is available."}],
        "warnings": [],
        "revisions": [],
    }


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True))


def _fingerprint(value: Any) -> str:
    return sha256(
        json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    ).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
