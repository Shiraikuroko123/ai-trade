from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import MagicMock, patch

from ai_trade.cli import build_parser, main
from ai_trade.config import load_config
from ai_trade.model_lab import ModelLabEngine, ModelLabStore, model_registry
from ai_trade.model_lab.engine import (
    MINIMUM_EVALUATED_DATES,
    MINIMUM_TRAIN_DATES,
    _fit_ridge,
    _solve,
)
from ai_trade.model_lab.store import ModelLabCapacityError
from ai_trade.models import Bar


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
    """Monotone geometric market: momentum ranks predict returns exactly."""

    def __init__(
        self,
        latest: date = date(2026, 7, 24),
        sessions: int = 520,
    ) -> None:
        self.calendar = _weekday_calendar(latest, sessions)
        self.rates = {
            "510300": 0.0002,
            "510500": 0.0006,
            "159915": 0.0010,
            "513100": 0.0014,
            "518880": 0.0018,
            "511010": 0.0022,
        }
        self._bars: dict[str, dict[date, Bar]] = {}
        for symbol, rate in self.rates.items():
            price = 1.0
            bars: dict[date, Bar] = {}
            for index, on_date in enumerate(self.calendar):
                price *= 1.0 + rate
                bars[on_date] = Bar(
                    date=on_date,
                    open=price,
                    close=price,
                    high=price,
                    low=price,
                    volume=1_000_000.0,
                    amount=100_000_000.0 * (1.0 + rate * index),
                )
            self._bars[symbol] = bars

    def active_symbols(self, on_date: date) -> tuple[str, ...]:
        return tuple(sorted(self.rates))

    def bar(self, symbol: str, on_date: date) -> Bar | None:
        return self._bars[symbol].get(on_date)

    def history(self, symbol: str, on_date: date, count: int) -> list[Bar]:
        dates = [value for value in self.calendar if value <= on_date]
        return [self._bars[symbol][value] for value in dates[-count:]]

    def latest_date(self) -> date:
        return self.calendar[-1]

    def snapshot_metadata(self) -> dict[str, object]:
        return {
            "provider": "test-cache",
            "latest_common_session": self.calendar[-1].isoformat(),
            "latest_benchmark_session": self.calendar[-1].isoformat(),
            "manifest": {"snapshot_id": f"model-{self.calendar[-1].isoformat()}"},
            "symbols": {
                symbol: {"last": self.calendar[-1].isoformat(), "sha256": "d" * 64}
                for symbol in sorted(self.rates)
            },
        }


class ModelMathTests(TestCase):
    def test_solver_inverts_a_known_system(self):
        matrix = [[2.0, 1.0], [1.0, 3.0]]
        vector = [5.0, 10.0]
        solution = _solve(matrix, vector)
        self.assertAlmostEqual(solution[0], 1.0)
        self.assertAlmostEqual(solution[1], 3.0)

    def test_ridge_recovers_a_linear_relationship(self):
        rows = []
        for index in range(200):
            x1 = float(index % 10)
            x2 = float((index * 7) % 13)
            rows.append(([x1, x2], 2.0 * x1 - 1.0 * x2))
        means, stds = (
            [sum(r[0][0] for r in rows) / len(rows), sum(r[0][1] for r in rows) / len(rows)],
            None,
        )
        from ai_trade.model_lab.engine import _feature_stats

        means, stds = _feature_stats(rows, 2)
        weights = _fit_ridge(rows, means, stds, 0.001)
        self.assertGreater(weights[0], 0)
        self.assertLess(weights[1], 0)
        self.assertAlmostEqual(
            weights[0] / abs(weights[1]),
            (2.0 * stds[0]) / (1.0 * stds[1]),
            places=2,
        )

    def test_registry_exposes_fixed_hyperparameters(self):
        registry = model_registry()
        identifiers = [item["model_id"] for item in registry["models"]]
        self.assertEqual(
            identifiers, ["ridge_v1", "factor_mean_v1", "gbdt_v1"]
        )
        ridge = registry["models"][0]
        self.assertEqual(ridge["hyperparameters"], {"lambda": 0.1})
        self.assertTrue(registry["safety"]["research_only"])


class ModelLabEngineTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.config = load_config(REPOSITORY_ROOT / "config" / "default.json")
        self.store = ModelLabStore(root / "model_lab")
        self.engine = ModelLabEngine(self.config, self.store)
        self.market = _Market()
        self.factors = ["momentum_60_5", "momentum_120_5"]

    def test_walk_forward_ridge_matches_the_perfect_factor(self):
        record = self.engine.evaluate(
            "alice",
            self.market,
            "ridge_v1",
            factor_ids=self.factors,
            horizon=10,
            step=5,
        )

        self.assertFalse(record["reused"])
        result = record["results"]["model"]
        self.assertGreaterEqual(result["dates"], MINIMUM_EVALUATED_DATES)
        self.assertAlmostEqual(result["mean_ic"], 1.0)
        self.assertAlmostEqual(result["positive_share"], 1.0)
        self.assertGreater(result["mean_spread"], 0.0)
        baselines = {
            item["factor_id"]: item for item in record["results"]["factor_baselines"]
        }
        self.assertAlmostEqual(baselines["momentum_60_5"]["mean_ic"], 1.0)
        self.assertAlmostEqual(
            record["results"]["best_factor_direction_adjusted_mean_ic"], 1.0
        )
        self.assertAlmostEqual(
            record["results"]["model_minus_best_factor_ic"], 0.0
        )
        coefficients = {
            item["factor_id"]: item for item in record["coefficients"]
        }
        self.assertGreater(
            coefficients["momentum_60_5"]["mean_abs"]
            + coefficients["momentum_120_5"]["mean_abs"],
            0.0,
        )
        coverage = record["coverage"]
        self.assertEqual(
            coverage["evaluated_dates"]
            + coverage["warmup_dates"]
            + coverage["skipped_dates"],
            coverage["sampled_dates"],
        )
        self.assertGreaterEqual(coverage["warmup_dates"], MINIMUM_TRAIN_DATES)
        self.assertEqual(
            record["safety"],
            {
                "research_only": True,
                "creates_no_signal": True,
                "may_create_candidate": False,
                "may_approve": False,
                "may_activate": False,
                "may_trade": False,
            },
        )
        self.assertEqual(self.store.list("bob")["evaluations"], [])

    def test_factor_mean_baseline_uses_registered_directions(self):
        record = self.engine.evaluate(
            "alice",
            self.market,
            "factor_mean_v1",
            factor_ids=self.factors,
            horizon=10,
            step=5,
        )
        self.assertAlmostEqual(record["results"]["model"]["mean_ic"], 1.0)
        coefficients = {
            item["factor_id"]: item["final"] for item in record["coefficients"]
        }
        self.assertAlmostEqual(coefficients["momentum_60_5"], 0.5)
        self.assertAlmostEqual(coefficients["momentum_120_5"], 0.5)

    def test_warmup_counts_match_the_leakage_guard(self):
        record = self.engine.evaluate(
            "alice",
            self.market,
            "ridge_v1",
            factor_ids=self.factors,
            horizon=10,
            step=5,
        )
        # At sampled position p (step=5, horizon=10) an earlier observation
        # qualifies for training when its forward window has completed, i.e.
        # 5*(p-q) >= 10, giving train_dates = p - 1. The first evaluated
        # position therefore satisfies p - 1 >= MINIMUM_TRAIN_DATES, and the
        # deterministic warm-up count is MINIMUM_TRAIN_DATES + horizon/step - 1.
        self.assertEqual(
            record["coverage"]["warmup_dates"],
            MINIMUM_TRAIN_DATES + 10 // 5 - 1,
        )
        self.assertGreater(record["coverage"]["final_train_observations"], 0)

    def test_repeated_evaluation_is_idempotent_and_parameters_fork(self):
        first = self.engine.evaluate(
            "alice", self.market, "ridge_v1", factor_ids=self.factors, horizon=10
        )
        second = self.engine.evaluate(
            "alice", self.market, "ridge_v1", factor_ids=self.factors, horizon=10
        )
        third = self.engine.evaluate(
            "alice", self.market, "ridge_v1", factor_ids=self.factors, horizon=15
        )
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["evaluation_id"], second["evaluation_id"])
        self.assertNotEqual(first["evaluation_id"], third["evaluation_id"])
        self.assertEqual(self.store.list("alice")["summary"]["total"], 2)

    def test_unknown_model_or_factor_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "Unknown model"):
            self.engine.evaluate("alice", self.market, "nope")
        with self.assertRaisesRegex(ValueError, "Unknown factor"):
            self.engine.evaluate(
                "alice", self.market, "ridge_v1", factor_ids=["nope"]
            )
        with self.assertRaisesRegex(ValueError, "unique"):
            self.engine.evaluate(
                "alice",
                self.market,
                "ridge_v1",
                factor_ids=["momentum_60_5", "momentum_60_5"],
            )
        self.assertEqual(self.store.list("alice")["summary"]["total"], 0)

    def test_short_history_fails_closed(self):
        short = _Market(sessions=120)
        with self.assertRaisesRegex(RuntimeError, "too short"):
            self.engine.evaluate(
                "alice", short, "ridge_v1", factor_ids=["momentum_120_5"], horizon=10
            )

    def test_tampered_record_is_rejected_on_read(self):
        record = self.engine.evaluate(
            "alice", self.market, "ridge_v1", factor_ids=self.factors, horizon=10
        )
        path = (
            self.store.owner_directory("alice")
            / "evaluations"
            / f"{record['evaluation_id']}.json"
        )
        value = json.loads(path.read_text(encoding="utf-8"))
        value["results"]["model"]["mean_ic"] = 0.5
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(
            RuntimeError, "Invalid model evaluation record"
        ):
            self.store.get("alice", record["evaluation_id"])

    def test_per_model_capacity_is_enforced(self):
        self.engine.evaluate(
            "alice", self.market, "ridge_v1", factor_ids=self.factors, horizon=10
        )
        with patch("ai_trade.model_lab.store.MAX_EVALUATIONS_PER_MODEL", 1):
            with self.assertRaisesRegex(
                ModelLabCapacityError, "capacity reached for this model"
            ):
                self.engine.evaluate(
                    "alice",
                    self.market,
                    "ridge_v1",
                    factor_ids=self.factors,
                    horizon=15,
                )


