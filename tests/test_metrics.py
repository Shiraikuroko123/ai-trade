import unittest
from datetime import date, timedelta

from ai_trade.metrics import calculate_metrics
from ai_trade.models import EquityPoint


class MetricsTests(unittest.TestCase):
    def test_positive_curve_has_positive_return(self):
        start = date(2024, 1, 1)
        curve = [
            EquityPoint(start + timedelta(days=index), 100 + index, 0, 0)
            for index in range(30)
        ]
        metrics = calculate_metrics(curve)
        self.assertGreater(metrics["total_return"], 0)
        self.assertGreater(metrics["sharpe"], 0)


if __name__ == "__main__":
    unittest.main()
