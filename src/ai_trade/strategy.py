from __future__ import annotations

import math
import statistics
from datetime import date

from .data.market import MarketData
from .models import Signal, SignalItem, StrategySettings


class MomentumTrendStrategy:
    def __init__(self, settings: StrategySettings):
        self.settings = settings

    def generate(self, market: MarketData, signal_date: date) -> Signal:
        settings = self.settings
        covariance_days = max(settings.covariance_days, settings.volatility_days)
        required = max(
            settings.lookback_days + settings.skip_days + 1,
            settings.trend_sma_days,
            settings.volatility_days + 1,
            covariance_days + 1,
        )
        ranked: list[SignalItem] = []

        for symbol, item in market.symbols.items():
            # A stale close must not compete with assets that completed this session.
            if market.bar(symbol, signal_date) is None:
                continue
            history = market.history(symbol, signal_date, required)
            if len(history) < required:
                continue
            closes = [bar.close for bar in history]
            current = closes[-1]
            momentum_end = len(closes) - 1 - settings.skip_days
            momentum_start = len(closes) - 1 - settings.lookback_days - settings.skip_days
            if momentum_start < 0 or closes[momentum_start] <= 0:
                continue
            momentum = closes[momentum_end] / closes[momentum_start] - 1.0
            trend = statistics.fmean(closes[-settings.trend_sma_days :])
            returns = _simple_returns(closes[-(settings.volatility_days + 1) :])
            annual_vol = statistics.stdev(returns) * math.sqrt(252) if len(returns) > 1 else 0.0
            average_amount = statistics.fmean(
                bar.amount for bar in history[-min(20, len(history)) :]
            )
            ranked.append(
                SignalItem(
                    symbol=symbol,
                    name=item.instrument.name,
                    momentum=momentum,
                    annual_volatility=annual_vol,
                    above_trend=current > trend,
                    average_amount=average_amount,
                )
            )

        ranked.sort(key=lambda value: value.momentum, reverse=True)
        eligible = [
            item
            for item in ranked
            if item.above_trend
            and item.momentum > settings.minimum_momentum
            and item.annual_volatility > 0
            and item.average_amount >= settings.minimum_average_amount
        ][: settings.top_n]

        if not eligible:
            return Signal(signal_date, {}, ranked, "No asset passed trend and momentum filters")

        symbols = [item.symbol for item in eligible]
        covariance = _covariance_matrix(
            market,
            symbols,
            signal_date,
            covariance_days,
            settings.covariance_shrinkage,
        )
        if covariance is None:
            covariance = [
                [
                    (eligible[i].annual_volatility**2 / 252.0) if i == j else 0.0
                    for j in range(len(eligible))
                ]
                for i in range(len(eligible))
            ]
        if settings.weighting_method == "risk_parity":
            relative_values = _risk_parity_weights(covariance)
        else:
            inverse = [1.0 / item.annual_volatility for item in eligible]
            inverse_total = sum(inverse)
            relative_values = [value / inverse_total for value in inverse]
        relative = dict(zip(symbols, relative_values))
        if settings.risk_model == "covariance":
            portfolio_volatility = _portfolio_volatility(relative_values, covariance)
        else:
            portfolio_volatility = sum(
                relative_values[index] * eligible[index].annual_volatility
                for index in range(len(eligible))
            )
        max_exposure = 1.0 - settings.minimum_cash_weight
        exposure = max_exposure
        if settings.target_annual_volatility > 0 and portfolio_volatility > 0:
            exposure = min(
                max_exposure,
                settings.target_annual_volatility / portfolio_volatility,
            )

        weights = _capped_weights(relative, exposure, settings.max_position_weight)
        final_values = [weights.get(symbol, 0.0) for symbol in symbols]
        if settings.risk_model == "covariance":
            estimated_volatility = _portfolio_volatility(final_values, covariance)
        else:
            estimated_volatility = sum(
                final_values[index] * eligible[index].annual_volatility
                for index in range(len(eligible))
            )
        if (
            settings.target_annual_volatility > 0
            and estimated_volatility > settings.target_annual_volatility
        ):
            scale = settings.target_annual_volatility / estimated_volatility
            weights = {symbol: weight * scale for symbol, weight in weights.items()}
        for item in ranked:
            item.weight = weights.get(item.symbol, 0.0)
        selected = ", ".join(symbol for symbol, weight in weights.items() if weight > 0)
        return Signal(
            signal_date,
            weights,
            ranked,
            (
                f"Selected {selected}; gross exposure {sum(weights.values()):.1%}; "
                f"estimated volatility {estimated_volatility:.1%}; "
                f"weighting {settings.weighting_method}; risk model {settings.risk_model}"
            ),
        )


