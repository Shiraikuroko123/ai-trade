import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import patch
from tempfile import TemporaryDirectory

from ai_trade.config import load_config
from ai_trade.strategy_lab import (
    StrategyLabConflictError,
    StrategyLabEngine,
    StrategyLabStore,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class _Market:
    def __init__(self):
        from datetime import date, timedelta

        start = date(2022, 1, 1)
        self.calendar = [start + timedelta(days=index) for index in range(180)]

    def snapshot_metadata(self):
        return {
            "provider": "test",
            "latest_common_session": self.calendar[-1].isoformat(),
            "manifest": {"snapshot_id": "fixed-test-snapshot"},
        }


class _Backtest:
    def __init__(self, config, market, strategy_settings=None):
        self.config = config
        self.strategy = strategy_settings or config.strategy

    def run(self, start=None, end=None, initial_cash=None):
        return SimpleNamespace(
            metrics={
                "total_return": 0.12,
                "cagr": 0.10,
                "sharpe": 1.0,
                "max_drawdown": -0.05,
                "turnover": 1.2,
                "transaction_costs": 100.0,
            }
        )


class _WeakCandidateBacktest(_Backtest):
    def run(self, start=None, end=None, initial_cash=None):
        if self.strategy.lookback_days == 127:
            return SimpleNamespace(
                metrics={
                    "total_return": -0.20,
                    "cagr": -0.18,
                    "sharpe": -1.0,
                    "max_drawdown": -0.25,
                    "turnover": 3.0,
                    "transaction_costs": 500.0,
                }
            )
        return super().run(start, end, initial_cash)


class StrategyLabTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.store = StrategyLabStore(Path(self.temporary.name) / "strategy_lab")
        self.config = load_config(REPOSITORY_ROOT / "config" / "default.json")
        self.engine = StrategyLabEngine(self.config, self.store)

    def test_schema_is_allowlisted_and_rejects_unsafe_or_invalid_changes(self):
        schema = self.engine.parameter_schema()
        self.assertEqual(schema["schema_version"], 1)
        self.assertGreater(len(schema["parameters"]), 20)
        for item in schema["parameters"]:
            self.assertEqual(
                set(item),
                {
                    "scope",
                    "name",
                    "label",
                    "type",
                    "min",
                    "max",
                    "step",
                    "unit",
                    "options",
                },
            )
        with self.assertRaisesRegex(ValueError, "not editable"):
            self.engine.create_manual_candidate(
                "alice",
                {"strategy": {"__class__": "payload"}},
                "Unsafe",
                "Try arbitrary code",
                "This must be rejected",
            )
        with self.assertRaisesRegex(ValueError, "must be numeric"):
            self.engine.create_manual_candidate(
                "alice",
                {"strategy": {"lookback_days": True}},
                "Boolean",
                "Try an invalid integer",
                "This must be rejected",
            )
        with self.assertRaisesRegex(ValueError, "exceeds"):
            self.engine.create_manual_candidate(
                "alice",
                {"strategy": {"top_n": 100}},
                "Too large",
                "Try an invalid universe selection",
                "This must be rejected",
            )

    def test_owner_directories_are_hashed_and_records_are_immutable(self):
        alice = self.store.owner_directory("Alice")
        normalized = self.store.owner_directory(" alice ")
        bob = self.store.owner_directory("bob")
        self.assertEqual(alice, normalized)
        self.assertNotEqual(alice, bob)
        self.assertNotIn("alice", str(alice).lower())

        candidate = self.engine.create_manual_candidate(
            "Alice",
            {"strategy": {"lookback_days": 127}},
            "Longer lookback",
            "A one-session change should remain stable",
            "Manual beta-user edit",
        )
        path = alice / "candidates" / f"{candidate['candidate_id']}.json"
        before = path.read_bytes()
        with self.assertRaises(FileExistsError):
            self.store.write_candidate("Alice", json.loads(before))
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(self.store.list_candidates("bob"), [])

    def test_candidate_creation_uses_display_actor_without_exposing_owner(self):
        owner_id = "acct_" + "6" * 32
        actor = "alice"
        manual = self.engine.create_manual_candidate(
            owner_id,
            {"strategy": {"lookback_days": 127}},
            "Manual candidate",
            "Review a small lookback change",
            "Human research",
            actor=actor,
        )
        proposed = self.engine.propose_local_ai_candidate(
            owner_id,
            "Local proposal",
            "Review a deterministic risk adjustment",
            "balanced",
            actor=actor,
        )
        default_actor = self.engine.create_manual_candidate(
            owner_id,
            {"risk": {"cooldown_days": 8}},
            "Library candidate",
            "Direct engine calls must not expose their storage owner",
            "Library compatibility path",
        )

        summary = self.engine.summary(owner_id)
        self.assertEqual(
            [item["actor"] for item in summary["history"]],
            [actor, actor, "system"],
        )
        for candidate in (manual, proposed, default_actor):
            self.assertNotIn("owner", candidate)
            serialized = json.dumps(candidate, sort_keys=True)
            self.assertNotIn(owner_id, serialized)
            self.assertNotIn("account_id", serialized)
            self.assertNotIn("principal_id", serialized)

    @patch("ai_trade.strategy_lab.engine.BacktestEngine", _Backtest)
    def test_manual_candidate_validation_approval_export_activation_and_rollback(self):
        owner_id = "acct_" + "5" * 32
        actor = "alice"
        candidate = self.engine.create_manual_candidate(
            owner_id,
            {"strategy": {"lookback_days": 127}},
            "Longer lookback",
            "A small change may improve signal stability",
            "Manual research hypothesis",
            actor=actor,
        )
        candidate_path = (
            self.store.owner_directory(owner_id)
            / "candidates"
            / f"{candidate['candidate_id']}.json"
        )
        immutable_bytes = candidate_path.read_bytes()
        self.assertNotIn("owner", candidate)
        self.assertEqual(candidate["status"], "DRAFT")
        self.assertEqual(
            candidate["candidate_settings"]["strategy"]["lookback_days"], 127
        )
        self.assertEqual(
            candidate["effective_changes"]["strategy"], {"lookback_days": 127}
        )

        validated = self.engine.validate_candidate(
            owner_id, candidate["candidate_id"], _Market(), actor=actor
        )
        self.engine.validate_candidate(
            owner_id, candidate["candidate_id"], _Market(), actor=actor
        )
        self.assertEqual(validated["status"], "ELIGIBLE")
        self.assertTrue(validated["validation"]["gates"]["eligible"])
        self.assertEqual(validated["validation"]["gates"]["total"], 5)
        self.assertEqual(
            validated["validation"]["market_snapshot"]["date"], "2022-06-29"
        )
        self.assertEqual(candidate_path.read_bytes(), immutable_bytes)

        approved = self.engine.approve_candidate(
            owner_id, candidate["candidate_id"], actor, "Reviewed all gates"
        )
        self.engine.approve_candidate(
            owner_id, candidate["candidate_id"], actor, "Duplicate request"
        )
        self.assertEqual(approved["status"], "APPROVED")
        self.assertTrue(approved["approval"]["explicit_human_approval"])
        self.assertFalse(approved["approval"]["live_trading_authorized"])

        exported = self.engine.export_paper_config(
            owner_id, candidate["candidate_id"], actor=actor
        )
        self.engine.export_paper_config(
            owner_id, candidate["candidate_id"], actor=actor
        )
        exported_config = load_config(exported["path"])
        self.assertEqual(exported["broker_mode"], "disabled")
        self.assertEqual(exported_config.raw["broker"]["mode"], "disabled")
        self.assertIsNone(exported_config.raw["broker"]["adapter"])
        self.assertIn(candidate["candidate_id"], str(exported_config.paper_state_file))
        self.assertEqual(self.config.raw["broker"]["mode"], "disabled")

        active = self.engine.activate_candidate(
            owner_id, candidate["candidate_id"], actor
        )
        self.engine.activate_candidate(
            owner_id, candidate["candidate_id"], actor
        )
        self.assertEqual(active["candidate_id"], candidate["candidate_id"])
        self.assertEqual(active["activated_by"], actor)
        self.assertTrue(active["can_rollback"])
        rolled_back = self.engine.rollback(
            owner_id,
            actor,
            expected_active_candidate_id=active["candidate_id"],
            expected_active_fingerprint=active["fingerprint"],
            note="Restore configured baseline",
        )
        self.assertIsNone(rolled_back["candidate_id"])
        self.assertFalse(rolled_back["can_rollback"])
        summary = self.engine.summary(owner_id)
        history = summary["history"]
        self.assertEqual(
            [item["action"] for item in history],
            ["create", "validate", "approve", "export", "activate", "rollback"],
        )
        self.assertTrue(
            all({"candidate_id", "actor", "source"} <= set(item) for item in history)
        )
        self.assertTrue(all(item["actor"] == actor for item in history))
        self.assertTrue(all(not item["live_trading_authorized"] for item in history))
        public_payload = json.dumps(
            {
                "candidate": candidate,
                "validated": validated,
                "approved": approved,
                "active": active,
                "rolled_back": rolled_back,
                "summary": summary,
            },
            sort_keys=True,
        )
        self.assertNotIn(owner_id, public_payload)
        self.assertNotIn("account_id", public_payload)
        self.assertNotIn("principal_id", public_payload)

    @patch("ai_trade.strategy_lab.engine.BacktestEngine", _Backtest)
    def test_stability_checks_every_changed_numeric_parameter(self):
        candidate = self.engine.create_manual_candidate(
            "alice",
            {
                "strategy": {"rebalance_days": 21, "lookback_days": 127},
                "risk": {"cooldown_days": 21},
            },
            "Three-parameter candidate",
            "Every changed numeric parameter needs a neighborhood test",
            "Coverage regression test",
        )
        validated = self.engine.validate_candidate(
            "alice", candidate["candidate_id"], _Market()
        )
        stability = validated["validation"]["stability"]
        self.assertEqual(
            set(stability["parameters"]),
            {
                "strategy.rebalance_days",
                "strategy.lookback_days",
                "risk.cooldown_days",
            },
        )
        self.assertEqual(stability["variant_count"], 6)
        self.assertEqual(len(stability["variants"]), 6)

    @patch("ai_trade.strategy_lab.engine.BacktestEngine", _Backtest)
    def test_stability_handles_zero_values_and_revalidates_combinations(self):
        zero_candidate = self.engine.create_manual_candidate(
            "alice",
            {"risk": {"cooldown_days": 0}},
            "Zero cooldown",
            "A zero-valued parameter still needs a valid neighborhood",
            "Zero-neighborhood regression test",
        )
        zero_validation = self.engine.validate_candidate(
            "alice", zero_candidate["candidate_id"], _Market()
        )
        zero_variants = zero_validation["validation"]["stability"]["variants"]
        self.assertEqual(
            zero_validation["validation"]["stability"]["parameters"],
            ["risk.cooldown_days"],
        )
        self.assertTrue(zero_variants)
        self.assertTrue(
            all(row["changes"]["risk"]["cooldown_days"] > 0 for row in zero_variants)
        )

        constrained = self.engine.create_manual_candidate(
            "bob",
            {"strategy": {"lookback_days": 20, "skip_days": 19}},
            "Constrained windows",
            "Only legal lookback and skip neighborhoods may be tested",
            "Combination-validation regression test",
        )
        constrained_validation = self.engine.validate_candidate(
            "bob", constrained["candidate_id"], _Market()
        )
        snapshot = constrained["candidate_settings"]
        for row in constrained_validation["validation"]["stability"]["variants"]:
            strategy = {**snapshot["strategy"], **row["changes"].get("strategy", {})}
            self.assertLess(strategy["skip_days"], strategy["lookback_days"])

    @patch("ai_trade.strategy_lab.engine.BacktestEngine", _Backtest)
    def test_concurrent_activations_preserve_the_complete_rollback_stack(self):
        candidate_ids = []
        for lookback in (127, 128):
            candidate = self.engine.create_manual_candidate(
                "alice",
                {"strategy": {"lookback_days": lookback}},
                f"Candidate {lookback}",
                "Exercise concurrent activation",
                "Concurrency regression test",
            )
            self.engine.validate_candidate(
                "alice", candidate["candidate_id"], _Market()
            )
            self.engine.approve_candidate("alice", candidate["candidate_id"], "alice")
            self.engine.export_paper_config("alice", candidate["candidate_id"])
            candidate_ids.append(candidate["candidate_id"])

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    self.engine.activate_candidate,
                    "alice",
                    candidate_id,
                    "alice",
                )
                for candidate_id in candidate_ids
            ]
        results = []
        errors = []
        for future in futures:
            try:
                results.append(future.result())
            except RuntimeError as exc:
                errors.append(exc)
        self.assertEqual(len(results), 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], StrategyLabConflictError)
        self.assertIn("baseline changed", str(errors[0]).lower())
        active = self.engine.summary("alice")["active"]
        self.assertEqual(active["rollback_depth"], 1)
        restored = self.engine.rollback(
            "alice",
            "alice",
            expected_active_candidate_id=active["candidate_id"],
            expected_active_fingerprint=active["fingerprint"],
        )
        self.assertIsNone(restored["candidate_id"])
        self.assertEqual(restored["rollback_depth"], 0)
        self.assertEqual(
            sum(
                event["action"] == "activate"
                for event in self.engine.summary("alice")["history"]
            ),
            1,
        )

    @patch("ai_trade.strategy_lab.engine.BacktestEngine", _Backtest)
    def test_concurrent_and_retried_rollbacks_consume_only_one_layer(self):
        def create_and_activate(lookback: int) -> dict[str, object]:
            candidate = self.engine.create_manual_candidate(
                "alice",
                {"strategy": {"lookback_days": lookback}},
                f"Candidate {lookback}",
                "Exercise rollback compare-and-swap protection",
                "Rollback concurrency regression test",
            )
            candidate_id = candidate["candidate_id"]
            self.engine.validate_candidate("alice", candidate_id, _Market())
            self.engine.approve_candidate("alice", candidate_id, "alice")
            self.engine.export_paper_config("alice", candidate_id)
            return self.engine.activate_candidate("alice", candidate_id, "alice")

        first = create_and_activate(127)
        second = create_and_activate(128)
        request = {
            "expected_active_candidate_id": second["candidate_id"],
            "expected_active_fingerprint": second["fingerprint"],
        }

        with ThreadPoolExecutor(max_workers=2) as executor:
            futures = [
                executor.submit(
                    self.engine.rollback,
                    "alice",
                    "alice",
                    **request,
                )
                for _ in range(2)
            ]
        results = []
        conflicts = []
        for future in futures:
            try:
                results.append(future.result())
            except StrategyLabConflictError as exc:
                conflicts.append(exc)

        self.assertEqual(len(results), 1)
        self.assertEqual(len(conflicts), 1)
        self.assertEqual(results[0]["candidate_id"], first["candidate_id"])
        after_first = self.engine.summary("alice")["active"]
        self.assertEqual(after_first["candidate_id"], first["candidate_id"])
        self.assertEqual(after_first["rollback_depth"], 1)

        with self.assertRaises(StrategyLabConflictError):
            self.engine.rollback("alice", "alice", **request)

        after_retry = self.engine.summary("alice")
        self.assertEqual(after_retry["active"]["candidate_id"], first["candidate_id"])
        self.assertEqual(after_retry["active"]["rollback_depth"], 1)
        self.assertEqual(
            sum(event["action"] == "rollback" for event in after_retry["history"]),
            1,
        )

    @patch("ai_trade.strategy_lab.engine.BacktestEngine", _Backtest)
    def test_activation_requires_an_existing_matching_export(self):
        candidate = self.engine.create_manual_candidate(
            "alice",
            {"strategy": {"lookback_days": 127}},
            "Export required",
            "Activation must consume the reviewed paper configuration",
            "Approval alone is insufficient",
        )
        self.engine.validate_candidate("alice", candidate["candidate_id"], _Market())
        self.engine.approve_candidate("alice", candidate["candidate_id"], "alice")
        with self.assertRaisesRegex(RuntimeError, "paper export"):
            self.engine.activate_candidate("alice", candidate["candidate_id"], "alice")

        exported = self.engine.export_paper_config("alice", candidate["candidate_id"])
        raw = json.loads(Path(exported["path"]).read_text(encoding="utf-8"))
        raw["_strategy_lab"]["approval_fingerprint"] = "0" * 64
        Path(exported["path"]).write_text(
            json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with self.assertRaisesRegex(RuntimeError, "approval fingerprint mismatch"):
            self.engine.activate_candidate("alice", candidate["candidate_id"], "alice")

    @patch("ai_trade.strategy_lab.engine.BacktestEngine", _Backtest)
    def test_candidate_content_tampering_is_rejected(self):
        candidate = self.engine.create_manual_candidate(
            "alice",
            {"strategy": {"lookback_days": 127}},
            "Immutable candidate",
            "Recorded settings must remain bound to their fingerprint",
            "Integrity regression test",
        )
        path = (
            self.store.owner_directory("alice")
            / "candidates"
            / f"{candidate['candidate_id']}.json"
        )
        raw = json.loads(path.read_text(encoding="utf-8"))
        raw["candidate"]["strategy"]["lookback_days"] = 128
        path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "settings fingerprint mismatch"):
            self.engine.validate_candidate(
                "alice", candidate["candidate_id"], _Market()
            )

    @patch("ai_trade.strategy_lab.engine.BacktestEngine", _Backtest)
    def test_validation_and_approval_fingerprint_mismatches_are_rejected(self):
        first = self.engine.create_manual_candidate(
            "alice",
            {"strategy": {"lookback_days": 127}},
            "Validation binding",
            "Validation must remain attached to this candidate",
            "Integrity regression test",
        )
        self.engine.validate_candidate("alice", first["candidate_id"], _Market())
        validation_path = (
            self.store.owner_directory("alice")
            / "validations"
            / f"{first['candidate_id']}.json"
        )
        validation = json.loads(validation_path.read_text(encoding="utf-8"))
        validation["candidate_fingerprint"] = "0" * 64
        validation_path.write_text(
            json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with self.assertRaisesRegex(RuntimeError, "candidate fingerprint mismatch"):
            self.engine.approve_candidate("alice", first["candidate_id"], "alice")

        second = self.engine.create_manual_candidate(
            "alice",
            {"strategy": {"lookback_days": 128}},
            "Approval binding",
            "Approval must remain attached to the exact validation",
            "Integrity regression test",
        )
        self.engine.validate_candidate("alice", second["candidate_id"], _Market())
        self.engine.approve_candidate("alice", second["candidate_id"], "alice")
        approval_path = (
            self.store.owner_directory("alice")
            / "approvals"
            / f"{second['candidate_id']}.json"
        )
        approval = json.loads(approval_path.read_text(encoding="utf-8"))
        approval["validation_fingerprint"] = "0" * 64
        approval_path.write_text(
            json.dumps(approval, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        with self.assertRaisesRegex(RuntimeError, "validation fingerprint mismatch"):
            self.engine.export_paper_config("alice", second["candidate_id"])

    @patch("ai_trade.strategy_lab.engine.BacktestEngine", _Backtest)
    def test_configuration_context_drift_invalidates_a_candidate(self):
        candidate = self.engine.create_manual_candidate(
            "alice",
            {"strategy": {"lookback_days": 127}},
            "Configuration binding",
            "Research results only apply to their full configuration",
            "Context regression test",
        )
        self.config.raw["reports_dir"] = "reports/changed-after-candidate"
        with self.assertRaisesRegex(RuntimeError, "configuration context changed"):
            self.engine.validate_candidate(
                "alice", candidate["candidate_id"], _Market()
            )

    @patch("ai_trade.strategy_lab.engine.BacktestEngine", _Backtest)
    def test_activated_sibling_invalidates_old_candidate_lifecycle(self):
        candidates = []
        for lookback in (127, 128):
            candidate = self.engine.create_manual_candidate(
                "alice",
                {"strategy": {"lookback_days": lookback}},
                f"Sibling {lookback}",
                "Only one sibling may advance from a shared parent",
                "Stale candidate regression test",
            )
            self.engine.validate_candidate(
                "alice", candidate["candidate_id"], _Market()
            )
            self.engine.approve_candidate("alice", candidate["candidate_id"], "alice")
            self.engine.export_paper_config("alice", candidate["candidate_id"])
            candidates.append(candidate)

        stale, selected = candidates
        self.engine.activate_candidate("alice", selected["candidate_id"], "alice")
        operations = (
            lambda: self.engine.approve_candidate(
                "alice", stale["candidate_id"], "alice"
            ),
            lambda: self.engine.export_paper_config("alice", stale["candidate_id"]),
            lambda: self.engine.activate_candidate(
                "alice", stale["candidate_id"], "alice"
            ),
        )
        for operation in operations:
            with self.assertRaisesRegex(RuntimeError, "baseline changed"):
                operation()

    @patch("ai_trade.strategy_lab.engine.BacktestEngine", _WeakCandidateBacktest)
    def test_failed_gates_reject_candidate_and_block_approval(self):
        candidate = self.engine.create_manual_candidate(
            "alice",
            {"strategy": {"lookback_days": 127}},
            "Weak candidate",
            "Test a deliberately weak result",
            "Validation must fail closed",
        )
        rejected = self.engine.validate_candidate(
            "alice", candidate["candidate_id"], _Market()
        )
        self.assertEqual(rejected["status"], "REJECTED")
        self.assertFalse(rejected["validation"]["gates"]["eligible"])
        failed = [
            check["id"]
            for check in rejected["validation"]["gates"]["checks"]
            if not check["passed"]
        ]
        self.assertIn("full_sample", failed)
        self.assertIn("drawdown", failed)
        with self.assertRaisesRegex(RuntimeError, "failed validation gates"):
            self.engine.approve_candidate("alice", candidate["candidate_id"], "alice")

    def test_local_ai_proposal_is_deterministic_and_cannot_skip_approval(self):
        first = self.engine.propose_local_ai_candidate(
            "alice", "Balanced proposal", "Reduce avoidable risk", "balanced"
        )
        second = self.engine.propose_local_ai_candidate(
            "bob", "Balanced proposal", "Reduce avoidable risk", "balanced"
        )
        self.assertEqual(first["source"], "ai_local")
        self.assertEqual(first["effective_changes"], second["effective_changes"])
        self.assertFalse(first["safety"]["may_place_orders"])
        with self.assertRaisesRegex(RuntimeError, "validated"):
            self.engine.approve_candidate("alice", first["candidate_id"], "alice")
        with self.assertRaisesRegex(RuntimeError, "approved"):
            self.engine.export_paper_config("alice", first["candidate_id"])
        with self.assertRaisesRegex(RuntimeError, "approved"):
            self.engine.activate_candidate("alice", first["candidate_id"], "alice")


if __name__ == "__main__":
    import unittest

    unittest.main()
