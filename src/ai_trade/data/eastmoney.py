from __future__ import annotations

import csv
import hashlib
import http.client
import logging
import math
import os
import random
import re
import shutil
import socket
import ssl
import time as time_module
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from ..config import AppConfig
from ..json_utils import load_unique_json, loads_unique_json
from ..models import Bar, Instrument
from . import tencent  # noqa: F401 - compatibility patch/import path
from .cache_snapshot import (
    install_snapshot,
    recover_pending_snapshot,
    snapshot_refresh_lock,
)

LOGGER = logging.getLogger(__name__)
ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
EASTMONEY_UT = "fa5fd1943c7b386f172d6893dbfba10b"
CHINA_TIMEZONE = timezone(timedelta(hours=8))
REQUIRED_COLUMNS = {"date", "open", "close", "high", "low", "volume", "amount"}
MAX_MANIFEST_BYTES = 2 * 1024 * 1024
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
LEGACY_START_TOLERANCE_DAYS = 10
REQUEST_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
    "Cache-Control": "no-cache",
    "Connection": "close",
    "Pragma": "no-cache",
    "Referer": "https://quote.eastmoney.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
}
DIRECT_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class EastmoneyDownloadError(RuntimeError):
    """A failed request with the complete set of retry attempt errors."""

    def __init__(self, message: str, attempt_errors: list[Exception]):
        super().__init__(message)
        self.attempt_errors = tuple(attempt_errors)


def download_universe(config: AppConfig, force: bool = False) -> dict[str, Path]:
    config.cache_dir.mkdir(parents=True, exist_ok=True)
    with snapshot_refresh_lock(config.cache_dir):
        recover_pending_snapshot(config.cache_dir)
        return _download_universe_locked(config, force)


