from __future__ import annotations

import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ai_trade.broker.base import (
    BrokerFill,
    BrokerOrderSnapshot,
    OrderSide,
    OrderStatus,
)
from ai_trade.broker.ledger import append_broker_observation
from ai_trade.config import load_config
from ai_trade.web.service import DashboardService


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class WebBrokerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        root = Path(self.temporary.name)
        source = load_config(REPOSITORY_ROOT / "config" / "default.json")
        self.config = replace(source, project_root=root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_trading_payload_exposes_recovered_states_without_authority(self):
        submitted_at = datetime(2026, 7, 17, 1, 30, tzinfo=timezone.utc)
        filled_at = submitted_at + timedelta(minutes=2)
        submitted = BrokerOrderSnapshot(
            "client-web-1",
            "broker-web-1",
            "510300",
            OrderSide.BUY,
            100,
            0,
            4.0,
            None,
            OrderStatus.SUBMITTED,
            submitted_at,
        )
        completed = BrokerOrderSnapshot(
            "client-web-1",
            "broker-web-1",
            "510300",
            OrderSide.BUY,
            100,
            100,
            4.0,
            3.99,
            OrderStatus.FILLED,
            filled_at,
        )
        fill = BrokerFill(
            "fill-web-1",
            "broker-web-1",
            "client-web-1",
            "510300",
            OrderSide.BUY,
            100,
            3.99,
            5.0,
            0.0,
            filled_at,
        )
        append_broker_observation(
            self.config.broker_orders_file,
            self.config.broker_fills_file,
            [submitted, completed],
            [fill],
        )

        payload = DashboardService(self.config).trading()

        lifecycle = payload["broker_lifecycle"]
        self.assertEqual(lifecycle["status"], "VERIFIED")
        self.assertEqual(lifecycle["order_count"], 1)
        self.assertEqual(lifecycle["orders"][0]["status"], "FILLED")
        self.assertEqual(len(payload["broker_orders"]), 2)
        self.assertEqual(len(payload["broker_fills"]), 1)
        self.assertFalse(lifecycle["qualifying_evidence"])
        self.assertFalse(lifecycle["execution_enabled"])
        self.assertFalse(payload["live"]["live_ready"])

    def test_corrupt_ledger_returns_an_explicit_non_authoritative_error(self):
        self.config.broker_orders_file.parent.mkdir(parents=True, exist_ok=True)
        self.config.broker_orders_file.write_text(
            "event_id,status\ninvalid,FILLED\n",
            encoding="utf-8",
        )

        payload = DashboardService(self.config).trading()

        lifecycle = payload["broker_lifecycle"]
        self.assertEqual(lifecycle["status"], "INTEGRITY_ERROR")
        self.assertEqual(
            lifecycle["integrity_errors"][0]["code"], "order_ledger_invalid"
        )
        self.assertEqual(lifecycle["orders"], [])
        self.assertFalse(lifecycle["qualifying_evidence"])
        self.assertFalse(lifecycle["execution_enabled"])


if __name__ == "__main__":
    unittest.main()
