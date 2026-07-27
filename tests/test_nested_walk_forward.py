from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from ai_trade.cli import build_parser, main
from ai_trade.config import load_config
from ai_trade.hypothesis_lab.nested import (
    MAX_TOTAL_BACKTESTS,
    NestedWalkForwardEngine,
    _fold_layout,
)
from ai_trade.strategy_lab import StrategyLabEngine, StrategyLabStore


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _weekday_calendar(end: date, sessions: int) -> list[date]:
    days: list[date] = []
    cursor = end
    while len(days) < sessions:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return sorted(days)


class _Market:
    def __init__(self, sessions: int = 400) -> None:
        self._latest = date(2026, 7, 24)
        self.calendar = _weekday_calendar(self._latest, sessions)
        self.manifest_sha256 = "b" * 64

    def latest_date(self):
        return self._latest

    def snapshot_metadata(self):
        return {
            "provider": "test-cache",
            "latest_common_session": self._latest.isoformat(),
            "latest_benchmark_session": self._latest.isoformat(),
            "manifest": {"snapshot_id": "nested-test-snapshot"},
            "universe": {"security_master_sha256": "c" * 64},
            "symbols": {
                "510300": {"last": self._latest.isoformat(), "sha256": "d" * 64}
            },
        }


class _Backtest:
    """Sharpe rewards lookback_days above the 126 baseline on every window."""

    def __init__(self, config, market, strategy_settings=None):
        self.settings = strategy_settings or config.strategy

    def run(self, start=None, end=None, initial_cash=None):
        if self.settings.lookback_days > 126:
            sharpe = 0.9
        elif self.settings.lookback_days == 126:
            sharpe = 0.5
        else:
            sharpe = 0.2
        return SimpleNamespace(
            metadata={"start": str(start), "end": str(end)},
            metrics={
                "total_return": 0.25,
                "cagr": 0.05,
                "sharpe": sharpe,
                "max_drawdown": -0.05,
                "turnover": 1.2,
                "transaction_costs": 100.0,
            },
        )


class _MappedBacktest:
    """Reads sharpe from a class-level (start, end, is_variant) map."""

    scores: dict[tuple[str, str, bool], float] = {}

    def __init__(self, config, market, strategy_settings=None):
        self.settings = strategy_settings or config.strategy

    def run(self, start=None, end=None, initial_cash=None):
        key = (str(start), str(end), self.settings.lookback_days != 126)
        sharpe = type(self).scores[key]
        return SimpleNamespace(
            metadata={"start": str(start), "end": str(end)},
            metrics={
                "total_return": 0.25,
                "cagr": 0.05,
                "sharpe": sharpe,
                "max_drawdown": -0.05,
                "turnover": 1.2,
                "transaction_costs": 100.0,
            },
        )


class FoldLayoutTests(TestCase):
    def test_layout_partitions_anchored_folds_with_embargo(self):
        layout = _fold_layout(400, 3, 2, 5)
        self.assertEqual(len(layout), 3)
        first = layout[0]
        self.assertEqual(first["test"], (100, 199))
        self.assertEqual(first["train"], (0, 94))
        self.assertEqual(first["validation"], [(55, 74), (75, 94)])
        second = layout[1]
        self.assertEqual(second["test"], (200, 299))
        self.assertEqual(second["train"], (0, 194))
        tail = max(40, int(195 * 0.3))
        self.assertEqual(
            second["validation"][0][0], 194 - tail + 1
        )
        self.assertEqual(second["validation"][-1][1], 194)
        third = layout[2]
        self.assertEqual(third["test"], (300, 399))
        for fold in layout:
            self.assertEqual(fold["train"][1], fold["test"][0] - 6)

    def test_layout_distributes_remainder_to_early_blocks(self):
        layout = _fold_layout(403, 3, 1, 0)
        self.assertEqual(layout[0]["test"], (101, 201))
        self.assertEqual(layout[1]["test"], (202, 302))
        self.assertEqual(layout[2]["test"], (303, 402))
        self.assertEqual(layout[0]["train"], (0, 100))

    def test_layout_fails_closed_when_regions_are_too_short(self):
        with self.assertRaisesRegex(ValueError, "per test fold"):
            _fold_layout(100, 3, 1, 0)
        with self.assertRaisesRegex(ValueError, "training region"):
            _fold_layout(140, 3, 1, 10)


class NestedWalkForwardTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.config = load_config(REPOSITORY_ROOT / "config" / "default.json")
        self.strategy_lab = StrategyLabEngine(
            self.config, StrategyLabStore(root / "strategy_lab")
        )
        self.engine = NestedWalkForwardEngine(self.config, self.strategy_lab)
        self.engine.root = root / "hypothesis_lab"
        self.market = _Market()

    def _execute(self, backtest=_Backtest, **kwargs):
        kwargs.setdefault("parameters", ["strategy.lookback_days"])
        kwargs.setdefault("outer_folds", 3)
        with patch("ai_trade.hypothesis_lab.nested.BacktestEngine", backtest):
            return self.engine.execute("alice", self.market, **kwargs)

    def test_selects_winner_inside_training_and_measures_out_of_fold(self):
        record = self._execute()

        self.assertFalse(record["reused"])
        self.assertEqual(record["parameters_swept"], ["strategy.lookback_days"])
        self.assertEqual(record["candidate_count"], 3)
        self.assertEqual(record["selection_rule"], "one_standard_error")
        self.assertEqual(len(record["folds"]), 3)
        for fold in record["folds"]:
            selected = fold["selected"]
            self.assertFalse(selected["is_baseline"])
            self.assertEqual(selected["parameter"], "strategy.lookback_days")
            self.assertGreater(selected["value"], 126)
            self.assertAlmostEqual(selected["mean_validation_score"], 0.9)
            self.assertAlmostEqual(fold["baseline_mean_validation_score"], 0.5)
            self.assertAlmostEqual(fold["objective_delta"], 0.4)
            self.assertLess(fold["train_end"], fold["test_start"])
            self.assertEqual(
                fold["validation_windows"][-1]["end"], fold["train_end"]
            )
        aggregate = record["aggregate"]
        self.assertEqual(aggregate["non_baseline_selections"], 3)
        self.assertEqual(aggregate["positive_delta_folds"], 3)
        self.assertAlmostEqual(aggregate["mean_objective_delta"], 0.4)
        self.assertEqual(aggregate["selection_regret_share"], 0.0)
        self.assertEqual(len(aggregate["selection_counts"]), 1)
        self.assertEqual(aggregate["selection_counts"][0]["folds_selected"], 3)
        self.assertIn("out-of-fold", record["disclosure"])
        self.assertEqual(
            record["safety"],
            {
                "research_only": True,
                "selection_inside_training_only": True,
                "may_register_hypothesis": False,
                "may_create_candidate": False,
                "may_approve": False,
                "may_activate": False,
                "may_trade": False,
            },
        )
        self.assertEqual(
            self.engine.list("bob")["nested_walk_forwards"], []
        )

    def test_marginal_winner_falls_back_to_baseline_by_one_standard_error(self):
        layout = _fold_layout(len(self.market.calendar), 3, 2, 5)
        scores: dict[tuple[str, str, bool], float] = {}
        calendar = self.market.calendar
        for fold in layout:
            windows = [
                (
                    calendar[window[0]].isoformat(),
                    calendar[window[1]].isoformat(),
                )
                for window in fold["validation"]
            ]
            test_window = (
                calendar[fold["test"][0]].isoformat(),
                calendar[fold["test"][1]].isoformat(),
            )
            for window, variant_score in zip(windows, (0.71, 0.31)):
                scores[(window[0], window[1], True)] = variant_score
                scores[(window[0], window[1], False)] = 0.5
            scores[(test_window[0], test_window[1], True)] = 0.9
            scores[(test_window[0], test_window[1], False)] = 0.5
        _MappedBacktest.scores = scores

        record = self._execute(backtest=_MappedBacktest, inner_folds=2)

        for fold in record["folds"]:
            self.assertTrue(fold["selected"]["is_baseline"])
            self.assertIsNone(fold["selected"]["parameter"])
            self.assertEqual(
                fold["test_metrics"], fold["baseline_test_metrics"]
            )
            self.assertEqual(fold["objective_delta"], 0.0)
        self.assertEqual(record["aggregate"]["non_baseline_selections"], 0)
        self.assertEqual(record["aggregate"]["selection_regret_share"], 0.0)
        self.assertEqual(record["aggregate"]["selection_counts"], [])

    def test_repeat_run_is_idempotent_and_objective_forks(self):
        first = self._execute()
        second = self._execute()
        third = self._execute(objective="turnover")
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["nested_id"], second["nested_id"])
        self.assertNotEqual(first["nested_id"], third["nested_id"])
        self.assertEqual(self.engine.list("alice")["summary"]["total"], 2)

    def test_budget_and_inputs_fail_closed(self):
        with patch(
            "ai_trade.hypothesis_lab.nested.MAX_TOTAL_BACKTESTS", 5
        ):
            with self.assertRaisesRegex(ValueError, "bound is 5"):
                self._execute()
        with self.assertRaisesRegex(ValueError, "not sweepable"):
            self._execute(parameters=["strategy.weighting_method"])
        with self.assertRaisesRegex(ValueError, "objective"):
            self._execute(objective="calmar")
        with self.assertRaisesRegex(ValueError, "points"):
            self._execute(points=1)
        with self.assertRaisesRegex(ValueError, "outer_folds"):
            self._execute(outer_folds=1)
        with self.assertRaisesRegex(ValueError, "inner_folds"):
            self._execute(inner_folds=9)
        with self.assertRaisesRegex(ValueError, "embargo_sessions"):
            self._execute(embargo_sessions=99)
        self.assertEqual(self.engine.list("alice")["summary"]["total"], 0)

    def test_snapshot_change_mid_run_fails_closed(self):
        market = self.market
        flips = {"count": 0}
        original = market.snapshot_metadata

        def unstable():
            flips["count"] += 1
            value = original()
            if flips["count"] > 1:
                value["symbols"]["510300"]["sha256"] = "e" * 64
            return value

        market.snapshot_metadata = unstable
        with self.assertRaisesRegex(RuntimeError, "snapshot changed"):
            self._execute()

    def test_tampered_record_is_rejected_on_read(self):
        record = self._execute()
        path = (
            self.engine.owner_directory("alice")
            / "nested"
            / f"{record['nested_id']}.json"
        )
        value = json.loads(path.read_text(encoding="utf-8"))
        value["aggregate"]["mean_objective_delta"] = 99.0
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(
            RuntimeError, "Invalid nested walk-forward record"
        ):
            self.engine.get("alice", record["nested_id"])

    def test_max_total_backtests_default_accommodates_full_grid(self):
        self.assertGreaterEqual(MAX_TOTAL_BACKTESTS, 4 * (45 * 2 + 2))