def _download_universe_locked(
    config: AppConfig, force: bool = False
) -> dict[str, Path]:
    # Keep the snapshot transaction in this module, but resolve concrete
    # network implementations through the shared provider boundary.  Existing
    # Eastmoney/Tencent functions remain compatibility entry points for tests
    # and third-party callers.
    from .providers import provider_for

    market_close = config.raw["data"].get("market_close_time", "15:30")
    target_session = completed_session_cutoff(market_close=market_close)
    final_paths = {
        instrument.symbol: config.cache_dir / f"{instrument.symbol}.csv"
        for instrument in config.instruments
    }
    if not force and all(
        cache_is_current(
            final_paths[instrument.symbol],
            today=_instrument_cutoff(instrument, market_close, target_session),
            market_close=market_close,
        )
        for instrument in config.instruments
    ):
        return final_paths

    staging = config.cache_dir / f".snapshot-{uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        staged_paths: dict[str, Path] = {}
        sources: dict[str, str] = {}
        network_errors: dict[str, list[str]] = {}
        fallback_reasons: dict[str, str | None] = {}
        provider_metadata: dict[str, dict[str, object]] = {}
        request_interval = max(
            0.0, float(config.raw["data"].get("request_interval_seconds", 2.0))
        )
        request_jitter = max(
            0.0, float(config.raw["data"].get("request_jitter_seconds", 0.5))
        )
        failure_cooldown = max(
            0.0, float(config.raw["data"].get("failure_cooldown_seconds", 20.0))
        )
        primary_provider_name = config.raw["data"].get("provider", "eastmoney")
        primary_provider = provider_for(primary_provider_name)
        fallback_provider_name = config.raw["data"].get(
            "fallback_provider", "tencent"
        )
        fallback_provider = (
            None
            if fallback_provider_name == "none"
            else provider_for(fallback_provider_name)
        )
        proxy_mode = _proxy_mode(config)
        circuit_reason: str | None = None
        circuit_symbol: str | None = None
        for index, instrument in enumerate(config.instruments):
            errors: list[str] = []
            network_errors[instrument.symbol] = errors
            provider_metadata[instrument.symbol] = {}
            staged = staging / f"{instrument.symbol}.csv"
            instrument_cutoff = _instrument_cutoff(
                instrument, market_close, target_session
            )
            primary_attempted = circuit_reason is None
            primary_error: Exception | None = None
            if circuit_reason is not None:
                detail = (
                    f"{primary_provider.descriptor.display_name} circuit breaker "
                    "open; skipped after transport "
                    f"failure on {circuit_symbol}: {circuit_reason}"
                )
                errors.append(detail)
                primary_error = RuntimeError(detail)
            else:
                try:
                    staged_paths[instrument.symbol] = primary_provider.download(
                        config,
                        instrument,
                        staged,
                        cache_path=final_paths[instrument.symbol],
                        cutoff=instrument_cutoff,
                        proxy_mode=proxy_mode,
                        network_errors=errors,
                        provider_metadata=provider_metadata[instrument.symbol],
                    )
                    sources[instrument.symbol] = primary_provider.primary_source_label
                    fallback_reasons[instrument.symbol] = None
                except Exception as exc:
                    primary_error = exc
                    if primary_provider.is_transport_failure(exc):
                        circuit_reason = f"{type(exc).__name__}: {exc}"
                        circuit_symbol = instrument.symbol

            if primary_error is not None:
                primary_reason = (
                    f"{primary_provider.descriptor.display_name} "
                    f"{type(primary_error).__name__}: {primary_error}"
                )
                fallback_error: Exception | None = None
                if fallback_provider is not None:
                    try:
                        staged_paths[instrument.symbol] = fallback_provider.download(
                            config,
                            instrument,
                            staged,
                            cache_path=final_paths[instrument.symbol],
                            cutoff=instrument_cutoff,
                            proxy_mode=proxy_mode,
                            network_errors=errors,
                            provider_metadata=provider_metadata[instrument.symbol],
                        )
                        sources[instrument.symbol] = (
                            fallback_provider.fallback_source_label
                        )
                        fallback_reasons[instrument.symbol] = primary_reason
                        LOGGER.warning(
                            "%s failed for %s; %s fallback succeeded: %s",
                            primary_provider.descriptor.display_name,
                            instrument.symbol,
                            fallback_provider.descriptor.display_name,
                            primary_error,
                        )
                    except Exception as exc:
                        fallback_error = exc
                        errors.append(
                            f"{fallback_provider.descriptor.key.title()} fallback "
                            f"failed: {type(exc).__name__}: {exc}"
                        )

                if fallback_provider is None or fallback_error is not None:
                    fallback = final_paths[instrument.symbol]
                    _stage_recent_completed_cache(
                        config,
                        instrument,
                        fallback,
                        staged,
                        instrument_cutoff,
                    )
                    staged_paths[instrument.symbol] = staged
                    sources[instrument.symbol] = "validated_local_fallback"
                    fallback_reason = primary_reason
                    if fallback_error is not None:
                        fallback_reason += (
                            f"; {fallback_provider.descriptor.display_name} "
                            f"{type(fallback_error).__name__}: {fallback_error}"
                        )
                    fallback_reasons[instrument.symbol] = fallback_reason
                    LOGGER.warning(
                        "Network providers failed for %s; reused recent validated "
                        "cache: %s",
                        instrument.symbol,
                        fallback_reason,
                    )
            if index + 1 < len(config.instruments):
                delay = request_interval + random.uniform(0.0, request_jitter)
                if primary_attempted and (
                    primary_error is not None
                    or errors
                    or sources[instrument.symbol] == "validated_local_fallback"
                ):
                    delay += failure_cooldown
                if delay > 0:
                    time_module.sleep(delay)

        staged_bars = {
            symbol: load_cached_bars(path) for symbol, path in staged_paths.items()
        }
        benchmark = config.strategy.benchmark
        reference_date = staged_bars[benchmark][-1].date
        common_symbols = set(config.active_symbols(reference_date))
        common_symbols.add(benchmark)
        common_sessions = {bar.date for bar in staged_bars[benchmark]}
        for symbol in sorted(common_symbols - {benchmark}):
            common_sessions.intersection_update(
                bar.date for bar in staged_bars[symbol]
            )
        if not common_sessions:
            raise RuntimeError(
                "Downloaded snapshot files share no common trading session"
            )
        latest_common_session = max(common_sessions)

        manifest = {
            "provider": config.raw["data"]["provider"],
            "adjustment": config.raw["data"].get("adjustment", "forward"),
            "downloaded_at": datetime.now(CHINA_TIMEZONE).isoformat(),
            "requested_from": config.raw["data"]["start"],
            "requested_through": target_session.isoformat(),
            "completed_session_cutoff": target_session.isoformat(),
            "completed_through": latest_common_session.isoformat(),
            "latest_common_session": latest_common_session.isoformat(),
            "request_policy": {
                "mode": "serial",
                "proxy_mode": proxy_mode,
                "primary_provider": primary_provider_name,
                "fallback_provider": fallback_provider_name,
                "provider_chain": [
                    primary_provider_name,
                    *(
                        []
                        if fallback_provider_name == "none"
                        else [fallback_provider_name]
                    ),
                    "validated_local_cache",
                ],
                "primary_provider_circuit_breaker": {
                    "opened": circuit_reason is not None,
                    "trigger_symbol": circuit_symbol,
                    "reason": circuit_reason,
                },
                "eastmoney_circuit_breaker": {
                    "opened": circuit_reason is not None,
                    "trigger_symbol": circuit_symbol,
                    "reason": circuit_reason,
                },
                "timeout_seconds": int(
                    config.raw["data"].get("timeout_seconds", 20)
                ),
                "request_interval_seconds": request_interval,
                "request_jitter_seconds": request_jitter,
                "failure_cooldown_seconds": failure_cooldown,
                "max_attempts": int(config.raw["data"].get("max_attempts", 4)),
                "eastmoney_max_attempts": int(
                    config.raw["data"].get(
                        "eastmoney_max_attempts",
                        config.raw["data"].get("max_attempts", 4),
                    )
                ),
                "retry_base_seconds": float(
                    config.raw["data"].get("retry_base_seconds", 1.0)
                ),
                "retry_max_seconds": float(
                    config.raw["data"].get("retry_max_seconds", 8.0)
                ),
                "retry_jitter_seconds": float(
                    config.raw["data"].get("retry_jitter_seconds", 0.5)
                ),
            },
            "files": {
                symbol: {
                    "rows": len(staged_bars[symbol]),
                    "sha256": _file_sha256(staged_paths[symbol]),
                    "source": sources[symbol],
                    "latest_session": staged_bars[symbol][-1].date.isoformat(),
                    "network_errors": network_errors[symbol],
                    "fallback_reason": fallback_reasons[symbol],
                    **provider_metadata[symbol],
                }
                for symbol in sorted(staged_paths)
            },
        }
        install_snapshot(
            config.cache_dir,
            {
                f"{symbol}.csv": staged
                for symbol, staged in staged_paths.items()
            },
            manifest,
        )
        return final_paths
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def download_instrument(
    config: AppConfig,
    instrument: Instrument,
    force: bool = False,
    output_path: Path | None = None,
    *,
    network_errors: list[str] | None = None,
    cutoff: date | None = None,
    proxy_mode: str | None = None,
) -> Path:
    output = output_path or config.cache_dir / f"{instrument.symbol}.csv"
    market_close = config.raw["data"].get("market_close_time", "15:30")
    if (
        output_path is None
        and output.exists()
        and not force
        and cache_is_current(
            output,
            today=_instrument_cutoff(instrument, market_close),
            market_close=market_close,
        )
    ):
        return output

    market_id = "1" if instrument.market.upper() == "SH" else "0"
    adjustment = {"none": "0", "forward": "1", "backward": "2"}[
        config.raw["data"].get("adjustment", "forward")
    ]
    params = {
        "secid": f"{market_id}.{instrument.symbol}",
        "fields1": "f1,f2,f3,f4,f5,f6",
        "fields2": "f51,f52,f53,f54,f55,f56,f57,f58,f59,f60,f61",
        "klt": "101",
        "fqt": adjustment,
        "beg": config.raw["data"]["start"].replace("-", ""),
        "end": config.raw["data"]["end"].replace("-", ""),
        "ut": EASTMONEY_UT,
    }
    timeout = int(config.raw["data"].get("timeout_seconds", 20))
    request_cutoff = cutoff or completed_session_cutoff(market_close=market_close)
    selected_proxy_mode = (
        _validated_proxy_mode(proxy_mode, "proxy_mode")
        if proxy_mode is not None
        else _proxy_mode(config)
    )
    max_attempts = max(
        1,
        int(
            config.raw["data"].get(
                "eastmoney_max_attempts",
                config.raw["data"].get("max_attempts", 4),
            )
        ),
    )
    retry_base = max(0.0, float(config.raw["data"].get("retry_base_seconds", 1.0)))
    retry_max = max(retry_base, float(config.raw["data"].get("retry_max_seconds", 8.0)))
    retry_jitter = max(0.0, float(config.raw["data"].get("retry_jitter_seconds", 0.5)))
    errors = network_errors if network_errors is not None else []
    rows: list[list[str]] | None = None
    last_error: Exception | None = None
    attempt_errors: list[Exception] = []
    for attempt in range(max_attempts):
        attempt_params = dict(params)
        attempt_params["_"] = _cache_buster(attempt)
        request = urllib.request.Request(
            f"{ENDPOINT}?{urllib.parse.urlencode(attempt_params)}",
            headers=REQUEST_HEADERS,
        )
        try:
            with _open_request(request, timeout, selected_proxy_mode) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
                if len(raw) > MAX_RESPONSE_BYTES:
                    raise ValueError(
                        f"Eastmoney response exceeds {MAX_RESPONSE_BYTES} bytes"
                    )
                payload = loads_unique_json(raw.decode("utf-8"))
            rows = _completed_rows(
                payload, instrument, market_close, cutoff=request_cutoff
            )
            break
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            last_error = exc
            attempt_errors.append(exc)
            detail = (
                f"attempt {attempt + 1}/{max_attempts}: {type(exc).__name__}: {exc}"
            )
            errors.append(detail)
            LOGGER.warning(
                "Eastmoney request failed for %s: %s", instrument.symbol, detail
            )
            if not _should_retry_eastmoney(exc):
                break
            if attempt + 1 < max_attempts:
                exponential = min(retry_max, retry_base * (2**attempt))
                time_module.sleep(exponential + random.uniform(0.0, retry_jitter))
    if rows is None:
        raise EastmoneyDownloadError(
            f"Failed to download {instrument.symbol} after "
            f"{len(attempt_errors)} attempt(s): {last_error}",
            attempt_errors,
        ) from last_error

    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["date", "open", "close", "high", "low", "volume", "amount", "amplitude"]
        )
        writer.writerows(rows)
    load_cached_bars(temporary)
    temporary.replace(output)
    LOGGER.info("Downloaded %s rows for %s", len(rows), instrument.symbol)
    return output


