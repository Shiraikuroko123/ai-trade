from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from typing import Any

from .models import CostSettings, Instrument, RiskSettings, StrategySettings


@dataclass(frozen=True)
class AppConfig:
    path: Path
    project_root: Path
    raw: dict[str, Any]
    instruments: tuple[Instrument, ...]
    strategy: StrategySettings
    risk: RiskSettings
    costs: CostSettings

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


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).resolve()
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    project_root = config_path.parent.parent
    instruments = tuple(Instrument(**item) for item in raw["universe"])
    strategy = StrategySettings(**raw["strategy"])
    risk = RiskSettings(**raw["risk"])
    costs = CostSettings(**raw["costs"])
    _validate(raw, instruments, strategy, risk, costs)
    return AppConfig(config_path, project_root, raw, instruments, strategy, risk, costs)


def _validate(
    raw: dict[str, Any],
    instruments: tuple[Instrument, ...],
    strategy: StrategySettings,
    risk: RiskSettings,
    costs: CostSettings,
) -> None:
    symbols = [item.symbol for item in instruments]
    if not instruments:
        raise ValueError("Universe must not be empty")
    if len(symbols) != len(set(symbols)):
        raise ValueError("Universe contains duplicate symbols")
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
    if risk.max_portfolio_drawdown <= 0 or risk.max_portfolio_drawdown >= 1:
        raise ValueError("max_portfolio_drawdown must be in (0, 1)")
    if risk.max_daily_loss <= 0 or risk.max_daily_loss >= 1:
        raise ValueError("max_daily_loss must be in (0, 1)")
    if risk.cooldown_days < 0:
        raise ValueError("cooldown_days must be non-negative")
    if any(
        not math.isfinite(value) or value < 0
        for value in (costs.commission_bps, costs.slippage_bps, costs.minimum_commission)
    ):
        raise ValueError("Trading costs must be finite and non-negative")
    backtest_cash = float(raw["backtest"]["initial_cash"])
    paper_cash = float(raw["paper"]["initial_cash"])
    if not math.isfinite(backtest_cash) or backtest_cash <= 0:
        raise ValueError("initial_cash must be positive")
    if not math.isfinite(paper_cash) or paper_cash <= 0:
        raise ValueError("paper.initial_cash must be positive")
    if int(raw["paper"].get("minimum_promotion_sessions", 60)) < 20:
        raise ValueError("paper.minimum_promotion_sessions must be at least 20")