class NestedWalkForwardCliTests(TestCase):
    def test_parser_exposes_nested_walk_forward_commands(self):
        parsed = build_parser().parse_args(
            [
                "nested-walk-forward",
                "--objective",
                "max_drawdown",
                "--parameters",
                "strategy.lookback_days",
                "--points",
                "3",
                "--outer-folds",
                "5",
                "--inner-folds",
                "3",
                "--embargo",
                "8",
            ]
        )
        self.assertEqual(parsed.objective, "max_drawdown")
        self.assertEqual(parsed.points, 3)
        self.assertEqual(parsed.outer_folds, 5)
        self.assertEqual(parsed.inner_folds, 3)
        self.assertEqual(parsed.embargo, 8)
        listed = build_parser().parse_args(
            ["nested-walk-forwards", "--limit", "7"]
        )
        self.assertEqual(listed.limit, 7)
        shown = build_parser().parse_args(
            ["nested-walk-forward-show", "nwf_" + "a" * 32]
        )
        self.assertEqual(shown.nested_id, "nwf_" + "a" * 32)

    def test_nested_walk_forward_reads_cache_without_refreshing_provider(self):
        config = object()
        market = object()
        engine = MagicMock()
        engine.execute.return_value = {
            "nested_id": "nwf_" + "a" * 32,
            "reused": False,
        }
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.MarketData", return_value=market) as market_data,
            patch("ai_trade.cli.NestedWalkForwardEngine", return_value=engine),
            patch("ai_trade.cli._ensure_cache") as ensure_cache,
            redirect_stdout(output),
        ):
            status = main(
                [
                    "nested-walk-forward",
                    "--parameters",
                    "strategy.lookback_days,strategy.top_n",
                ]
            )
        self.assertEqual(status, 0)
        market_data.assert_called_once_with(config, recover_snapshot=False)
        ensure_cache.assert_not_called()
        engine.execute.assert_called_once_with(
            "local-owner",
            market,
            objective="sharpe",
            parameters=["strategy.lookback_days", "strategy.top_n"],
            points=2,
            outer_folds=4,
            inner_folds=2,
            embargo_sessions=5,
        )
        self.assertEqual(
            json.loads(output.getvalue())["nested_id"], "nwf_" + "a" * 32
        )

    def test_show_dispatch_verifies_record(self):
        config = object()
        engine = MagicMock()
        engine.get.return_value = {"nested_id": "nwf_" + "b" * 32}
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.NestedWalkForwardEngine", return_value=engine),
            redirect_stdout(output),
        ):
            status = main(["nested-walk-forward-show", "nwf_" + "b" * 32])
        self.assertEqual(status, 0)
        engine.get.assert_called_once_with("local-owner", "nwf_" + "b" * 32)


if __name__ == "__main__":
    import unittest

    unittest.main()
