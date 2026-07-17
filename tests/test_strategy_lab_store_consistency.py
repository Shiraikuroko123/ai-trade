from __future__ import annotations

import json
import multiprocessing
from pathlib import Path
import time
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import ai_trade.strategy_lab.store as store_module
from ai_trade.strategy_lab.store import (
    MAX_APPROVAL_RECORD_BYTES,
    MAX_CANDIDATE_RECORD_BYTES,
    MAX_MONITOR_RECORD_BYTES,
    MAX_VALIDATION_RECORD_BYTES,
    StrategyLabCapacityError,
    StrategyLabConflictError,
    StrategyLabStore,
)


def _active_record(
    candidate_id: str | None,
    fingerprint: str,
    rollback_stack: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    marker = candidate_id or "baseline"
    return {
        "candidate_id": candidate_id,
        "fingerprint": fingerprint,
        "snapshot": {"strategy": {"marker": marker}, "risk": {}},
        "activated_at": "2026-07-14T00:00:00Z",
        "activated_by": "test",
        "rollback_stack": list(rollback_stack or []),
    }


def _rollback_entry(active: dict[str, object]) -> dict[str, object]:
    return {
        "candidate_id": active["candidate_id"],
        "fingerprint": active["fingerprint"],
        "snapshot": active["snapshot"],
    }


def _process_transition(
    root: str,
    ready: multiprocessing.Queue,
    start: multiprocessing.Event,
    suffix: str,
) -> None:
    store = StrategyLabStore(root)
    ready.put(suffix)
    if not start.wait(10):
        raise RuntimeError("Timed out waiting to start the transition")

    candidate_id = f"cand_{suffix * 32}"
    event_id = f"event_{suffix * 32}"

    def transition(
        current: dict[str, object] | None,
    ) -> tuple[dict[str, object], dict[str, object]]:
        if current is None:
            raise RuntimeError("The shared baseline disappeared")
        time.sleep(0.2)
        stack = list(current.get("rollback_stack", []))
        stack.append(_rollback_entry(current))
        active = _active_record(candidate_id, suffix * 64, stack)
        event = {
            "event_id": event_id,
            "action": "activate",
            "created_at": f"2026-07-14T00:00:0{suffix}Z",
            "candidate_id": candidate_id,
        }
        return active, event

    store.transition_active("alice", transition)


class StrategyLabStoreConsistencyTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "strategy_lab"
        self.store = StrategyLabStore(self.root)

    def test_read_active_recovers_an_interrupted_event_and_active_commit(self):
        initial = _active_record(None, "0" * 64)
        intended = _active_record(
            "cand_" + "1" * 32,
            "1" * 64,
            [_rollback_entry(initial)],
        )
        event = {
            "event_id": "event_" + "1" * 32,
            "action": "activate",
            "created_at": "2026-07-14T00:00:01Z",
            "candidate_id": intended["candidate_id"],
        }
        self.store.write_active("alice", initial)
        original_atomic_write = store_module._atomic_write_json

        def fail_active_write(path, value, *, replace_existing=True):
            if path.name == "active.json":
                raise OSError("simulated process interruption")
            return original_atomic_write(path, value, replace_existing=replace_existing)

        with (
            patch.object(
                store_module, "_atomic_write_json", side_effect=fail_active_write
            ),
            self.assertRaisesRegex(OSError, "simulated process interruption"),
        ):
            self.store.transition_active("alice", lambda _current: (intended, event))

        owner_directory = self.store.owner_directory("alice")
        transaction_path = owner_directory / ".active-transition.json"
        event_path = owner_directory / "events" / f"{event['event_id']}.json"
        self.assertTrue(transaction_path.exists())
        self.assertTrue(event_path.exists())
        self.assertEqual(
            json.loads((owner_directory / "active.json").read_text(encoding="utf-8")),
            initial,
        )

        restarted = StrategyLabStore(self.root)
        self.assertEqual(restarted.read_active("alice"), intended)
        self.assertFalse(transaction_path.exists())
        self.assertEqual(restarted.list_events("alice"), [event])

    def test_transition_recovers_prior_transaction_before_building_next_stack(self):
        initial = _active_record(None, "0" * 64)
        first = _active_record(
            "cand_" + "1" * 32,
            "1" * 64,
            [_rollback_entry(initial)],
        )
        first_event = {
            "event_id": "event_" + "1" * 32,
            "action": "activate",
            "created_at": "2026-07-14T00:00:01Z",
            "candidate_id": first["candidate_id"],
        }
        self.store.write_active("alice", initial)
        original_atomic_write = store_module._atomic_write_json

        def fail_active_write(path, value, *, replace_existing=True):
            if path.name == "active.json":
                raise OSError("simulated process interruption")
            return original_atomic_write(path, value, replace_existing=replace_existing)

        with (
            patch.object(
                store_module, "_atomic_write_json", side_effect=fail_active_write
            ),
            self.assertRaises(OSError),
        ):
            self.store.transition_active("alice", lambda _current: (first, first_event))

        second_event = {
            "event_id": "event_" + "2" * 32,
            "action": "activate",
            "created_at": "2026-07-14T00:00:02Z",
            "candidate_id": "cand_" + "2" * 32,
        }

        def second_transition(current):
            self.assertEqual(current, first)
            stack = list(current["rollback_stack"])
            stack.append(_rollback_entry(current))
            return (
                _active_record("cand_" + "2" * 32, "2" * 64, stack),
                second_event,
            )

        restarted = StrategyLabStore(self.root)
        active = restarted.transition_active("alice", second_transition)

        self.assertEqual(len(active["rollback_stack"]), 2)
        self.assertEqual(
            [item["candidate_id"] for item in active["rollback_stack"]],
            [None, first["candidate_id"]],
        )
        self.assertEqual(
            {event["event_id"] for event in restarted.list_events("alice")},
            {first_event["event_id"], second_event["event_id"]},
        )

    def test_two_processes_preserve_the_complete_shared_rollback_stack(self):
        initial = _active_record(None, "0" * 64)
        self.store.write_active("alice", initial)
        context = multiprocessing.get_context("spawn")
        ready = context.Queue()
        start = context.Event()
        processes = [
            context.Process(
                target=_process_transition,
                args=(str(self.root), ready, start, suffix),
            )
            for suffix in ("1", "2")
        ]
        try:
            for process in processes:
                process.start()
            self.assertEqual({ready.get(timeout=15), ready.get(timeout=15)}, {"1", "2"})
            start.set()
            for process in processes:
                process.join(15)
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
                    self.fail("A strategy-lab transition process did not finish")
            self.assertEqual([process.exitcode for process in processes], [0, 0])
        finally:
            for process in processes:
                if process.is_alive():
                    process.terminate()
                    process.join(5)
            ready.close()
            ready.join_thread()

        active = self.store.read_active("alice")
        self.assertIsNotNone(active)
        lineage = [item["candidate_id"] for item in active["rollback_stack"]] + [
            active["candidate_id"]
        ]
        self.assertEqual(
            set(lineage),
            {None, "cand_" + "1" * 32, "cand_" + "2" * 32},
        )
        self.assertEqual(len(active["rollback_stack"]), 2)
        self.assertEqual(len(self.store.list_events("alice")), 2)

    def test_candidate_limit_is_checked_inside_the_owner_lock(self):
        first = {"candidate_id": "cand_" + "1" * 32}
        second = {"candidate_id": "cand_" + "2" * 32}
        self.store.write_candidate("alice", first, max_records=1)

        with self.assertRaisesRegex(
            StrategyLabCapacityError, "candidate limit reached \(1\)"
        ):
            self.store.write_candidate("alice", second, max_records=1)

    def test_transition_limit_fails_before_active_journal_or_event_changes(self):
        initial = _active_record(None, "0" * 64)
        self.store.write_active("alice", initial)
        self.store.write_event(
            "alice",
            {
                "event_id": "event_" + "1" * 32,
                "action": "activate",
                "created_at": "2026-07-14T00:00:01Z",
            },
        )
        intended = _active_record("cand_" + "2" * 32, "2" * 64)
        new_event = {
            "event_id": "event_" + "2" * 32,
            "action": "rollback",
            "created_at": "2026-07-14T00:00:02Z",
        }
        owner_directory = self.store.owner_directory("alice")
        active_path = owner_directory / "active.json"
        active_before = active_path.read_bytes()

        with self.assertRaisesRegex(
            StrategyLabCapacityError, "transition event limit reached \(1\)"
        ):
            self.store.transition_active(
                "alice",
                lambda _current: (intended, new_event),
                max_transition_events=1,
            )

        self.assertEqual(active_path.read_bytes(), active_before)
        self.assertFalse((owner_directory / ".active-transition.json").exists())
        self.assertFalse(
            (owner_directory / "events" / f"{new_event['event_id']}.json").exists()
        )
        self.assertEqual(len(self.store.list_events("alice")), 1)

    def test_read_only_cas_remains_available_at_the_transition_limit(self):
        initial = _active_record(None, "0" * 64)
        self.store.write_active("alice", initial)
        self.store.write_event(
            "alice",
            {
                "event_id": "event_" + "1" * 32,
                "action": "rollback",
                "created_at": "2026-07-14T00:00:01Z",
            },
        )

        active = self.store.transition_active(
            "alice",
            lambda current: (current, None),
            expected_active_fingerprint="0" * 64,
            max_transition_events=1,
        )

        self.assertEqual(active, initial)
        self.assertEqual(self.store.read_active("alice"), initial)
        self.assertEqual(len(self.store.list_events("alice")), 1)

    def test_active_fingerprint_mismatch_is_a_conflict(self):
        initial = _active_record(None, "0" * 64)
        self.store.write_active("alice", initial)
        transition_called = False

        def transition(current):
            nonlocal transition_called
            transition_called = True
            return current, None

        with self.assertRaises(StrategyLabConflictError) as raised:
            self.store.transition_active(
                "alice",
                transition,
                expected_active_fingerprint="1" * 64,
            )

        self.assertNotIsInstance(raised.exception, StrategyLabCapacityError)
        self.assertFalse(transition_called)

    def test_strategy_records_use_bounded_unique_json_and_allowlisted_fields(self):
        owner_directory = self.store.owner_directory("alice")
        cases = (
            (
                "candidates",
                "cand_" + "1" * 32,
                MAX_CANDIDATE_RECORD_BYTES,
                self.store.read_candidate,
            ),
            (
                "validations",
                "cand_" + "2" * 32,
                MAX_VALIDATION_RECORD_BYTES,
                self.store.read_validation,
            ),
            (
                "approvals",
                "cand_" + "3" * 32,
                MAX_APPROVAL_RECORD_BYTES,
                self.store.read_approval,
            ),
            (
                "monitors",
                "monitor_" + "4" * 32,
                MAX_MONITOR_RECORD_BYTES,
                self.store.read_monitor,
            ),
        )
        for directory, record_id, limit, reader in cases:
            path = owner_directory / directory / f"{record_id}.json"
            path.parent.mkdir(parents=True, exist_ok=True)

            path.write_text('{"value": 1, "value": 2}', encoding="utf-8")
            with self.subTest(directory=directory, failure="duplicate"):
                with self.assertRaisesRegex(RuntimeError, "duplicate JSON object key"):
                    reader("alice", record_id)

            path.write_bytes(b" " * (limit + 1))
            with self.subTest(directory=directory, failure="oversized"):
                with self.assertRaisesRegex(RuntimeError, "exceeds"):
                    reader("alice", record_id)

            path.write_text('{"unexpected": true}', encoding="utf-8")
            with self.subTest(directory=directory, failure="unknown field"):
                with self.assertRaisesRegex(RuntimeError, "schema fields"):
                    reader("alice", record_id)


if __name__ == "__main__":
    import unittest

    unittest.main()