def _simple_returns(values: list[float]) -> list[float]:
    return [values[index] / values[index - 1] - 1.0 for index in range(1, len(values))]


def _covariance_matrix(
    market: MarketData,
    symbols: list[str],
    signal_date: date,
    count: int,
    shrinkage: float,
) -> list[list[float]] | None:
    return_maps: list[dict[date, float]] = []
    for symbol in symbols:
        history = market.history(symbol, signal_date, count + 1)
        values: dict[date, float] = {}
        for index in range(1, len(history)):
            previous = history[index - 1].close
            if previous > 0:
                values[history[index].date] = history[index].close / previous - 1.0
        return_maps.append(values)
    if not return_maps:
        return None
    common_dates = sorted(set.intersection(*(set(values) for values in return_maps)))[-count:]
    if len(common_dates) < max(5, len(symbols) + 1):
        return None
    series = [[values[value] for value in common_dates] for values in return_maps]
    means = [statistics.fmean(values) for values in series]
    denominator = len(common_dates) - 1
    covariance = []
    for i, left in enumerate(series):
        row = []
        for j, right in enumerate(series):
            value = sum(
                (left[index] - means[i]) * (right[index] - means[j])
                for index in range(len(common_dates))
            ) / denominator
            row.append(value if i == j else value * (1.0 - shrinkage))
        covariance.append(row)
    if any(covariance[index][index] <= 0 for index in range(len(covariance))):
        return None
    return covariance


def _risk_parity_weights(covariance: list[list[float]]) -> list[float]:
    count = len(covariance)
    if count == 0:
        return []
    weights = [1.0 / math.sqrt(covariance[index][index]) for index in range(count)]
    budget = 1.0 / count

    # Cyclical coordinate descent for x_i * (Sigma x)_i = risk_budget_i.
    for _ in range(100):
        previous = list(weights)
        for i in range(count):
            diagonal = covariance[i][i]
            cross = sum(
                covariance[i][j] * weights[j] for j in range(count) if j != i
            )
            discriminant = max(0.0, cross * cross + 4.0 * diagonal * budget)
            weights[i] = max(
                1e-12,
                (-cross + math.sqrt(discriminant)) / (2.0 * diagonal),
            )
        if max(abs(weights[i] - previous[i]) for i in range(count)) < 1e-10:
            break
    scale = sum(weights)
    return [value / scale for value in weights]


def _portfolio_volatility(
    weights: list[float], covariance: list[list[float]]
) -> float:
    variance = sum(
        weights[i] * weights[j] * covariance[i][j]
        for i in range(len(weights))
        for j in range(len(weights))
    )
    return math.sqrt(max(0.0, variance) * 252.0)


def _capped_weights(raw: dict[str, float], total: float, cap: float) -> dict[str, float]:
    if not raw or total <= 0:
        return {}
    target_total = min(total, cap * len(raw))
    remaining = dict(raw)
    weights: dict[str, float] = {}
    unallocated = target_total

    while remaining and unallocated > 1e-12:
        denominator = sum(remaining.values())
        proposed = {
            symbol: unallocated * value / denominator for symbol, value in remaining.items()
        }
        capped = [symbol for symbol, weight in proposed.items() if weight > cap]
        if not capped:
            weights.update(proposed)
            break
        for symbol in capped:
            weights[symbol] = cap
            unallocated -= cap
            remaining.pop(symbol)
    return weights
