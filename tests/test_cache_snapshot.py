from __future__ import annotations

import csv
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from ai_trade.config import load_config
from ai_trade.data import cache_snapshot
from ai_trade.data.cache_snapshot import (
    CacheSnapshotRecoveryError,
    install_snapshot,
    recover_pending_snapshot,
)
from ai_trade.data.eastmoney import download_universe
from ai_trade.data.market import MarketData


class CacheSnapshotTransactionTests(unittest.TestCase):
    def test_middle_csv_replace_failure_restores_complete_old_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            old = _write_active_snapshot(cache, ("a.csv", "b.csv", "c.csv"))
            staged = _write_staged_snapshot(cache, ("a.csv", "b.csv", "c.csv"))
            original_replace = cache_snapshot._replace_path
            failed = False

            def fail_middle_once(source: Path, destination: Path) -> None:
                nonlocal failed
                if destination == cache / "b.csv" and not failed:
                    failed = True
                    raise OSError("injected middle CSV replace failure")
                original_replace(source, destination)

            with (
                patch.object(
                    cache_snapshot, "_replace_path", side_effect=fail_middle_once
                ),
                self.assertRaisesRegex(OSError, "middle CSV"),
            ):
                install_snapshot(cache, staged, _manifest_for_paths(staged))

            _assert_snapshot_bytes(self, cache, old)
            self.assertFalse((cache / cache_snapshot.MARKER_NAME).exists())

    def test_manifest_replace_failure_restores_complete_old_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            old = _write_active_snapshot(cache, ("a.csv", "b.csv"))
            staged = _write_staged_snapshot(cache, ("a.csv", "b.csv"))
            original_replace = cache_snapshot._replace_path
            failed = False

            def fail_manifest_once(source: Path, destination: Path) -> None:
                nonlocal failed
                if destination == cache / "manifest.json" and not failed:
                    failed = True
                    raise OSError("injected manifest replace failure")
                original_replace(source, destination)

            with (
                patch.object(
                    cache_snapshot, "_replace_path", side_effect=fail_manifest_once
                ),
                self.assertRaisesRegex(OSError, "manifest replace"),
            ):
                install_snapshot(cache, staged, _manifest_for_paths(staged))

            _assert_snapshot_bytes(self, cache, old)
            self.assertFalse((cache / cache_snapshot.MARKER_NAME).exists())

    def test_first_install_failure_removes_every_partially_installed_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            cache.mkdir()
            staged = _write_staged_snapshot(cache, ("a.csv", "b.csv"))
            original_replace = cache_snapshot._replace_path
            failed = False

            def fail_second_once(source: Path, destination: Path) -> None:
                nonlocal failed
                if destination == cache / "b.csv" and not failed:
                    failed = True
                    raise OSError("injected first-install failure")
                original_replace(source, destination)

            with (
                patch.object(
                    cache_snapshot, "_replace_path", side_effect=fail_second_once
                ),
                self.assertRaisesRegex(OSError, "first-install"),
            ):
                install_snapshot(cache, staged, _manifest_for_paths(staged))

            for name in ("a.csv", "b.csv", "manifest.json"):
                self.assertFalse((cache / name).exists())
            self.assertFalse((cache / cache_snapshot.MARKER_NAME).exists())

    def test_failed_rollback_remains_recoverable_on_next_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            old = _write_active_snapshot(cache, ("a.csv", "b.csv"))
            staged = _write_staged_snapshot(cache, ("a.csv", "b.csv"))
            original_replace = cache_snapshot._replace_path
            failed = False

            def fail_install_once(source: Path, destination: Path) -> None:
                nonlocal failed
                if destination == cache / "b.csv" and not failed:
                    failed = True
                    raise OSError("injected install failure")
                original_replace(source, destination)

            with (
                patch.object(
                    cache_snapshot, "_replace_path", side_effect=fail_install_once
                ),
                patch.object(
                    cache_snapshot,
                    "_restore_file",
                    side_effect=OSError("injected rollback failure"),
                ),
                self.assertRaisesRegex(
                    CacheSnapshotRecoveryError, "rollback could not be completed"
                ),
            ):
                install_snapshot(cache, staged, _manifest_for_paths(staged))

            self.assertTrue((cache / cache_snapshot.MARKER_NAME).exists())
            recover_pending_snapshot(cache)
            _assert_snapshot_bytes(self, cache, old)
            self.assertFalse((cache / cache_snapshot.MARKER_NAME).exists())

    def test_installing_marker_is_rolled_back_on_next_recovery(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            old = _write_active_snapshot(cache, ("a.csv", "b.csv"))
            _prepare_interrupted_install(cache, old)
            (cache / "a.csv").write_bytes(b"new-a")
            (cache / "manifest.json").write_text('{"snapshot":"new"}', encoding="utf-8")

            recover_pending_snapshot(cache)

            _assert_snapshot_bytes(self, cache, old)
            self.assertFalse((cache / cache_snapshot.MARKER_NAME).exists())

    def test_complete_committed_snapshot_is_kept_and_transaction_is_cleaned(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            _write_active_snapshot(cache, ("a.csv", "b.csv"))
            staged = _write_staged_snapshot(cache, ("a.csv", "b.csv"))
            expected = {name: path.read_bytes() for name, path in staged.items()}

            with (
                patch.object(
                    cache_snapshot,
                    "_remove_marker",
                    side_effect=OSError("injected crash before committed cleanup"),
                ),
                self.assertRaisesRegex(OSError, "committed cleanup"),
            ):
                install_snapshot(cache, staged, _manifest_for_paths(staged))

            marker = json.loads(
                (cache / cache_snapshot.MARKER_NAME).read_text(encoding="utf-8")
            )
            self.assertEqual(marker["state"], "committed")
            self.assertTrue(
                all(entry["installed_sha256"] for entry in marker["entries"])
            )

            recover_pending_snapshot(cache)

            for name, content in expected.items():
                self.assertEqual((cache / name).read_bytes(), content)
            self.assertFalse((cache / cache_snapshot.MARKER_NAME).exists())
            self.assertFalse((cache / cache_snapshot.TRANSACTIONS_DIR_NAME).exists())

    def test_mixed_committed_snapshot_rolls_back_to_complete_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            old = _write_active_snapshot(cache, ("a.csv", "b.csv"))
            staged = _write_staged_snapshot(cache, ("a.csv", "b.csv"))

            with (
                patch.object(
                    cache_snapshot,
                    "_remove_marker",
                    side_effect=OSError("injected crash before committed cleanup"),
                ),
                self.assertRaisesRegex(OSError, "committed cleanup"),
            ):
                install_snapshot(cache, staged, _manifest_for_paths(staged))
            (cache / "a.csv").write_bytes(old["a.csv"])

            recover_pending_snapshot(cache)

            _assert_snapshot_bytes(self, cache, old)
            self.assertFalse((cache / cache_snapshot.MARKER_NAME).exists())
            self.assertFalse((cache / cache_snapshot.TRANSACTIONS_DIR_NAME).exists())

    def test_markerless_installing_transaction_restores_mixed_active_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            old = _write_active_snapshot(cache, ("a.csv", "b.csv"))
            transaction_dir = _prepare_interrupted_install(cache, old)
            (cache / "a.csv").write_bytes(b"new-a")
            (cache / cache_snapshot.MARKER_NAME).unlink()

            recover_pending_snapshot(cache)

            _assert_snapshot_bytes(self, cache, old)
            self.assertFalse(transaction_dir.exists())

    def test_markerless_committed_transaction_rolls_back_mixed_active_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            old = _write_active_snapshot(cache, ("a.csv", "b.csv"))
            staged = _write_staged_snapshot(cache, ("a.csv", "b.csv"))

            with (
                patch.object(
                    cache_snapshot,
                    "_remove_marker",
                    side_effect=OSError("injected crash before committed cleanup"),
                ),
                self.assertRaisesRegex(OSError, "committed cleanup"),
            ):
                install_snapshot(cache, staged, _manifest_for_paths(staged))
            marker = json.loads(
                (cache / cache_snapshot.MARKER_NAME).read_text(encoding="utf-8")
            )
            transaction_dir = (
                cache / cache_snapshot.TRANSACTIONS_DIR_NAME / marker["transaction_id"]
            )
            (cache / cache_snapshot.MARKER_NAME).unlink()
            (cache / "a.csv").write_bytes(old["a.csv"])

            recover_pending_snapshot(cache)

            _assert_snapshot_bytes(self, cache, old)
            self.assertFalse(transaction_dir.exists())

    def test_markerless_legacy_backups_fail_closed_without_deletion(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            old = _write_active_snapshot(cache, ("a.csv", "b.csv"))
            transaction_dir = _prepare_interrupted_install(cache, old)
            journal = transaction_dir / cache_snapshot.TRANSACTION_JOURNAL_NAME
            journal.unlink()
            (cache / "a.csv").write_bytes(b"new-a")
            (cache / cache_snapshot.MARKER_NAME).unlink()

            with self.assertRaisesRegex(
                CacheSnapshotRecoveryError, "journal is unavailable"
            ):
                recover_pending_snapshot(cache)

            self.assertTrue(transaction_dir.exists())
            self.assertEqual(
                (transaction_dir / "backup" / "a.csv").read_bytes(),
                old["a.csv"],
            )
            self.assertEqual((cache / "a.csv").read_bytes(), b"new-a")

    def test_corrupt_marker_and_missing_backup_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            cache.mkdir()
            marker = cache / cache_snapshot.MARKER_NAME
            marker.write_text("not-json", encoding="utf-8")
            with self.assertRaisesRegex(
                CacheSnapshotRecoveryError, "marker is unreadable"
            ):
                recover_pending_snapshot(cache)
            self.assertTrue(marker.exists())

        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary) / "cache"
            old = _write_active_snapshot(cache, ("a.csv",))
            transaction_dir = _prepare_interrupted_install(cache, old)
            (transaction_dir / "backup" / "a.csv").unlink()
            with self.assertRaisesRegex(
                CacheSnapshotRecoveryError, "backup is missing"
            ):
                recover_pending_snapshot(cache)
            self.assertTrue((cache / cache_snapshot.MARKER_NAME).exists())


class CacheSnapshotIntegrationTests(unittest.TestCase):
    def test_market_data_recovers_before_reading_any_csv(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(_write_config(root))
            cache = config.cache_dir
            for instrument in config.instruments:
                _write_bars(cache / f"{instrument.symbol}.csv")
            old_manifest = {
                "provider": "eastmoney",
                "adjustment": "forward",
                "files": {
                    instrument.symbol: {
                        "sha256": _sha256(cache / f"{instrument.symbol}.csv")
                    }
                    for instrument in config.instruments
                },
            }
            (cache / "manifest.json").write_text(
                json.dumps(old_manifest), encoding="utf-8"
            )
            old = {
                path.name: path.read_bytes()
                for path in cache.iterdir()
                if path.name.endswith(".csv") or path.name == "manifest.json"
            }
            _prepare_interrupted_install(cache, old)
            _write_bars(
                cache / f"{config.instruments[0].symbol}.csv",
                dates=("2024-01-02", "2024-01-03", "2024-01-04"),
            )

            market = MarketData(config, as_of=datetime(2024, 1, 3, 16, 0))

            self.assertEqual(market.latest_date(), date(2024, 1, 3))
            _assert_snapshot_bytes(self, cache, old)

    def test_manifest_distinguishes_requested_cutoff_from_common_completion(self):
        target = date(2024, 1, 5)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(
                _write_config(
                    root,
                    {
                        "request_interval_seconds": 0.0,
                        "request_jitter_seconds": 0.0,
                        "failure_cooldown_seconds": 0.0,
                    },
                )
            )

            def fake_download(
                config,
                instrument,
                force,
                output_path,
                **kwargs,
            ):
                dates = (
                    ("2024-01-02", "2024-01-03", "2024-01-05")
                    if instrument.symbol == config.strategy.benchmark
                    else ("2024-01-02", "2024-01-03")
                )
                _write_bars(output_path, dates=dates)
                return output_path

            with (
                patch(
                    "ai_trade.data.eastmoney.completed_session_cutoff",
                    return_value=target,
                ),
                patch(
                    "ai_trade.data.eastmoney.download_instrument",
                    side_effect=fake_download,
                ),
            ):
                download_universe(config, force=True)

            manifest = json.loads(
                (config.cache_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["requested_through"], "2024-01-05")
            self.assertEqual(manifest["completed_session_cutoff"], "2024-01-05")
            self.assertEqual(manifest["completed_through"], "2024-01-03")
            self.assertEqual(manifest["latest_common_session"], "2024-01-03")


def _write_active_snapshot(cache: Path, csv_names: tuple[str, ...]) -> dict[str, bytes]:
    cache.mkdir(parents=True, exist_ok=True)
    values = {name: f"old-{name}".encode() for name in csv_names}
    values["manifest.json"] = b'{"snapshot":"old"}'
    for name, content in values.items():
        (cache / name).write_bytes(content)
    return values


def _write_staged_snapshot(cache: Path, csv_names: tuple[str, ...]) -> dict[str, Path]:
    staging = cache / ".download"
    staging.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name in csv_names:
        path = staging / name
        path.write_text(
            f"date,open,close,high,low,volume,amount\n2024-01-02,1,1,1,1,1,{name}\n",
            encoding="utf-8",
        )
        paths[name] = path
    return paths


def _manifest_for_paths(paths: dict[str, Path]) -> dict[str, object]:
    return {
        "files": {
            Path(name).stem: {"rows": 1, "sha256": _sha256(path)}
            for name, path in paths.items()
        }
    }


def _prepare_interrupted_install(cache: Path, originals: dict[str, bytes]) -> Path:
    transaction_id = "a" * 32
    transaction_dir = cache / cache_snapshot.TRANSACTIONS_DIR_NAME / transaction_id
    backup_dir = transaction_dir / "backup"
    backup_dir.mkdir(parents=True)
    entries = []
    for name, content in originals.items():
        backup = backup_dir / name
        backup.write_bytes(content)
        entries.append(
            {
                "name": name,
                "had_original": True,
                "backup": f"backup/{name}",
                "backup_sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    marker = {
        "schema_version": cache_snapshot.SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "state": "installing",
        "entries": entries,
    }
    (transaction_dir / cache_snapshot.TRANSACTION_JOURNAL_NAME).write_text(
        json.dumps(marker), encoding="utf-8"
    )
    (cache / cache_snapshot.MARKER_NAME).write_text(
        json.dumps(marker), encoding="utf-8"
    )
    return transaction_dir


def _assert_snapshot_bytes(
    case: unittest.TestCase, cache: Path, expected: dict[str, bytes]
) -> None:
    for name, content in expected.items():
        case.assertEqual((cache / name).read_bytes(), content)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_bars(
    path: Path, dates: tuple[str, ...] = ("2024-01-02", "2024-01-03")
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "open", "close", "high", "low", "volume", "amount"])
        for index, value in enumerate(dates):
            price = 10 + index
            writer.writerow(
                [value, price, price + 0.1, price + 0.2, price - 0.2, 100, 1000]
            )


def _write_config(root: Path, data_overrides: dict | None = None) -> Path:
    path = root / "config" / "default.json"
    path.parent.mkdir(parents=True)
    value = {
        "data": {
            "provider": "eastmoney",
            "fallback_provider": "tencent",
            "start": "2024-01-01",
            "end": "2024-12-31",
            "cache_dir": "data/cache",
            "market_close_time": "15:30",
            "adjustment": "forward",
        },
        "universe": [
            {
                "symbol": "510300",
                "name": "A",
                "market": "SH",
                "asset": "a",
                "lot_size": 100,
            },
            {
                "symbol": "510500",
                "name": "B",
                "market": "SH",
                "asset": "b",
                "lot_size": 100,
            },
        ],
        "strategy": {
            "benchmark": "510300",
            "rebalance_days": 2,
            "lookback_days": 2,
            "skip_days": 0,
            "trend_sma_days": 2,
            "volatility_days": 2,
            "top_n": 1,
            "minimum_momentum": 0,
            "target_annual_volatility": 0.12,
            "minimum_cash_weight": 0.05,
            "max_position_weight": 0.95,
        },
        "risk": {
            "max_portfolio_drawdown": 0.15,
            "max_daily_loss": 0.1,
            "cooldown_days": 2,
        },
        "costs": {
            "commission_bps": 2,
            "slippage_bps": 3,
            "minimum_commission": 5,
        },
        "backtest": {
            "initial_cash": 100000,
            "start": "2024-01-01",
            "end": "2024-12-31",
        },
        "paper": {
            "initial_cash": 100000,
            "state_file": "state/paper_state.json",
            "trades_file": "state/paper_trades.csv",
        },
        "reports_dir": "reports",
        "logs_dir": "logs",
    }
    if data_overrides:
        value["data"].update(data_overrides)
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()
