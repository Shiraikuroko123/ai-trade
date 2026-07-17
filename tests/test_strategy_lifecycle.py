from __future__ import annotations

from datetime import date, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from tempfile import TemporaryDirectory

from ai_trade.config import load_config
from ai_trade.strategy_lab import (
    LifecyclePolicy,
    StrategyLabConflictError,
    StrategyLabEngine,
    StrategyLabStore,
)
from ai_trade.strategy_lab.lifecycle import (
    INSUFFICIENT_DATA,
    MONITORING_OK,
    REVIEW_REQUIRED,
    evaluate_strategy_decay,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class _Market:
    def __init__(self, sessions: int = 180):
        start = date(2022, 1, 1)
        self.calendar = [start + timedelta(days=index) for index in range(sessions)]

    def snapshot_metadata(self):
        return {
            "provider": "test",
            "latest_common_session": self.calendar[-1].isoformat(),
            "manifest": {"snapshot_id": "strategy-lifecycle-test"},
        }


def _metrics(sharpe: float = 1.0, drawdown: float = -0.05):
    return {
        "total_return": 0.12,
        "cagr": 0.10,
        "sharpe": sharpe,
        "max_drawdown": drawdown,
        "turnover": 1.2,
        "transaction_costs": 100.0,
    }


class _StableBacktest:
    def __init__(self, config, market, strategy_settings=None):
        self.strategy = strategy_settings or config.strategy

    def run(self, start=None, end=None, initial_cash=None):
        return SimpleNamespace(metrics=_metrics())


class _DegradedBacktest(_StableBacktest):
    def run(self, start=None, end=None, initial_cash=None):
        if self.strategy.lookback_days == 127:
            return SimpleNamespace(metrics=_metrics(-1.0, -0.30))
        return SimpleNamespace(metrics=_metrics(1.0, -0.05))


class StrategyLifecycleEvaluationTests(TestCase):
    def test_decay_evaluation_is_deterministic_and_never_changes_state(self):
        policy = LifecyclePolicy()
        healthy = evaluate_strategy_decay(
            session_count=60,
            recent_candidate={"sharpe": 0.9, "max_drawdown": -0.06},
            recent_parent={"sharpe": 0.8, "max_drawdown": -0.05},
            validation_candidate={"sharpe": 1.0, "max_drawdown": -0.05},
            maximum_drawdown=0.2,
            policy=policy,
        )
        self.assertEqual(healthy["verdict"], MONITORING_OK)
        self.assertFalse(healthy["review_required"])
        self.assertFalse(healthy["automatic_state_change"])

        degraded = evaluate_strategy_decay(
            session_count=60,
            recent_candidate={"sharpe": -1.0, "max_drawdown": -0.30},
            recent_parent={"sharpe": 1.0, "max_drawdown": -0.05},
            validation_candidate={"sharpe": 1.0, "max_drawdown": -0.05},
            maximum_drawdown=0.2,
            policy=policy,
        )
        self.assertEqual(degraded["verdict"], REVIEW_REQUIRED)
        self.assertTrue(degraded["review_required"])
        self.assertIn("drawdown_limit", degraded["failed_checks"])
        self.assertFalse(degraded["automatic_state_change"])

    def test_insufficient_data_is_not_misreported_as_decay(self):
        result = evaluate_strategy_decay(
            session_count=10,
            recent_candidate=None,
            recent_parent=None,
            validation_candidate={"sharpe": 1.0, "max_drawdown": -0.05},
            maximum_drawdown=0.2,
            policy=LifecyclePolicy(),
        )
        self.assertEqual(result["verdict"], INSUFFICIENT_DATA)
        self.assertFalse(result["review_required"])
        self.assertEqual(result["failed_checks"], [])


class StrategyLifecycleEngineTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = StrategyLabStore(Path(self.temporary.name) / "strategy_lab")
        self.config = load_config(REPOSITORY_ROOT / "config" / "default.json")
        self.engine = StrategyLabEngine(self.config, self.store)
        self.owner = "acct_" + "8" * 32
        self.actor = "alice"

    def _activate(self):
        candidate = self.engine.create_manual_candidate(
            self.owner,
            {"strategy": {"lookback_days": 127}},
            "Lifecycle candidate",
            "A small parameter change should remain stable after activation",
            "Lifecycle regression test",
            actor=self.actor,
        )
        with patch("ai_trade.strategy_lab.engine.BacktestEngine", _StableBacktest):
            self.engine.validate_candidate(
                self.owner, candidate["candidate_id"], _Market(), actor=self.actor
            )
        self.engine.approve_candidate(
            self.owner, candidate["candidate_id"], self.actor, "Reviewed"
        )
        self.engine.export_paper_config(
            self.owner, candidate["candidate_id"], actor=self.actor
        )
        active = self.engine.activate_candidate(
            self.owner, candidate["candidate_id"], self.actor, "Paper observation"
        )
        return candidate, active

    def test_monitor_suspend_resume_and_retire_are_auditable_human_transitions(self):
        candidate, active = self._activate()
        self.assertEqual(active["lifecycle_state"], "ACTIVE")
        with patch("ai_trade.strategy_lab.engine.BacktestEngine", _DegradedBacktest):
            monitor = self.engine.monitor_active_candidate(
                self.owner, _Market(), actor=self.actor
            )

        after_monitor = self.engine.summary(self.owner)
        self.assertEqual(after_monitor["active"]["lifecycle_state"], "ACTIVE")
        self.assertEqual(monitor["evidence"]["verdict"], REVIEW_REQUIRED)
        self.assertFalse(monitor["state_changed"])
        self.assertFalse(monitor["live_trading_authorized"])
        self.assertEqual(
            after_monitor["monitoring"]["latest"]["monitor_id"],
            monitor["monitor_id"],
        )

        monitor_path = (
            self.store.owner_directory(self.owner)
            / "monitors"
            / f"{monitor['monitor_id']}.json"
        )
        immutable = monitor_path.read_bytes()
        with self.assertRaises(StrategyLabConflictError):
            self.store.write_monitor(
                self.owner,
                monitor,
                expected_active_candidate_id="cand_" + "f" * 32,
                expected_active_fingerprint=active["fingerprint"],
                expected_lifecycle_state="ACTIVE",
            )
        with self.assertRaises(FileExistsError):
            self.store.write_monitor(
                self.owner,
                monitor,
                expected_active_candidate_id=candidate["candidate_id"],
                expected_active_fingerprint=active["fingerprint"],
                expected_lifecycle_state="ACTIVE",
            )
        self.assertEqual(monitor_path.read_bytes(), immutable)

        suspended = self.engine.suspend_active_candidate(
            self.owner,
            actor=self.actor,
            expected_active_candidate_id=candidate["candidate_id"],
            expected_active_fingerprint=active["fingerprint"],
            note="Review the failed decay checks",
            monitor_id=monitor["monitor_id"],
        )
        self.assertEqual(suspended["lifecycle_state"], "SUSPENDED")
        self.assertTrue(suspended["can_resume"])
        with self.assertRaises(StrategyLabConflictError):
            self.engine.suspend_active_candidate(
                self.owner,
                actor=self.actor,
                expected_active_candidate_id=candidate["candidate_id"],
                expected_active_fingerprint=active["fingerprint"],
                note="Duplicate stale suspension",
            )

        resumed = self.engine.resume_active_candidate(
            self.owner,
            actor=self.actor,
            expected_active_candidate_id=candidate["candidate_id"],
            expected_active_fingerprint=active["fingerprint"],
            note="Human review completed",
            monitor_id=monitor["monitor_id"],
        )
        self.assertEqual(resumed["lifecycle_state"], "ACTIVE")

        retired = self.engine.retire_active_candidate(
            self.owner,
            actor=self.actor,
            expected_active_candidate_id=candidate["candidate_id"],
            expected_active_fingerprint=active["fingerprint"],
            note="Retire after human review",
            monitor_id=monitor["monitor_id"],
        )
        self.assertIsNone(retired["candidate_id"])
        self.assertEqual(retired["lifecycle_state"], "CONFIGURED")
        self.assertEqual(retired["retired_count"], 1)
        with self.assertRaisesRegex(RuntimeError, "Retired candidates"):
            self.engine.activate_candidate(
                self.owner, candidate["candidate_id"], self.actor, "Reactivate"
            )

        summary = self.engine.summary(self.owner)
        lifecycle = self.engine.get_candidate(
            self.owner, candidate["candidate_id"]
        )["lifecycle"]
        self.assertEqual(lifecycle["state"], "RETIRED")
        self.assertFalse(summary["safety"]["automatic_strategy_suspension"])
        self.assertFalse(summary["safety"]["automatic_strategy_retirement"])
        actions = [item["action"] for item in summary["history"]]
        self.assertEqual(actions[-4:], ["monitor", "suspend", "resume", "retire"])
        for event in summary["history"][-4:]:
            self.assertFalse(event["live_trading_authorized"])

    def test_lifecycle_evidence_must_match_the_active_candidate(self):
        candidate, active = self._activate()
        with patch("ai_trade.strategy_lab.engine.BacktestEngine", _StableBacktest):
            monitor = self.engine.monitor_active_candidate(
                self.owner, _Market(), actor=self.actor
            )
        with self.assertRaises(StrategyLabConflictError):
            self.engine.suspend_active_candidate(
                self.owner,
                actor=self.actor,
                expected_active_candidate_id=candidate["candidate_id"],
                expected_active_fingerprint="0" * 64,
                note="Stale fingerprint",
                monitor_id=monitor["monitor_id"],
            )
        self.assertEqual(self.store.read_active(self.owner)["lifecycle_state"], "ACTIVE")
        self.assertEqual(active["fingerprint"], candidate["candidate_fingerprint"])

    def test_tampered_monitoring_evidence_cannot_support_a_transition(self):
        candidate, active = self._activate()
        with patch("ai_trade.strategy_lab.engine.BacktestEngine", _StableBacktest):
            monitor = self.engine.monitor_active_candidate(
                self.owner, _Market(), actor=self.actor
            )
        path = (
            self.store.owner_directory(self.owner)
            / "monitors"
            / f"{monitor['monitor_id']}.json"
        )
        tampered = json.loads(path.read_text(encoding="utf-8"))
        tampered["evidence"]["verdict"] = REVIEW_REQUIRED
        path.write_text(json.dumps(tampered), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "fingerprint mismatch"):
            self.engine.suspend_active_candidate(
                self.owner,
                actor=self.actor,
                expected_active_candidate_id=candidate["candidate_id"],
                expected_active_fingerprint=active["fingerprint"],
                note="Do not trust edited evidence",
                monitor_id=monitor["monitor_id"],
            )
        self.assertEqual(self.store.read_active(self.owner)["lifecycle_state"], "ACTIVE")
        with self.assertRaisesRegex(RuntimeError, "fingerprint mismatch"):
            self.engine.summary(self.owner)
