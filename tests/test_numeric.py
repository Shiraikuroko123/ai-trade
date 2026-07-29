import math
import random
import statistics
import unittest

from ai_trade.numeric import (
    population_standard_deviation,
    sample_standard_deviation,
)


class NumericTests(unittest.TestCase):
    def test_standard_deviations_match_standard_library_estimators(self):
        rng = random.Random(90210)
        cases = [
            [1.0, 2.0],
            [0.0] * 20,
            [1e12 + index / 10 for index in range(60)],
            [rng.uniform(-0.15, 0.15) for _ in range(252)],
        ]

        for values in cases:
            with self.subTest(count=len(values), first=values[0]):
                expected_sample = statistics.stdev(values)
                expected_population = statistics.pstdev(values)
                self.assertTrue(
                    math.isclose(
                        sample_standard_deviation(values),
                        expected_sample,
                        rel_tol=1e-12,
                        abs_tol=1e-15,
                    )
                )
                self.assertTrue(
                    math.isclose(
                        population_standard_deviation(values),
                        expected_population,
                        rel_tol=1e-12,
                        abs_tol=1e-15,
                    )
                )

    def test_population_singleton_is_zero(self):
        self.assertEqual(population_standard_deviation([4.5]), 0.0)

    def test_invalid_series_lengths_are_rejected(self):
        with self.assertRaises(ValueError):
            sample_standard_deviation([])
        with self.assertRaises(ValueError):
            sample_standard_deviation([1.0])
        with self.assertRaises(ValueError):
            population_standard_deviation([])


if __name__ == "__main__":
    unittest.main()
