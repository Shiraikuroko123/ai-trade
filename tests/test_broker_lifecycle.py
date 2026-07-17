from __future__ import annotations

import csv
import hashlib
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from ai_trade.broker.base import (
    BrokerFill,
    BrokerOrderRequest,
    BrokerOrderSnapshot,
    OrderSide,
    OrderStatus,
)
from ai_trade.broker.ledger import (
    append_broker_observation,
    append_fills,
    append_order_events,
    read_order_events,
    recover_order_lifecycle,
    reserve_order_intents,
)


START = datetime(2026, 7, 17, 1, 30, tzinfo=timezone.utc)


def _event(
    status: OrderStatus,
    minute: int,
    *,
    client_order_id: str = "client-1",
    broker_order_id: str = "broker-1",
    symbol: str = "510300",
    quantity: int = 100,
    filled_quantity: int = 0,
    average_fill_price: float | None = None,
) -> BrokerOrderSnapshot:
    return BrokerOrderSnapshot(
        client_order_id=client_order_id,
        broker_order_id=broker_order_id,
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=quantity,
        filled_quantity=filled_quantity,
        limit_price=11.0,
        average_fill_price=average_fill_price,
        status=status,
        updated_at=START + timedelta(minutes=minute),
    )


def _fill(
    fill_id: str,
    minute: int,
    quantity: int,
    price: float,
    *,
    client_order_id: str = "client-1",
    broker_order_id: str = "broker-1",
    symbol: str = "510300",
) -> BrokerFill:
    return BrokerFill(
        fill_id=fill_id,
        broker_order_id=broker_order_id,
        client_order_id=client_order_id,
        symbol=symbol,
        side=OrderSide.BUY,
        quantity=quantity,
        price=price,
        commission=1.0,
        tax=0.0,
        filled_at=START + timedelta(minutes=minute),
    )


