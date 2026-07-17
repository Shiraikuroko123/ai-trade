from __future__ import annotations

import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ai_trade.broker.base import (
    BrokerEnvironment,
    BrokerFill,
    BrokerOrderSnapshot,
    OrderSide,
    OrderStatus,
)
from ai_trade.broker.ledger import (
    append_broker_observation,
    append_order_events,
    initialize_broker_ledger_scope,
    recover_order_lifecycle,
)
from ai_trade.broker.scope import create_broker_ledger_scope, read_scope_manifest


START = datetime(2026, 7, 18, 1, 0, tzinfo=timezone.utc)
CONFIG_FINGERPRINT = "a" * 64


def _scope(root: Path, **overrides):
    values = {
        "adapter": "sandbox-adapter",
        "account_id": "private-account-123456",
        "environment": BrokerEnvironment.SANDBOX,
        "config_fingerprint": CONFIG_FINGERPRINT,
        "orders_path": root / "orders.csv",
        "fills_path": root / "fills.csv",
    }
    values.update(overrides)
    return create_broker_ledger_scope(**values)


def _observations():
    submitted = BrokerOrderSnapshot(
        "scope-client",
        "scope-broker",
        "510300",
        OrderSide.BUY,
        100,
        0,
        10.0,
        None,
        OrderStatus.SUBMITTED,
        START,
    )
    filled = BrokerOrderSnapshot(
        "scope-client",
        "scope-broker",
        "510300",
        OrderSide.BUY,
        100,
        100,
        10.0,
        10.0,
        OrderStatus.FILLED,
        START + timedelta(minutes=1),
    )
    fill = BrokerFill(
        "scope-fill",
        "scope-broker",
        "scope-client",
        "510300",
        OrderSide.BUY,
        100,
        10.0,
        1.0,
        0.0,
        START + timedelta(minutes=1),
    )
    return [submitted, filled], [fill]


