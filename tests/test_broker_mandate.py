import copy
import json
import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from ai_trade.broker.base import BrokerOrderRequest, OrderSide
from ai_trade.broker.mandate import (
    BrokerMandate,
    authorization_mandate_status,
    consume_batch_approval,
    create_batch_approval,
    order_batch_fingerprint,
    write_batch_approval,
)


FINGERPRINT = "a" * 64
BATCH_FINGERPRINT = "b" * 64


def _order(
    order_id: str = "order-1",
    *,
    symbol: str = "510300",
    side: OrderSide = OrderSide.BUY,
    quantity: int = 100,
    price: float = 10.0,
) -> BrokerOrderRequest:
    return BrokerOrderRequest(order_id, symbol, side, quantity, price)


def _mandate_payload() -> dict[str, object]:
    return {
        "schema_version": 1,
        "allowed_symbols": ["510300", "510500"],
        "allowed_sides": ["BUY", "SELL"],
        "max_order_notional": 5_000.0,
        "max_daily_notional": 10_000.0,
        "max_orders_per_day": 5,
        "require_batch_approval": True,
    }


def _authorization() -> dict[str, object]:
    now = datetime.now(timezone.utc)
    return {
        "schema_version": 2,
        "approved": True,
        "approved_by": "local-owner",
        "approved_at": (now - timedelta(minutes=1)).isoformat(),
        "adapter": "mock",
        "account_id": "account",
        "config_fingerprint": FINGERPRINT,
        "expires_at": (now + timedelta(hours=1)).isoformat(),
        "mandate": _mandate_payload(),
    }


class BrokerMandateTests(unittest.TestCase):
    def test_authorization_mandate_requires_exact_bounded_schema(self):
        mandate, reason = authorization_mandate_status(
            _authorization(),
            configured_max_order_notional=5_000.0,
            configured_max_daily_notional=10_000.0,
        )
        self.assertIsNotNone(mandate)
        self.assertIn("bounded", reason)

        cases = []
        stale_schema = _authorization()
        stale_schema["schema_version"] = 1
        cases.append(stale_schema)
        unknown_field = _authorization()
        unknown_field["ignored_policy"] = True
        cases.append(unknown_field)
        optional_approval = _authorization()
        optional_approval["mandate"]["require_batch_approval"] = False
        cases.append(optional_approval)
        excessive_limit = _authorization()
        excessive_limit["mandate"]["max_order_notional"] = 5_001.0
        cases.append(excessive_limit)
        duplicate_symbol = _authorization()
        duplicate_symbol["mandate"]["allowed_symbols"] = ["510300", "510300"]
        cases.append(duplicate_symbol)

        for value in cases:
            with self.subTest(value=value):
                parsed, error = authorization_mandate_status(
                    value,
                    configured_max_order_notional=5_000.0,
                    configured_max_daily_notional=10_000.0,
                )
                self.assertIsNone(parsed)
                self.assertTrue(error)

    def test_mandate_enforces_symbols_sides_notional_and_daily_count(self):
        mandate = BrokerMandate(
            allowed_symbols=frozenset({"510300"}),
            allowed_sides=frozenset({OrderSide.BUY}),
            max_order_notional=2_000.0,
            max_daily_notional=3_000.0,
            max_orders_per_day=2,
        )
        result = mandate.enforce(
            [_order()], submitted_orders=0, submitted_notional=0.0
        )
        self.assertEqual(result["daily_orders_after_batch"], 1)
        for order, count, notional, message in (
            (_order(symbol="510500"), 0, 0.0, "symbol"),
            (_order(side=OrderSide.SELL), 0, 0.0, "side"),
            (_order(quantity=300), 0, 0.0, "Order notional"),
            (_order(), 2, 0.0, "order count"),
            (_order(), 0, 2_500.0, "daily notional"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                PermissionError, message
            ):
                mandate.enforce(
                    [order], submitted_orders=count, submitted_notional=notional
                )

    def test_batch_fingerprint_binds_order_ordering_and_broker_identity(self):
        orders = [_order("one"), _order("two", quantity=200)]
        kwargs = {
            "on_date": date(2026, 7, 17),
            "adapter": "mock",
            "account_id": "account",
            "config_fingerprint": FINGERPRINT,
        }
        first = order_batch_fingerprint(orders, **kwargs)
        self.assertEqual(first, order_batch_fingerprint(orders, **kwargs))
        self.assertNotEqual(first, order_batch_fingerprint(list(reversed(orders)), **kwargs))
        changed = copy.copy(kwargs)
        changed["account_id"] = "other-account"
        self.assertNotEqual(first, order_batch_fingerprint(orders, **changed))

    def test_batch_fingerprint_rejects_unbound_order_metadata(self):
        order = BrokerOrderRequest(
            "order-with-metadata",
            "510300",
            OrderSide.BUY,
            100,
            10.0,
            metadata={"routing": "adapter-defined"},
        )

        with self.assertRaisesRegex(ValueError, "empty order metadata"):
            order_batch_fingerprint(
                [order],
                on_date=date(2026, 7, 18),
                adapter="mock",
                account_id="account",
                config_fingerprint=FINGERPRINT,
            )

    def test_batch_approval_is_one_time_and_retained_as_audit_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "live_batch_approval.json"
            now = datetime.now(timezone.utc)
            approval = create_batch_approval(
                approved_by="local-owner",
                adapter="mock",
                account_id="account",
                config_fingerprint=FINGERPRINT,
                batch_fingerprint=BATCH_FINGERPRINT,
                now=now,
            )
            write_batch_approval(path, approval)
            consumed = consume_batch_approval(
                path,
                adapter="mock",
                account_id="account",
                config_fingerprint=FINGERPRINT,
                batch_fingerprint=BATCH_FINGERPRINT,
                now=now + timedelta(seconds=1),
            )
            self.assertFalse(path.exists())
            audit_path = Path(consumed["audit_file"])
            self.assertTrue(audit_path.exists())
            self.assertEqual(
                json.loads(audit_path.read_text(encoding="utf-8")), approval
            )
            with self.assertRaisesRegex(PermissionError, "required"):
                consume_batch_approval(
                    path,
                    adapter="mock",
                    account_id="account",
                    config_fingerprint=FINGERPRINT,
                    batch_fingerprint=BATCH_FINGERPRINT,
                )

    def test_invalid_batch_approval_is_not_consumed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "live_batch_approval.json"
            now = datetime.now(timezone.utc)
            approval = create_batch_approval(
                approved_by="local-owner",
                adapter="mock",
                account_id="account",
                config_fingerprint=FINGERPRINT,
                batch_fingerprint=BATCH_FINGERPRINT,
                now=now - timedelta(minutes=20),
            )
            write_batch_approval(path, approval)
            with self.assertRaisesRegex(PermissionError, "does not match"):
                consume_batch_approval(
                    path,
                    adapter="mock",
                    account_id="account",
                    config_fingerprint=FINGERPRINT,
                    batch_fingerprint="c" * 64,
                    now=now,
                )
            self.assertTrue(path.exists())


if __name__ == "__main__":
    unittest.main()