class ModelCliTests(TestCase):
    def test_parser_exposes_model_commands(self):
        listed = build_parser().parse_args(["model-list"])
        self.assertEqual(listed.command, "model-list")
        evaluated = build_parser().parse_args(
            [
                "model-evaluate",
                "--model",
                "ridge_v1",
                "--factors",
                "momentum_60_5,volatility_60",
                "--horizon",
                "10",
                "--step",
                "3",
            ]
        )
        self.assertEqual(evaluated.model, "ridge_v1")
        self.assertEqual(evaluated.factors, "momentum_60_5,volatility_60")
        self.assertEqual(evaluated.horizon, 10)
        self.assertEqual(evaluated.step, 3)
        shown = build_parser().parse_args(["model-show", "mdl_" + "a" * 32])
        self.assertEqual(shown.evaluation_id, "mdl_" + "a" * 32)

    def test_evaluate_reads_existing_cache_without_refreshing_provider(self):
        config = object()
        market = object()
        engine = MagicMock()
        engine.evaluate.return_value = {
            "evaluation_id": "mdl_" + "a" * 32,
            "reused": False,
        }
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.MarketData", return_value=market) as market_data,
            patch("ai_trade.cli.ModelLabEngine", return_value=engine),
            patch("ai_trade.cli._ensure_cache") as ensure_cache,
            redirect_stdout(output),
        ):
            status = main(
                [
                    "model-evaluate",
                    "--factors",
                    "momentum_60_5, momentum_120_5",
                    "--horizon",
                    "10",
                ]
            )

        self.assertEqual(status, 0)
        market_data.assert_called_once_with(config, recover_snapshot=False)
        ensure_cache.assert_not_called()
        engine.evaluate.assert_called_once_with(
            "local-owner",
            market,
            "ridge_v1",
            factor_ids=["momentum_60_5", "momentum_120_5"],
            horizon=10,
            step=5,
        )
        self.assertEqual(
            json.loads(output.getvalue())["evaluation_id"], "mdl_" + "a" * 32
        )

    def test_listing_and_show_do_not_open_market_data(self):
        config = object()
        engine = MagicMock()
        engine.registry.return_value = {"models": []}
        engine.list.return_value = {"evaluations": [], "summary": {"total": 0}}
        engine.get.return_value = {"evaluation_id": "mdl_" + "b" * 32}
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.ModelLabEngine", return_value=engine),
            patch("ai_trade.cli.MarketData") as market_data,
            redirect_stdout(io.StringIO()),
        ):
            self.assertEqual(main(["model-list"]), 0)
            self.assertEqual(
                main(["model-evaluations", "--model", "ridge_v1", "--limit", "9"]),
                0,
            )
            self.assertEqual(main(["model-show", "mdl_" + "b" * 32]), 0)

        market_data.assert_not_called()
        engine.list.assert_called_once_with(
            "local-owner", limit=9, model_id="ridge_v1"
        )
        engine.get.assert_called_once_with("local-owner", "mdl_" + "b" * 32)


