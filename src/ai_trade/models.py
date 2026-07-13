from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any


@dataclass(frozen=True)
class Bar:
    date: date
    open: float
    close: float
    high: float
    low: float
    volume: float
    amount: float


@dataclass(frozen=True)
class Instrument:
    symbol: str
    name: str
    market: str
    asset: str
    lot_size: int = 100


@dataclass(frozen=True)
class StrategySettings:
    benchmark: str
    rebalance_days: int
    lookback_days: int
    skip_days: int
    trend_sma_days: int
    volatility_days: int
    top_n: int
    minimum_momentum: float
    target_annual_volatility: float
    minimum_cash_weight: float
    max_position_weight: float
    covariance_days: int = 0
    covariance_shrinkage: float = 0.25
    minimum_average_amount: float = 0.0
    minimum_rebalance_weight: float = 0.0
    weighting_method: str = "inverse_volatility"
    risk_model: str = "conservative_sum"


@dataclass(frozen=True)
class RiskSettings:
    max_portfolio_drawdown: float
    max_daily_loss: float
    cooldown_days: int


@dataclass(frozen=True)
class CostSettings:
    commission_bps: float
    slippage_bps: float
    minimum_commission: float


@dataclass
class SignalItem:
    symbol: str
    name: str
    momentum: float
    annual_volatility: float
    above_trend: bool
    average_amount: float = 0.0
    weight: float = 0.0


@dataclass
class Signal:
    date: date
    target_weights: dict[str, float]
    ranked: list[SignalItem]
    reason: str


@dataclass
class Trade:
    date: date
    symbol: str
    side: str
    quantity: int
    price: float
    notional: float
    commission: float
    reason: str


@dataclass
class EquityPoint:
    date: date
    equity: float
    cash: float
    drawdown: float


@dataclass
class BacktestResult:
    equity_curve: list[EquityPoint]
    benchmark_curve: list[EquityPoint]
    trades: list[Trade]
    metrics: dict[str, float]
    benchmark_metrics: dict[str, float]
    latest_signal: Signal | None
    metadata: dict[str, Any] = field(default_factory=dict)
