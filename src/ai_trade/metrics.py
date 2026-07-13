from __future__ import annotations

import math
import statistics

from .models import EquityPoint, Trade


def calculate_metrics(curve: list[EquityPoint], trades: list[Trade] | None = None) -> dict[str, float]:
    if len(curve) < 2:
        return {
            "total_return": 0.0,
            "cagr": 0.0,
            "annual_volatility": 0.0,
            "sharpe": 0.0,
            "sortino": 0.0,
            "max_drawdown": 0.0,
            "calmar": 0.0,
            "positive_day_ratio": 0.0,
            "value_at_risk_95": 0.0,
            "expected_shortfall_95": 0.0,
            "worst_day": 0.0,
            "best_day": 0.0,
            "longest_drawdown_sessions": 0.0,
            "monthly_win_ratio": 0.0,
            "trade_count": float(len(trades or [])),
            "invested_days": 0.0,
            "turnover": 0.0,
            "commissions": 0.0,
            "stamp_duty": 0.0,
            "transfer_fees": 0.0,
            "slippage_cost": 0.0,
            "transaction_costs": 0.0,
        }

    equities = [point.equity for point in curve]
    returns = [equities[i] / equities[i - 1] - 1.0 for i in range(1, len(equities)) if equities[i - 1] > 0]
    years = max((curve[-1].date - curve[0].date).days / 365.25, 1.0 / 252.0)
    total_return = equities[-1] / equities[0] - 1.0
    cagr = (equities[-1] / equities[0]) ** (1.0 / years) - 1.0 if equities[0] > 0 else 0.0
    volatility = statistics.stdev(returns) * math.sqrt(252) if len(returns) > 1 else 0.0
    mean_return = statistics.fmean(returns) if returns else 0.0
    sharpe = mean_return / statistics.stdev(returns) * math.sqrt(252) if len(returns) > 1 and statistics.stdev(returns) > 0 else 0.0
    downside = [min(value, 0.0) for value in returns]
    downside_dev = math.sqrt(statistics.fmean([value * value for value in downside])) if downside else 0.0
    sortino = mean_return / downside_dev * math.sqrt(252) if downside_dev > 0 else 0.0
    max_drawdown = min((point.drawdown for point in curve), default=0.0)
    calmar = cagr / abs(max_drawdown) if max_drawdown < 0 else 0.0
    positive_ratio = sum(1 for value in returns if value > 0) / len(returns) if returns else 0.0
    value_at_risk = _percentile(returns, 0.05) if returns else 0.0
    tail = [value for value in returns if value <= value_at_risk]
    expected_shortfall = statistics.fmean(tail) if tail else value_at_risk
    longest_drawdown = _longest_drawdown(curve)
    monthly_win_ratio = _monthly_win_ratio(curve)
    trade_list = trades or []
    average_equity = statistics.fmean(equities)
    turnover = sum(trade.notional for trade in trade_list) / average_equity if average_equity > 0 else 0.0
    commissions = sum(trade.commission for trade in trade_list)
    stamp_duty = sum(trade.stamp_duty for trade in trade_list)
    transfer_fees = sum(trade.transfer_fee for trade in trade_list)
    slippage_cost = sum(trade.slippage_cost for trade in trade_list)
    invested_days = sum(
        1 for point in curve if point.equity > 0 and point.cash < point.equity - 1e-8
    )
    return {
        "total_return": total_return,
        "cagr": cagr,
        "annual_volatility": volatility,
        "sharpe": sharpe,
        "sortino": sortino,
        "max_drawdown": max_drawdown,
        "calmar": calmar,
        "positive_day_ratio": positive_ratio,
        "value_at_risk_95": value_at_risk,
        "expected_shortfall_95": expected_shortfall,
        "worst_day": min(returns, default=0.0),
        "best_day": max(returns, default=0.0),
        "longest_drawdown_sessions": float(longest_drawdown),
        "monthly_win_ratio": monthly_win_ratio,
        "trade_count": float(len(trade_list)),
        "invested_days": float(invested_days),
        "turnover": turnover,
        "commissions": commissions,
        "stamp_duty": stamp_duty,
        "transfer_fees": transfer_fees,
        "slippage_cost": slippage_cost,
        "transaction_costs": commissions + stamp_duty + transfer_fees + slippage_cost,
    }


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _longest_drawdown(curve: list[EquityPoint]) -> int:
    longest = 0
    current = 0
    for point in curve:
        if point.drawdown < -1e-12:
            current += 1
            longest = max(longest, current)
        else:
            current = 0
    return longest


def _monthly_win_ratio(curve: list[EquityPoint]) -> float:
    month_end: dict[tuple[int, int], float] = {}
    for point in curve:
        month_end[(point.date.year, point.date.month)] = point.equity
    values = list(month_end.values())
    if len(values) < 2:
        return 0.0
    returns = [values[index] / values[index - 1] - 1.0 for index in range(1, len(values))]
    return sum(value > 0 for value in returns) / len(returns)
