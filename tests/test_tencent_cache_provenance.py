import csv
import hashlib
import json
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_trade.data.eastmoney import load_cached_bars
from ai_trade.data.tencent import _recent_cached_bars
from ai_trade.models import Instrument

_DELETE = object()


class TencentCacheProvenanceTests(unittest.TestCase):
    def test_matching_manifest_allows_incremental_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, instrument, cache = _cache_fixture(root)
            _write_manifest(config, instrument, cache)
            provenance: dict[str, object] = {}

            bars = _recent_cached_bars(
                config,
                instrument,
                cache,
                date(2024, 1, 1),
                date(2024, 1, 5),
                "forward",
                provenance=provenance,
            )

            self.assertEqual(
                [bar.date.isoformat() for bar in bars],
                ["2024-01-02", "2024-01-03", "2024-01-05"],
            )
            self.assertEqual(provenance["cached_seed_source"], "network")
            self.assertEqual(provenance["cached_seed_rows"], 3)
            self.assertEqual(
                provenance["cached_seed_sha256"],
                hashlib.sha256(cache.read_bytes()).hexdigest(),
            )

    def test_missing_manifest_contract_fields_force_full_history(self):
        cases = (
            ("provider", "manifest", "provider"),
            ("adjustment", "manifest", "adjustment"),
            ("requested_from", "manifest", "requested_from"),
            ("rows", "file", "rows"),
            ("sha256", "file", "sha256"),
            ("source", "file", "source"),
            ("latest_session", "file", "latest_session"),
        )
        for name, scope, field in cases:
            with tempfile.TemporaryDirectory() as temporary, self.subTest(name=name):
                root = Path(temporary)
                config, instrument, cache = _cache_fixture(root)
                changes = {field: _DELETE}
                _write_manifest(
                    config,
                    instrument,
                    cache,
                    manifest_changes=changes if scope == "manifest" else None,
                    file_changes=changes if scope == "file" else None,
                )

                self.assertEqual(self._cached_bars(config, instrument, cache), [])

    def test_wrong_manifest_contract_fields_force_full_history(self):
        cases = (
            ("provider", "manifest", "provider", "other"),
            ("adjustment", "manifest", "adjustment", "backward"),
            ("requested_from_type", "manifest", "requested_from", None),
            ("requested_from_date", "manifest", "requested_from", "not-a-date"),
            ("requested_from_coverage", "manifest", "requested_from", "2024-01-02"),
            ("rows_bool", "file", "rows", True),
            ("rows_count", "file", "rows", 2),
            ("sha256_shape", "file", "sha256", "not-a-hash"),
            ("sha256_content", "file", "sha256", "0" * 64),
            ("source", "file", "source", "unknown"),
            ("source_type", "file", "source", ["network"]),
            ("latest_session_type", "file", "latest_session", None),
            ("latest_session_date", "file", "latest_session", "not-a-date"),
            ("latest_session_value", "file", "latest_session", "2024-01-03"),
        )
        for name, scope, field, value in cases:
            with tempfile.TemporaryDirectory() as temporary, self.subTest(name=name):
                root = Path(temporary)
                config, instrument, cache = _cache_fixture(root)
                changes = {field: value}
                _write_manifest(
                    config,
                    instrument,
                    cache,
                    manifest_changes=changes if scope == "manifest" else None,
                    file_changes=changes if scope == "file" else None,
                )

                self.assertEqual(self._cached_bars(config, instrument, cache), [])

    def test_missing_malformed_or_wrong_symbol_entry_forces_full_history(self):
        cases = (
            "missing_manifest",
            "malformed_manifest",
            "missing_symbol",
            "wrong_symbol",
        )
        for name in cases:
            with tempfile.TemporaryDirectory() as temporary, self.subTest(name=name):
                root = Path(temporary)
                config, instrument, cache = _cache_fixture(root)
                manifest_path = config.cache_dir / "manifest.json"
                if name == "malformed_manifest":
                    manifest_path.write_text("{not-json", encoding="utf-8")
                elif name in {"missing_symbol", "wrong_symbol"}:
                    _write_manifest(
                        config,
                        instrument,
                        cache,
                        omit_symbol=name == "missing_symbol",
                        symbol="510500" if name == "wrong_symbol" else None,
                    )

                self.assertEqual(self._cached_bars(config, instrument, cache), [])

    def test_file_level_requested_from_must_cover_current_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, instrument, cache = _cache_fixture(root)
            _write_manifest(
                config,
                instrument,
                cache,
                file_changes={"requested_from": "2024-01-02"},
            )

            self.assertEqual(self._cached_bars(config, instrument, cache), [])

    def test_file_changed_while_loading_cannot_become_incremental_seed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, instrument, cache = _cache_fixture(root)
            _write_manifest(config, instrument, cache)

            def load_then_mutate(path: Path):
                bars = load_cached_bars(path)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write("\n")
                return bars

            with patch(
                "ai_trade.data.eastmoney.load_cached_bars",
                side_effect=load_then_mutate,
            ):
                self.assertEqual(self._cached_bars(config, instrument, cache), [])

    def test_complete_manifest_accepts_small_non_trading_start_gap(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, instrument, cache = _cache_fixture(
                root,
                first_date="2024-01-04",
            )
            _write_manifest(config, instrument, cache)

            bars = _recent_cached_bars(
                config,
                instrument,
                cache,
                date(2024, 1, 1),
                date(2024, 1, 5),
                "forward",
            )

            self.assertTrue(bars)
            self.assertEqual(bars[0].date, date(2024, 1, 4))

    def test_complete_manifest_rejects_uncovered_earlier_history(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, instrument, cache = _cache_fixture(
                root,
                first_date="2024-02-01",
            )
            _write_manifest(config, instrument, cache)

            bars = _recent_cached_bars(
                config,
                instrument,
                cache,
                date(2024, 1, 1),
                date(2024, 2, 1),
                "forward",
            )

            self.assertEqual(bars, [])

    def test_cache_filename_must_match_manifest_symbol(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, instrument, cache = _cache_fixture(root)
            wrong_name = cache.with_name("510500.csv")
            wrong_name.write_bytes(cache.read_bytes())
            _write_manifest(config, instrument, wrong_name)

            self.assertEqual(
                self._cached_bars(config, instrument, wrong_name),
                [],
            )

    def test_cache_outside_active_directory_is_not_eligible(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config, instrument, cache = _cache_fixture(root)
            _write_manifest(config, instrument, cache)
            other_cache = root / "other" / cache.name
            other_cache.parent.mkdir()
            other_cache.write_bytes(cache.read_bytes())
            _write_manifest(config, instrument, other_cache)

            bars = _recent_cached_bars(
                config,
                instrument,
                other_cache,
                date(2024, 1, 1),
                date(2024, 1, 5),
                "forward",
            )

            self.assertEqual(bars, [])

    @staticmethod
    def _cached_bars(config, instrument, cache):
        return _recent_cached_bars(
            config,
            instrument,
            cache,
            date(2024, 1, 1),
            date(2024, 1, 5),
            "forward",
        )


def _cache_fixture(
    root: Path, *, first_date: str = "2024-01-02"
) -> tuple[SimpleNamespace, Instrument, Path]:
    cache_dir = root / "data" / "cache"
    cache_dir.mkdir(parents=True)
    config = SimpleNamespace(
        cache_dir=cache_dir,
        raw={"data": {"provider": "eastmoney", "adjustment": "forward"}},
    )
    instrument = Instrument("510300", "Test", "SH", "equity")
    cache = cache_dir / f"{instrument.symbol}.csv"
    _write_cache(cache, first_date)
    return config, instrument, cache


def _write_cache(path: Path, first_date: str) -> None:
    first = date.fromisoformat(first_date)
    dates = (first, first + timedelta(days=1), first + timedelta(days=3))
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "open", "close", "high", "low", "volume", "amount"])
        for index, value in enumerate(dates):
            price = 10 + index
            writer.writerow(
                [
                    value.isoformat(),
                    price,
                    price + 0.1,
                    price + 0.2,
                    price - 0.2,
                    100,
                    1000,
                ]
            )


def _write_manifest(
    config: SimpleNamespace,
    instrument: Instrument,
    cache: Path,
    *,
    manifest_changes: dict[str, object] | None = None,
    file_changes: dict[str, object] | None = None,
    symbol: str | None = None,
    omit_symbol: bool = False,
) -> None:
    with cache.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    file_metadata = {
        "rows": len(rows),
        "sha256": hashlib.sha256(cache.read_bytes()).hexdigest(),
        "source": "network",
        "latest_session": rows[-1]["date"],
    }
    manifest = {
        "provider": "eastmoney",
        "adjustment": "forward",
        "requested_from": "2024-01-01",
        "files": ({} if omit_symbol else {symbol or instrument.symbol: file_metadata}),
    }
    _apply_changes(manifest, manifest_changes)
    _apply_changes(file_metadata, file_changes)
    manifest_path = cache.parent / "manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def _apply_changes(
    target: dict[str, object], changes: dict[str, object] | None
) -> None:
    for key, value in (changes or {}).items():
        if value is _DELETE:
            target.pop(key, None)
        else:
            target[key] = value


if __name__ == "__main__":
    unittest.main()
