from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from uuid import uuid4

from ai_trade.config import load_config
from ai_trade.hypothesis_lab import HypothesisLabEngine, HypothesisLabStore
from ai_trade.hypothesis_lab.schema import (
    design_fingerprint,
    finalize_record,
    record_fingerprint,
)
from ai_trade.hypothesis_lab.store import (
    MAX_HYPOTHESIS_RECORD_BYTES,
    HypothesisLabCapacityError,
)
from ai_trade.strategy_lab import StrategyLabEngine, StrategyLabStore


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class _Market:
    def __init__(self) -> None:
        from datetime import date

        self._latest = date(2026, 7, 24)
        self.manifest_sha256 = "b" * 64

    def latest_date(self):
        return self._latest

    def snapshot_metadata(self):
        return {
            "provider": "test-cache",
            "latest_common_session": self._latest.isoformat(),
            "latest_benchmark_session": self._latest.isoformat(),
            "manifest": {"snapshot_id": "immutable-test-snapshot"},
            "universe": {"security_master_sha256": "c" * 64},
            "symbols": {
                "510300": {
                    "last": self._latest.isoformat(),
                    "sha256": "d" * 64,
                }
            },
        }


class _Backtest:
    turnover = 1.2
    max_drawdown = -0.05

    def __init__(self, config, market, strategy_settings=None):
        self.config = config
        self.market = market
        self.strategy_settings = strategy_settings

    def run(self):
        return SimpleNamespace(
            metadata={"start": "2022-01-04", "end": "2026-07-24"},
            metrics={
                "total_return": 0.25,
                "cagr": 0.05,
                "sharpe": 0.8,
                "max_drawdown": self.max_drawdown,
                "turnover": self.turnover,
                "transaction_costs": 123.45,
            },
        )


class _HighTurnoverBacktest(_Backtest):
    turnover = 5.0


class HypothesisLabTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.config = load_config(REPOSITORY_ROOT / "config" / "default.json")
        self.store = HypothesisLabStore(root / "hypothesis_lab")
        self.strategy_store = StrategyLabStore(root / "strategy_lab")
        self.strategy_lab = StrategyLabEngine(self.config, self.strategy_store)
        self.engine = HypothesisLabEngine(
            self.config,
            self.store,
            self.strategy_lab,
        )
        self.market = _Market()

    @patch("ai_trade.hypothesis_lab.engine.BacktestEngine", _Backtest)
    def test_generates_immutable_falsifiable_owner_isolated_record(self):
        record = self.engine.generate_local(
            "Alice",
            self.market,
            objective="drawdown",
            title="Pre-registered drawdown test",
        )

        self.assertFalse(record["reused"])
        self.assertEqual(record["source"]["kind"], "local_deterministic")
        self.assertEqual(record["source"]["objective"], "drawdown")
        self.assertFalse(record["source"]["model_used"])
        self.assertGreaterEqual(len(record["predictions"]), 3)
        self.assertEqual(
            len(record["predictions"]), len(record["falsification_criteria"])
        )
        self.assertEqual(len(record["competing_explanations"]), 3)
        self.assertTrue(record["quality_assessment"]["distinguishable"])
        self.assertEqual(
            record["experiment_plan"]["multiple_testing"]["correction"],
            "holm",
        )
        self.assertEqual(
            record["evidence"]["snapshot"]["fingerprint"],
            record["experiment_plan"]["multiple_testing"]["family_id"].removeprefix(
                "family_"
            )
            + record["evidence"]["snapshot"]["fingerprint"][32:],
        )
        self.assertEqual(
            record["safety"],
            {
                "research_only": True,
                "may_create_candidate": False,
                "may_approve": False,
                "may_activate": False,
                "may_trade": False,
                "may_change_broker_configuration": False,
                "may_weaken_validation_gates": False,
            },
        )
        self.assertEqual(self.strategy_store.list_candidates("Alice"), [])
        self.assertEqual(self.store.list("bob")["hypotheses"], [])

        stored = self.store.get(" alice ", record["hypothesis_id"])
        self.assertNotIn("reused", stored)
        path = (
            self.store.owner_directory("alice")
            / "hypotheses"
            / f"{record['hypothesis_id']}.json"
        )
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            from ai_trade.data.evidence_io import atomic_create_json

            atomic_create_json(
                self.store.root,
                path,
                stored,
                label="hypothesis record",
                maximum_bytes=512 * 1024,
            )
        self.assertEqual(path.read_bytes(), before)

    @patch("ai_trade.hypothesis_lab.engine.BacktestEngine", _Backtest)
    def test_duplicate_design_is_reused_without_spending_the_family_budget(self):
        first = self.engine.generate_local(
            "alice", self.market, objective="balanced", title="First title"
        )
        second = self.engine.generate_local(
            "alice", self.market, objective="balanced", title="Different title"
        )

        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(second["hypothesis_id"], first["hypothesis_id"])
        self.assertEqual(self.store.list("alice")["summary"]["total"], 1)

    @patch("ai_trade.hypothesis_lab.engine.BacktestEngine", _HighTurnoverBacktest)
    def test_auto_objective_uses_predeclared_local_threshold(self):
        record = self.engine.generate_local("alice", self.market, objective="auto")

        self.assertEqual(record["source"]["objective"], "turnover")
        self.assertIn("at least 4.0", record["source"]["selection_reason"])
        self.assertEqual(
            record["predictions"][0]["metric"], "full.turnover_ratio"
        )

    @patch("ai_trade.hypothesis_lab.engine.BacktestEngine", _Backtest)
    def test_snapshot_family_rejects_a_fourth_distinct_design(self):
        records = [
            self.engine.generate_local("alice", self.market, objective=objective)
            for objective in ("balanced", "drawdown", "turnover")
        ]
        draft = dict(records[0])
        draft.pop("reused")
        draft.pop("design_fingerprint")
        draft.pop("record_fingerprint")
        draft["hypothesis_id"] = f"hyp_{uuid4().hex}"
        draft["observation"] += " This is a separate pre-registered design."
        fourth = finalize_record(draft)

        with self.assertRaisesRegex(
            HypothesisLabCapacityError, "multiple-testing budget"
        ):
            self.store.publish("alice", fourth)

        self.assertEqual(self.store.list("alice")["summary"]["total"], 3)

    @patch("ai_trade.hypothesis_lab.engine.BacktestEngine", _Backtest)
    def test_tampered_record_is_rejected_on_read(self):
        record = self.engine.generate_local("alice", self.market)
        path = (
            self.store.owner_directory("alice")
            / "hypotheses"
            / f"{record['hypothesis_id']}.json"
        )
        value = json.loads(path.read_text(encoding="utf-8"))
        value["observation"] = "Tampered after publication."
        path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "fingerprint"):
            self.store.get("alice", record["hypothesis_id"])

    @patch("ai_trade.hypothesis_lab.engine.BacktestEngine", _Backtest)
    def test_engine_v1_hypothesis_remains_readable(self):
        record = self.engine.generate_local("alice", self.market)
        path = (
            self.store.owner_directory("alice")
            / "hypotheses"
            / f"{record['hypothesis_id']}.json"
        )
        value = json.loads(path.read_text(encoding="utf-8"))
        value["engine_version"] = 1
        value["design_fingerprint"] = design_fingerprint(value)
        value["record_fingerprint"] = record_fingerprint(value)
        path.write_text(json.dumps(value), encoding="utf-8")

        stored = self.store.get("alice", record["hypothesis_id"])
        self.assertEqual(stored["engine_version"], 1)

    @patch("ai_trade.hypothesis_lab.engine.BacktestEngine", _Backtest)
    def test_explicit_materialization_creates_one_bound_draft_only(self):
        record = self.engine.generate_local(
            "alice", self.market, objective="balanced"
        )

        first = self.engine.materialize_candidate(
            "alice",
            record["hypothesis_id"],
            confirmed_by="alice",
        )
        second = self.engine.materialize_candidate(
            "alice",
            record["hypothesis_id"],
            confirmed_by="alice",
        )

        candidate = first["candidate"]
        self.assertEqual(candidate["candidate_id"], second["candidate"]["candidate_id"])
        self.assertEqual(candidate["status"], "DRAFT")
        self.assertEqual(candidate["source"], "hypothesis_lab_human")
        self.assertEqual(
            candidate["proposal"]["hypothesis_fingerprint"],
            record["record_fingerprint"],
        )
        self.assertTrue(candidate["proposal"]["explicit_human_materialization"])
        self.assertFalse(candidate["proposal"]["model_authority"])
        self.assertFalse(candidate["safety"]["may_place_orders"])
        self.assertEqual(len(self.strategy_store.list_candidates("alice")), 1)
        self.assertEqual(
            first["safety"],
            {
                "explicit_human_materialization": True,
                "candidate_status": "DRAFT",
                "validation_completed": False,
                "approval_granted": False,
                "strategy_activated": False,
                "live_trading_authorized": False,
            },
        )

    def test_store_rejects_duplicate_keys_and_oversized_records(self):
        owner = "alice"
        hypothesis_id = "hyp_" + "a" * 32
        path = (
            self.store.owner_directory(owner)
            / "hypotheses"
            / f"{hypothesis_id}.json"
        )
        path.parent.mkdir(parents=True)
        path.write_text(
            '{"schema_version":1,"schema_version":1}', encoding="utf-8"
        )
        with self.assertRaisesRegex(RuntimeError, "duplicate JSON object key"):
            self.store.get(owner, hypothesis_id)

        path.write_bytes(b" " * (MAX_HYPOTHESIS_RECORD_BYTES + 1))
        with self.assertRaisesRegex(RuntimeError, "exceeds"):
            self.store.get(owner, hypothesis_id)


