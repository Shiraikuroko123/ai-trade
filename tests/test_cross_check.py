from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from ai_trade.config import load_config
from ai_trade.data.cache_snapshot import install_snapshot
from ai_trade.data.cross_check import (
    cross_check_market_snapshot,
    cross_source_projection,
)


class _FakeProvider:
    def __init__(self, *, mismatch: bool = False):
        self.mismatch = mismatch

    def download(
        self,
        config,
        instrument,
        output_path,
        *,
        cache_path,
        cutoff,
        proxy_mode,
        network_errors,
        provider_metadata,
    ):
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["date", "open", "close", "high", "low", "volume", "amount"])
            for index in range(5):
                close = 10.0 + index + (0.5 if self.mismatch and index == 4 else 0.0)
                writer.writerow(
                    [
                        f"2024-01-0{index + 2}",
                        9.0 + index,
                        close,
                        11.0 + index,
                        8.0 + index,
                        100.0 + index,
                        1000.0 + index,
                    ]
                )
        return output_path


class CrossCheckTests(unittest.TestCase):
    def test_matching_reference_is_bound_to_manifest_and_verifies(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = _config(root)
            config = load_config(config_path)
            staged = config.cache_dir / ".test-staged" / "510300.csv"
            _write_primary(staged)
            manifest = _manifest(staged)
            install_snapshot(config.cache_dir, {"510300.csv": staged}, manifest)

            with patch(
                "ai_trade.data.cross_check.provider_for",
                return_value=_FakeProvider(),
            ), patch(
                "ai_trade.data.cross_check.completed_session_cutoff",
                return_value=date(2024, 1, 8),
            ):
                result = cross_check_market_snapshot(config, force=True)

            self.assertEqual(result["status"], "passed")
            self.assertTrue(result["persisted"])
            stored = json.loads(
                (config.cache_dir / "manifest.json").read_text(encoding="utf-8")
            )
            projection = cross_source_projection(
                stored,
                file_hashes={"510300": stored["files"]["510300"]["sha256"]},
            )
            self.assertTrue(projection["valid"])
            self.assertEqual(projection["status"], "passed")
            stored["cross_source_check"]["summary"]["matched"] = 0
            tampered = cross_source_projection(stored)
            self.assertFalse(tampered["valid"])
            self.assertEqual(tampered["status"], "invalid")
            self.assertEqual(tampered["reason"], "audit_digest_mismatch")

    def test_material_difference_is_failed_without_replacing_primary_csv(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = _config(root)
            config = load_config(config_path)
            staged = config.cache_dir / ".test-staged" / "510300.csv"
            _write_primary(staged)
            primary_bytes = staged.read_bytes()
            install_snapshot(config.cache_dir, {"510300.csv": staged}, _manifest(staged))

            with patch(
                "ai_trade.data.cross_check.provider_for",
                return_value=_FakeProvider(mismatch=True),
            ), patch(
                "ai_trade.data.cross_check.completed_session_cutoff",
                return_value=date(2024, 1, 8),
            ):
                result = cross_check_market_snapshot(config, force=True)

            self.assertEqual(result["status"], "failed")
            self.assertTrue(result["persisted"])
            self.assertEqual(
                (config.cache_dir / "510300.csv").read_bytes(), primary_bytes
            )
            projection = cross_source_projection(
                json.loads(
                    (config.cache_dir / "manifest.json").read_text(encoding="utf-8")
                ),
                file_hashes={"510300": hashlib.sha256(primary_bytes).hexdigest()},
            )
            self.assertTrue(projection["valid"])
            self.assertEqual(projection["status"], "failed")

    def test_fallback_file_uses_primary_as_independent_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = load_config(_config(root))
            staged = config.cache_dir / ".test-staged" / "510300.csv"
            _write_primary(staged)
            manifest = _manifest(staged)
            file_entry = manifest["files"]["510300"]
            file_entry["source"] = "tencent_network_fallback"
            file_entry["source_provider"] = "tencent_newfqkline"
            install_snapshot(config.cache_dir, {"510300.csv": staged}, manifest)
            requested: list[str] = []

            def provider(name: str):
                requested.append(name)
                return _FakeProvider()

            with patch(
                "ai_trade.data.cross_check.provider_for",
                side_effect=provider,
            ), patch(
                "ai_trade.data.cross_check.completed_session_cutoff",
                return_value=date(2024, 1, 8),
            ):
                result = cross_check_market_snapshot(config, force=True)

            self.assertEqual(result["status"], "passed")
            self.assertEqual(result["symbols"][0]["actual_provider"], "tencent")
            self.assertEqual(result["symbols"][0]["reference_provider"], "eastmoney")
            self.assertEqual(requested, ["tencent", "eastmoney"])


def _config(root: Path) -> Path:
    path = root / "config" / "default.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        json.dumps(
            {
                "data": {
                    "provider": "eastmoney",
                    "fallback_provider": "tencent",
                    "start": "2024-01-01",
                    "end": "2024-12-31",
                    "cache_dir": "data/cache",
                    "market_close_time": "15:30",
                    "adjustment": "forward",
                    "cross_check": {
                        "enabled": True,
                        "reference_provider": "tencent",
                        "lookback_sessions": 5,
                        "minimum_overlap_sessions": 3,
                    },
                },
                "universe": [
                    {"symbol": "510300", "name": "A", "market": "SH", "asset": "a", "lot_size": 100}
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
                "risk": {"max_portfolio_drawdown": 0.15, "max_daily_loss": 0.1, "cooldown_days": 2},
                "costs": {"commission_bps": 2, "slippage_bps": 3, "minimum_commission": 5},
                "backtest": {"initial_cash": 100000, "start": "2024-01-01", "end": "2024-12-31"},
                "paper": {"initial_cash": 100000, "state_file": "state/paper_state.json", "trades_file": "state/paper_trades.csv"},
                "reports_dir": "reports",
                "logs_dir": "logs",
            }
        ),
        encoding="utf-8",
    )
    return path


def _write_primary(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "open", "close", "high", "low", "volume", "amount"])
        for index in range(5):
            writer.writerow(
                [
                    f"2024-01-0{index + 2}",
                    9.0 + index,
                    10.0 + index,
                    11.0 + index,
                    8.0 + index,
                    100.0 + index,
                    1000.0 + index,
                ]
            )


def _manifest(path: Path) -> dict[str, object]:
    return {
        "provider": "eastmoney",
        "adjustment": "forward",
        "files": {
            "510300": {
                "rows": 5,
                "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                "source": "network",
            }
        },
    }


if __name__ == "__main__":
    unittest.main()
