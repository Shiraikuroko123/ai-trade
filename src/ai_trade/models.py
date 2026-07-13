from __future__ import annotations

from dataclasses import dataclass, field, replace
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
    instrument_type: str = "ETF"
    asset_class: str = "other"
    sector: str = "other"
    currency: str = "CNY"
    board: str = ""
    listing_date: date | None = None
    delisting_date: date | None = None
    price_limit_pct: float | None = None
    tick_size: float = 0.01


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
    max_asset_class_weight: float = 1.0
    max_sector_weight: float = 1.0
    capacity_reference_cash: float = 0.0
    max_average_amount_participation: float = 1.0
    capacity_days: int = 1


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
    sell_stamp_duty_bps: float = 0.0
    transfer_fee_bps: float = 0.0
    by_instrument_type: dict[str, dict[str, float]] = field(default_factory=dict)
    history: tuple[dict[str, Any], ...] = ()

    def for_instrument(self, instrument: Instrument, on_date: date) -> "CostSettings":
        values = {
            "commission_bps": self.commission_bps,
            "slippage_bps": self.slippage_bps,
            "minimum_commission": self.minimum_commission,
            "sell_stamp_duty_bps": self.sell_stamp_duty_bps,
            "transfer_fee_bps": self.transfer_fee_bps,
        }
        values.update(self.by_instrument_type.get(instrument.instrument_type, {}))
        for period in self.history:
            if period.get("instrument_type") != instrument.instrument_type:
                continue
            start = date.fromisoformat(str(period["start"]))
            end_raw = period.get("end")
            end = date.fromisoformat(str(end_raw)) if end_raw else None
            if start <= on_date and (end is None or on_date <= end):
                values.update(
                    {
                        key: float(period[key])
                        for key in _COST_VALUE_FIELDS
                        if key in period
                    }
                )
        return CostSettings(**values)

    def scaled(self, multiplier: float) -> "CostSettings":
        if multiplier <= 0:
            raise ValueError("Cost multiplier must be positive")

        def scale(values: dict[str, Any]) -> dict[str, Any]:
            return {
                key: (float(value) * multiplier if key in _COST_VALUE_FIELDS else value)
                for key, value in values.items()
            }

        return replace(
            self,
            commission_bps=self.commission_bps * multiplier,
            slippage_bps=self.slippage_bps * multiplier,
            minimum_commission=self.minimum_commission * multiplier,
            sell_stamp_duty_bps=self.sell_stamp_duty_bps * multiplier,
            transfer_fee_bps=self.transfer_fee_bps * multiplier,
            by_instrument_type={
                key: scale(value) for key, value in self.by_instrument_type.items()
            },
            history=tuple(scale(value) for value in self.history),
        )


_COST_VALUE_FIELDS = {
    "commission_bps",
    "slippage_bps",
    "minimum_commission",
    "sell_stamp_duty_bps",
    "transfer_fee_bps",
}


@dataclass
class SignalItem:
    symbol: str
    name: str
    momentum: float
    annual_volatility: float
    above_trend: bool
    average_amount: float = 0.0
    weight: float = 0.0
    asset_class: str = "other"
    sector: str = "other"
    capacity_weight: float = 1.0


@dataclass
class Signal:
    date: date
    target_weights: dict[str, float]
    ranked: list[SignalItem]
    reason: str
    diagnostics: dict[str, Any] = field(default_factory=dict)


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
    stamp_duty: float = 0.0
    transfer_fee: float = 0.0
    slippage_cost: float = 0.0

    @property
    def total_transaction_cost(self) -> float:
        return self.commission + self.stamp_duty + self.transfer_fee + self.slippage_cost


@dataclass(frozen=True)
class OrderRejection:
    date: date
    symbol: str
    side: str
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
