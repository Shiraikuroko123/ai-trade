from __future__ import annotations

import math
from unittest import TestCase

from ai_trade.research_statistics import (
    apply_holm_correction,
    deterministic_seed,
    moving_block_bootstrap_mean,
)


class MovingBlockBootstrapTests(TestCase):
    def test_positive_effect_reports_reproducible_uncertainty_and_stability(self):
        values = [0.1] * 30
        seed = deterministic_seed("snapshot", "factor", 20)

        first = moving_block_bootstrap_mean(
            values, block_size=4, seed=seed
        )
        second = moving_block_bootstrap_mean(
            values, block_size=4, seed=seed
        )

        self.assertEqual(first, second)
        self.assertAlmostEqual(first["effect_size"], 0.1)
        self.assertAlmostEqual(first["ci_low"], 0.1)
        self.assertAlmostEqual(first["ci_high"], 0.1)
        self.assertAlmostEqual(first["standard_error"], 0.0)
        self.assertEqual(first["p_value"], 0.001)
        self.assertEqual(first["subperiods"], 3)
        self.assertEqual(first["subperiod_means"], [0.1, 0.1, 0.1])
        self.assertEqual(first["positive_subperiods"], 3)
        self.assertAlmostEqual(first["minimum_subperiod_mean"], 0.1)

    def test_zero_effect_does_not_create_significance(self):
        validation = moving_block_bootstrap_mean(
            [0.0] * 30, block_size=3, seed=7
        )
        corrected = apply_holm_correction([validation])[0]

        self.assertEqual(corrected["p_value"], 1.0)
        self.assertEqual(corrected["adjusted_p_value"], 1.0)
        self.assertFalse(corrected["reject_null"])

    def test_invalid_series_and_protocol_parameters_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "at least two"):
            moving_block_bootstrap_mean([0.1], block_size=1, seed=1)
        with self.assertRaisesRegex(ValueError, "finite"):
            moving_block_bootstrap_mean(
                [0.1, math.nan], block_size=1, seed=1
            )
        with self.assertRaisesRegex(ValueError, "block_size"):
            moving_block_bootstrap_mean(
                [0.1, 0.2], block_size=3, seed=1
            )
        with self.assertRaisesRegex(ValueError, "resamples"):
            moving_block_bootstrap_mean(
                [0.1, 0.2], block_size=1, seed=1, resamples=10
            )


class HolmCorrectionTests(TestCase):
    def test_holm_adjustment_controls_the_whole_family(self):
        corrected = apply_holm_correction(
            [{"p_value": 0.01}, {"p_value": 0.03}, {"p_value": 0.04}]
        )

        self.assertEqual(
            [item["adjusted_p_value"] for item in corrected],
            [0.03, 0.06, 0.06],
        )
        self.assertEqual(
            [item["reject_null"] for item in corrected],
            [True, False, False],
        )
        self.assertTrue(all(item["family_size"] == 3 for item in corrected))
        self.assertTrue(all(item["correction"] == "holm" for item in corrected))

    def test_ties_are_deterministic_and_adjusted_values_are_monotone(self):
        corrected = apply_holm_correction(
            [{"p_value": 0.02}, {"p_value": 0.02}, {"p_value": 0.8}]
        )

        self.assertEqual(
            [item["adjusted_p_value"] for item in corrected],
            [0.06, 0.06, 0.8],
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
