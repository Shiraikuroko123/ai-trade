import unittest
from datetime import date
from types import SimpleNamespace

from ai_trade.execution import Portfolio, execute_target_weights
from ai_trade.models import Bar, CostSettings, Instrument


class FakeMarket:
    def __init__(self):
        self.day = date(2024, 1, 2)
        self._bar = Bar(self.day, 10, 10, 10, 10, 100, 1000)

    def bar(self, symbol, on_date):
        return self._bar

    def latest_bar_on_or_before(self, symbol, on_date):
        return self._bar

    def instrument(self, symbol):
        return Instrument(symbol, symbol, "SH", "test", 100)


class ExecutionTests(unittest.TestCase):
    def test_buys_whole_lots_and_keeps_nonnegative_cash(self):
        market = FakeMarket()
        portfolio = Portfolio(10000)
        trades = execute_target_weights(
            portfolio,
            market,
            market.day,
            {"TEST": 0.5},
            CostSettings(2, 3, 5),
            "unit test",
        )
        self.assertEqual(portfolio.positions["TEST"] % 100, 0)
        self.assertGreaterEqual(portfolio.cash, 0)
        self.assertEqual(len(trades), 1)

    def test_no_trade_band_keeps_small_existing_drift(self):
        market = FakeMarket()
        portfolio = Portfolio(50000, {"TEST": 500})
        trades = execute_target_weights(
            portfolio,
            market,
            market.day,
            {"TEST": 0.11},
            CostSettings(2, 3, 5),
            "unit test",
            minimum_rebalance_weight=0.02,
        )
        self.assertEqual(trades, [])
        self.assertEqual(portfolio.positions["TEST"], 500)


if __name__ == "__main__":
    unittest.main()
