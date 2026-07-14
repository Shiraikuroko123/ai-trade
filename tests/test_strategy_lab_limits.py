from datetime import date, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from ai_trade.config import load_config
from ai_trade.strategy_lab.engine import (
    MAX_TRANSITION_EVENTS_PER_OWNER,
    StrategyLabEngine,
)
from ai_trade.strategy_lab.store import (
    StrategyLabCapacityError,
    StrategyLabStore,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class _Market:
    def __init__(self):
        start = date(2022, 1, 1)
        self.calendar = [start + timedelta(days=index) for index in range(180)]

    def snapshot_metadata(self):
        return {
            "provider": "test",
            "latest_common_session": self.calendar[-1].isoformat(),
            "manifest": {"snapshot_id": "fixed-limit-test-snapshot"},
        }


class _Backtest:
    def __init__(self, config, market, strategy_settings=None):
        self.config = config

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


class StrategyLabLimitTests(TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = load_config(REPOSITORY_ROOT / "config" / "default.json")

    def _ready_candidate(
        self, engine: StrategyLabEngine, owner: str, lookback: int
    ) -> dict[str, object]:
        candidate = engine.create_manual_candidate(
            owner,
            {"strategy": {"lookback_days": lookback}},
            f"Candidate {lookback}",
            "Check the transition-event capacity boundary.",
            "Focused resource-limit test.",
        )
        candidate_id = candidate["candidate_id"]
        engine.validate_candidate(owner, candidate_id, _Market())
        engine.approve_candidate(owner, candidate_id, owner)
        engine.export_paper_config(owner, candidate_id)
        return candidate

    def test_candidate_limit_is_per_owner_and_has_a_clear_error(self):
        store = StrategyLabStore(Path(self.temporary.name) / "strategy_lab")
        engine = StrategyLabEngine(self.config, store)

        with patch("ai_trade.strategy_lab.engine.MAX_CANDIDATES_PER_OWNER", 2):
            for index in range(2):
                engine.create_manual_candidate(
                    "alice",
                    {"strategy": {"lookback_days": 127 + index}},
                    f"Candidate {index}",
                    "Check the candidate capacity boundary.",
                    "Focused resource-limit test.",
                )

            with self.assertRaisesRegex(
                StrategyLabCapacityError,
                "每个账号 2 个的上限",
            ):
                engine.create_manual_candidate(
                    "alice",
                    {"strategy": {"lookback_days": 129}},
                    "Candidate at limit",
                    "This candidate must be rejected before it is stored.",
                    "Focused resource-limit test.",
                )

            bob = engine.create_manual_candidate(
                "bob",
                {"strategy": {"lookback_days": 127}},
                "Independent owner",
                "Another owner keeps an independent capacity budget.",
                "Focused resource-limit test.",
            )

        self.assertEqual(len(store.list_candidates("alice")), 2)
        self.assertEqual(bob["status"], "DRAFT")

    @patch("ai_trade.strategy_lab.engine.BacktestEngine", _Backtest)
    def test_activation_limit_is_clear_and_leaves_active_state_unchanged(self):
        self.assertEqual(MAX_TRANSITION_EVENTS_PER_OWNER, 1000)
        store = StrategyLabStore(Path(self.temporary.name) / "strategy_lab")
        engine = StrategyLabEngine(self.config, store)

        with patch("ai_trade.strategy_lab.engine.MAX_TRANSITION_EVENTS_PER_OWNER", 1):
            first = self._ready_candidate(engine, "alice", 127)
            active = engine.activate_candidate("alice", first["candidate_id"], "alice")
            second = self._ready_candidate(engine, "alice", 128)
            before = store.read_active("alice")

            with self.assertRaisesRegex(
                StrategyLabCapacityError,
                "每个账号最多允许 1 次策略激活/回滚",
            ):
                engine.activate_candidate("alice", second["candidate_id"], "alice")

        self.assertEqual(store.read_active("alice"), before)
        self.assertEqual(active["candidate_id"], first["candidate_id"])
        self.assertEqual(engine.summary("alice")["active"]["rollback_depth"], 1)

    @patch("ai_trade.strategy_lab.engine.BacktestEngine", _Backtest)
    def test_rollback_limit_is_clear_and_leaves_active_state_unchanged(self):
        store = StrategyLabStore(Path(self.temporary.name) / "strategy_lab")
        engine = StrategyLabEngine(self.config, store)

        with patch("ai_trade.strategy_lab.engine.MAX_TRANSITION_EVENTS_PER_OWNER", 1):
            candidate = self._ready_candidate(engine, "alice", 127)
            active = engine.activate_candidate(
                "alice", candidate["candidate_id"], "alice"
            )
            before = store.read_active("alice")

            with self.assertRaisesRegex(
                StrategyLabCapacityError,
                "每个账号最多允许 1 次策略激活/回滚",
            ):
                engine.rollback(
                    "alice",
                    "alice",
                    expected_active_candidate_id=active["candidate_id"],
                    expected_active_fingerprint=active["fingerprint"],
                )

        self.assertEqual(store.read_active("alice"), before)
        summary = engine.summary("alice")
        self.assertEqual(summary["active"]["candidate_id"], candidate["candidate_id"])
        self.assertEqual(summary["active"]["rollback_depth"], 1)

    def test_summary_returns_a_stable_recent_candidate_window(self):
        store = MagicMock()
        store.read_active.return_value = None
        store.read_validation.return_value = None
        store.read_approval.return_value = None
        store.read_export.return_value = None
        engine = StrategyLabEngine(self.config, store)
        snapshot = engine._configured_baseline()["snapshot"]

        def candidate(suffix: str, created_at: str) -> dict[str, object]:
            return {
                "candidate_id": f"cand_{suffix * 32}",
                "created_at": created_at,
                "baseline": snapshot,
                "changes": {},
                "candidate": snapshot,
                "title": suffix,
            }

        store.list_candidates.return_value = [
            candidate("a", "2026-07-14T09:00:00+00:00"),
            candidate("b", "2026-07-14T10:00:00+00:00"),
            candidate("c", "2026-07-14T10:00:00+00:00"),
        ]
        store.recent_events.return_value = (
            [
                {
                    "event_id": "event_" + "b" * 32,
                    "created_at": "2026-07-14T10:00:00+00:00",
                },
                {
                    "event_id": "event_" + "c" * 32,
                    "created_at": "2026-07-14T10:00:00+00:00",
                },
            ],
            3,
        )

        with (
            patch("ai_trade.strategy_lab.engine.SUMMARY_CANDIDATE_LIMIT", 2),
            patch("ai_trade.strategy_lab.engine.SUMMARY_HISTORY_LIMIT", 2),
        ):
            summary = engine.summary("alice")

        self.assertEqual(
            [item["candidate_id"] for item in summary["candidates"]],
            ["cand_" + "c" * 32, "cand_" + "b" * 32],
        )
        self.assertEqual(
            summary["candidate_summary"],
            {
                "total": 3,
                "count": 2,
                "limit": 2,
                "maximum": 100,
                "truncated": True,
            },
        )
        self.assertEqual(
            [item["event_id"] for item in summary["history"]],
            ["event_" + "b" * 32, "event_" + "c" * 32],
        )
        self.assertEqual(summary["history_total"], 3)
        self.assertEqual(summary["history_count"], 2)
        self.assertEqual(summary["history_limit"], 2)
        self.assertTrue(summary["history_truncated"])
        store.recent_events.assert_called_once_with("alice", 2)
        store.list_events.assert_not_called()

    def test_store_recent_events_returns_a_bounded_stable_window_and_total(self):
        store = StrategyLabStore(Path(self.temporary.name) / "strategy_lab")
        events = [
            ("1", "2026-07-14T09:00:00+00:00"),
            ("6", "2026-07-14T12:00:00+00:00"),
            ("3", "2026-07-14T10:00:00+00:00"),
            ("5", "2026-07-14T11:00:00+00:00"),
            ("4", "2026-07-14T11:00:00+00:00"),
            ("2", "2026-07-14T09:30:00+00:00"),
        ]
        for suffix, created_at in events:
            store.write_event(
                "alice",
                {
                    "event_id": f"event_{suffix * 32}",
                    "created_at": created_at,
                    "action": "test",
                },
            )

        recent, total = store.recent_events("alice", 3)

        self.assertEqual(total, 6)
        self.assertEqual(
            [event["event_id"] for event in recent],
            [
                "event_" + "4" * 32,
                "event_" + "5" * 32,
                "event_" + "6" * 32,
            ],
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
