from __future__ import annotations

import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ai_trade.config import load_config
from ai_trade.data.eastmoney import download_universe
from ai_trade.data.providers import (
    ProviderConfigurationError,
    provider_catalog,
    provider_for,
    registered_provider_names,
    snapshot_provider_names,
)


class DataProviderRegistryTests(unittest.TestCase):
    def test_registry_exposes_only_implemented_providers(self):
        self.assertEqual(
            registered_provider_names(), ("eastmoney", "tencent", "tushare", "yahoo")
        )
        self.assertEqual(snapshot_provider_names(), ("eastmoney", "tencent"))
        catalog = provider_catalog()
        self.assertEqual(
            [item["key"] for item in catalog],
            ["eastmoney", "tencent", "tushare", "yahoo"],
        )
        self.assertTrue(all(item["daily_bars"] for item in catalog))
        intraday = {item["key"]: item["intraday_bars"] for item in catalog}
        self.assertTrue(intraday["eastmoney"])
        self.assertFalse(intraday["tencent"])
        self.assertFalse(intraday["tushare"])
        self.assertFalse(intraday["yahoo"])
        yahoo = next(item for item in catalog if item["key"] == "yahoo")
        self.assertFalse(yahoo["snapshot_eligible"])
        self.assertEqual(
            yahoo["cross_check_fields"],
            ["open", "high", "low", "close", "volume"],
        )
        tushare = next(item for item in catalog if item["key"] == "tushare")
        self.assertFalse(tushare["snapshot_eligible"])
        self.assertEqual(tushare["status"], "implemented_reference_only_requires_token")

    def test_provider_lookup_is_normalized_and_rejects_unknown_sources(self):
        self.assertEqual(provider_for(" EASTMONEY ").descriptor.key, "eastmoney")
        self.assertEqual(provider_for(" Yahoo ").descriptor.key, "yahoo")
        self.assertEqual(provider_for(" Tushare ").descriptor.key, "tushare")
        with self.assertRaisesRegex(ProviderConfigurationError, "registered providers"):
            provider_for("akshare")

    def test_reference_only_provider_cannot_supply_primary_or_fallback_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = _write_config(root)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["data"]["provider"] = "yahoo"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "data.provider"):
                load_config(path)

            raw["data"]["provider"] = "eastmoney"
            raw["data"]["fallback_provider"] = "yahoo"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "data.fallback_provider"):
                load_config(path)

            raw["data"]["fallback_provider"] = "tushare"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "data.fallback_provider"):
                load_config(path)

    def test_yahoo_is_allowed_as_forward_adjusted_cross_check_reference(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = _write_config(root)
            raw = json.loads(path.read_text(encoding="utf-8"))
            raw["data"]["cross_check"] = {
                "enabled": True,
                "reference_provider": " Yahoo ",
                "lookback_sessions": 5,
                "minimum_overlap_sessions": 3,
            }
            path.write_text(json.dumps(raw), encoding="utf-8")
            config = load_config(path)
            self.assertEqual(
                config.raw["data"]["cross_check"]["reference_provider"], "yahoo"
            )

            raw["data"]["adjustment"] = "backward"
            path.write_text(json.dumps(raw), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "supports adjustment modes"):
                load_config(path)

    def test_config_can_select_tencent_with_eastmoney_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = _write_config(root)
            config = load_config(path)
            self.assertEqual(config.raw["data"]["provider"], "eastmoney")

            raw = path.read_text(encoding="utf-8").replace(
                '"provider": "eastmoney"',
                '"provider": " Tencent "',
                1,
            ).replace(
                '"fallback_provider": "tencent"',
                '"fallback_provider": " EASTMONEY "',
                1,
            )
            path.write_text(raw, encoding="utf-8")
            configured = load_config(path)
            self.assertEqual(configured.raw["data"]["provider"], "tencent")
            self.assertEqual(configured.raw["data"]["fallback_provider"], "eastmoney")

    def test_config_rejects_same_primary_and_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = _write_config(root)
            raw = path.read_text(encoding="utf-8").replace(
                '"fallback_provider": "tencent"',
                '"fallback_provider": "eastmoney"',
                1,
            )
            path.write_text(raw, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "must differ"):
                load_config(path)

    def test_primary_provider_dispatch_is_recorded_in_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = _write_config(root)
            raw = path.read_text(encoding="utf-8").replace(
                '"provider": "eastmoney"',
                '"provider": "tencent"',
                1,
            ).replace(
                '"fallback_provider": "tencent"',
                '"fallback_provider": "eastmoney"',
                1,
            )
            path.write_text(raw, encoding="utf-8")
            config = load_config(path)

            def fake_tencent(
                config,
                instrument,
                output_path,
                *,
                cache_path,
                cutoff,
                proxy_mode,
                provider_metadata,
            ):
                _write_bars(output_path, cutoff.isoformat())
                return output_path

            with patch(
                "ai_trade.data.tencent.download_instrument",
                side_effect=fake_tencent,
            ) as primary:
                download_universe(config, force=True)

            primary.assert_called_once()
            manifest = json.loads(
                (config.cache_dir / "manifest.json").read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["provider"], "tencent")
            self.assertEqual(manifest["files"]["510300"]["source"], "network")
            self.assertEqual(
                manifest["request_policy"]["provider_chain"],
                ["tencent", "eastmoney", "validated_local_cache"],
            )


def _write_config(root: Path) -> Path:
    path = root / "config/default.json"
    path.parent.mkdir(parents=True)
    path.write_text(
        """{
          "data": {
            "provider": "eastmoney",
            "fallback_provider": "tencent",
            "start": "2024-01-01",
            "end": "2024-12-31",
            "cache_dir": "data/cache",
            "market_close_time": "15:30",
            "adjustment": "forward"
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
            "max_position_weight": 0.95
          },
          "risk": {"max_portfolio_drawdown": 0.15, "max_daily_loss": 0.1, "cooldown_days": 2},
          "costs": {"commission_bps": 2, "slippage_bps": 3, "minimum_commission": 5},
          "backtest": {"initial_cash": 100000, "start": "2024-01-01", "end": "2024-12-31"},
          "paper": {"initial_cash": 100000, "state_file": "state/paper_state.json", "trades_file": "state/paper_trades.csv"},
          "reports_dir": "reports",
          "logs_dir": "logs"
        }""",
        encoding="utf-8",
    )
    return path


def _write_bars(path: Path, latest: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "open", "close", "high", "low", "volume", "amount"])
        writer.writerow(["2024-01-02", 10, 10.1, 10.2, 9.8, 100, 1000])
        if latest != "2024-01-02":
            writer.writerow([latest, 10.1, 10.2, 10.3, 10, 110, 1120])


if __name__ == "__main__":
    unittest.main()
