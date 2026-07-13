from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime
from typing import Mapping

from .config import AppConfig
from .data.market import MarketData
from .execution import Portfolio, execute_target_weights, portfolio_value
from .metrics import calculate_metrics
from .models import BacktestResult, EquityPoint, Signal, StrategySettings
from .strategy import MomentumTrendStrategy


class BacktestEngine:
    def __init__(
        self,
        config: AppConfig,
        market: MarketData,
        strategy_settings: StrategySettings | None = None,
    ):
        self.config = config
        self.market = market
        self.strategy_settings = strategy_settings or config.strategy
        self.strategy = MomentumTrendStrategy(self.strategy_settings)

    def run(
        self,
        start: date | None = None,
        end: date | None = None,
        initial_cash: float | None = None,
    ) -> BacktestResult:
        return self._run(start, end, initial_cash, {})

    def run_scheduled(
        self,
        start: date,
        end: date,
        settings_by_date: Mapping[date, StrategySettings],
        initial_cash: float | None = None,
    ) -> BacktestResult:
        if not settings_by_date:
            raise ValueError("A scheduled backtest requires at least one settings update")
        return self._run(start, end, initial_cash, settings_by_date)

    def _run(
        self,
        start: date | None,
        end: date | None,
        initial_cash: float | None,
        settings_by_date: Mapping[date, StrategySettings],
    ) -> BacktestResult:
        backtest_cfg = self.config.raw["backtest"]
        start = start or _parse_date(backtest_cfg["start"])
        end = end or _parse_date(backtest_cfg["end"])
        initial_cash = float(
            backtest_cfg["initial_cash"] if initial_cash is None else initial_cash
        )
        if initial_cash <= 0:
            raise ValueError("initial_cash must be positive")
        calendar = [day for day in self.market.calendar if start <= day <= end]
        if len(calendar) < 2:
            raise ValueError("Backtest date range has fewer than two trading days")

        portfolio = Portfolio(cash=initial_cash, high_water_mark=initial_cash)
        pending_targets: dict[str, float] | None = None
        pending_reason = ""
        pending_rebalance_band = 0.0
        cooldown_remaining = 0
        trades = []
        order_rejections = []
        curve: list[EquityPoint] = []
        previous_equity = initial_cash
        active_settings = self.strategy_settings
        last_signal_index: int | None = None
        last_rebalance_signal: Signal | None = None

        for index, on_date in enumerate(calendar):
            schedule_changed = on_date in settings_by_date
            if schedule_changed:
                active_settings = settings_by_date[on_date]
            if pending_targets is not None:
                trades.extend(
                    execute_target_weights(
                        portfolio,
                        self.market,
                        on_date,
                        pending_targets,
                        self.config.costs,
                        pending_reason,
                        pending_rebalance_band,
                        order_rejections,
                    )
                )
                pending_targets = None
                pending_rebalance_band = 0.0

            equity = portfolio_value(portfolio, self.market, on_date, "close")
            portfolio.high_water_mark = max(portfolio.high_water_mark, equity)
            drawdown = equity / portfolio.high_water_mark - 1.0 if portfolio.high_water_mark > 0 else 0.0
            daily_return = equity / previous_equity - 1.0 if previous_equity > 0 else 0.0
            curve.append(EquityPoint(on_date, equity, portfolio.cash, drawdown))
            previous_equity = equity

            risk_triggered = bool(portfolio.positions) and (
                drawdown <= -self.config.risk.max_portfolio_drawdown
                or daily_return <= -self.config.risk.max_daily_loss
            )
            if risk_triggered:
                cooldown_remaining = self.config.risk.cooldown_days
                pending_targets = {}
                pending_reason = "Portfolio risk stop"
                pending_rebalance_band = 0.0
                continue

            if cooldown_remaining > 0:
                cooldown_remaining -= 1
                pending_targets = {}
                pending_reason = "Risk cooldown"
                pending_rebalance_band = 0.0
                if cooldown_remaining == 0:
                    portfolio.high_water_mark = equity
                continue

            signal_due = (
                schedule_changed
                or last_signal_index is None
                or index - last_signal_index >= active_settings.rebalance_days
            )
            if signal_due:
                last_rebalance_signal = MomentumTrendStrategy(active_settings).generate(
                    self.market, on_date, equity
                )
                pending_targets = last_rebalance_signal.target_weights
                pending_reason = last_rebalance_signal.reason
                pending_rebalance_band = active_settings.minimum_rebalance_weight
                last_signal_index = index

        latest_signal = MomentumTrendStrategy(active_settings).generate(
            self.market, calendar[-1], curve[-1].equity
        )

        benchmark_curve = _benchmark_curve(
            self.market,
            active_settings.benchmark,
            calendar,
            initial_cash,
        )
        metrics = calculate_metrics(curve, trades)
        benchmark_metrics = calculate_metrics(benchmark_curve)
        return BacktestResult(
            equity_curve=curve,
            benchmark_curve=benchmark_curve,
            trades=trades,
            metrics=metrics,
            benchmark_metrics=benchmark_metrics,
            latest_signal=latest_signal,
            metadata={
                "start": calendar[0].isoformat(),
                "end": calendar[-1].isoformat(),
                "initial_cash": initial_cash,
                "strategy": self.strategy_settings.__dict__,
                "last_rebalance_signal_date": (
                    last_rebalance_signal.date.isoformat() if last_rebalance_signal else None
                ),
                "data_snapshot": self.market.snapshot_metadata(),
                "settings_schedule": [
                    {"date": value.isoformat(), "settings": settings_by_date[value].__dict__}
                    for value in sorted(settings_by_date)
                ],
                "order_rejection_count": len(order_rejections),
                "order_rejections": [
                    {
                        "date": value.date.isoformat(),
                        "symbol": value.symbol,
                        "side": value.side,
                        "reason": value.reason,
                    }
                    for value in order_rejections
                ],
            },
        )

    def with_settings(self, **changes: object) -> "BacktestEngine":
        return BacktestEngine(self.config, self.market, replace(self.strategy_settings, **changes))


def _benchmark_curve(
    market: MarketData,
    symbol: str,
    calendar: list[date],
    initial_cash: float,
) -> list[EquityPoint]:
    entry = market.bar(symbol, calendar[1])
    if entry is None or entry.open <= 0:
        return []
    units = initial_cash / entry.open
    high_water = initial_cash
    result = []
    for index, on_date in enumerate(calendar):
        bar = market.bar(symbol, on_date) or market.latest_bar_on_or_before(symbol, on_date)
        if bar is None:
            continue
        equity = initial_cash if index == 0 else units * bar.close
        high_water = max(high_water, equity)
        drawdown = equity / high_water - 1.0
        result.append(EquityPoint(on_date, equity, initial_cash if index == 0 else 0.0, drawdown))
    return result


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()
