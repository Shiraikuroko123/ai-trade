from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from ..config import (
    AppConfig,
    DEFAULT_BROKER_MAX_DAILY_NOTIONAL,
    DEFAULT_BROKER_MAX_ORDER_NOTIONAL,
)
from ..data.market import MarketData
from .base import (
    Broker,
    BrokerEnvironment,
    BrokerOrderRequest,
    BrokerOrderSnapshot,
    OrderSide,
)
from .ledger import (
    append_order_events,
    reserve_order_intents,
    submitted_order_notional,
)
from .live_guard import assert_live_submission_allowed


CHINA_STANDARD_TIME = timezone(timedelta(hours=8))
MAX_BROKER_HEALTH_AGE = timedelta(minutes=5)


class LiveOrderRouter:
    def __init__(self, config: AppConfig, broker: Broker):
        self.config = config
        self.broker = broker

    def validate(
        self,
        orders: list[BrokerOrderRequest],
        market: MarketData,
        on_date: date,
    ) -> dict[str, Any]:
        if not orders:
            raise ValueError("At least one order is required")
        broker_cfg = self.config.raw.get("broker", {})
        max_order = float(
            broker_cfg.get(
                "max_order_notional", DEFAULT_BROKER_MAX_ORDER_NOTIONAL
            )
        )
        max_daily = float(
            broker_cfg.get(
                "max_daily_notional", DEFAULT_BROKER_MAX_DAILY_NOTIONAL
            )
        )
        active_symbols = set(market.active_symbols(on_date))
        available_positions: dict[str, int] = {}
        for value in self.broker.positions():
            if (
                not value.symbol
                or isinstance(value.available_quantity, bool)
                or not isinstance(value.available_quantity, int)
                or value.available_quantity < 0
                or value.available_quantity > value.quantity
            ):
                raise RuntimeError("Broker reported an invalid available position")
            available_positions[value.symbol] = (
                available_positions.get(value.symbol, 0) + value.available_quantity
            )
        account = self.broker.account()
        if (
            not math.isfinite(account.cash)
            or not math.isfinite(account.available_cash)
            or not math.isfinite(account.equity)
            or account.cash < 0
            or account.available_cash < 0
            or account.available_cash > account.cash + 1e-8
            or account.equity < 0
        ):
            raise RuntimeError("Broker reported invalid account balances")
        seen: set[str] = set()
        sell_quantities: dict[str, int] = {}
        total_notional = 0.0
        buy_cash_required = 0.0
        rows = []
        for order in orders:
            if not order.client_order_id or order.client_order_id in seen:
                raise ValueError("client_order_id values must be non-empty and unique")
            seen.add(order.client_order_id)
            if not isinstance(order.side, OrderSide):
                raise ValueError(f"Unsupported live order side: {order.side!r}")
            if order.time_in_force != "DAY":
                raise ValueError("Only DAY live orders are supported")
            if order.symbol not in active_symbols:
                raise ValueError(
                    f"Order symbol is outside the active universe on {on_date}: "
                    f"{order.symbol}"
                )
            instrument = market.instrument(order.symbol)
            if account.currency != instrument.currency:
                raise ValueError(
                    f"Order currency for {order.symbol} does not match the broker account"
                )
            if (
                isinstance(order.quantity, bool)
                or not isinstance(order.quantity, int)
                or order.quantity <= 0
                or order.quantity % instrument.lot_size != 0
            ):
                raise ValueError(
                    f"Order quantity for {order.symbol} must use lot size {instrument.lot_size}"
                )
            if not math.isfinite(order.limit_price) or order.limit_price <= 0:
                raise ValueError("Live orders require a positive finite limit price")
            if not _is_tick_aligned(order.limit_price, instrument.tick_size):
                raise ValueError(
                    f"Limit price for {order.symbol} must use tick size "
                    f"{instrument.tick_size:g}"
                )
            if order.side == OrderSide.SELL:
                available = available_positions.get(order.symbol, 0)
                cumulative = sell_quantities.get(order.symbol, 0) + order.quantity
                if cumulative > available:
                    raise ValueError(
                        f"Cumulative sell quantity for {order.symbol} exceeds "
                        "available broker position"
                    )
                sell_quantities[order.symbol] = cumulative
            status = market.trading_status(order.symbol, on_date)
            if not status.tradable:
                raise ValueError(f"{order.symbol} is not tradable: {status.status}")
            limit_pct = status.price_limit_pct
            if limit_pct is not None:
                previous = market.previous_bar(order.symbol, on_date)
                if previous is None or previous.close <= 0:
                    raise ValueError(
                        f"Cannot verify daily price limits for {order.symbol}"
                    )
                lower, upper = _price_limits(
                    previous.close, limit_pct, instrument.tick_size
                )
                tolerance = instrument.tick_size / 2.0
                if not lower - tolerance <= order.limit_price <= upper + tolerance:
                    raise ValueError(
                        f"Limit price for {order.symbol} is outside the daily range "
                        f"{lower:.2f}-{upper:.2f}"
                    )
            notional = order.quantity * order.limit_price
            if max_order <= 0 or notional > max_order:
                raise ValueError(f"Order notional for {order.symbol} exceeds configured limit")
            total_notional += notional
            if order.side == OrderSide.BUY:
                buy_cash_required += _estimated_buy_cash(
                    self.config, instrument, on_date, notional
                )
            rows.append(
                {
                    "client_order_id": order.client_order_id,
                    "symbol": order.symbol,
                    "side": order.side.value,
                    "quantity": order.quantity,
                    "limit_price": order.limit_price,
                    "notional": notional,
                }
            )
        if buy_cash_required > account.available_cash:
            raise ValueError("Buy orders exceed broker available cash")
        submitted_today = submitted_order_notional(
            self.config.broker_orders_file, on_date
        )
        if max_daily <= 0 or submitted_today + total_notional > max_daily:
            raise ValueError("Orders exceed configured daily notional limit")
        return {
            "orders": rows,
            "batch_notional": total_notional,
            "submitted_today": submitted_today,
            "daily_notional_after_batch": submitted_today + total_notional,
        }

    def submit(
        self,
        orders: list[BrokerOrderRequest],
        market: MarketData,
        on_date: date,
        paper_audit: dict[str, Any],
    ) -> list[BrokerOrderSnapshot]:
        assert_live_submission_allowed(self.config, paper_audit, market)
        if self.broker.environment != BrokerEnvironment.LIVE:
            raise RuntimeError("A live broker environment is required for submission")
        self._assert_broker_identity()
        self.validate(orders, market, on_date)
        health = self.broker.health()
        if not health.connected or not health.trading_session:
            raise RuntimeError(f"Broker is not ready for submission: {health.message}")
        _assert_current_trading_session(on_date, health.checked_at)
        broker_cfg = self.config.raw.get("broker", {})
        max_daily = float(
            broker_cfg.get(
                "max_daily_notional", DEFAULT_BROKER_MAX_DAILY_NOTIONAL
            )
        )
        reserve_order_intents(
            self.config.broker_orders_file,
            orders,
            on_date,
            max_daily,
        )
        # Re-evaluate the authorization and kill switch after all broker and disk I/O.
        # A failed final gate intentionally leaves reserved IDs behind, preventing a
        # blind retry when submission status is uncertain.
        assert_live_submission_allowed(self.config, paper_audit, market)
        submitted = self.broker.submit_orders(orders)
        append_order_events(self.config.broker_orders_file, submitted)
        _validate_submission_response(orders, submitted)
        return submitted

    def _assert_broker_identity(self) -> None:
        broker_cfg = self.config.raw.get("broker", {})
        configured_adapter = str(broker_cfg.get("adapter") or "")
        configured_account = str(broker_cfg.get("account_id") or "")
        if (
            not configured_adapter
            or getattr(self.broker, "adapter_name", None) != configured_adapter
        ):
            raise RuntimeError("Live broker adapter does not match the authorized configuration")
        actual_account = str(self.broker.account().account_id)
        if not configured_account or actual_account != configured_account:
            raise RuntimeError("Live broker account does not match the authorized configuration")


