from __future__ import annotations

from bisect import bisect_right
from dataclasses import dataclass
from datetime import date, datetime
from hashlib import sha256
import json

from ..config import AppConfig
from ..models import Bar, Instrument
from ..security import TradingStatus
from .cache_snapshot import non_mutating_snapshot, readable_snapshot
from .eastmoney import completed_session_cutoff, load_cached_bars


@dataclass
class SymbolData:
    instrument: Instrument
    bars: list[Bar]
    dates: list[date]
    by_date: dict[date, Bar]


class MarketData:
    def __init__(
        self,
        config: AppConfig,
        as_of: datetime | None = None,
        *,
        recover_snapshot: bool = True,
    ):
        self.config = config
        snapshot = readable_snapshot if recover_snapshot else non_mutating_snapshot
        with snapshot(config.cache_dir):
            self._initialize(as_of)

    def _initialize(self, as_of: datetime | None) -> None:
        config = self.config
        self.symbols: dict[str, SymbolData] = {}
        self.market_close_time = config.raw["data"].get("market_close_time", "15:30")
        self.completed_through = completed_session_cutoff(as_of, self.market_close_time)
        self.excluded_dates: dict[str, list[date]] = {}
        self.file_hashes: dict[str, str] = {}
        for instrument in config.instruments:
            path = config.cache_dir / f"{instrument.symbol}.csv"
            if not path.exists():
                raise FileNotFoundError(f"Missing cache for {instrument.symbol}: run download first")
            raw_bars = load_cached_bars(path)
            bars = [bar for bar in raw_bars if bar.date <= self.completed_through]
            excluded = [bar.date for bar in raw_bars if bar.date > self.completed_through]
            if not bars:
                raise RuntimeError(
                    f"No completed bars for {instrument.symbol} through {self.completed_through}"
                )
            self.excluded_dates[instrument.symbol] = excluded
            self.file_hashes[instrument.symbol] = sha256(path.read_bytes()).hexdigest()
            self.symbols[instrument.symbol] = SymbolData(
                instrument=instrument,
                bars=bars,
                dates=[bar.date for bar in bars],
                by_date={bar.date: bar for bar in bars},
            )

        self.manifest = self._load_and_validate_manifest()

        benchmark = config.strategy.benchmark
        self.calendar = list(self.symbols[benchmark].dates)
        if not self.calendar:
            raise RuntimeError("No completed benchmark market date is available")
        common_symbols = set(config.active_symbols(self.calendar[-1]))
        common_symbols.add(benchmark)
        common_dates = set(self.symbols[benchmark].dates)
        for symbol in sorted(common_symbols - {benchmark}):
            if symbol in self.symbols:
                common_dates.intersection_update(self.symbols[symbol].dates)
        if not common_dates:
            raise RuntimeError("Active instruments share no common completed market session")
        self.latest_common_session = max(common_dates)

    def bar(self, symbol: str, on_date: date) -> Bar | None:
        return self.symbols[symbol].by_date.get(on_date)

    def latest_bar_on_or_before(self, symbol: str, on_date: date) -> Bar | None:
        item = self.symbols[symbol]
        index = bisect_right(item.dates, on_date) - 1
        return item.bars[index] if index >= 0 else None

    def previous_bar(self, symbol: str, on_date: date) -> Bar | None:
        item = self.symbols[symbol]
        index = bisect_right(item.dates, on_date) - 1
        if index >= 0 and item.dates[index] == on_date:
            index -= 1
        return item.bars[index] if index >= 0 else None

    def history(self, symbol: str, on_date: date, count: int) -> list[Bar]:
        item = self.symbols[symbol]
        end = bisect_right(item.dates, on_date)
        start = max(0, end - count)
        return item.bars[start:end]

    def instrument(self, symbol: str) -> Instrument:
        return self.symbols[symbol].instrument

    def active_symbols(self, on_date: date) -> tuple[str, ...]:
        return tuple(
            symbol for symbol in self.config.active_symbols(on_date) if symbol in self.symbols
        )

    def trading_status(self, symbol: str, on_date: date) -> TradingStatus:
        return self.config.security_master.trading_status(symbol, on_date)

    def latest_date(self) -> date:
        return self.calendar[-1]

    def snapshot_metadata(self) -> dict[str, object]:
        return {
            "provider": self.config.raw["data"]["provider"],
            "adjustment": self.config.raw["data"].get("adjustment", "none"),
            "completed_session_cutoff": self.completed_through.isoformat(),
            "latest_common_session": self.latest_common_session.isoformat(),
            "latest_benchmark_session": self.latest_date().isoformat(),
            "universe": {
                "name": self.config.universe_name,
                "active_symbols": list(self.active_symbols(self.latest_date())),
                "minimum_listing_days": self.config.minimum_listing_days,
                "security_master_sha256": self.config.security_master.fingerprint(),
                "selection_method": self.config.security_master.metadata.get(
                    "selection_method"
                ),
            },
            "manifest": self.manifest,
            "symbols": {
                symbol: {
                    "rows": len(item.bars),
                    "first": item.bars[0].date.isoformat(),
                    "last": item.bars[-1].date.isoformat(),
                    "sha256": self.file_hashes[symbol],
                    "excluded_incomplete_dates": [
                        value.isoformat() for value in self.excluded_dates[symbol]
                    ],
                }
                for symbol, item in self.symbols.items()
            },
        }

    def _load_and_validate_manifest(self) -> dict[str, object] | None:
        path = self.config.cache_dir / "manifest.json"
        self.manifest_sha256: str | None = None
        if not path.exists():
            return None
        try:
            raw_manifest = path.read_bytes()
            manifest = json.loads(raw_manifest.decode("utf-8"))
            if not isinstance(manifest, dict):
                raise RuntimeError(
                    f"Invalid cache manifest: {path}: top-level JSON must be an object"
                )
            if manifest.get("provider") != self.config.raw["data"]["provider"]:
                raise RuntimeError("Cache manifest provider does not match configuration")
            configured_adjustment = self.config.raw["data"].get("adjustment", "none")
            if manifest.get("adjustment") != configured_adjustment:
                raise RuntimeError("Cache manifest adjustment does not match configuration")
            files = manifest["files"]
            for symbol, digest in self.file_hashes.items():
                expected = files[symbol]["sha256"]
                if expected != digest:
                    raise RuntimeError(
                        f"Cache hash mismatch for {symbol}; refresh the complete data snapshot"
                    )
            self.manifest_sha256 = sha256(raw_manifest).hexdigest()
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Invalid cache manifest: {path}: {exc}") from exc
        return manifest