if __name__ == "__main__":
    import unittest

    unittest.main()


class GbdtTests(TestCase):
    def test_gbdt_learns_a_nonlinear_step_that_a_line_cannot(self):
        from ai_trade.model_lab.gbdt import fit_gbdt

        features = []
        targets = []
        for index in range(400):
            x0 = (index % 40) / 20.0 - 1.0
            x1 = ((index * 7) % 40) / 20.0 - 1.0
            features.append([x0, x1])
            targets.append(1.0 if abs(x0) > 0.5 else -1.0)
        predict, importance = fit_gbdt(
            features,
            targets,
            trees=30,
            depth=2,
            learning_rate=0.3,
            min_leaf=10,
            split_candidates=8,
        )
        correct = sum(
            (predict(row) > 0) == (target > 0)
            for row, target in zip(features, targets)
        )
        self.assertGreater(correct / len(targets), 0.95)
        self.assertAlmostEqual(sum(importance), 1.0, places=6)
        self.assertGreater(importance[0], importance[1])

    def test_gbdt_model_evaluates_walk_forward_with_importance_disclosure(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        config = load_config(REPOSITORY_ROOT / "config" / "default.json")
        store = ModelLabStore(Path(temporary.name) / "model_lab")
        engine = ModelLabEngine(config, store)
        market = _Market()

        record = engine.evaluate(
            "alice",
            market,
            "gbdt_v1",
            factor_ids=["momentum_60_5", "momentum_120_5"],
            horizon=10,
            step=5,
        )

        self.assertEqual(record["model"]["kind"], "gbdt")
        self.assertEqual(record["model"]["hyperparameters"]["refit_interval"], 8)
        result = record["results"]["model"]
        self.assertGreaterEqual(result["dates"], 24)
        self.assertGreater(result["mean_ic"], 0.9)
        importance = {
            item["factor_id"]: item for item in record["coefficients"]
        }
        total_final = sum(item["final"] for item in importance.values())
        self.assertAlmostEqual(total_final, 1.0, places=6)
        for item in importance.values():
            self.assertGreaterEqual(item["mean_abs"], 0.0)

    def test_registry_exposes_gbdt_with_fixed_hyperparameters(self):
        registry = model_registry()
        identifiers = [item["model_id"] for item in registry["models"]]
        self.assertEqual(
            identifiers, ["ridge_v1", "factor_mean_v1", "gbdt_v1"]
        )
        gbdt = registry["models"][2]
        self.assertEqual(gbdt["hyperparameters"]["trees"], 24)
        self.assertEqual(gbdt["hyperparameters"]["max_train_rows"], 2000)
