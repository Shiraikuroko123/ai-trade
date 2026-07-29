from __future__ import annotations

from dataclasses import dataclass
import math
import statistics
from typing import Any, Callable

from ..models import Bar
from ..numeric import sample_standard_deviation


LIBRARY_VERSION = 1


@dataclass(frozen=True)
class FactorDefinition:
    """One deterministic, point-in-time factor over completed daily bars.

    ``direction`` is the registered research hypothesis only: +1 means the
    mechanism expects higher values to precede higher forward returns, -1 the
    opposite. Evaluation reports raw rank correlations next to this registered
    direction; the definition never creates a signal, weight, or order.
    """

    factor_id: str
    version: int
    label: str
    family: str
    direction: int
    minimum_history: int
    formula: str
    compute: Callable[[list[Bar]], float | None]

    def to_dict(self) -> dict[str, Any]:
        return {
            "factor_id": self.factor_id,
            "version": self.version,
            "label": self.label,
            "family": self.family,
            "direction": self.direction,
            "minimum_history": self.minimum_history,
            "formula": self.formula,
        }


def _closes(history: list[Bar]) -> list[float]:
    return [bar.close for bar in history]


def _momentum(lookback: int, skip: int) -> Callable[[list[Bar]], float | None]:
    def compute(history: list[Bar]) -> float | None:
        closes = _closes(history)
        end = len(closes) - 1 - skip
        start = len(closes) - 1 - lookback - skip
        if start < 0 or closes[start] <= 0 or closes[end] <= 0:
            return None
        return closes[end] / closes[start] - 1.0

    return compute


def _trend_gap(window: int) -> Callable[[list[Bar]], float | None]:
    def compute(history: list[Bar]) -> float | None:
        closes = _closes(history)
        if len(closes) < window or closes[-1] <= 0:
            return None
        mean = statistics.fmean(closes[-window:])
        if mean <= 0:
            return None
        return closes[-1] / mean - 1.0

    return compute


def _volatility(window: int) -> Callable[[list[Bar]], float | None]:
    def compute(history: list[Bar]) -> float | None:
        closes = _closes(history)
        if len(closes) < window + 1:
            return None
        tail = closes[-(window + 1) :]
        if any(value <= 0 for value in tail):
            return None
        returns = [
            tail[index] / tail[index - 1] - 1.0 for index in range(1, len(tail))
        ]
        if len(returns) < 2:
            return None
        return sample_standard_deviation(returns) * math.sqrt(252)

    return compute


def _reversal(window: int) -> Callable[[list[Bar]], float | None]:
    def compute(history: list[Bar]) -> float | None:
        closes = _closes(history)
        if len(closes) < window + 1:
            return None
        start = closes[-(window + 1)]
        if start <= 0 or closes[-1] <= 0:
            return None
        return closes[-1] / start - 1.0

    return compute


def _amount_surge(short: int, long: int) -> Callable[[list[Bar]], float | None]:
    def compute(history: list[Bar]) -> float | None:
        amounts = [bar.amount for bar in history]
        if len(amounts) < long:
            return None
        long_mean = statistics.fmean(amounts[-long:])
        short_mean = statistics.fmean(amounts[-short:])
        if long_mean <= 0 or short_mean < 0:
            return None
        return short_mean / long_mean - 1.0

    return compute


def _drawdown_from_high(window: int) -> Callable[[list[Bar]], float | None]:
    def compute(history: list[Bar]) -> float | None:
        closes = _closes(history)
        if len(closes) < window:
            return None
        tail = closes[-window:]
        peak = max(tail)
        if peak <= 0 or tail[-1] <= 0:
            return None
        return tail[-1] / peak - 1.0

    return compute


FACTORS: tuple[FactorDefinition, ...] = (
    FactorDefinition(
        "momentum_120_5",
        1,
        "120日动量（跳过5日）",
        "momentum",
        1,
        126,
        "close[t-5] / close[t-125] - 1",
        _momentum(120, 5),
    ),
    FactorDefinition(
        "momentum_60_5",
        1,
        "60日动量（跳过5日）",
        "momentum",
        1,
        66,
        "close[t-5] / close[t-65] - 1",
        _momentum(60, 5),
    ),
    FactorDefinition(
        "trend_gap_100",
        1,
        "收盘价相对100日均线偏离",
        "trend",
        1,
        100,
        "close[t] / SMA(close, 100) - 1",
        _trend_gap(100),
    ),
    FactorDefinition(
        "volatility_60",
        1,
        "60日年化波动率",
        "risk",
        -1,
        61,
        "stdev(daily returns, 60) * sqrt(252)",
        _volatility(60),
    ),
    FactorDefinition(
        "reversal_5",
        1,
        "5日短期反转",
        "reversal",
        -1,
        6,
        "close[t] / close[t-5] - 1",
        _reversal(5),
    ),
    FactorDefinition(
        "amount_surge_20_60",
        1,
        "20/60日成交额放大",
        "liquidity",
        1,
        60,
        "mean(amount, 20) / mean(amount, 60) - 1",
        _amount_surge(20, 60),
    ),
    FactorDefinition(
        "drawdown_from_high_120",
        1,
        "距120日高点回撤",
        "momentum",
        1,
        120,
        "close[t] / max(close, 120) - 1",
        _drawdown_from_high(120),
    ),
)

_BY_ID = {item.factor_id: item for item in FACTORS}


def factor_definition(factor_id: str) -> FactorDefinition:
    try:
        return _BY_ID[factor_id]
    except KeyError as exc:
        raise ValueError(f"Unknown factor: {factor_id!r}") from exc


def factor_registry() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "library_version": LIBRARY_VERSION,
        "factors": [item.to_dict() for item in FACTORS],
        "safety": {
            "research_only": True,
            "creates_no_signal": True,
            "orders_created": False,
        },
    }


__all__ = [
    "FACTORS",
    "FactorDefinition",
    "LIBRARY_VERSION",
    "factor_definition",
    "factor_registry",
]
