from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .data.market import MarketData
from .models import CostSettings, Trade


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
) -> list[Trade]:
    equity_open = portfolio_value(portfolio, market, on_date, "open")
    target_quantities: dict[str, int] = {}
    for symbol, weight in target_weights.items():
        bar = market.bar(symbol, on_date)
        if bar is None or bar.open <= 0 or weight <= 0:
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

    for symbol in sorted(all_symbols):
        current = portfolio.positions.get(symbol, 0)
        target = target_quantities.get(symbol, 0)
        if current <= target:
            continue
        bar = market.bar(symbol, on_date)
        if bar is None:
            continue
        quantity = current - target
        price = bar.open * (1.0 - costs.slippage_bps / 10000.0)
        notional = quantity * price
        commission = _commission(notional, costs)
        portfolio.cash += notional - commission
        _set_position(portfolio, symbol, target)
        trades.append(Trade(on_date, symbol, "SELL", quantity, price, notional, commission, reason))

    for symbol, target in sorted(
        target_quantities.items(), key=lambda item: target_weights.get(item[0], 0.0), reverse=True
    ):
        current = portfolio.positions.get(symbol, 0)
        desired = target - current
        if desired <= 0:
            continue
        bar = market.bar(symbol, on_date)
        if bar is None:
            continue
        lot = market.instrument(symbol).lot_size
        price = bar.open * (1.0 + costs.slippage_bps / 10000.0)
        quantity = desired
        while quantity >= lot:
            notional = quantity * price
            commission = _commission(notional, costs)
            if notional + commission <= portfolio.cash + 1e-8:
                break
            quantity -= lot
        if quantity < lot:
            continue
        notional = quantity * price
        commission = _commission(notional, costs)
        portfolio.cash -= notional + commission
        _set_position(portfolio, symbol, current + quantity)
        trades.append(Trade(on_date, symbol, "BUY", quantity, price, notional, commission, reason))
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
