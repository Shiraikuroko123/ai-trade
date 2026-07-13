import unittest
from datetime import date, timedelta
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


class RuleMarket(FakeMarket):
    def __init__(self, opening_price=10.0):
        super().__init__()
        self._bar = Bar(
            self.day,
            opening_price,
            opening_price,
            opening_price,
            opening_price,
            10000,
            100000,
        )
        self._previous = Bar(
            self.day - timedelta(days=1), 10, 10, 10, 10, 10000, 100000
        )
        self._instrument = Instrument(
            "600000",
            "Test stock",
            "SH",
            "equity",
            100,
            instrument_type="STOCK",
            asset_class="equity",
            sector="bank",
            price_limit_pct=0.10,
            tick_size=0.01,
        )

    def instrument(self, symbol):
        return self._instrument

    def previous_bar(self, symbol, on_date):
        return self._previous


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

    def test_upper_limit_buy_is_rejected_and_audited(self):
        market = RuleMarket(opening_price=11.0)
        portfolio = Portfolio(10000)
        rejections = []
        trades = execute_target_weights(
            portfolio,
            market,
            market.day,
            {"600000": 0.5},
            CostSettings(2, 0, 5),
            "limit test",
            rejections=rejections,
        )
        self.assertEqual(trades, [])
        self.assertEqual(len(rejections), 1)
        self.assertIn("upper price limit", rejections[0].reason)

    def test_stock_sell_uses_date_effective_tax_and_transfer_fee(self):
        market = RuleMarket()
        portfolio = Portfolio(9000, {"600000": 100})
        costs = CostSettings(
            2,
            0,
            5,
            by_instrument_type={
                "STOCK": {
                    "commission_bps": 2,
                    "slippage_bps": 0,
                    "minimum_commission": 5,
                    "sell_stamp_duty_bps": 10,
                    "transfer_fee_bps": 0.2,
                }
            },
            history=(
                {
                    "instrument_type": "STOCK",
                    "start": "2023-08-28",
                    "end": None,
                    "sell_stamp_duty_bps": 5,
                    "transfer_fee_bps": 0.1,
                },
            ),
        )
        trades = execute_target_weights(
            portfolio, market, market.day, {}, costs, "fee test"
        )
        self.assertEqual(len(trades), 1)
        self.assertAlmostEqual(trades[0].commission, 5.0)
        self.assertAlmostEqual(trades[0].stamp_duty, 0.5)
        self.assertAlmostEqual(trades[0].transfer_fee, 0.01)
        self.assertAlmostEqual(portfolio.cash, 9994.49)


if __name__ == "__main__":
    unittest.main()
