from __future__ import annotations

from datetime import date, timedelta
from unittest import TestCase

from ai_trade.factor_lab.expression import (
    ExpressionError,
    compile_expression,
)
from ai_trade.models import Bar


def _bars(closes: list[float]) -> list[Bar]:
    start = date(2026, 1, 5)
    bars = []
    for index, close in enumerate(closes):
        bars.append(
            Bar(
                date=start + timedelta(days=index),
                open=close,
                close=close,
                high=close,
                low=close,
                volume=1000.0,
                amount=close * 1000.0,
            )
        )
    return bars


class ExtendedFunctionTests(TestCase):
    def test_ts_sum_matches_hand_computation(self):
        compiled = compile_expression("ts_sum(close, 3)")
        self.assertEqual(compiled.minimum_history, 3)
        value = compiled.compute(_bars([1.0, 2.0, 3.0, 4.0]))
        self.assertAlmostEqual(value, 9.0)

    def test_ts_rank_uses_midranks_for_ties(self):
        compiled = compile_expression("ts_rank(close, 5)")
        self.assertEqual(compiled.minimum_history, 5)
        self.assertAlmostEqual(
            compiled.compute(_bars([1.0, 2.0, 3.0, 4.0, 5.0])), 1.0
        )
        self.assertAlmostEqual(
            compiled.compute(_bars([5.0, 4.0, 3.0, 2.0, 1.0])), 0.0
        )
        # Last value 2.0 ties with one window member: below=0, equal=2,
        # midrank = 0 + 1.5 = 1.5 -> (1.5 - 1) / 4 = 0.125.
        self.assertAlmostEqual(
            compiled.compute(_bars([2.0, 3.0, 4.0, 5.0, 2.0])), 0.125
        )
        # And with a strictly smaller member: below=1, equal=1,
        # midrank = 2 -> (2 - 1) / 4 = 0.25.
        self.assertAlmostEqual(
            compiled.compute(_bars([1.0, 3.0, 4.0, 5.0, 2.0])), 0.25
        )

    def test_ts_argmax_prefers_most_recent_peak(self):
        compiled = compile_expression("ts_argmax(close, 4)")
        self.assertAlmostEqual(
            compiled.compute(_bars([9.0, 1.0, 2.0, 3.0])), 3.0
        )
        self.assertAlmostEqual(
            compiled.compute(_bars([1.0, 9.0, 2.0, 9.0])), 0.0
        )
        trough = compile_expression("ts_argmin(close, 4)")
        self.assertAlmostEqual(
            trough.compute(_bars([1.0, 9.0, 8.0, 7.0])), 3.0
        )
        self.assertAlmostEqual(
            trough.compute(_bars([9.0, 1.0, 8.0, 1.0])), 0.0
        )

    def test_delta_subtracts_the_lagged_value(self):
        compiled = compile_expression("delta(close, 2)")
        self.assertEqual(compiled.minimum_history, 3)
        self.assertAlmostEqual(
            compiled.compute(_bars([1.0, 5.0, 4.0])), 3.0
        )

    def test_nested_history_accounting_is_additive(self):
        compiled = compile_expression("ts_rank(delta(close, 5), 10)")
        self.assertEqual(compiled.minimum_history, 15)
        self.assertIsNone(compiled.compute(_bars([1.0] * 14)))
        self.assertIsNotNone(
            compile_expression("ts_sum(close, 2)").compute(_bars([1.0, 2.0]))
        )

    def test_window_minimums_fail_closed(self):
        for source in ("ts_rank(close, 1)", "ts_argmax(close, 1)", "ts_argmin(close, 1)"):
            with self.assertRaises(ExpressionError):
                compile_expression(source)
        with self.assertRaises(ExpressionError):
            compile_expression("ts_sum(close, 0)")
        with self.assertRaises(ExpressionError):
            compile_expression("delta(close, 501)")

    def test_existing_expressions_keep_their_canonical_form(self):
        compiled = compile_expression("sma( close , 5 ) / std(close, 5)")
        self.assertEqual(compiled.source, "sma(close,5)/std(close,5)")
        extended = compile_expression("ts_rank( close , 20 )")
        self.assertEqual(extended.source, "ts_rank(close,20)")

    def test_extended_functions_compose_with_arithmetic(self):
        compiled = compile_expression(
            "ts_rank(close, 3) - ts_argmax(close, 3) / 2"
        )
        value = compiled.compute(_bars([1.0, 3.0, 2.0]))
        # ts_rank window [1,3,2]: last=2 -> below=1, equal=1 -> midrank 2
        # -> (2-1)/2 = 0.5; ts_argmax: peak 3 at offset 1 -> 0.5 - 0.5 = 0.0.
        self.assertAlmostEqual(value, 0.0)


if __name__ == "__main__":
    import unittest

    unittest.main()
