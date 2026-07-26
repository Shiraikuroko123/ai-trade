from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch

from ai_trade.config import load_config
from ai_trade.hypothesis_lab import (
    HypothesisExperimentRunner,
    HypothesisLabEngine,
    HypothesisLabStore,
)
from ai_trade.hypothesis_lab.store import HypothesisLabCapacityError
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
    def __init__(self, latest: date, sessions: int = 60) -> None:
        self._latest = latest
        self.calendar = _weekday_calendar(latest, sessions)
        self.manifest_sha256 = "b" * 64

    def latest_date(self) -> date:
        return self._latest

    def snapshot_metadata(self) -> dict[str, object]:
        return {
            "provider": "test-cache",
            "latest_common_session": self._latest.isoformat(),
            "latest_benchmark_session": self._latest.isoformat(),
            "manifest": {"snapshot_id": f"snapshot-{self._latest.isoformat()}"},
            "universe": {"security_master_sha256": "c" * 64},
            "symbols": {
                "510300": {
                    "last": self._latest.isoformat(),
                    "sha256": "d" * 64,
                }
            },
        }


class _RegistrationBacktest:
    """Baseline-profile fake used only while registering the hypothesis."""

    def __init__(self, config, market, strategy_settings=None):
        self.config = config

    def run(self, start=None, end=None, initial_cash=None):
        return SimpleNamespace(
            metadata={"start": "2026-05-04", "end": "2026-07-24"},
            metrics={
                "total_return": 0.25,
                "cagr": 0.05,
                "sharpe": 0.8,
                "max_drawdown": -0.05,
                "turnover": 1.2,
                "transaction_costs": 123.45,
            },
        )


def _execution_backtest(
    baseline_strategy, default_commission_bps: float, candidate_sharpe: float
):
    class _ExecutionBacktest:
        def __init__(self, config, market, strategy_settings=None):
            self.config = config
            self.settings = strategy_settings or config.strategy

        def run(self, start=None, end=None, initial_cash=None):
            is_baseline = self.settings == baseline_strategy
            stressed = self.config.costs.commission_bps > default_commission_bps
            if is_baseline:
                total_return = 0.20 if stressed else 0.25
                sharpe = 0.8
                drawdown = -0.05
                turnover = 1.2
            else:
                total_return = 0.18 if stressed else 0.30
                sharpe = candidate_sharpe
                drawdown = -0.04
                turnover = 1.0
            return SimpleNamespace(
                metadata={},
                metrics={
                    "total_return": total_return,
                    "cagr": 0.05,
                    "sharpe": sharpe,
                    "max_drawdown": drawdown,
                    "turnover": turnover,
                    "transaction_costs": 100.0,
                },
            )

    return _ExecutionBacktest


class HypothesisRunnerTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.config = load_config(REPOSITORY_ROOT / "config" / "default.json")
        self.store = HypothesisLabStore(root / "hypothesis_lab")
        self.strategy_store = StrategyLabStore(root / "strategy_lab")
        self.strategy_lab = StrategyLabEngine(self.config, self.strategy_store)
        self.engine = HypothesisLabEngine(
            self.config, self.store, self.strategy_lab
        )
        self.runner = HypothesisExperimentRunner(
            self.config, self.store, self.strategy_lab
        )
        self.market = _Market(date(2026, 7, 24))

    def _register(self, objective: str = "balanced") -> dict[str, object]:
        with patch(
            "ai_trade.hypothesis_lab.engine.BacktestEngine", _RegistrationBacktest
        ):
            return self.engine.generate_local(
                "alice", self.market, objective=objective
            )

    def _execute(self, market, candidate_sharpe: float = 0.9):
        fake = _execution_backtest(
            self.config.strategy,
            self.config.costs.commission_bps,
            candidate_sharpe,
        )
        with patch("ai_trade.hypothesis_lab.runner.BacktestEngine", fake):
            return self.runner.execute("alice", self._hypothesis_id, market)

    def _register_and_keep_id(self, objective: str = "balanced") -> None:
        record = self._register(objective)
        self._hypothesis_id = record["hypothesis_id"]

    def test_same_snapshot_run_supports_every_preregistered_prediction(self):
        self._register_and_keep_id()
        run = self._execute(self.market)

        self.assertFalse(run["reused"])
        self.assertEqual(run["mode"], "same_snapshot")
        self.assertEqual(run["verdict"]["status"], "SUPPORTED")
        self.assertEqual(run["verdict"]["predictions_total"], 4)
        self.assertEqual(run["verdict"]["predictions_supported"], 4)
        self.assertEqual(run["verdict"]["falsified_criteria"], [])
        self.assertEqual(
            run["registered_snapshot"]["fingerprint"],
            run["executed_snapshot"]["fingerprint"],
        )
        self.assertEqual(
            run["executed_snapshot"]["sessions_after_registration"], 0
        )
        self.assertEqual(run["period"]["sessions"], 60)
        self.assertEqual(run["period"]["holdout_sessions"], 20)
        self.assertEqual(run["results"]["rolling"]["fold_count"], 3)
        self.assertEqual(run["results"]["rolling"]["consistent_folds"], 3)
        self.assertEqual(len(run["results"]["cost_stress"]), 1)
        self.assertEqual(run["results"]["cost_stress"][0]["multiplier"], 2.0)
        self.assertEqual(run["multiple_testing"]["family_position"], 1)
        self.assertEqual(
            run["safety"],
            {
                "research_only": True,
                "verdict_grants_no_authority": True,
                "may_create_candidate": False,
                "may_approve": False,
                "may_activate": False,
                "may_trade": False,
                "may_change_broker_configuration": False,
                "may_weaken_validation_gates": False,
            },
        )
        judged = {item["metric"]: item["observed"] for item in run["judgments"]}
        self.assertAlmostEqual(judged["full.sharpe_delta"], 0.1)
        self.assertAlmostEqual(judged["holdout.sharpe_delta"], 0.1)
        self.assertAlmostEqual(judged["cost_stress.total_return_delta"], -0.02)
        self.assertAlmostEqual(judged["stability.minimum_sharpe_delta"], 0.0)
        self.assertEqual(self.strategy_store.list_candidates("alice"), [])
        self.assertEqual(self.store.list_runs("bob")["runs"], [])

    def test_repeated_execution_is_idempotent(self):
        self._register_and_keep_id()
        first = self._execute(self.market)
        second = self._execute(self.market)

        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["run_id"], second["run_id"])
        listing = self.store.list_runs("alice")
        self.assertEqual(listing["summary"]["total"], 1)
        self.assertEqual(
            listing["runs"][0]["verdict"]["status"], "SUPPORTED"
        )

    def test_falsified_objective_prediction_is_recorded_not_hidden(self):
        self._register_and_keep_id()
        run = self._execute(self.market, candidate_sharpe=0.6)

        self.assertEqual(run["verdict"]["status"], "FALSIFIED")
        self.assertEqual(run["verdict"]["predictions_supported"], 3)
        self.assertEqual(run["verdict"]["falsified_criteria"], ["fals_01"])
        outcomes = {
            item["prediction_id"]: item["outcome"] for item in run["judgments"]
        }
        self.assertEqual(outcomes["pred_01"], "FALSIFIED")
        self.assertEqual(outcomes["pred_02"], "SUPPORTED")

    def test_later_snapshot_runs_as_independent_replication(self):
        self._register_and_keep_id()
        later = _Market(date(2026, 7, 31), sessions=65)
        run = self._execute(later)

        self.assertEqual(run["mode"], "independent_replication")
        self.assertEqual(run["verdict"]["status"], "REPLICATED")
        self.assertNotEqual(
            run["executed_snapshot"]["fingerprint"],
            run["registered_snapshot"]["fingerprint"],
        )
        self.assertEqual(run["executed_snapshot"]["as_of"], "2026-07-31")
        self.assertEqual(
            run["executed_snapshot"]["sessions_after_registration"], 5
        )

    def test_older_or_inconsistent_snapshot_fails_closed(self):
        self._register_and_keep_id()
        older = _Market(date(2026, 7, 20), sessions=60)
        with self.assertRaisesRegex(RuntimeError, "not newer"):
            self._execute(older)
        self.assertEqual(self.store.list_runs("alice")["summary"]["total"], 0)

    def test_configuration_context_drift_fails_closed(self):
        self._register_and_keep_id()
        with patch.object(
            self.strategy_lab,
            "config_context_fingerprint",
            return_value="f" * 64,
        ):
            with self.assertRaisesRegex(RuntimeError, "context changed"):
                self._execute(self.market)

    def test_tampered_run_record_is_rejected_on_read(self):
        self._register_and_keep_id()
        run = self._execute(self.market)
        path = (
            self.store.owner_directory("alice")
            / "runs"
            / f"{run['run_id']}.json"
        )
        value = json.loads(path.read_text(encoding="utf-8"))
        value["verdict"]["status"] = "FALSIFIED"
        path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "Invalid hypothesis run record"):
            self.store.get_run("alice", run["run_id"])

    def test_per_hypothesis_run_capacity_is_enforced(self):
        self._register_and_keep_id()
        self._execute(self.market)
        later = _Market(date(2026, 7, 31), sessions=65)
        with patch("ai_trade.hypothesis_lab.store.MAX_RUNS_PER_HYPOTHESIS", 1):
            with self.assertRaisesRegex(
                HypothesisLabCapacityError, "capacity reached for this hypothesis"
            ):
                self._execute(later)

    def test_turnover_objective_uses_ratio_and_fold_direction(self):
        with patch(
            "ai_trade.hypothesis_lab.engine.BacktestEngine",
            _RegistrationBacktest,
        ):
            self.engine.generate_local(
                "alice", self.market, objective="balanced"
            )
            record = self.engine.generate_local(
                "alice", self.market, objective="turnover"
            )
        self._hypothesis_id = record["hypothesis_id"]
        run = self._execute(self.market)

        judged = {item["metric"]: item for item in run["judgments"]}
        ratio = judged["full.turnover_ratio"]
        self.assertAlmostEqual(ratio["observed"], 1.0 / 1.2)
        self.assertEqual(ratio["outcome"], "SUPPORTED")
        self.assertIn("turnover", run["results"]["rolling"]["direction_rule"])
        self.assertEqual(run["results"]["rolling"]["consistent_folds"], 3)
        self.assertEqual(run["multiple_testing"]["family_position"], 2)

    def test_run_is_bound_to_the_stored_hypothesis_record(self):
        self._register_and_keep_id()
        run = self._execute(self.market)
        stored = self.store.get(" alice ", self._hypothesis_id)
        self.assertEqual(
            run["hypothesis_record_fingerprint"], stored["record_fingerprint"]
        )
        self.assertEqual(
            run["hypothesis_design_fingerprint"], stored["design_fingerprint"]
        )
        self.assertEqual(
            run["candidate_settings_fingerprint"],
            stored["experiment_plan"]["candidate_settings_fingerprint"],
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
