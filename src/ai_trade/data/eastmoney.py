from __future__ import annotations

import csv
import hashlib
import json
import logging
import math
import shutil
import time as time_module
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from ..config import AppConfig
from ..models import Bar, Instrument

LOGGER = logging.getLogger(__name__)
ENDPOINT = "https://push2his.eastmoney.com/api/qt/stock/kline/get"
CHINA_TIMEZONE = timezone(timedelta(hours=8))
REQUIRED_COLUMNS = {"date", "open", "close", "high", "low", "volume", "amount"}


def download_universe(config: AppConfig, force: bool = False) -> dict[str, Path]:
    config.cache_dir.mkdir(parents=True, exist_ok=True)
    market_close = config.raw["data"].get("market_close_time", "15:30")
    final_paths = {
        instrument.symbol: config.cache_dir / f"{instrument.symbol}.csv"
        for instrument in config.instruments
    }
    if not force and all(
        cache_is_current(path, market_close=market_close) for path in final_paths.values()
    ):
        return final_paths

    staging = config.cache_dir / f".snapshot-{uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    try:
        staged_paths: dict[str, Path] = {}
        sources: dict[str, str] = {}
        with ThreadPoolExecutor(max_workers=min(2, len(config.instruments))) as pool:
            futures = {
                pool.submit(
                    download_instrument,
                    config,
                    instrument,
                    True,
                    staging / f"{instrument.symbol}.csv",
                ): instrument
                for instrument in config.instruments
            }
            for future in as_completed(futures):
                instrument = futures[future]
                try:
                    staged_paths[instrument.symbol] = future.result()
                    sources[instrument.symbol] = "network"
                except Exception as exc:
                    fallback = final_paths[instrument.symbol]
                    staged = staging / f"{instrument.symbol}.csv"
                    _stage_recent_completed_cache(
                        fallback,
                        staged,
                        completed_session_cutoff(market_close=market_close),
                    )
                    staged_paths[instrument.symbol] = staged
                    sources[instrument.symbol] = "validated_local_fallback"
                    LOGGER.warning(
                        "Download failed for %s; reused recent validated cache: %s",
                        instrument.symbol,
                        exc,
                    )

        manifest = {
            "provider": config.raw["data"]["provider"],
            "adjustment": config.raw["data"].get("adjustment", "forward"),
            "downloaded_at": datetime.now(CHINA_TIMEZONE).isoformat(),
            "completed_through": completed_session_cutoff(
                market_close=market_close
            ).isoformat(),
            "files": {
                symbol: {
                    "rows": len(load_cached_bars(staged_paths[symbol])),
                    "sha256": _file_sha256(staged_paths[symbol]),
                    "source": sources[symbol],
                }
                for symbol in sorted(staged_paths)
            },
        }
        for symbol, staged in staged_paths.items():
            staged.replace(final_paths[symbol])
        manifest_path = config.cache_dir / "manifest.json"
        temporary_manifest = manifest_path.with_suffix(".json.tmp")
        temporary_manifest.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        temporary_manifest.replace(manifest_path)
        return final_paths
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def download_instrument(
    config: AppConfig,
    instrument: Instrument,
    force: bool = False,
    output_path: Path | None = None,
) -> Path:
    output = output_path or config.cache_dir / f"{instrument.symbol}.csv"
    market_close = config.raw["data"].get("market_close_time", "15:30")
    if (
        output_path is None
        and output.exists()
        and not force
        and cache_is_current(output, market_close=market_close)
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
    }
    request = urllib.request.Request(
        f"{ENDPOINT}?{urllib.parse.urlencode(params)}",
        headers={"User-Agent": "Mozilla/5.0 ai-trade/0.1"},
    )
    timeout = int(config.raw["data"].get("timeout_seconds", 20))
    payload = None
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
            break
        except (OSError, ValueError) as exc:
            last_error = exc
            if attempt < 2:
                time_module.sleep(1.5 * (2**attempt))
    if payload is None:
        raise RuntimeError(
            f"Failed to download {instrument.symbol} after 3 attempts: {last_error}"
        ) from last_error

    if payload.get("rc") not in (None, 0):
        raise RuntimeError(f"Eastmoney returned rc={payload.get('rc')} for {instrument.symbol}")
    data = payload.get("data")
    if not data or not data.get("klines"):
        raise RuntimeError(f"No data returned for {instrument.symbol}")

    cutoff = completed_session_cutoff(market_close=market_close)
    rows = []
    for raw_line in data["klines"]:
        fields = raw_line.split(",")
        if len(fields) < 8:
            raise RuntimeError(f"Malformed kline returned for {instrument.symbol}: {raw_line!r}")
        row_date = datetime.strptime(fields[0], "%Y-%m-%d").date()
        if row_date <= cutoff:
            rows.append(fields[:8])
    if not rows:
        raise RuntimeError(f"No completed daily bars returned for {instrument.symbol}")

    temporary = output.with_suffix(output.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "open", "close", "high", "low", "volume", "amount", "amplitude"])
        writer.writerows(rows)
    load_cached_bars(temporary)
    temporary.replace(output)
    LOGGER.info("Downloaded %s rows for %s", len(rows), instrument.symbol)
    return output


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _stage_recent_completed_cache(source: Path, destination: Path, cutoff: date) -> None:
    bars = [bar for bar in load_cached_bars(source) if bar.date <= cutoff]
    if not bars or (cutoff - bars[-1].date).days > 7:
        raise RuntimeError(
            f"Network refresh failed and local cache is too old for safe fallback: {source}"
        )
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


def load_cached_bars(path: Path) -> list[Bar]:
    bars: list[Bar] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames or not REQUIRED_COLUMNS.issubset(reader.fieldnames):
            missing = sorted(REQUIRED_COLUMNS - set(reader.fieldnames or []))
            raise RuntimeError(f"Cache schema is invalid for {path}; missing columns: {missing}")
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
                raise RuntimeError(f"Invalid cache row at {path}:{line_number}: {exc}") from exc
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