class BrokerLifecycleTests(unittest.TestCase):
    def test_china_session_intent_precedes_pre_utc_midnight_submission(self):
        with tempfile.TemporaryDirectory() as temporary:
            orders = Path(temporary) / "orders.csv"
            request = BrokerOrderRequest(
                client_order_id="overnight-order",
                symbol="510300",
                side=OrderSide.BUY,
                quantity=100,
                limit_price=10.0,
            )
            reserve_order_intents(
                orders,
                [request],
                date(2026, 7, 18),
                10_000.0,
            )
            submitted = BrokerOrderSnapshot(
                client_order_id=request.client_order_id,
                broker_order_id="overnight-broker",
                symbol=request.symbol,
                side=request.side,
                quantity=request.quantity,
                filled_quantity=0,
                limit_price=request.limit_price,
                average_fill_price=None,
                status=OrderStatus.SUBMITTED,
                updated_at=datetime(2026, 7, 17, 18, 0, tzinfo=timezone.utc),
            )

            append_order_events(orders, [submitted])

            events = read_order_events(orders)
            self.assertEqual(events[-1], submitted)
            self.assertEqual(events[0].updated_at.utcoffset(), timedelta(hours=8))
            self.assertEqual(events[0].updated_at.date(), date(2026, 7, 18))

    def test_legacy_event_id_remains_readable_and_exact_retry_is_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            orders = Path(temporary) / "orders.csv"
            event = _event(OrderStatus.SUBMITTED, 0)
            legacy_raw = "|".join(
                [
                    event.client_order_id,
                    event.broker_order_id,
                    event.status.value,
                    event.updated_at.isoformat(),
                ]
            )
            legacy_id = hashlib.sha256(legacy_raw.encode("utf-8")).hexdigest()[:24]
            row = {
                "client_order_id": event.client_order_id,
                "broker_order_id": event.broker_order_id,
                "symbol": event.symbol,
                "side": event.side.value,
                "quantity": str(event.quantity),
                "filled_quantity": str(event.filled_quantity),
                "limit_price": "11",
                "average_fill_price": "",
                "status": event.status.value,
                "updated_at": event.updated_at.isoformat(),
                "message": event.message,
                "event_id": legacy_id,
            }
            with orders.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)

            self.assertEqual(read_order_events(orders), [event])
            append_order_events(orders, [event])

            with orders.open("r", encoding="utf-8", newline="") as handle:
                persisted = list(csv.DictReader(handle))
            self.assertEqual(len(persisted), 1)
            self.assertEqual(persisted[0]["event_id"], legacy_id)
            self.assertEqual(persisted[0]["limit_price"], "11")

    def test_v2_event_id_detects_content_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            orders = Path(temporary) / "orders.csv"
            append_order_events(orders, [_event(OrderStatus.SUBMITTED, 0)])
            with orders.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                fieldnames = reader.fieldnames
                rows = list(reader)
            self.assertIsNotNone(fieldnames)
            self.assertTrue(rows[0]["event_id"].startswith("v2_"))
            rows[0]["message"] = "tampered after persistence"
            with orders.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

            with self.assertRaisesRegex(RuntimeError, "failed content validation"):
                read_order_events(orders)
            report = recover_order_lifecycle(orders, Path(temporary) / "fills.csv")
            self.assertEqual(report["status"], "INTEGRITY_ERROR")
            self.assertEqual(
                report["integrity_errors"][0]["code"], "order_ledger_invalid"
            )

    def test_new_events_canonicalize_integer_prices_without_duplicate_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            orders = Path(temporary) / "orders.csv"
            event = _event(OrderStatus.SUBMITTED, 0)
            event = BrokerOrderSnapshot(
                client_order_id=event.client_order_id,
                broker_order_id=event.broker_order_id,
                symbol=event.symbol,
                side=event.side,
                quantity=event.quantity,
                filled_quantity=event.filled_quantity,
                limit_price=11,
                average_fill_price=event.average_fill_price,
                status=event.status,
                updated_at=event.updated_at,
                message=event.message,
            )

            append_order_events(orders, [event])
            append_order_events(orders, [event])

            with orders.open("r", encoding="utf-8", newline="") as handle:
                persisted = list(csv.DictReader(handle))
            self.assertEqual(len(persisted), 1)
            self.assertEqual(persisted[0]["limit_price"], "11.0")
            self.assertEqual(read_order_events(orders), [event])

    def test_legacy_fill_numeric_text_remains_idempotent_after_upgrade(self):
        with tempfile.TemporaryDirectory() as temporary:
            fills = Path(temporary) / "fills.csv"
            fill = _fill("fill-legacy", 1, 100, 10)
            row = {
                "fill_id": fill.fill_id,
                "broker_order_id": fill.broker_order_id,
                "client_order_id": fill.client_order_id,
                "symbol": fill.symbol,
                "side": fill.side.value,
                "quantity": str(fill.quantity),
                "price": "10",
                "commission": "1",
                "tax": "0",
                "filled_at": fill.filled_at.isoformat(),
            }
            with fills.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=list(row))
                writer.writeheader()
                writer.writerow(row)

            append_fills(fills, [fill])

            with fills.open("r", encoding="utf-8", newline="") as handle:
                persisted = list(csv.DictReader(handle))
            self.assertEqual(persisted, [row])

    def test_partial_fills_reconstruct_a_verified_terminal_order(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orders = root / "orders.csv"
            fills = root / "fills.csv"
            append_order_events(
                orders,
                [
                    _event(OrderStatus.SUBMITTED, 0),
                    _event(
                        OrderStatus.PARTIALLY_FILLED,
                        1,
                        filled_quantity=50,
                        average_fill_price=10.0,
                    ),
                    _event(
                        OrderStatus.FILLED,
                        2,
                        filled_quantity=100,
                        average_fill_price=11.0,
                    ),
                ],
            )
            append_fills(
                fills,
                [_fill("fill-1", 1, 50, 10.0), _fill("fill-2", 2, 50, 12.0)],
            )

            report = recover_order_lifecycle(orders, fills)

            self.assertEqual(report["status"], "VERIFIED")
            self.assertEqual(report["terminal_order_count"], 1)
            self.assertEqual(report["fill_count"], 2)
            self.assertEqual(report["orders"][0]["filled_quantity"], 100)
            self.assertEqual(report["orders"][0]["remaining_quantity"], 0)
            self.assertTrue(report["orders"][0]["integrity_ok"])
            self.assertFalse(report["qualifying_evidence"])
            self.assertFalse(report["execution_enabled"])

    def test_cancel_race_preserves_the_partial_fill_before_cancellation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orders = root / "orders.csv"
            fills = root / "fills.csv"
            append_order_events(
                orders,
                [
                    _event(OrderStatus.SUBMITTED, 0),
                    _event(OrderStatus.CANCEL_PENDING, 1),
                    _event(
                        OrderStatus.PARTIALLY_FILLED,
                        2,
                        filled_quantity=20,
                        average_fill_price=10.0,
                    ),
                    _event(
                        OrderStatus.CANCELLED,
                        3,
                        filled_quantity=20,
                        average_fill_price=10.0,
                    ),
                ],
            )
            append_fills(fills, [_fill("fill-race", 2, 20, 10.0)])

            report = recover_order_lifecycle(orders, fills)

            row = report["orders"][0]
            self.assertEqual(report["status"], "VERIFIED")
            self.assertEqual(row["status"], OrderStatus.CANCELLED.value)
            self.assertEqual(row["remaining_quantity"], 80)
            self.assertTrue(row["cancel_race_observed"])

    def test_cancel_request_can_lose_to_a_complete_fill(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orders = root / "orders.csv"
            fills = root / "fills.csv"
            append_order_events(
                orders,
                [
                    _event(OrderStatus.SUBMITTED, 0),
                    _event(OrderStatus.CANCEL_PENDING, 1),
                    _event(
                        OrderStatus.FILLED,
                        2,
                        filled_quantity=100,
                        average_fill_price=10.0,
                    ),
                ],
            )
            append_fills(fills, [_fill("fill-complete", 2, 100, 10.0)])

            report = recover_order_lifecycle(orders, fills)

            self.assertEqual(report["status"], "VERIFIED")
            self.assertEqual(report["orders"][0]["status"], "FILLED")
            self.assertTrue(report["orders"][0]["cancel_race_observed"])

    def test_out_of_order_event_is_inserted_by_timestamp_without_state_regression(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orders = root / "orders.csv"
            fills = root / "fills.csv"
            append_order_events(
                orders,
                [
                    _event(OrderStatus.SUBMITTED, 0),
                    _event(
                        OrderStatus.FILLED,
                        2,
                        filled_quantity=100,
                        average_fill_price=11.0,
                    ),
                ],
            )
            append_order_events(
                orders,
                [
                    _event(
                        OrderStatus.PARTIALLY_FILLED,
                        1,
                        filled_quantity=50,
                        average_fill_price=10.0,
                    )
                ],
            )
            append_fills(
                fills,
                [_fill("fill-1", 1, 50, 10.0), _fill("fill-2", 2, 50, 12.0)],
            )

            report = recover_order_lifecycle(orders, fills)

            self.assertEqual(report["status"], "RECOVERED")
            self.assertEqual(report["orders"][0]["status"], "FILLED")
            self.assertEqual(report["orders"][0]["out_of_order_events"], 1)
            self.assertEqual(
                report["recovery_warnings"][0]["code"],
                "out_of_order_events_recovered",
            )

    def test_terminal_state_and_filled_quantity_cannot_move_backwards(self):
        with tempfile.TemporaryDirectory() as temporary:
            orders = Path(temporary) / "orders.csv"
            append_order_events(
                orders,
                [
                    _event(OrderStatus.SUBMITTED, 0),
                    _event(
                        OrderStatus.FILLED,
                        1,
                        filled_quantity=100,
                        average_fill_price=10.0,
                    ),
                ],
            )

            with self.assertRaisesRegex(RuntimeError, "Illegal order transition"):
                append_order_events(orders, [_event(OrderStatus.SUBMITTED, 2)])

            events = read_order_events(orders)
            self.assertEqual(len(events), 2)
            self.assertEqual(events[-1].status, OrderStatus.FILLED)

    def test_identity_drift_and_broker_id_reuse_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orders = root / "orders.csv"
            append_order_events(orders, [_event(OrderStatus.SUBMITTED, 0)])

            with self.assertRaisesRegex(RuntimeError, "Immutable order fields changed"):
                append_order_events(
                    orders,
                    [_event(OrderStatus.SUBMITTED, 1, symbol="510500")],
                )
            with self.assertRaisesRegex(RuntimeError, "multiple client order IDs"):
                append_order_events(
                    orders,
                    [
                        _event(
                            OrderStatus.SUBMITTED,
                            1,
                            client_order_id="client-2",
                        )
                    ],
                )

    def test_status_specific_fill_invariants_are_enforced(self):
        with tempfile.TemporaryDirectory() as temporary:
            orders = Path(temporary) / "orders.csv"
            invalid = (
                _event(OrderStatus.PARTIALLY_FILLED, 0),
                _event(
                    OrderStatus.PARTIALLY_FILLED,
                    0,
                    filled_quantity=10,
                    average_fill_price=None,
                ),
                _event(
                    OrderStatus.CANCELLED,
                    0,
                    filled_quantity=100,
                    average_fill_price=10.0,
                ),
            )
            for event in invalid:
                with self.subTest(status=event.status, filled=event.filled_quantity):
                    with self.assertRaises(ValueError):
                        append_order_events(orders, [event])

    def test_restart_recovery_tolerates_a_stale_lock_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orders = root / "orders.csv"
            fills = root / "fills.csv"
            orders.with_suffix(".csv.lock").write_text("stale-process", encoding="ascii")
            append_order_events(orders, [_event(OrderStatus.SUBMITTED, 0)])

            first = recover_order_lifecycle(orders, fills)
            second = recover_order_lifecycle(orders, fills)

            self.assertEqual(first, second)
            self.assertEqual(first["status"], "VERIFIED")
            self.assertEqual(first["open_order_count"], 1)

    def test_restart_observation_repairs_an_interrupted_order_fill_write(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orders = root / "orders.csv"
            fills = root / "fills.csv"
            completed = _event(
                OrderStatus.FILLED,
                1,
                filled_quantity=100,
                average_fill_price=10.0,
            )
            append_order_events(
                orders,
                [_event(OrderStatus.SUBMITTED, 0), completed],
            )
            interrupted = recover_order_lifecycle(orders, fills)
            self.assertEqual(interrupted["status"], "INTEGRITY_ERROR")

            repaired = append_broker_observation(
                orders,
                fills,
                [completed],
                [_fill("fill-after-restart", 1, 100, 10.0)],
            )

            self.assertEqual(repaired["status"], "VERIFIED")
            self.assertEqual(
                recover_order_lifecycle(orders, fills)["status"], "VERIFIED"
            )

    def test_atomic_replace_failure_keeps_the_prior_ledger_intact(self):
        with tempfile.TemporaryDirectory() as temporary:
            orders = Path(temporary) / "orders.csv"
            initial = _event(OrderStatus.SUBMITTED, 0)
            later = _event(
                OrderStatus.PARTIALLY_FILLED,
                1,
                filled_quantity=20,
                average_fill_price=10.0,
            )
            append_order_events(orders, [initial])
            before = orders.read_bytes()

            with patch(
                "ai_trade.broker.ledger.os.replace",
                side_effect=OSError("injected replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected replace failure"):
                    append_order_events(orders, [later])

            self.assertEqual(orders.read_bytes(), before)
            self.assertEqual(read_order_events(orders), [initial])
            self.assertEqual(list(orders.parent.glob(".orders.csv.*.tmp")), [])

            append_order_events(orders, [later])
            self.assertEqual(read_order_events(orders), [initial, later])

    def test_parallel_thread_appends_are_serialized_without_lost_events(self):
        with tempfile.TemporaryDirectory() as temporary:
            orders = Path(temporary) / "orders.csv"
            events = [
                _event(
                    OrderStatus.SUBMITTED,
                    0,
                    client_order_id=f"client-{index}",
                    broker_order_id=f"broker-{index}",
                )
                for index in range(12)
            ]

            with ThreadPoolExecutor(max_workers=6) as executor:
                list(executor.map(lambda event: append_order_events(orders, [event]), events))

            recovered = read_order_events(orders)
            self.assertEqual(len(recovered), len(events))
            self.assertEqual(
                {event.client_order_id for event in recovered},
                {event.client_order_id for event in events},
            )

    def test_injected_second_ledger_failure_is_repaired_by_exact_retry(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orders = root / "orders.csv"
            fills = root / "fills.csv"
            submitted = _event(OrderStatus.SUBMITTED, 0)
            completed = _event(
                OrderStatus.FILLED,
                1,
                filled_quantity=100,
                average_fill_price=10.0,
            )
            fill = _fill("fill-interrupted", 1, 100, 10.0)

            from ai_trade.broker import ledger

            original = ledger._append_rows
            calls = 0

            def fail_second_append(*args, **kwargs):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("injected fill ledger failure")
                return original(*args, **kwargs)

            with patch.object(ledger, "_append_rows", side_effect=fail_second_append):
                with self.assertRaisesRegex(OSError, "injected fill ledger failure"):
                    append_broker_observation(
                        orders,
                        fills,
                        [submitted, completed],
                        [fill],
                    )

            self.assertEqual(
                recover_order_lifecycle(orders, fills)["status"],
                "INTEGRITY_ERROR",
            )
            repaired = append_broker_observation(
                orders,
                fills,
                [submitted, completed],
                [fill],
            )
            self.assertEqual(repaired["status"], "VERIFIED")

    def test_fill_mismatch_is_reported_without_changing_authority(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            orders = root / "orders.csv"
            fills = root / "fills.csv"
            append_order_events(
                orders,
                [
                    _event(OrderStatus.SUBMITTED, 0),
                    _event(
                        OrderStatus.FILLED,
                        1,
                        filled_quantity=100,
                        average_fill_price=10.0,
                    ),
                ],
            )
            append_fills(fills, [_fill("fill-short", 1, 80, 10.0)])

            report = recover_order_lifecycle(orders, fills)

            self.assertEqual(report["status"], "INTEGRITY_ERROR")
            self.assertIn(
                "fill_quantity_mismatch",
                {value["code"] for value in report["integrity_errors"]},
            )
            self.assertFalse(report["qualifying_evidence"])
            self.assertFalse(report["execution_enabled"])


if __name__ == "__main__":
    unittest.main()
