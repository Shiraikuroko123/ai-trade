from __future__ import annotations

import csv
import json
import shutil
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from ai_trade.broker.base import BrokerEnvironment, BrokerOperation
from ai_trade.broker.sandbox import (
    SANDBOX_ADAPTER_NAME,
    SANDBOX_CAPABILITIES,
    SandboxCycleEngine,
)
from ai_trade.broker.scope import read_scope_manifest
from ai_trade.config import load_config


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _write_cache(path: Path, symbol: str, rows: list[tuple[str, float, float, float, float]]):
    path.mkdir(parents=True, exist_ok=True)
    with (path / f"{symbol}.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "open", "close", "high", "low", "volume", "amount"])
        for trade_date, open_, close, high, low in rows:
            writer.writerow([trade_date, open_, close, high, low, 1_000_000, 3_450_000])


class SandboxCycleTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        (root / "config").mkdir()
        for name in ("default.json", "security_master.json"):
            shutil.copy(
                REPOSITORY_ROOT / "config" / name, root / "config" / name
            )
        self.config = load_config(root / "config" / "default.json")
        self.symbol = self.config.instruments[0].symbol
        _write_cache(
            self.config.cache_dir,
            self.symbol,
            [
                ("2026-07-23", 3.40, 3.45, 3.48, 3.38),
                ("2026-07-24", 3.45, 3.50, 3.55, 3.42),
            ],
        )

    def test_capabilities_declare_a_sandbox_only_adapter(self):
        self.assertEqual(SANDBOX_CAPABILITIES.adapter_name, SANDBOX_ADAPTER_NAME)
        self.assertEqual(
            SANDBOX_CAPABILITIES.environments,
            frozenset({BrokerEnvironment.SANDBOX}),
        )
        self.assertNotIn(
            BrokerOperation.CANCEL_ORDERS, SANDBOX_CAPABILITIES.operations
        )
        self.assertFalse(SANDBOX_CAPABILITIES.qualifying_reconciliation_supported)
        with self.assertRaises(PermissionError):
            SANDBOX_CAPABILITIES.require(
                frozenset({BrokerOperation.SUBMIT_ORDERS}),
                BrokerEnvironment.LIVE,
            )

    def test_buy_cycle_fills_deterministically_and_binds_a_sandbox_scope(self):
        engine = SandboxCycleEngine(self.config)
        record = engine.cycle(self.symbol)

        # A buy at the session open always crosses the session low.
        self.assertEqual(record["outcome"]["status"], "FILLED")
        self.assertEqual(record["outcome"]["fill_price"], 3.45)
        self.assertGreater(record["outcome"]["commission"], 0.0)
        self.assertEqual(record["outcome"]["tax"], 0.0)
        self.assertEqual(record["session"], "2026-07-24")
        self.assertEqual(record["lifecycle"]["status"], "VERIFIED")
        self.assertEqual(record["lifecycle"]["open_order_count"], 0)
        self.assertEqual(record["lifecycle"]["fill_count"], 1)
        self.assertFalse(record["safety"]["qualifying_evidence"])
        self.assertFalse(record["safety"]["execution_enabled"])
        self.assertFalse(record["safety"]["promotion_countable"])

        scope = read_scope_manifest(engine.scope_path)
        self.assertEqual(scope.adapter, SANDBOX_ADAPTER_NAME)
        self.assertIs(scope.environment, BrokerEnvironment.SANDBOX)
        self.assertEqual(record["scope"]["scope_id"], scope.scope_id)
        self.assertTrue(engine.orders_path.is_file())
        self.assertTrue(engine.fills_path.is_file())

    def test_uncrossed_limit_expires_instead_of_filling(self):
        record = SandboxCycleEngine(self.config).cycle(
            self.symbol, limit_price=3.00
        )
        self.assertEqual(record["outcome"]["status"], "EXPIRED")
        self.assertIsNone(record["outcome"]["fill_price"])
        self.assertEqual(record["lifecycle"]["fill_count"], 0)
        self.assertEqual(record["lifecycle"]["status"], "VERIFIED")

    def test_sell_cycle_charges_stamp_duty_from_a_virtual_position(self):
        record = SandboxCycleEngine(self.config).cycle(
            self.symbol, side="SELL", session=date(2026, 7, 23)
        )
        self.assertEqual(record["outcome"]["status"], "FILLED")
        expected_tax = (
            100
            * 3.40
            * self.config.costs.for_instrument(
                self.config.instruments[0], date(2026, 7, 23)
            ).sell_stamp_duty_bps
            / 10_000.0
        )
        self.assertAlmostEqual(record["outcome"]["tax"], expected_tax)

    def test_promotion_countable_evidence_is_never_written(self):
        engine = SandboxCycleEngine(self.config)
        record = engine.cycle(self.symbol)
        for entry in record["protected_evidence"]:
            self.assertTrue(entry["unchanged"], entry)
            self.assertIsNone(entry["digest"], entry)
        self.assertFalse(self.config.broker_reconciliation_file.exists())
        self.assertFalse(self.config.broker_orders_file.exists())
        self.assertFalse(self.config.broker_fills_file.exists())
        self.assertFalse(self.config.broker_ledger_scope_file.exists())
        names = {entry["name"] for entry in record["protected_evidence"]}
        self.assertIn("broker_reconciliation_file", names)

    def test_mandate_rejects_a_symbol_outside_the_universe(self):
        with self.assertRaisesRegex(ValueError, "outside the configured universe"):
            SandboxCycleEngine(self.config).cycle("999999")

    def test_drills_are_immutable_and_tamper_evident(self):
        engine = SandboxCycleEngine(self.config)
        record = engine.cycle(self.symbol)
        listing = engine.list_drills()
        self.assertEqual(listing["summary"]["total"], 1)
        self.assertTrue(listing["drills"][0]["protected_evidence_unchanged"])

        fetched = engine.get_drill(record["drill_id"])
        self.assertEqual(fetched["record_fingerprint"], record["record_fingerprint"])

        path = engine.drills_root / f"{record['drill_id']}.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["outcome"]["status"] = "EXPIRED"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "fingerprint"):
            engine.get_drill(record["drill_id"])

    def test_repeated_cycles_share_one_scoped_ledger(self):
        engine = SandboxCycleEngine(self.config)
        first = engine.cycle(self.symbol)
        second = engine.cycle(self.symbol, limit_price=3.00)
        self.assertNotEqual(
            first["order"]["client_order_id"], second["order"]["client_order_id"]
        )
        self.assertEqual(second["lifecycle"]["order_count"], 2)
        self.assertEqual(second["lifecycle"]["fill_count"], 1)
        status = engine.status()
        self.assertEqual(status["lifecycle"]["status"], "VERIFIED")
        self.assertEqual(status["lifecycle"]["scope"]["status"], "BOUND")

    def test_status_is_safe_before_any_drill(self):
        status = SandboxCycleEngine(self.config).status()
        self.assertEqual(status["lifecycle"]["status"], "EMPTY")
        self.assertFalse(status["safety"]["execution_enabled"])


if __name__ == "__main__":
    import unittest

    unittest.main()
