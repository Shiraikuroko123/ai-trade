from __future__ import annotations

import math
from datetime import datetime

from .base import (
    BrokerAccount,
    BrokerFill,
    BrokerHealth,
    BrokerOrderSnapshot,
    BrokerPosition,
)
from .lifecycle import validate_broker_fill, validate_order_snapshot


def validated_broker_account(value: object) -> BrokerAccount:
    if (
        not isinstance(value, BrokerAccount)
        or not _broker_text(value.account_id)
        or not _broker_text(value.currency)
        or not _broker_number(value.cash)
        or not _broker_number(value.available_cash)
        or not _broker_number(value.equity)
        or value.cash < 0
        or value.available_cash < 0
        or value.available_cash > value.cash + 1e-8
        or value.equity < 0
    ):
        raise RuntimeError("Broker reported an invalid account snapshot")
    return value


def validated_broker_positions(value: object) -> list[BrokerPosition]:
    if not isinstance(value, list):
        raise RuntimeError("Broker reported an invalid position collection")
    for position in value:
        if (
            not isinstance(position, BrokerPosition)
            or not _broker_text(position.symbol)
            or isinstance(position.quantity, bool)
            or not isinstance(position.quantity, int)
            or position.quantity < 0
            or isinstance(position.available_quantity, bool)
            or not isinstance(position.available_quantity, int)
            or not 0 <= position.available_quantity <= position.quantity
            or not _broker_number(position.average_cost)
            or position.average_cost < 0
            or not _broker_number(position.market_value)
            or position.market_value < 0
        ):
            raise RuntimeError("Broker reported an invalid position snapshot")
    return value


def validated_broker_health(value: object) -> BrokerHealth:
    if (
        not isinstance(value, BrokerHealth)
        or type(value.connected) is not bool
        or type(value.trading_session) is not bool
        or not isinstance(value.message, str)
        or len(value.message) > 2_000
        or not isinstance(value.checked_at, datetime)
        or value.checked_at.tzinfo is None
        or value.checked_at.utcoffset() is None
    ):
        raise RuntimeError("Broker reported an invalid health snapshot")
    return value


def validated_broker_orders(value: object) -> list[BrokerOrderSnapshot]:
    if not isinstance(value, list):
        raise RuntimeError("Broker reported an invalid order collection")
    client_ids: set[str] = set()
    broker_ids: set[str] = set()
    for order in value:
        try:
            validate_order_snapshot(order)
        except ValueError as exc:
            raise RuntimeError("Broker reported an invalid order snapshot") from exc
        if order.client_order_id in client_ids:
            raise RuntimeError("Broker reported duplicate open client order IDs")
        client_ids.add(order.client_order_id)
        if order.broker_order_id:
            if order.broker_order_id in broker_ids:
                raise RuntimeError("Broker reported duplicate open broker order IDs")
            broker_ids.add(order.broker_order_id)
    return value


def validated_broker_fills(value: object) -> list[BrokerFill]:
    if not isinstance(value, list):
        raise RuntimeError("Broker reported an invalid fill collection")
    fill_ids: set[str] = set()
    for fill in value:
        try:
            validate_broker_fill(fill)
        except ValueError as exc:
            raise RuntimeError("Broker reported an invalid fill snapshot") from exc
        if fill.fill_id in fill_ids:
            raise RuntimeError("Broker reported duplicate fill IDs")
        fill_ids.add(fill.fill_id)
    return value


def validated_broker_flag(value: object, name: str) -> bool:
    if type(value) is not bool:
        raise RuntimeError(f"Broker reported an invalid {name} flag")
    return value


def _broker_text(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and value.strip() == value
        and not any(ord(character) < 32 for character in value)
    )


def _broker_number(value: object) -> bool:
    return (
        not isinstance(value, bool)
        and isinstance(value, (int, float))
        and math.isfinite(float(value))
    )
