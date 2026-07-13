import unittest
from dataclasses import replace
from datetime import date, timedelta
from types import SimpleNamespace

from ai_trade.models import Bar, Instrument, StrategySettings
from ai_trade.strategy import MomentumTrendStrategy


class FakeMarket:
    def __init__(self):
        self.symbols = {
            "UP": SimpleNamespace(instrument=Instrument("UP", "Up", "SH", "test", 100)),
            "DOWN": SimpleNamespace(instrument=Instrument("DOWN", "Down", "SH", "test", 100)),
        }
        start = date(2023, 1, 1)
        self._bars = {
            "UP": [Bar(start + timedelta(days=i), 10 + i * 0.05, 10 + i * 0.05, 0, 0, 1, 1) for i in range(240)],
            "DOWN": [Bar(start + timedelta(days=i), 20 - i * 0.03, 20 - i * 0.03, 0, 0, 1, 1) for i in range(240)],
        }

    def history(self, symbol, on_date, count):
        values = [bar for bar in self._bars[symbol] if bar.date <= on_date]
        return values[-count:]

    def bar(self, symbol, on_date):
        return next((bar for bar in self._bars[symbol] if bar.date == on_date), None)


class StrategyTests(unittest.TestCase):
    def test_selects_positive_trending_asset(self):
        settings = StrategySettings(
            benchmark="UP",
            rebalance_days=20,
            lookback_days=63,
            skip_days=5,
            trend_sma_days=100,
            volatility_days=20,
            top_n=1,
            minimum_momentum=0,
            target_annual_volatility=0.12,
            minimum_cash_weight=0.05,
            max_position_weight=0.95,
        )
        market = FakeMarket()
        signal = MomentumTrendStrategy(settings).generate(market, market._bars["UP"][-1].date)
        self.assertIn("UP", signal.target_weights)
        self.assertNotIn("DOWN", signal.target_weights)
        self.assertLessEqual(sum(signal.target_weights.values()), 0.95)

    def test_liquidity_filter_rejects_small_average_amount(self):
        settings = StrategySettings(
            benchmark="UP", rebalance_days=20, lookback_days=63, skip_days=5,
            trend_sma_days=100, volatility_days=20, top_n=1, minimum_momentum=0,
            target_annual_volatility=0.12, minimum_cash_weight=0.05,
            max_position_weight=0.95,
        )
        market = FakeMarket()
        signal = MomentumTrendStrategy(
            replace(settings, minimum_average_amount=2.0)
        ).generate(market, market._bars["UP"][-1].date)
        self.assertEqual(signal.target_weights, {})

    def test_asset_class_constraint_reduces_target_exposure(self):
        settings = StrategySettings(
            benchmark="UP", rebalance_days=20, lookback_days=63, skip_days=5,
            trend_sma_days=100, volatility_days=20, top_n=1, minimum_momentum=0,
            target_annual_volatility=0, minimum_cash_weight=0,
            max_position_weight=0.95, max_asset_class_weight=0.05,
        )
        market = FakeMarket()
        signal = MomentumTrendStrategy(settings).generate(
            market, market._bars["UP"][-1].date
        )
        self.assertAlmostEqual(sum(signal.target_weights.values()), 0.05)
        self.assertEqual(signal.diagnostics["constraints"]["asset_class_caps_applied"], ["other"])


if __name__ == "__main__":
    unittest.main()
