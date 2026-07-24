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
from ai_trade.hypothesis_lab.schema import finalize_record
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


if __name__ == "__main__":
    import unittest

    unittest.main()