def _model_evaluation(
    snapshot_fingerprint: str,
    *,
    mean_ic: float = 0.07,
    dates: int = 400,
    delta: float = 0.02,
    dominant: str = "momentum_60_5",
    statistically_significant: bool = True,
    stable: bool = True,
    positive_ci: bool = True,
) -> dict:
    adjusted_p = 0.01 if statistically_significant else 0.20

    def validation(effect: float) -> dict:
        return {
            "effect_size": effect,
            "ci_low": effect / 2 if positive_ci else -abs(effect) / 2,
            "p_value": 0.005,
            "adjusted_p_value": adjusted_p,
            "alpha": 0.05,
            "correction": "holm",
            "reject_null": statistically_significant,
            "subperiods": 3,
            "positive_subperiods": 3 if stable else 2,
            "minimum_subperiod_mean": effect / 2 if stable else -abs(effect) / 2,
        }

    return {
        "evaluation_id": "mdl_" + "a" * 32,
        "record_fingerprint": "e" * 64,
        "model": {"model_id": "gbdt_v1"},
        "parameters": {"horizon": 20},
        "coefficients": [
            {"factor_id": dominant, "mean_abs": 0.3, "mean": 0.3, "final": 0.3},
            {"factor_id": "trend_gap_100", "mean_abs": 0.1, "mean": 0.1, "final": 0.1},
        ],
        "evidence": {
            "snapshot": {
                "as_of": "2026-07-24",
                "fingerprint": snapshot_fingerprint,
            }
        },
        "results": {
            "best_factor_id": "volatility_60",
            "model_minus_best_factor_ic": delta,
            "model": {"dates": dates, "mean_ic": mean_ic},
            "statistical_validation": {
                "model_ic": validation(mean_ic),
                "factor_comparisons": [
                    {
                        "factor_id": "volatility_60",
                        "mean_delta": delta,
                        "validation": validation(delta),
                    }
                ],
            },
        },
    }


