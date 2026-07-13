import unittest
from datetime import date, timedelta
from types import SimpleNamespace

from ai_trade.backtest import BacktestEngine
from ai_trade.models import Bar, CostSettings, Instrument, RiskSettings, StrategySettings


class FakeMarket:
    def __init__(self):
        start = date(2024, 1, 1)
        self.calendar = [start + timedelta(days=index) for index in range(12)]
        self._bars = {}
        self.symbols = {}
        for symbol, drift in (("510300", 0.05), ("510500", 0.12)):
            bars = []
            for index, on_date in enumerate(self.calendar):
                open_price = 10 + index * drift
                bars.append(
                    Bar(on_date, open_price, open_price + drift, open_price + drift, open_price, 100, 1000)
                )
            self._bars[symbol] = bars
            self.symbols[symbol] = SimpleNamespace(
                instrument=Instrument(symbol, symbol, "SH", "test", 100)
            )

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

    def snapshot_metadata(self):
        return {"test": True}


def _config(settings):
    return SimpleNamespace(
        raw={"backtest": {"start": "2024-01-04", "end": "2024-01-12", "initial_cash": 10000}},
        strategy=settings,
        risk=RiskSettings(0.5, 0.5, 2),
        costs=CostSettings(0, 0, 0),
    )


class BacktestTests(unittest.TestCase):
    def test_signal_fills_next_open_and_benchmark_uses_same_entry_session(self):
        settings = StrategySettings("510300", 3, 2, 0, 2, 2, 1, 0, 1, 0, 1)
        market = FakeMarket()
        result = BacktestEngine(_config(settings), market).run()
        self.assertTrue(result.trades)
        self.assertEqual(result.trades[0].date, result.equity_curve[1].date)
        self.assertEqual(result.benchmark_curve[0].equity, 10000)
        entry = market.bar("510300", result.benchmark_curve[1].date)
        self.assertAlmostEqual(
            result.benchmark_curve[1].equity,
            10000 * entry.close / entry.open,
        )
        self.assertEqual(result.latest_signal.date, result.equity_curve[-1].date)

    def test_scheduled_backtest_keeps_one_continuous_account(self):
        settings = StrategySettings("510300", 3, 2, 0, 2, 2, 1, 0, 1, 0, 1)
        market = FakeMarket()
        start = date(2024, 1, 4)
        boundary = date(2024, 1, 8)
        result = BacktestEngine(_config(settings), market).run_scheduled(
            start,
            date(2024, 1, 12),
            {start: settings, boundary: settings},
        )
        boundary_point = next(point for point in result.equity_curve if point.date == boundary)
        self.assertNotEqual(boundary_point.equity, 10000)
        self.assertEqual(len(result.metadata["settings_schedule"]), 2)


if __name__ == "__main__":
    unittest.main()
