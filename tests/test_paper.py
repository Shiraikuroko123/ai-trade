import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace

from ai_trade.broker.paper import initialize_paper, run_paper
from ai_trade.broker.paper_audit import audit_paper
from ai_trade.models import Bar, CostSettings, Instrument, RiskSettings, StrategySettings


class MutableMarket:
    def __init__(self):
        start = date(2024, 1, 1)
        self.all_dates = [start + timedelta(days=index) for index in range(10)]
        self.calendar = self.all_dates[:6]
        self._bars = {}
        self.symbols = {}
        self.file_hashes = {"510300": "a" * 64, "510500": "b" * 64}
        for symbol, drift in (("510300", 0.03), ("510500", 0.10)):
            bars = []
            for index, on_date in enumerate(self.all_dates):
                price = 10 + index * drift
                bars.append(Bar(on_date, price, price + drift, price + drift, price, 100, 1000))
            self._bars[symbol] = bars
            self.symbols[symbol] = SimpleNamespace(
                instrument=Instrument(symbol, symbol, "SH", "test", 100)
            )

    def latest_date(self):
        return self.calendar[-1]

    def bar(self, symbol, on_date):
        return next((bar for bar in self._bars[symbol] if bar.date == on_date), None)

    def latest_bar_on_or_before(self, symbol, on_date):
        values = [bar for bar in self._bars[symbol] if bar.date <= on_date]
        return values[-1] if values else None

    def history(self, symbol, on_date, count):
        values = [bar for bar in self._bars[symbol] if bar.date <= on_date]
        return values[-count:]

    def instrument(self, symbol):
        return self.symbols[symbol].instrument


def _config(root: Path):
    settings = StrategySettings("510300", 3, 2, 0, 2, 2, 1, 0, 1, 0, 1)
    return SimpleNamespace(
        raw={"paper": {"initial_cash": 10000, "minimum_promotion_sessions": 20}},
        strategy=settings,
        risk=RiskSettings(0.5, 0.5, 2),
        costs=CostSettings(0, 0, 0),
        paper_state_file=root / "state/paper_state.json",
        paper_trades_file=root / "state/paper_trades.csv",
        paper_equity_file=root / "state/paper_equity.csv",
        reports_dir=root / "reports",
    )


class PaperTests(unittest.TestCase):
    def test_replays_missed_sessions_and_preserves_daily_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _config(root)
            market = MutableMarket()
            initialize_paper(config)
            first = run_paper(config, market)
            self.assertIsNotNone(first["pending_targets"])
            first_path = config.reports_dir / "paper_20240106.json"
            original = first_path.read_text(encoding="utf-8")

            market.calendar = market.all_dates[:9]
            latest = run_paper(config, market)
            self.assertEqual(latest["date"], "2024-01-09")
            self.assertEqual(latest["sessions_since_rebalance"], 0)
            ledger = config.paper_trades_file.read_text(encoding="utf-8")
            self.assertIn("2024-01-07", ledger)

            repeated = run_paper(config, market)
            self.assertEqual(repeated["status"], "already_processed")
            self.assertEqual(
                (config.reports_dir / "paper_20240109.json").read_text(encoding="utf-8"),
                json.dumps({key: value for key, value in repeated.items() if key != "status"}, ensure_ascii=False, indent=2),
            )
            self.assertEqual(first_path.read_text(encoding="utf-8"), original)

    def test_rejects_nonpositive_starting_cash(self):
        with tempfile.TemporaryDirectory() as temporary:
            with self.assertRaisesRegex(ValueError, "positive"):
                initialize_paper(_config(Path(temporary)), cash=-1)

    def test_rejects_configuration_drift(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            initialize_paper(config)
            config.strategy = replace(config.strategy, rebalance_days=4)
            with self.assertRaisesRegex(RuntimeError, "configuration changed"):
                run_paper(config, MutableMarket())

    def test_forward_audit_requires_independent_sessions(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            market = MutableMarket()
            initialize_paper(config)
            run_paper(config, market)
            report = audit_paper(config, market)
            self.assertEqual(report["sessions"], 1)
            self.assertEqual(report["remaining_sessions"], 19)
            self.assertFalse(report["eligible_for_broker_sandbox"])
            self.assertFalse(report["live_ready"])
            self.assertEqual(report["integrity_errors"], [])


if __name__ == "__main__":
    unittest.main()