class BrokerLedgerScopeTests(unittest.TestCase):
    def test_scoped_observation_is_bound_without_exposing_the_account(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orders = root / "orders.csv"
            fills = root / "fills.csv"
            manifest = root / "scope.json"
            scope = _scope(root)
            order_events, fill_events = _observations()

            append_broker_observation(
                orders,
                fills,
                order_events,
                fill_events,
                scope_path=manifest,
                scope=scope,
            )
            report = recover_order_lifecycle(
                orders,
                fills,
                scope_path=manifest,
                expected_scope=scope,
            )

            self.assertEqual(report["status"], "VERIFIED")
            self.assertEqual(report["scope"]["status"], "BOUND")
            self.assertEqual(report["scope"]["adapter"], "sandbox-adapter")
            self.assertEqual(report["scope"]["environment"], "sandbox")
            self.assertEqual(len(report["scope"]["account_reference"]), 12)
            self.assertNotIn("private-account-123456", manifest.read_text("utf-8"))
            self.assertNotIn("private-account-123456", json.dumps(report))
            self.assertFalse(report["qualifying_evidence"])
            self.assertFalse(report["execution_enabled"])

    def test_scope_manifest_rejects_ambiguous_or_unbounded_json(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "scope.json"
            cases = {
                "duplicate key": b'{"schema_version":1,"schema_version":1}',
                "invalid utf8": b"\xff\xfe",
                "oversized": b" " * (64 * 1024 + 1),
            }
            for label, content in cases.items():
                with self.subTest(label=label):
                    path.write_bytes(content)
                    with self.assertRaisesRegex(RuntimeError, "cannot be read"):
                        read_scope_manifest(path)

    def test_unscoped_legacy_ledgers_remain_readable_but_cannot_be_extended(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orders = root / "orders.csv"
            fills = root / "fills.csv"
            manifest = root / "scope.json"
            scope = _scope(root)
            order_events, _ = _observations()
            append_order_events(orders, [order_events[0]])

            report = recover_order_lifecycle(
                orders,
                fills,
                scope_path=manifest,
                expected_scope=scope,
            )

            self.assertEqual(report["status"], "RECOVERED")
            self.assertEqual(report["scope"]["status"], "UNSCOPED")
            self.assertEqual(report["order_count"], 1)
            self.assertFalse(report["qualifying_evidence"])
            with self.assertRaisesRegex(RuntimeError, "archive them"):
                initialize_broker_ledger_scope(
                    manifest,
                    orders,
                    fills,
                    scope,
                )
            self.assertFalse(manifest.exists())

    def test_wrong_account_or_configuration_cannot_append_to_bound_ledgers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orders = root / "orders.csv"
            fills = root / "fills.csv"
            manifest = root / "scope.json"
            scope = _scope(root)
            order_events, fill_events = _observations()
            append_broker_observation(
                orders,
                fills,
                order_events,
                fill_events,
                scope_path=manifest,
                scope=scope,
            )
            before = orders.read_bytes()
            changed_account = _scope(root, account_id="different-account")
            changed_config = _scope(root, config_fingerprint="b" * 64)
            later = BrokerOrderSnapshot(
                "other-client",
                "other-broker",
                "510300",
                OrderSide.BUY,
                100,
                0,
                10.0,
                None,
                OrderStatus.SUBMITTED,
                START + timedelta(minutes=2),
            )

            for candidate in (changed_account, changed_config):
                with self.subTest(scope=candidate.scope_id):
                    with self.assertRaisesRegex(RuntimeError, "does not match"):
                        append_order_events(
                            orders,
                            [later],
                            scope_path=manifest,
                            scope=candidate,
                        )
                    self.assertEqual(orders.read_bytes(), before)

            mismatch = recover_order_lifecycle(
                orders,
                fills,
                scope_path=manifest,
                expected_scope=changed_account,
            )
            self.assertEqual(mismatch["status"], "INTEGRITY_ERROR")
            self.assertEqual(mismatch["scope"]["status"], "MISMATCH")
            self.assertEqual(mismatch["scope"]["mismatch_dimensions"], ["account"])
            self.assertNotIn("different-account", json.dumps(mismatch))

    def test_wrong_environment_or_ledger_path_cannot_reuse_a_bound_scope(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orders = root / "orders.csv"
            fills = root / "fills.csv"
            manifest = root / "scope.json"
            scope = _scope(root)
            order_events, fill_events = _observations()
            append_broker_observation(
                orders,
                fills,
                order_events,
                fill_events,
                scope_path=manifest,
                scope=scope,
            )
            later = BrokerOrderSnapshot(
                "later-client",
                "later-broker",
                "510300",
                OrderSide.BUY,
                100,
                0,
                10.0,
                None,
                OrderStatus.SUBMITTED,
                START + timedelta(minutes=2),
            )
            changed_environment = _scope(
                root,
                environment=BrokerEnvironment.LIVE,
            )
            other_orders = root / "other-orders.csv"
            changed_path = _scope(root, orders_path=other_orders)

            with self.assertRaisesRegex(RuntimeError, "does not match"):
                append_order_events(
                    orders,
                    [later],
                    scope_path=manifest,
                    scope=changed_environment,
                )
            with self.assertRaisesRegex(RuntimeError, "does not match"):
                append_order_events(
                    other_orders,
                    [later],
                    scope_path=manifest,
                    scope=changed_path,
                )

            self.assertFalse(other_orders.exists())
            environment_report = recover_order_lifecycle(
                orders,
                fills,
                scope_path=manifest,
                expected_scope=changed_environment,
            )
            path_report = recover_order_lifecycle(
                orders,
                fills,
                scope_path=manifest,
                expected_scope=changed_path,
            )
            self.assertEqual(
                environment_report["scope"]["mismatch_dimensions"],
                ["environment"],
            )
            self.assertEqual(
                path_report["scope"]["mismatch_dimensions"],
                ["order_ledger_path"],
            )

    def test_manifest_tampering_fails_closed_without_hiding_recovered_orders(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orders = root / "orders.csv"
            fills = root / "fills.csv"
            manifest = root / "scope.json"
            scope = _scope(root)
            order_events, fill_events = _observations()
            append_broker_observation(
                orders,
                fills,
                order_events,
                fill_events,
                scope_path=manifest,
                scope=scope,
            )
            payload = json.loads(manifest.read_text("utf-8"))
            payload["environment"] = "live"
            manifest.write_text(json.dumps(payload), encoding="utf-8")

            report = recover_order_lifecycle(
                orders,
                fills,
                scope_path=manifest,
                expected_scope=scope,
            )

            self.assertEqual(report["status"], "INTEGRITY_ERROR")
            self.assertEqual(report["scope"]["status"], "INVALID")
            self.assertEqual(report["order_count"], 1)
            self.assertTrue(report["orders"][0]["integrity_ok"])

    def test_parallel_initialization_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orders = root / "orders.csv"
            fills = root / "fills.csv"
            manifest = root / "scope.json"
            scope = _scope(root)

            with ThreadPoolExecutor(max_workers=8) as executor:
                list(
                    executor.map(
                        lambda _: initialize_broker_ledger_scope(
                            manifest,
                            orders,
                            fills,
                            scope,
                        ),
                        range(24),
                    )
                )

            payload = json.loads(manifest.read_text("utf-8"))
            self.assertEqual(payload["scope_id"], scope.scope_id)

    def test_parallel_conflicting_initialization_has_one_durable_winner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orders = root / "orders.csv"
            fills = root / "fills.csv"
            manifest = root / "scope.json"
            first = _scope(root, account_id="first-account")
            second = _scope(root, account_id="second-account")

            def initialize(candidate):
                try:
                    initialize_broker_ledger_scope(
                        manifest,
                        orders,
                        fills,
                        candidate,
                    )
                except RuntimeError:
                    return candidate.scope_id, False
                return candidate.scope_id, True

            candidates = [first, second] * 12
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(initialize, candidates))

            winner = json.loads(manifest.read_text("utf-8"))["scope_id"]
            self.assertIn(winner, {first.scope_id, second.scope_id})
            self.assertTrue(any(scope_id == winner and ok for scope_id, ok in results))
            self.assertTrue(
                all(scope_id == winner for scope_id, ok in results if ok)
            )
            self.assertTrue(any(not ok for _, ok in results))

    def test_manifest_replace_failure_leaves_no_partial_scope_or_ledgers(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orders = root / "orders.csv"
            fills = root / "fills.csv"
            manifest = root / "scope.json"
            scope = _scope(root)

            with patch(
                "ai_trade.broker.scope.os.replace",
                side_effect=OSError("injected scope replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected scope"):
                    initialize_broker_ledger_scope(
                        manifest,
                        orders,
                        fills,
                        scope,
                    )

            self.assertFalse(manifest.exists())
            self.assertFalse(orders.exists())
            self.assertFalse(fills.exists())
            self.assertEqual(list(root.glob(".scope.json.*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
