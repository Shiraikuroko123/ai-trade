import csv
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from ai_trade.config import load_config
from ai_trade.data.eastmoney import completed_session_cutoff, load_cached_bars
from ai_trade.data.market import MarketData


class DataTests(unittest.TestCase):
    def test_completed_session_cutoff_excludes_market_day_before_close(self):
        china = timezone(timedelta(hours=8))
        morning = datetime(2024, 1, 3, 10, 0, tzinfo=china)
        evening = datetime(2024, 1, 3, 16, 0, tzinfo=china)
        self.assertEqual(completed_session_cutoff(morning).isoformat(), "2024-01-02")
        self.assertEqual(completed_session_cutoff(evening).isoformat(), "2024-01-03")

    def test_completed_session_cutoff_rolls_weekend_to_friday(self):
        china = timezone(timedelta(hours=8))
        sunday = datetime(2024, 1, 7, 18, 0, tzinfo=china)
        monday_morning = datetime(2024, 1, 8, 10, 0, tzinfo=china)
        self.assertEqual(completed_session_cutoff(sunday).isoformat(), "2024-01-05")
        self.assertEqual(completed_session_cutoff(monday_morning).isoformat(), "2024-01-05")

    def test_market_data_filters_unfinished_bar(self):
        china = timezone(timedelta(hours=8))
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _write_config(root)
            _write_bars(root / "data/cache/510300.csv")
            _write_bars(root / "data/cache/510500.csv")
            market = MarketData(
                load_config(config),
                as_of=datetime(2024, 1, 3, 10, 0, tzinfo=china),
            )
            self.assertEqual(market.latest_date().isoformat(), "2024-01-02")
            self.assertEqual(
                [value.isoformat() for value in market.excluded_dates["510300"]],
                ["2024-01-03"],
            )

    def test_cache_rejects_non_increasing_dates(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "bad.csv"
            _write_bars(path, dates=("2024-01-02", "2024-01-02"))
            with self.assertRaisesRegex(RuntimeError, "strictly increasing"):
                load_cached_bars(path)

    def test_market_data_rejects_manifest_policy_mismatch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _write_config(root)
            cache = root / "data/cache"
            _write_bars(cache / "510300.csv")
            _write_bars(cache / "510500.csv")
            (cache / "manifest.json").write_text(
                json.dumps(
                    {"provider": "other", "adjustment": "forward", "files": {}}
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "provider"):
                MarketData(load_config(config))


def _write_config(root: Path) -> Path:
    path = root / "config/default.json"
    path.parent.mkdir(parents=True)
    value = {
        "data": {
            "provider": "eastmoney",
            "start": "2024-01-01",
            "end": "2024-12-31",
            "cache_dir": "data/cache",
            "market_close_time": "15:30",
            "adjustment": "forward",
        },
        "universe": [
            {"symbol": "510300", "name": "A", "market": "SH", "asset": "a", "lot_size": 100},
            {"symbol": "510500", "name": "B", "market": "SH", "asset": "b", "lot_size": 100},
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
        "paper": {
            "initial_cash": 100000,
            "state_file": "state/paper_state.json",
            "trades_file": "state/paper_trades.csv",
        },
        "reports_dir": "reports",
        "logs_dir": "logs",
    }
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def _write_bars(path: Path, dates=("2024-01-02", "2024-01-03")) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "open", "close", "high", "low", "volume", "amount"])
        for index, value in enumerate(dates):
            price = 10 + index
            writer.writerow([value, price, price + 0.1, price + 0.2, price - 0.2, 100, 1000])


if __name__ == "__main__":
    unittest.main()
