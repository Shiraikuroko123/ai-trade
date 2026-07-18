from __future__ import annotations

import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ai_trade.broker.base import (
    BrokerFill,
    BrokerOrderRequest,
    BrokerOrderSnapshot,
    OrderSide,
    OrderStatus,
)
from ai_trade.broker.ledger import append_broker_observation, reserve_order_intents
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
        self.assertEqual(lifecycle["status"], "RECOVERED")
        self.assertEqual(lifecycle["scope"]["status"], "UNSCOPED")
        self.assertEqual(lifecycle["order_count"], 1)
        self.assertEqual(lifecycle["orders"][0]["status"], "FILLED")
        self.assertNotIn("broker_orders", payload)
        self.assertEqual(len(payload["broker_fills"]), 1)
        self.assertEqual(payload["broker_fills"][0]["fill_id"], "fill-web-1")
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
        self.assertNotIn("broker_orders", payload)
        self.assertEqual(payload["broker_fills"], [])
        self.assertFalse(lifecycle["qualifying_evidence"])
        self.assertFalse(lifecycle["execution_enabled"])

    def test_trading_payload_keeps_unconfirmed_submission_visible(self):
        reserve_order_intents(
            self.config.broker_orders_file,
            [
                BrokerOrderRequest(
                    "client-web-unconfirmed",
                    "510300",
                    OrderSide.BUY,
                    100,
                    4.0,
                )
            ],
            date(2026, 7, 18),
            10_000.0,
        )

        lifecycle = DashboardService(self.config).trading()["broker_lifecycle"]

        self.assertEqual(lifecycle["status"], "RECOVERED")
        self.assertEqual(lifecycle["submission_unconfirmed_count"], 1)
        self.assertTrue(lifecycle["orders"][0]["submission_unconfirmed"])
        self.assertEqual(
            lifecycle["recovery_warnings"][0]["code"],
            "submission_unconfirmed",
        )

    def test_invalid_fill_rows_are_not_returned_to_the_browser(self):
        self.config.broker_fills_file.parent.mkdir(parents=True, exist_ok=True)
        self.config.broker_fills_file.write_text(
            "fill_id,price\nforged,999\n",
            encoding="utf-8",
        )

        payload = DashboardService(self.config).trading()

        lifecycle = payload["broker_lifecycle"]
        self.assertEqual(lifecycle["status"], "INTEGRITY_ERROR")
        self.assertEqual(
            lifecycle["integrity_errors"][0]["code"], "fill_ledger_invalid"
        )
        self.assertEqual(payload["broker_fills"], [])
        self.assertNotIn("forged", json.dumps(payload))

    def test_browser_payload_omits_broker_account_and_control_paths(self):
        secret_account = "private-broker-account-123456"
        raw = {
            **self.config.raw,
            "broker": {
                **self.config.raw.get("broker", {}),
                "mode": "sandbox",
                "adapter": "qmt-readonly",
                "account_id": secret_account,
            },
        }
        config = replace(self.config, raw=raw)
        service = DashboardService(config)

        for surface, payload in (
            ("overview", service.overview()),
            ("trading", service.trading()),
        ):
            with self.subTest(surface=surface):
                live = payload["live"]
                rendered = json.dumps(payload, ensure_ascii=False)
                self.assertTrue(live["checks"]["account_configured"])
                self.assertNotIn("account_id", live)
                self.assertNotIn("kill_switch_file", live)
                self.assertNotIn("batch_approval_file", live)
                self.assertNotIn(secret_account, rendered)
                self.assertNotIn(str(config.live_kill_switch_file), rendered)
                self.assertNotIn(str(config.live_batch_approval_file), rendered)


if __name__ == "__main__":
    unittest.main()
