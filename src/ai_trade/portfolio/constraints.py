from __future__ import annotations

from dataclasses import asdict, dataclass
import math
from typing import Any

from ..config import AppConfig


@dataclass(frozen=True)
class PortfolioConstraints:
    minimum_cash_weight: float
    max_position_weight: float
    max_asset_class_weight: float
    max_sector_weight: float
    max_turnover: float = 0.50
    target_annual_volatility: float = 0.12
    max_average_amount_participation: float = 0.05
    capacity_days: int = 1
    minimum_net_alpha_bps: float = 1.0
    uncertainty_penalty: float = 0.25

    def __post_init__(self) -> None:
        bounded = {
            "minimum_cash_weight": self.minimum_cash_weight,
            "max_position_weight": self.max_position_weight,
            "max_asset_class_weight": self.max_asset_class_weight,
            "max_sector_weight": self.max_sector_weight,
            "max_turnover": self.max_turnover,
            "max_average_amount_participation": self.max_average_amount_participation,
        }
        for name, value in bounded.items():
            if not math.isfinite(value) or not 0 <= value <= 1:
                raise ValueError(f"Portfolio constraint {name} must be in [0, 1]")
        if self.minimum_cash_weight >= 1:
            raise ValueError("Portfolio minimum cash must be below 1")
        if self.max_position_weight <= 0:
            raise ValueError("Portfolio max position weight must be positive")
        if not math.isfinite(self.target_annual_volatility) or self.target_annual_volatility < 0:
            raise ValueError("Portfolio target volatility must be non-negative")
        if isinstance(self.capacity_days, bool) or not isinstance(self.capacity_days, int) or self.capacity_days < 1:
            raise ValueError("Portfolio capacity_days must be a positive integer")
        if not math.isfinite(self.minimum_net_alpha_bps) or self.minimum_net_alpha_bps < 0:
            raise ValueError("Portfolio minimum net alpha must be non-negative")
        if not math.isfinite(self.uncertainty_penalty) or self.uncertainty_penalty < 0:
            raise ValueError("Portfolio uncertainty penalty must be non-negative")

    @classmethod
    def from_config(cls, config: AppConfig) -> "PortfolioConstraints":
        raw = config.raw.get("portfolio_construction", {})
        if not isinstance(raw, dict):
            raise ValueError("portfolio_construction must be an object")
        return cls(
            minimum_cash_weight=config.strategy.minimum_cash_weight,
            max_position_weight=config.strategy.max_position_weight,
            max_asset_class_weight=config.strategy.max_asset_class_weight,
            max_sector_weight=config.strategy.max_sector_weight,
            max_turnover=float(raw.get("max_turnover", 0.50)),
            target_annual_volatility=config.strategy.target_annual_volatility,
            max_average_amount_participation=config.strategy.max_average_amount_participation,
            capacity_days=config.strategy.capacity_days,
            minimum_net_alpha_bps=float(raw.get("minimum_net_alpha_bps", 1.0)),
            uncertainty_penalty=float(raw.get("uncertainty_penalty", 0.25)),
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


__all__ = ["PortfolioConstraints"]