def _cache_buster(attempt: int) -> str:
    return str((time_module.time_ns() // 1_000_000) * 100 + attempt)


def _is_transport_failure(error: Exception) -> bool:
    if isinstance(error, EastmoneyDownloadError):
        return bool(error.attempt_errors) and all(
            _provider_wide_failure(attempt) is True
            for attempt in error.attempt_errors
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


def _should_retry_eastmoney(error: Exception) -> bool:
    return _provider_wide_failure(error) is True


def _provider_wide_failure(error: BaseException) -> bool | None:
    if isinstance(error, urllib.error.HTTPError):
        return _retryable_http_status(error.code)
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
        # Generic OSError also represents local file failures, which must never
        # suppress requests for unrelated instruments.
        return False
    return None


def _retryable_http_status(status: int) -> bool:
    return status in {408, 425, 429} or 500 <= status <= 599


def _proxy_mode(config: AppConfig) -> str:
    environment_value = os.environ.get("AI_TRADE_EASTMONEY_PROXY_MODE")
    if environment_value is not None:
        return _validated_proxy_mode(
            environment_value, "AI_TRADE_EASTMONEY_PROXY_MODE"
        )
    return _validated_proxy_mode(
        config.raw["data"].get("proxy_mode", "system"), "data.proxy_mode"
    )


def _validated_proxy_mode(value: object, source: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{source} must be system or direct")
    mode = value.strip().lower()
    if mode not in {"system", "direct"}:
        raise ValueError(f"{source} must be system or direct")
    return mode


def _open_request(
    request: urllib.request.Request, timeout: int, proxy_mode: str
):
    if proxy_mode == "direct":
        return DIRECT_OPENER.open(request, timeout=timeout)
    if proxy_mode == "system":
        return urllib.request.urlopen(request, timeout=timeout)
    raise ValueError(f"Unsupported Eastmoney proxy mode: {proxy_mode!r}")


def _completed_rows(
    payload: object,
    instrument: Instrument,
    market_close: str,
    *,
    cutoff: date | None = None,
) -> list[list[str]]:
    if not isinstance(payload, dict):
        raise RuntimeError(f"Invalid response envelope for {instrument.symbol}")
    if payload.get("rc") not in (None, 0):
        raise RuntimeError(
            f"Eastmoney returned rc={payload.get('rc')} for {instrument.symbol}"
        )
    data = payload.get("data")
    if not isinstance(data, dict) or not data.get("klines"):
        raise RuntimeError(f"No data returned for {instrument.symbol}")

    completed_cutoff = cutoff or completed_session_cutoff(market_close=market_close)
    rows: list[list[str]] = []
    for raw_line in data["klines"]:
        if not isinstance(raw_line, str):
            raise RuntimeError(
                f"Malformed kline returned for {instrument.symbol}: {raw_line!r}"
            )
        fields = raw_line.split(",")
        if len(fields) < 8:
            raise RuntimeError(
                f"Malformed kline returned for {instrument.symbol}: {raw_line!r}"
            )
        row_date = datetime.strptime(fields[0], "%Y-%m-%d").date()
        if row_date <= completed_cutoff:
            rows.append(fields[:8])
    if not rows:
        raise RuntimeError(f"No completed daily bars returned for {instrument.symbol}")
    return rows


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _instrument_cutoff(
    instrument: Instrument, market_close: str, cutoff: date | None = None
) -> date:
    cutoff = cutoff or completed_session_cutoff(market_close=market_close)
    if instrument.delisting_date is not None:
        return min(cutoff, instrument.delisting_date)
    return cutoff


def _stage_recent_completed_cache(
    config: AppConfig,
    instrument: Instrument,
    source: Path,
    destination: Path,
    cutoff: date,
) -> None:
    source_bars = load_cached_bars(source)
    bars = [bar for bar in source_bars if bar.date <= cutoff]
    if not bars or (cutoff - bars[-1].date).days > 7:
        raise RuntimeError(
            f"Network refresh failed and local cache is too old for safe fallback: {source}"
        )
    _validate_local_cache_provenance(config, instrument, source, source_bars)
    if len(bars) == len(source_bars):
        shutil.copyfile(source, destination)
        load_cached_bars(destination)
        return
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            ["date", "open", "close", "high", "low", "volume", "amount", "amplitude"]
        )
        for bar in bars:
            writer.writerow(
                [
                    bar.date.isoformat(),
                    bar.open,
                    bar.close,
                    bar.high,
                    bar.low,
                    bar.volume,
                    bar.amount,
                    "",
                ]
            )
    load_cached_bars(destination)


def _validate_local_cache_provenance(
    config: AppConfig,
    instrument: Instrument,
    source: Path,
    bars: list[Bar],
) -> None:
    try:
        cache_dir = config.cache_dir.resolve()
        if source.resolve().parent != cache_dir:
            raise RuntimeError("Local fallback is outside the active cache directory")
        manifest_path = cache_dir / "manifest.json"
        if not manifest_path.is_file():
            raise RuntimeError("Local fallback cache manifest is missing")
        if manifest_path.stat().st_size > MAX_MANIFEST_BYTES:
            raise RuntimeError("Local fallback cache manifest is too large")
        manifest = load_unique_json(
            manifest_path,
            max_bytes=MAX_MANIFEST_BYTES,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError("Local fallback cache manifest is invalid") from exc

    data = config.raw["data"]
    if not isinstance(manifest, dict):
        raise RuntimeError("Local fallback cache manifest must be an object")
    if manifest.get("provider") != data.get("provider"):
        raise RuntimeError("Local fallback cache provider does not match configuration")
    adjustment = data.get("adjustment", "forward")
    if manifest.get("adjustment") != adjustment:
        raise RuntimeError(
            "Local fallback cache adjustment does not match configuration"
        )
    files = manifest.get("files")
    metadata = files.get(instrument.symbol) if isinstance(files, dict) else None
    if not isinstance(metadata, dict):
        raise RuntimeError(
            f"Local fallback cache manifest omits {instrument.symbol}"
        )
    rows = metadata.get("rows")
    if isinstance(rows, bool) or not isinstance(rows, int) or rows != len(bars):
        raise RuntimeError(
            f"Local fallback cache row count does not match {instrument.symbol}"
        )
    expected_sha256 = metadata.get("sha256")
    if (
        not isinstance(expected_sha256, str)
        or re.fullmatch(r"[0-9a-fA-F]{64}", expected_sha256) is None
        or _file_sha256(source) != expected_sha256.lower()
    ):
        raise RuntimeError(
            f"Local fallback cache SHA-256 does not match {instrument.symbol}"
        )
    if metadata.get("source") not in {
        "network",
        "eastmoney_network_fallback",
        "tencent_network_fallback",
        "validated_local_fallback",
    }:
        raise RuntimeError(
            f"Local fallback cache source is invalid for {instrument.symbol}"
        )

    configured_start = date.fromisoformat(data["start"])
    requested_from = (
        metadata.get("requested_from")
        if "requested_from" in metadata
        else manifest.get("requested_from")
    )
    if not isinstance(requested_from, str):
        raise RuntimeError("Local fallback cache requested_from is invalid")
    try:
        requested_start = date.fromisoformat(requested_from)
    except ValueError as exc:
        raise RuntimeError("Local fallback cache requested_from is invalid") from exc
    if requested_start > configured_start:
        raise RuntimeError(
            "Local fallback cache does not cover the configured start date"
        )
    expected_start = max(
        configured_start,
        instrument.listing_date or configured_start,
    )
    if bars[0].date > expected_start + timedelta(days=LEGACY_START_TOLERANCE_DAYS):
        raise RuntimeError(
            "Local fallback cache history starts after the configured range"
        )
    latest_session = metadata.get("latest_session")
    if not isinstance(latest_session, str):
        raise RuntimeError(
            f"Local fallback cache latest session is invalid for {instrument.symbol}"
        )
    try:
        latest_date = date.fromisoformat(latest_session)
    except ValueError as exc:
        raise RuntimeError(
            f"Local fallback cache latest session is invalid for {instrument.symbol}"
        ) from exc
    if latest_date != bars[-1].date:
        raise RuntimeError(
            f"Local fallback cache latest session does not match {instrument.symbol}"
        )


def load_cached_bars(path: Path) -> list[Bar]:
    bars: list[Bar] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(reader.fieldnames):
            missing = sorted(REQUIRED_COLUMNS - set(reader.fieldnames or []))
            raise RuntimeError(
                f"Cache schema is invalid for {path}; missing columns: {missing}"
            )
        for line_number, row in enumerate(reader, start=2):
            try:
                bar = Bar(
                    date=datetime.strptime(row["date"], "%Y-%m-%d").date(),
                    open=float(row["open"]),
                    close=float(row["close"]),
                    high=float(row["high"]),
                    low=float(row["low"]),
                    volume=float(row["volume"]),
                    amount=float(row["amount"]),
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"Invalid cache row at {path}:{line_number}: {exc}"
                ) from exc
            _validate_bar(bar, path, line_number)
            if bars and bar.date <= bars[-1].date:
                raise RuntimeError(
                    f"Cache dates must be strictly increasing at {path}:{line_number}"
                )
            bars.append(bar)
    if not bars:
        raise RuntimeError(f"Cache file is empty: {path}")
    return bars


def cache_is_current(
    path: Path,
    today: date | None = None,
    *,
    now: datetime | None = None,
    market_close: str = "15:30",
) -> bool:
    if not path.exists():
        return False
    local_now = _china_now(now)
    cutoff = today or completed_session_cutoff(local_now, market_close)
    try:
        latest = load_cached_bars(path)[-1].date
    except (OSError, RuntimeError):
        return False
    if latest < cutoff:
        return False
    close = _parse_market_close(market_close)
    modified = datetime.fromtimestamp(path.stat().st_mtime, CHINA_TIMEZONE)
    if latest == local_now.date() and modified.time() < close:
        return False
    return True


def completed_session_cutoff(
    now: datetime | None = None,
    market_close: str = "15:30",
) -> date:
    local_now = _china_now(now)
    cutoff = local_now.date()
    if local_now.time() < _parse_market_close(market_close):
        cutoff -= timedelta(days=1)
    while cutoff.weekday() >= 5:
        cutoff -= timedelta(days=1)
    return cutoff


def _china_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(CHINA_TIMEZONE)
    if now.tzinfo is None:
        return now.replace(tzinfo=CHINA_TIMEZONE)
    return now.astimezone(CHINA_TIMEZONE)


def _parse_market_close(value: str) -> time:
    try:
        return time.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"Invalid data.market_close_time: {value!r}") from exc


def _validate_bar(bar: Bar, path: Path, line_number: int) -> None:
    numbers = (bar.open, bar.close, bar.high, bar.low, bar.volume, bar.amount)
    if not all(math.isfinite(value) for value in numbers):
        raise RuntimeError(f"Non-finite cache value at {path}:{line_number}")
    if min(bar.open, bar.close, bar.high, bar.low) <= 0:
        raise RuntimeError(f"Non-positive price at {path}:{line_number}")
    if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(
        bar.open, bar.close, bar.high
    ):
        raise RuntimeError(f"Invalid OHLC relationship at {path}:{line_number}")
    if bar.volume < 0 or bar.amount < 0:
        raise RuntimeError(f"Negative volume or amount at {path}:{line_number}")
