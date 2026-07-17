from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from ai_trade.broker.base import BrokerOrderSnapshot, OrderSide, OrderStatus
from ai_trade.broker.lifecycle import recover_order_states


START = datetime(2026, 7, 18, 1, 0, tzinfo=timezone.utc)

EXPECTED_TRANSITIONS = {
    OrderStatus.PENDING_SUBMIT: frozenset(OrderStatus),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELLED,
            OrderStatus.REJECTED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCEL_PENDING,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.CANCEL_PENDING: frozenset(
        {
            OrderStatus.CANCEL_PENDING,
            OrderStatus.SUBMITTED,
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
        }
    ),
    OrderStatus.FILLED: frozenset({OrderStatus.FILLED}),
    OrderStatus.CANCELLED: frozenset({OrderStatus.CANCELLED}),
    OrderStatus.REJECTED: frozenset({OrderStatus.REJECTED}),
    OrderStatus.EXPIRED: frozenset({OrderStatus.EXPIRED}),
}


def _snapshot(
    status: OrderStatus,
    minute: int,
    *,
    filled_quantity: int = 0,
) -> BrokerOrderSnapshot:
    return BrokerOrderSnapshot(
        client_order_id="model-client",
        broker_order_id=("" if status == OrderStatus.PENDING_SUBMIT else "model-broker"),
        symbol="510300",
        side=OrderSide.BUY,
        quantity=100,
        filled_quantity=filled_quantity,
        limit_price=10.0,
        average_fill_price=10.0 if filled_quantity else None,
        status=status,
        updated_at=START + timedelta(minutes=minute),
    )


def _pair(previous: OrderStatus, current: OrderStatus):
    previous_fill = 10 if previous == OrderStatus.PARTIALLY_FILLED else 0
    if previous == OrderStatus.FILLED:
        previous_fill = 100
    if current == OrderStatus.PARTIALLY_FILLED:
        current_fill = max(previous_fill, 10)
    elif current == OrderStatus.FILLED:
        current_fill = 100
    elif current in {
        OrderStatus.CANCEL_PENDING,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
    }:
        current_fill = previous_fill if previous_fill < 100 else 0
    else:
        current_fill = 0
    return (
        _snapshot(previous, 0, filled_quantity=previous_fill),
        _snapshot(current, 1, filled_quantity=current_fill),
    )


class BrokerLifecycleModelTests(unittest.TestCase):
    def test_every_status_pair_matches_the_independent_transition_contract(self):
        for previous in OrderStatus:
            for current in OrderStatus:
                with self.subTest(previous=previous.value, current=current.value):
                    events = list(_pair(previous, current))
                    if current in EXPECTED_TRANSITIONS[previous]:
                        recovered = recover_order_states(events)
                        self.assertEqual(
                            recovered["model-client"].current.status,
                            current,
                        )
                    else:
                        with self.assertRaises((RuntimeError, ValueError)):
                            recover_order_states(events)

    def test_valid_late_observation_preserves_the_event_time_terminal_state(self):
        submitted = _snapshot(OrderStatus.SUBMITTED, 0)
        partial = _snapshot(
            OrderStatus.PARTIALLY_FILLED,
            1,
            filled_quantity=40,
        )
        filled = _snapshot(OrderStatus.FILLED, 2, filled_quantity=100)

        recovered = recover_order_states([submitted, filled, partial])["model-client"]

        self.assertEqual(recovered.current, filled)
        self.assertEqual(recovered.event_count, 3)
        self.assertEqual(recovered.out_of_order_events, 1)


if __name__ == "__main__":
    unittest.main()
