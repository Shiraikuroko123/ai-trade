from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any

from ..config import (
    AppConfig,
    DEFAULT_BROKER_MAX_DAILY_NOTIONAL,
    DEFAULT_BROKER_MAX_ORDER_NOTIONAL,
)
from ..data.market import MarketData
from .base import (
    Broker,
    BrokerAccount,
    BrokerEnvironment,
    BrokerHealth,
    BrokerOperation,
    BrokerOrderRequest,
    BrokerOrderSnapshot,
    BrokerPosition,
    OrderSide,
)
from .ledger import (
    append_order_events,
    initialize_broker_ledger_scope,
    preflight_broker_ledger_scope,
    reserve_order_intents,
    submitted_order_count,
    submitted_order_notional,
)
from .live_guard import assert_live_submission_allowed
from .mandate import (
    consume_batch_approval,
    order_batch_fingerprint,
    parse_mandate,
)
from .scope import BrokerLedgerScope, create_broker_ledger_scope


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
        *,
        ledger_scope: BrokerLedgerScope | None = None,
    ) -> dict[str, Any]:
        if not orders:
            raise ValueError("At least one order is required")
        scope_path = self._scope_path(ledger_scope)
        if ledger_scope is not None and scope_path is not None:
            preflight_broker_ledger_scope(
                scope_path,
                self.config.broker_orders_file,
                self.config.broker_fills_file,
                ledger_scope,
            )
        self.broker.capabilities.require(
            frozenset(
                {
                    BrokerOperation.READ_ACCOUNT,
                    BrokerOperation.READ_POSITIONS,
                }
            ),
            self.broker.environment,
        )
        account = self._assert_broker_identity() if ledger_scope is not None else None
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
        positions = _validated_broker_positions(self.broker.positions())
        for value in positions:
            available_positions[value.symbol] = (
                available_positions.get(value.symbol, 0) + value.available_quantity
            )
        if account is None:
            account = _validated_broker_account(self.broker.account())
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
            if not isinstance(order.metadata, dict) or order.metadata:
                raise ValueError(
                    "Live order metadata is unsupported without an explicit schema"
                )
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
            self.config.broker_orders_file,
            on_date,
            scope_path=scope_path,
            scope=ledger_scope,
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
        readiness = assert_live_submission_allowed(self.config, paper_audit, market)
        if self.broker.environment != BrokerEnvironment.LIVE:
            raise RuntimeError("A live broker environment is required for submission")
        self.broker.capabilities.require(
            frozenset(
                {
                    BrokerOperation.READ_ACCOUNT,
                    BrokerOperation.READ_POSITIONS,
                    BrokerOperation.SUBMIT_ORDERS,
                }
            ),
            BrokerEnvironment.LIVE,
        )
        broker_cfg = self.config.raw.get("broker", {})
        configured_account = str(broker_cfg.get("account_id") or "")
        config_fingerprint = str(readiness.get("config_fingerprint") or "")
        ledger_scope = create_broker_ledger_scope(
            adapter=self.broker.adapter_name,
            account_id=configured_account,
            environment=self.broker.environment,
            config_fingerprint=config_fingerprint,
            orders_path=self.config.broker_orders_file,
            fills_path=self.config.broker_fills_file,
        )
        scope_path = self._scope_path(ledger_scope)
        validation = self.validate(
            orders,
            market,
            on_date,
            ledger_scope=ledger_scope,
        )
        mandate = parse_mandate(
            readiness.get("authorization", {}).get("mandate"),
            configured_max_order_notional=float(
                broker_cfg.get(
                    "max_order_notional", DEFAULT_BROKER_MAX_ORDER_NOTIONAL
                )
            ),
            configured_max_daily_notional=float(
                broker_cfg.get(
                    "max_daily_notional", DEFAULT_BROKER_MAX_DAILY_NOTIONAL
                )
            ),
        )
        submitted_count = submitted_order_count(
            self.config.broker_orders_file,
            on_date,
            scope_path=scope_path,
            scope=ledger_scope,
        )
        mandate.enforce(
            orders,
            submitted_orders=submitted_count,
            submitted_notional=float(validation["submitted_today"]),
        )
        health = _validated_broker_health(self.broker.health())
        if not health.connected or not health.trading_session:
            raise RuntimeError(f"Broker is not ready for submission: {health.message}")
        _assert_current_trading_session(on_date, health.checked_at)
        batch_fingerprint = order_batch_fingerprint(
            orders,
            on_date=on_date,
            adapter=self.broker.adapter_name,
            account_id=configured_account,
            config_fingerprint=config_fingerprint,
        )
        approval = consume_batch_approval(
            self.config.live_batch_approval_file,
            adapter=self.broker.adapter_name,
            account_id=configured_account,
            config_fingerprint=config_fingerprint,
            batch_fingerprint=batch_fingerprint,
        )
        initialize_broker_ledger_scope(
            scope_path,
            self.config.broker_orders_file,
            self.config.broker_fills_file,
            ledger_scope,
        )
        reserve_order_intents(
            self.config.broker_orders_file,
            orders,
            on_date,
            mandate.max_daily_notional,
            mandate.max_orders_per_day,
            approval_id=str(approval["approval_id"]),
            batch_fingerprint=batch_fingerprint,
            scope_path=scope_path,
            scope=ledger_scope,
        )
        # Re-evaluate the authorization and kill switch after all broker and disk I/O.
        # A failed final gate intentionally leaves reserved IDs behind, preventing a
        # blind retry when submission status is uncertain.
        assert_live_submission_allowed(self.config, paper_audit, market)
        submitted = self.broker.submit_orders(orders)
        append_order_events(
            self.config.broker_orders_file,
            submitted,
            scope_path=scope_path,
            scope=ledger_scope,
        )
        _validate_submission_response(orders, submitted)
        return submitted

    def _assert_broker_identity(self):
        broker_cfg = self.config.raw.get("broker", {})
        configured_adapter = str(broker_cfg.get("adapter") or "")
        configured_account = str(broker_cfg.get("account_id") or "")
        if (
            not configured_adapter
            or getattr(self.broker, "adapter_name", None) != configured_adapter
        ):
            raise RuntimeError("Live broker adapter does not match the authorized configuration")
        account = _validated_broker_account(self.broker.account())
        actual_account = account.account_id
        if not configured_account or actual_account != configured_account:
            raise RuntimeError("Live broker account does not match the authorized configuration")
        return account

    def _scope_path(self, scope: BrokerLedgerScope | None) -> Path | None:
        if scope is None:
            return None
        path = getattr(self.config, "broker_ledger_scope_file", None)
        if path is None:
            raise RuntimeError("Broker ledger scope file is not configured")
        return path


def _is_tick_aligned(value: float, tick_size: float) -> bool:
    price = Decimal(str(value))
    tick = Decimal(str(tick_size))
    return price.remainder_near(tick) == 0


def _validated_broker_account(value: object) -> BrokerAccount:
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


def _validated_broker_positions(value: object) -> list[BrokerPosition]:
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


def _validated_broker_health(value: object) -> BrokerHealth:
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
