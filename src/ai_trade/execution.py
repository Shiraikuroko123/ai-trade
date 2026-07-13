from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal, ROUND_HALF_UP

from .data.market import MarketData
from .models import CostSettings, OrderRejection, Trade


@dataclass
class Portfolio:
    cash: float
    positions: dict[str, int] = field(default_factory=dict)
    high_water_mark: float = 0.0


def portfolio_value(
    portfolio: Portfolio,
    market: MarketData,
    on_date: date,
    price_field: str = "close",
) -> float:
    value = portfolio.cash
    for symbol, quantity in portfolio.positions.items():
        bar = market.bar(symbol, on_date) or market.latest_bar_on_or_before(symbol, on_date)
        if bar is None:
            continue
        value += quantity * float(getattr(bar, price_field))
    return value


def execute_target_weights(
    portfolio: Portfolio,
    market: MarketData,
    on_date: date,
    target_weights: dict[str, float],
    costs: CostSettings,
    reason: str,
    minimum_rebalance_weight: float = 0.0,
    rejections: list[OrderRejection] | None = None,
) -> list[Trade]:
    rejection_log = rejections if rejections is not None else []
    equity_open = portfolio_value(portfolio, market, on_date, "open")
    target_quantities: dict[str, int] = {}
    for symbol, weight in target_weights.items():
        bar = market.bar(symbol, on_date)
        if bar is None or bar.open <= 0 or weight <= 0:
            if weight > 0:
                rejection_log.append(
                    OrderRejection(on_date, symbol, "BUY", "No valid opening bar")
                )
            continue
        lot = market.instrument(symbol).lot_size
        lots = int((equity_open * weight) // (bar.open * lot))
        target_quantities[symbol] = max(0, lots * lot)

    if minimum_rebalance_weight > 0 and equity_open > 0:
        for symbol in set(portfolio.positions) & set(target_weights):
            bar = market.bar(symbol, on_date)
            if bar is None:
                continue
            current = portfolio.positions[symbol]
            current_weight = current * bar.open / equity_open
            if abs(target_weights[symbol] - current_weight) < minimum_rebalance_weight:
                target_quantities[symbol] = current

    trades: list[Trade] = []
    all_symbols = set(portfolio.positions) | set(target_quantities)
    sell_blocked = False

    for symbol in sorted(all_symbols):
        current = portfolio.positions.get(symbol, 0)
        target = target_quantities.get(symbol, 0)
        if current <= target:
            continue
        bar = market.bar(symbol, on_date)
        if bar is None:
            sell_blocked = True
            rejection_log.append(
                OrderRejection(on_date, symbol, "SELL", "No opening bar")
            )
            continue
        restriction = _order_restriction(market, symbol, on_date, "SELL")
        if restriction:
            sell_blocked = True
            rejection_log.append(OrderRejection(on_date, symbol, "SELL", restriction))
            continue
        quantity = current - target
        schedule = costs.for_instrument(market.instrument(symbol), on_date)
        price = bar.open * (1.0 - schedule.slippage_bps / 10000.0)
        notional = quantity * price
        commission = _commission(notional, schedule)
        stamp_duty = notional * schedule.sell_stamp_duty_bps / 10000.0
        transfer_fee = notional * schedule.transfer_fee_bps / 10000.0
        portfolio.cash += notional - commission - stamp_duty - transfer_fee
        _set_position(portfolio, symbol, target)
        trades.append(
            Trade(
                on_date,
                symbol,
                "SELL",
                quantity,
                price,
                notional,
                commission,
                reason,
                stamp_duty=stamp_duty,
                transfer_fee=transfer_fee,
                slippage_cost=quantity * abs(price - bar.open),
            )
        )

    if sell_blocked:
        for symbol, target in sorted(target_quantities.items()):
            if target > portfolio.positions.get(symbol, 0):
                rejection_log.append(
                    OrderRejection(
                        on_date,
                        symbol,
                        "BUY",
                        "Buy phase blocked because a required sell could not execute",
                    )
                )
        return trades

    for symbol, target in sorted(
        target_quantities.items(), key=lambda item: target_weights.get(item[0], 0.0), reverse=True
    ):
        current = portfolio.positions.get(symbol, 0)
        desired = target - current
        if desired <= 0:
            continue
        bar = market.bar(symbol, on_date)
        if bar is None:
            rejection_log.append(
                OrderRejection(on_date, symbol, "BUY", "No opening bar")
            )
            continue
        restriction = _order_restriction(market, symbol, on_date, "BUY")
        if restriction:
            rejection_log.append(OrderRejection(on_date, symbol, "BUY", restriction))
            continue
        lot = market.instrument(symbol).lot_size
        schedule = costs.for_instrument(market.instrument(symbol), on_date)
        price = bar.open * (1.0 + schedule.slippage_bps / 10000.0)
        quantity = desired
        while quantity >= lot:
            notional = quantity * price
            commission = _commission(notional, schedule)
            transfer_fee = notional * schedule.transfer_fee_bps / 10000.0
            if notional + commission + transfer_fee <= portfolio.cash + 1e-8:
                break
            quantity -= lot
        if quantity < lot:
            continue
        notional = quantity * price
        commission = _commission(notional, schedule)
        transfer_fee = notional * schedule.transfer_fee_bps / 10000.0
        portfolio.cash -= notional + commission + transfer_fee
        _set_position(portfolio, symbol, current + quantity)
        trades.append(
            Trade(
                on_date,
                symbol,
                "BUY",
                quantity,
                price,
                notional,
                commission,
                reason,
                transfer_fee=transfer_fee,
                slippage_cost=quantity * abs(price - bar.open),
            )
        )
    return trades


def _commission(notional: float, costs: CostSettings) -> float:
    if notional <= 0:
        return 0.0
    return max(costs.minimum_commission, notional * costs.commission_bps / 10000.0)


def _set_position(portfolio: Portfolio, symbol: str, quantity: int) -> None:
    if quantity > 0:
        portfolio.positions[symbol] = quantity
    else:
        portfolio.positions.pop(symbol, None)


def _order_restriction(
    market: MarketData, symbol: str, on_date: date, side: str
) -> str | None:
    instrument = market.instrument(symbol)
    status = (
        market.trading_status(symbol, on_date)
        if hasattr(market, "trading_status")
        else None
    )
    if status is not None and not status.tradable:
        return f"Security status is {status.status}"
    bar = market.bar(symbol, on_date)
    if bar is None or bar.volume <= 0 or bar.amount <= 0:
        return "Security is suspended or has no executable volume"
    limit_pct = status.price_limit_pct if status is not None else instrument.price_limit_pct
    if limit_pct is None:
        return None
    previous = (
        market.previous_bar(symbol, on_date)
        if hasattr(market, "previous_bar")
        else None
    )
    if previous is None or previous.close <= 0:
        return None
    upper = _round_to_tick(previous.close * (1.0 + limit_pct), instrument.tick_size)
    lower = _round_to_tick(previous.close * (1.0 - limit_pct), instrument.tick_size)
    tolerance = instrument.tick_size / 2.0
    if side == "BUY" and bar.open >= upper - tolerance:
        return f"Opening price is at the {limit_pct:.1%} upper price limit"
    if side == "SELL" and bar.open <= lower + tolerance:
        return f"Opening price is at the {limit_pct:.1%} lower price limit"
    return None


def _round_to_tick(value: float, tick_size: float) -> float:
    tick = Decimal(str(tick_size))
    ticks = (Decimal(str(value)) / tick).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return float(ticks * tick)