def _is_tick_aligned(value: float, tick_size: float) -> bool:
    price = Decimal(str(value))
    tick = Decimal(str(tick_size))
    return price.remainder_near(tick) == 0


def _price_limits(
    previous_close: float, limit_pct: float, tick_size: float
) -> tuple[float, float]:
    tick = Decimal(str(tick_size))

    def rounded(value: Decimal) -> float:
        ticks = (value / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
        return float(ticks * tick)

    close = Decimal(str(previous_close))
    limit = Decimal(str(limit_pct))
    return rounded(close * (1 - limit)), rounded(close * (1 + limit))


def _assert_current_trading_session(on_date: date, checked_at: datetime) -> None:
    if checked_at.tzinfo is None:
        raise RuntimeError("Broker health timestamp must include a timezone")
    now = datetime.now(timezone.utc)
    checked_utc = checked_at.astimezone(timezone.utc)
    age = now - checked_utc
    if age < timedelta(0) or age > MAX_BROKER_HEALTH_AGE:
        raise RuntimeError("Broker health status is stale or timestamped in the future")
    if checked_at.astimezone(CHINA_STANDARD_TIME).date() != on_date:
        raise RuntimeError("Order date does not match the broker trading session date")


def _validate_submission_response(
    requested: list[BrokerOrderRequest],
    submitted: list[BrokerOrderSnapshot],
) -> None:
    expected = {order.client_order_id: order for order in requested}
    actual: dict[str, BrokerOrderSnapshot] = {}
    for snapshot in submitted:
        if snapshot.client_order_id in actual:
            raise RuntimeError("Broker returned duplicate client_order_id values")
        actual[snapshot.client_order_id] = snapshot
    if set(actual) != set(expected):
        raise RuntimeError("Broker submission response does not match the requested order IDs")
    for order_id, order in expected.items():
        snapshot = actual[order_id]
        if not snapshot.broker_order_id:
            raise RuntimeError(f"Broker returned no broker_order_id for {order_id}")
        if (
            snapshot.symbol != order.symbol
            or snapshot.side != order.side
            or snapshot.quantity != order.quantity
            or not math.isclose(
                snapshot.limit_price, order.limit_price, rel_tol=0.0, abs_tol=1e-12
            )
        ):
            raise RuntimeError(f"Broker submission response changed order details for {order_id}")


def _estimated_buy_cash(
    config: AppConfig, instrument: Any, on_date: date, notional: float
) -> float:
    costs = getattr(config, "costs", None)
    if costs is None:
        return notional
    schedule = costs.for_instrument(instrument, on_date)
    commission = max(
        schedule.minimum_commission,
        notional * schedule.commission_bps / 10_000.0,
    )
    transfer_fee = notional * schedule.transfer_fee_bps / 10_000.0
    return notional + commission + transfer_fee
