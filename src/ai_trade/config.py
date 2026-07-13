from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from typing import Any

from .models import CostSettings, Instrument, RiskSettings, StrategySettings
from .security import SecurityMaster


@dataclass(frozen=True)
class AppConfig:
    path: Path
    project_root: Path
    raw: dict[str, Any]
    instruments: tuple[Instrument, ...]
    strategy: StrategySettings
    risk: RiskSettings
    costs: CostSettings
    security_master: SecurityMaster
    universe_name: str
    minimum_listing_days: int

    def resolve(self, value: str) -> Path:
        candidate = Path(value)
        return candidate if candidate.is_absolute() else self.project_root / candidate

    @property
    def cache_dir(self) -> Path:
        return self.resolve(self.raw["data"]["cache_dir"])

    @property
    def reports_dir(self) -> Path:
        return self.resolve(self.raw["reports_dir"])

    @property
    def logs_dir(self) -> Path:
        return self.resolve(self.raw["logs_dir"])

    @property
    def paper_state_file(self) -> Path:
        return self.resolve(self.raw["paper"]["state_file"])

    @property
    def paper_trades_file(self) -> Path:
        return self.resolve(self.raw["paper"]["trades_file"])

    @property
    def paper_equity_file(self) -> Path:
        return self.resolve(self.raw["paper"]["equity_file"])

    @property
    def paper_rejections_file(self) -> Path:
        return self.resolve(
            self.raw["paper"].get("rejections_file", "state/paper_rejections.csv")
        )

    def active_symbols(self, on_date: date) -> tuple[str, ...]:
        return self.security_master.active_symbols(
            self.universe_name, on_date, self.minimum_listing_days
        )


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).resolve()
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    project_root = config_path.parent.parent
    master_spec = raw.get("security_master")
    if master_spec:
        master_path = Path(str(master_spec["file"]))
        if not master_path.is_absolute():
            master_path = project_root / master_path
        security_master = SecurityMaster.load(master_path.resolve())
        universe_name = str(master_spec["universe"])
        minimum_listing_days = int(master_spec.get("minimum_listing_days", 0))
        benchmark = str(raw["strategy"]["benchmark"])
        instruments = security_master.required_instruments(universe_name, benchmark)
    else:
        security_master = SecurityMaster.from_legacy(raw["universe"])
        universe_name = "legacy"
        minimum_listing_days = 0
        instruments = security_master.required_instruments(
            universe_name, str(raw["strategy"]["benchmark"])
        )
    strategy = StrategySettings(**raw["strategy"])
    risk = RiskSettings(**raw["risk"])
    cost_values = dict(raw["costs"])
    cost_values["history"] = tuple(cost_values.get("history", ()))
    costs = CostSettings(**cost_values)
    _validate(
        raw,
        instruments,
        strategy,
        risk,
        costs,
        security_master,
        universe_name,
        minimum_listing_days,
    )
    return AppConfig(
        config_path,
        project_root,
        raw,
        instruments,
        strategy,
        risk,
        costs,
        security_master,
        universe_name,
        minimum_listing_days,
    )


