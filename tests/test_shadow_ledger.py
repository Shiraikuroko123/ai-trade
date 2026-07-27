from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ai_trade.broker import (
    ShadowEventLedger,
    ShadowLedgerConflictError,
    project_shadow_account,
    reconcile_shadow_projection,
)


class ShadowEventLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.ledger = ShadowEventLedger(
            Path(self.temporary.name) / "shadow_ledger",
            "broker-account-001",
        )
        self.session = date(2026, 7, 24)
        self.occurred = datetime(2026, 7, 24, 7, 0, tzinfo=timezone.utc)

    def _append(self, event_type, external_id, payload, minutes=0):
        return self.ledger.append(
            event_type,
            occurred_at=self.occurred + timedelta(minutes=minutes),
            trading_session=self.session,
            source="qmt-export",
            external_id=external_id,
            payload=payload,
        )

    def _opening(self):
        return self._append(
            "opening_balance",
            "opening-1",
            {"cash": "10000", "currency": "CNY"},
        )

    def test_replays_cash_positions_cost_basis_equity_and_reconciliation(self):
        self._opening()
        self._append(
            "fill",
            "fill-buy-1",
            {
                "symbol": "510300",
                "side": "BUY",
                "quantity": 100,
                "price": "10",
                "commission": "5",
                "stamp_duty": "0",
                "transfer_fee": "0",
                "order_id": "order-1",
                "portfolio_plan_id": "pp_" + "1" * 32,
                "prediction_snapshot_id": "ps_" + "2" * 32,
                "model_artifact_id": "ma_" + "3" * 32,
            },
            minutes=1,
        )
        self._append(
            "cash_deposit",
            "deposit-1",
            {"amount": "100", "currency": "CNY"},
            minutes=2,
        )
        self._append(
            "fill",
            "fill-sell-1",
            {
                "symbol": "510300",
                "side": "SELL",
                "quantity": 40,
                "price": "11",
                "commission": "5",
                "stamp_duty": "0",
                "transfer_fee": "0",
                "order_id": "order-2",
                "portfolio_plan_id": None,
                "prediction_snapshot_id": None,
                "model_artifact_id": None,
            },
            minutes=3,
        )
        self._append(
            "mark",
            "mark-1",
            {"symbol": "510300", "price": "12"},
            minutes=4,
        )
        self._append(
            "account_snapshot",
            "snapshot-1",
            {"cash": "9530", "positions": {"510300": "60"}},
            minutes=5,
        )

        projection = project_shadow_account(self.ledger)
        self.assertEqual(projection["cash"], "9530")
        self.assertEqual(projection["positions"]["510300"]["quantity"], "60")
        self.assertEqual(projection["positions"]["510300"]["average_cost"], "10.05")
        self.assertEqual(projection["realized_pnl"], "33")
        self.assertEqual(projection["fees"], "10")
        self.assertEqual(projection["equity"], "10250")
        self.assertEqual(projection["missing_marks"], [])
        self.assertFalse(
            projection["safety"]["qualifying_broker_sandbox_evidence"]
        )

        clean = reconcile_shadow_projection(
            projection,
            broker_cash="9530.001",
            broker_positions={"510300": "60"},
        )
        self.assertTrue(clean["clean"])
        tampered_projection = json.loads(json.dumps(projection))
        tampered_projection["realized_pnl"] = "34"
        with self.assertRaisesRegex(ValueError, "projection fingerprint"):
            reconcile_shadow_projection(
                tampered_projection,
                broker_cash="9530",
                broker_positions={"510300": "60"},
            )
        mismatch = reconcile_shadow_projection(
            projection,
            broker_cash="9500",
            broker_positions={"510300": "50"},
        )
        self.assertFalse(mismatch["clean"])
        self.assertEqual(
            [item["kind"] for item in mismatch["issues"]],
            ["cash", "position"],
        )

    def test_duplicate_is_idempotent_and_conflicting_external_id_fails(self):
        first = self._opening()
        second = self._opening()
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["event_id"], second["event_id"])
        self.assertEqual(len(self.ledger.events()), 1)

        with self.assertRaises(ShadowLedgerConflictError):
            self._append(
                "opening_balance",
                "opening-1",
                {"cash": "9999", "currency": "CNY"},
            )

    def test_corporate_action_and_mark_rebuild_exact_equity(self):
        self._opening()
        self._append(
            "position_adjustment",
            "migration-position",
            {
                "symbol": "510300",
                "quantity_delta": "60",
                "cash_delta": "-600",
                "reason": "legacy opening position migration",
            },
            minutes=1,
        )
        self._append(
            "corporate_action",
            "split-1",
            {
                "symbol": "510300",
                "quantity_multiplier": "2",
                "cash_per_share": "0.5",
                "reason": "2-for-1 split with cash distribution",
            },
            minutes=2,
        )
        self._append(
            "mark",
            "mark-after-split",
            {"symbol": "510300", "price": "6"},
            minutes=3,
        )

        projection = project_shadow_account(self.ledger)
        self.assertEqual(projection["cash"], "9430")
        self.assertEqual(projection["positions"]["510300"]["quantity"], "120")
        self.assertEqual(projection["positions"]["510300"]["average_cost"], "5")
        self.assertEqual(projection["equity"], "10150")

    def test_tampered_event_and_oversell_fail_closed(self):
        opening = self._opening()
        path = next(self.ledger.events_root.glob("*.json"))
        value = json.loads(path.read_text(encoding="utf-8"))
        value["payload"]["cash"] = "10001"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "fingerprint"):
            self.ledger.events()

        other = ShadowEventLedger(
            Path(self.temporary.name) / "other-ledger", "other-account"
        )
        other.append(
            "opening_balance",
            occurred_at=self.occurred,
            trading_session=self.session,
            source="fixture",
            external_id="opening",
            payload={"cash": "1000", "currency": "CNY"},
        )
        other.append(
            "fill",
            occurred_at=self.occurred + timedelta(minutes=1),
            trading_session=self.session,
            source="fixture",
            external_id="oversell",
            payload={
                "symbol": "510300",
                "side": "SELL",
                "quantity": 100,
                "price": "10",
                "commission": "5",
                "stamp_duty": "0",
                "transfer_fee": "0",
                "order_id": "order",
                "portfolio_plan_id": None,
                "prediction_snapshot_id": None,
                "model_artifact_id": None,
            },
        )
        with self.assertRaisesRegex(RuntimeError, "exceeds"):
            project_shadow_account(other)
        self.assertEqual(opening["sequence"], 1)

    def test_event_time_session_and_reconciliation_tolerance_fail_closed(self):
        self._opening()
        self._append(
            "cash_deposit",
            "deposit-later",
            {"amount": "1", "currency": "CNY"},
            minutes=2,
        )
        with self.assertRaisesRegex(ShadowLedgerConflictError, "occurrence-time"):
            self._append(
                "cash_deposit",
                "deposit-late-arrival",
                {"amount": "1", "currency": "CNY"},
                minutes=1,
            )

        mismatched = ShadowEventLedger(
            Path(self.temporary.name) / "mismatched-session", "account"
        )
        with self.assertRaisesRegex(ValueError, "China-local"):
            mismatched.append(
                "opening_balance",
                occurred_at=self.occurred,
                trading_session=self.session + timedelta(days=1),
                source="fixture",
                external_id="opening",
                payload={"cash": "100", "currency": "CNY"},
            )

        projection = project_shadow_account(self.ledger)
        with self.assertRaisesRegex(ValueError, "0.01"):
            reconcile_shadow_projection(
                projection,
                broker_cash="10002",
                broker_positions={},
                cash_tolerance="1000000",
            )
        with self.assertRaisesRegex(ValueError, "snapshot symbol"):
            reconcile_shadow_projection(
                projection,
                broker_cash="10001",
                broker_positions={"../510300": "1"},
            )


if __name__ == "__main__":
    unittest.main()