class ModelEvidenceBridgeTests(TestCase):
    """The bridge derives bounded drafts from verified model evidence only."""

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.config = load_config(REPOSITORY_ROOT / "config" / "default.json")
        self.store = HypothesisLabStore(root / "hypothesis_lab")
        self.strategy_store = StrategyLabStore(root / "strategy_lab")
        self.strategy_lab = StrategyLabEngine(self.config, self.strategy_store)
        self.engine = HypothesisLabEngine(
            self.config,
            self.store,
            self.strategy_lab,
        )
        self.market = _Market()
        from ai_trade.hypothesis_lab.schema import json_fingerprint

        self.snapshot_fingerprint = json_fingerprint(
            dict(self.market.snapshot_metadata())
        )

    def _derive(self, evaluation):
        engine = SimpleNamespace(get=lambda owner, evaluation_id: evaluation)
        with patch(
            "ai_trade.model_lab.ModelLabEngine", return_value=engine
        ):
            return self.engine.derive_from_model(
                "alice", self.market, evaluation["evaluation_id"]
            )

    @patch("ai_trade.hypothesis_lab.engine.BacktestEngine", _Backtest)
    def test_derives_bound_draft_and_maps_defensive_evidence_to_drawdown(self):
        record = self._derive(
            _model_evaluation(self.snapshot_fingerprint, dominant="volatility_60")
        )

        self.assertFalse(record["reused"])
        self.assertEqual(record["source"]["kind"], "model_evidence_deterministic")
        self.assertEqual(record["source"]["objective"], "drawdown")
        self.assertFalse(record["source"]["model_used"])
        self.assertIn("model-evidence-derivation-v2", record["source"]["selection_reason"])
        self.assertIn("Holm-adjusted", record["source"]["selection_reason"])
        reference = next(
            item
            for item in record["evidence"]["references"]
            if item["kind"] == "model_evaluation"
        )
        self.assertEqual(reference["evidence_id"], "model_evaluation:mdl_" + "a" * 32)
        self.assertEqual(reference["fingerprint"], "e" * 64)
        self.assertTrue(
            any("no authority" in item for item in record["quality_assessment"]["limitations"])
        )
        # Round-trips through fail-closed schema validation on read.
        stored = self.store.get("alice", record["hypothesis_id"])
        self.assertEqual(stored["record_fingerprint"], record["record_fingerprint"])
        # Momentum-dominant evidence maps to balanced instead.
        balanced = self._derive(_model_evaluation(self.snapshot_fingerprint))
        self.assertEqual(balanced["source"]["objective"], "balanced")
        # Same derivation is idempotent: identical design is reused.
        again = self._derive(_model_evaluation(self.snapshot_fingerprint))
        self.assertTrue(again["reused"])
        self.assertEqual(again["hypothesis_id"], balanced["hypothesis_id"])

    @patch("ai_trade.hypothesis_lab.engine.BacktestEngine", _Backtest)
    def test_gates_fail_closed_on_weak_or_stale_evidence(self):
        with self.assertRaisesRegex(ValueError, "有效评估日"):
            self._derive(_model_evaluation(self.snapshot_fingerprint, dates=10))
        with self.assertRaisesRegex(ValueError, "IC 不为正"):
            self._derive(_model_evaluation(self.snapshot_fingerprint, mean_ic=-0.01))
        with self.assertRaisesRegex(ValueError, "未跑赢最优单因子"):
            self._derive(_model_evaluation(self.snapshot_fingerprint, delta=-0.005))
        with self.assertRaisesRegex(RuntimeError, "快照"):
            self._derive(_model_evaluation("f" * 64))
        with self.assertRaisesRegex(ValueError, "Holm-corrected"):
            self._derive(
                _model_evaluation(
                    self.snapshot_fingerprint,
                    statistically_significant=False,
                )
            )
        with self.assertRaisesRegex(ValueError, "three subperiods"):
            self._derive(
                _model_evaluation(self.snapshot_fingerprint, stable=False)
            )
        with self.assertRaisesRegex(ValueError, "confidence interval"):
            self._derive(
                _model_evaluation(self.snapshot_fingerprint, positive_ci=False)
            )
        self.assertEqual(self.store.list("alice")["hypotheses"], [])
        self.assertEqual(self.strategy_store.list_candidates("alice"), [])

    @patch("ai_trade.hypothesis_lab.engine.BacktestEngine", _Backtest)
    def test_derived_draft_counts_against_the_family_budget(self):
        self.engine.generate_local("alice", self.market, objective="balanced")
        self.engine.generate_local("alice", self.market, objective="drawdown")
        self._derive(_model_evaluation(self.snapshot_fingerprint))
        with self.assertRaises(HypothesisLabCapacityError):
            self.engine.generate_local("alice", self.market, objective="turnover")


if __name__ == "__main__":
    import unittest

    unittest.main()
