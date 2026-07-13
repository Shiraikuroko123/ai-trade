import unittest
from datetime import date, timedelta

from ai_trade.models import EquityPoint
from ai_trade.strategy import _portfolio_volatility, _risk_parity_weights
from ai_trade.validation import moving_block_bootstrap


class ValidationTests(unittest.TestCase):
    def test_moving_block_bootstrap_is_deterministic(self):
        start = date(2020, 1, 1)
        equity = 100.0
        curve = [EquityPoint(start, equity, 0, 0)]
        high = equity
        for index in range(1, 121):
            equity *= 1.002 if index % 5 else 0.997
            high = max(high, equity)
            curve.append(
                EquityPoint(start + timedelta(days=index), equity, 0, equity / high - 1)
            )
        first = moving_block_bootstrap(curve, samples=100, block_days=10, seed=7)
        second = moving_block_bootstrap(curve, samples=100, block_days=10, seed=7)
        self.assertEqual(first, second)
        self.assertGreater(first["probability_cagr_positive"], 0.9)

    def test_risk_parity_weights_are_positive_and_normalized(self):
        covariance = [
            [0.0004, 0.00005, 0.00002],
            [0.00005, 0.0001, 0.00001],
            [0.00002, 0.00001, 0.0002],
        ]
        weights = _risk_parity_weights(covariance)
        self.assertAlmostEqual(sum(weights), 1.0)
        self.assertTrue(all(value > 0 for value in weights))
        self.assertGreater(_portfolio_volatility(weights, covariance), 0)


if __name__ == "__main__":
    unittest.main()