def _validate(
    raw: dict[str, Any],
    instruments: tuple[Instrument, ...],
    strategy: StrategySettings,
    risk: RiskSettings,
    costs: CostSettings,
    security_master: SecurityMaster,
    universe_name: str,
    minimum_listing_days: int,
) -> None:
    symbols = [item.symbol for item in instruments]
    if not instruments:
        raise ValueError("Universe must not be empty")
    if len(symbols) != len(set(symbols)):
        raise ValueError("Universe contains duplicate symbols")
    if universe_name not in security_master.universes:
        raise ValueError(f"Unknown configured universe: {universe_name!r}")
    if minimum_listing_days < 0:
        raise ValueError("security_master.minimum_listing_days must be non-negative")
    if raw["data"].get("provider") != "eastmoney":
        raise ValueError("Only data.provider='eastmoney' is supported")
    if raw["data"].get("adjustment", "forward") not in {"none", "forward", "backward"}:
        raise ValueError("data.adjustment must be none, forward, or backward")
    try:
        data_start = date.fromisoformat(raw["data"]["start"])
        data_end = date.fromisoformat(raw["data"]["end"])
        backtest_start = date.fromisoformat(raw["backtest"]["start"])
        backtest_end = date.fromisoformat(raw["backtest"]["end"])
        time.fromisoformat(raw["data"].get("market_close_time", "15:30"))
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Configuration contains an invalid date or time: {exc}") from exc
    if data_start > data_end or backtest_start > backtest_end:
        raise ValueError("Configuration start date must not be after end date")
    for instrument in instruments:
        if not re.fullmatch(r"\d{6}", instrument.symbol):
            raise ValueError(f"Invalid six-digit symbol: {instrument.symbol!r}")
        if instrument.market not in {"SH", "SZ"}:
            raise ValueError(f"Unsupported market for {instrument.symbol}: {instrument.market!r}")
        if not isinstance(instrument.lot_size, int) or instrument.lot_size <= 0:
            raise ValueError(f"lot_size must be a positive integer for {instrument.symbol}")
        if instrument.instrument_type not in {"ETF", "STOCK", "INDEX", "FUND"}:
            raise ValueError(
                f"Unsupported instrument_type for {instrument.symbol}: "
                f"{instrument.instrument_type!r}"
            )
        if instrument.listing_date and instrument.delisting_date:
            if instrument.delisting_date < instrument.listing_date:
                raise ValueError(f"delisting_date precedes listing_date for {instrument.symbol}")
        if instrument.price_limit_pct is not None and not 0 < instrument.price_limit_pct < 1:
            raise ValueError(f"price_limit_pct must be in (0, 1) for {instrument.symbol}")
        if not math.isfinite(instrument.tick_size) or instrument.tick_size <= 0:
            raise ValueError(f"tick_size must be positive for {instrument.symbol}")
    if strategy.benchmark not in symbols:
        raise ValueError("Benchmark must be part of the universe")
    for name in (
        "rebalance_days",
        "lookback_days",
        "trend_sma_days",
        "volatility_days",
    ):
        if getattr(strategy, name) <= 0:
            raise ValueError(f"strategy.{name} must be positive")
    if strategy.skip_days < 0:
        raise ValueError("strategy.skip_days must be non-negative")
    if strategy.top_n < 1 or strategy.top_n > len(symbols):
        raise ValueError("top_n is outside the universe size")
    if strategy.max_position_weight <= 0 or strategy.max_position_weight > 1:
        raise ValueError("max_position_weight must be in (0, 1]")
    if strategy.minimum_cash_weight < 0 or strategy.minimum_cash_weight >= 1:
        raise ValueError("minimum_cash_weight must be in [0, 1)")
    if strategy.target_annual_volatility < 0:
        raise ValueError("target_annual_volatility must be non-negative")
    if strategy.covariance_days < 0:
        raise ValueError("strategy.covariance_days must be non-negative")
    if strategy.covariance_shrinkage < 0 or strategy.covariance_shrinkage > 1:
        raise ValueError("strategy.covariance_shrinkage must be in [0, 1]")
    if not math.isfinite(strategy.minimum_average_amount) or strategy.minimum_average_amount < 0:
        raise ValueError("strategy.minimum_average_amount must be finite and non-negative")
    if strategy.minimum_rebalance_weight < 0 or strategy.minimum_rebalance_weight >= 1:
        raise ValueError("strategy.minimum_rebalance_weight must be in [0, 1)")
    if strategy.weighting_method not in {"inverse_volatility", "risk_parity"}:
        raise ValueError(
            "strategy.weighting_method must be inverse_volatility or risk_parity"
        )
    if strategy.risk_model not in {"conservative_sum", "covariance"}:
        raise ValueError("strategy.risk_model must be conservative_sum or covariance")
    for name in ("max_asset_class_weight", "max_sector_weight"):
        value = getattr(strategy, name)
        if value <= 0 or value > 1:
            raise ValueError(f"strategy.{name} must be in (0, 1]")
    if not math.isfinite(strategy.capacity_reference_cash) or strategy.capacity_reference_cash < 0:
        raise ValueError("strategy.capacity_reference_cash must be finite and non-negative")
    if (
        strategy.max_average_amount_participation <= 0
        or strategy.max_average_amount_participation > 1
    ):
        raise ValueError("strategy.max_average_amount_participation must be in (0, 1]")
    if strategy.capacity_days <= 0:
        raise ValueError("strategy.capacity_days must be positive")
    if risk.max_portfolio_drawdown <= 0 or risk.max_portfolio_drawdown >= 1:
        raise ValueError("max_portfolio_drawdown must be in (0, 1)")
    if risk.max_daily_loss <= 0 or risk.max_daily_loss >= 1:
        raise ValueError("max_daily_loss must be in (0, 1)")
    if risk.cooldown_days < 0:
        raise ValueError("cooldown_days must be non-negative")
    _validate_costs(costs)
    backtest_cash = float(raw["backtest"]["initial_cash"])
    paper_cash = float(raw["paper"]["initial_cash"])
    if not math.isfinite(backtest_cash) or backtest_cash <= 0:
        raise ValueError("initial_cash must be positive")
    if not math.isfinite(paper_cash) or paper_cash <= 0:
        raise ValueError("paper.initial_cash must be positive")
    if int(raw["paper"].get("minimum_promotion_sessions", 60)) < 20:
        raise ValueError("paper.minimum_promotion_sessions must be at least 20")


def _validate_costs(costs: CostSettings) -> None:
    names = (
        "commission_bps",
        "slippage_bps",
        "minimum_commission",
        "sell_stamp_duty_bps",
        "transfer_fee_bps",
    )
    schedules: list[dict[str, Any]] = [
        {name: getattr(costs, name) for name in names},
        *costs.by_instrument_type.values(),
        *costs.history,
    ]
    allowed_values = set(names)
    for instrument_type, schedule in costs.by_instrument_type.items():
        unknown = set(schedule) - allowed_values
        if unknown:
            raise ValueError(
                f"Unknown cost fields for {instrument_type}: {sorted(unknown)}"
            )
    for schedule in costs.history:
        unknown = set(schedule) - allowed_values - {"instrument_type", "start", "end"}
        if unknown:
            raise ValueError(f"Unknown cost history fields: {sorted(unknown)}")
    for schedule in schedules:
        for name in names:
            if name not in schedule:
                continue
            value = float(schedule[name])
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"Trading cost {name} must be finite and non-negative")
    periods: dict[str, list[tuple[date, date | None]]] = {}
    for value in costs.history:
        instrument_type = str(value.get("instrument_type", ""))
        if not instrument_type:
            raise ValueError("Each cost history row requires instrument_type")
        try:
            start = date.fromisoformat(str(value["start"]))
            end = date.fromisoformat(str(value["end"])) if value.get("end") else None
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid cost history period: {exc}") from exc
        if end and end < start:
            raise ValueError("Cost history end must not precede start")
        periods.setdefault(instrument_type, []).append((start, end))
    for instrument_type, values in periods.items():
        ordered = sorted(values)
        for previous, current in zip(ordered, ordered[1:]):
            previous_end = previous[1]
            if previous_end is None or current[0] <= previous_end:
                raise ValueError(f"Overlapping cost history for {instrument_type}")
