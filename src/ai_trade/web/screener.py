"""Deterministic, read-only universe screening helpers.

The screener deliberately works on rows that were derived from one validated
market snapshot.  It does not fetch data or mutate strategy/account state.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Iterable


SCREEN_SORT_FIELDS = frozenset(
    {
        "symbol",
        "momentum",
        "average_amount",
        "annual_volatility",
        "latest_close",
        "coverage",
    }
)
SCREEN_TRENDS = frozenset({"any", "up", "mixed", "down", "not_down"})
SCREEN_COVERAGE = frozenset({"all", "ready", "complete"})
SCREEN_LIMIT_MIN = 1
SCREEN_LIMIT_MAX = 500
SCREEN_LIMIT_DEFAULT = 200


@dataclass(frozen=True)
class ScreeningFilters:
    """Bounded filters accepted by the read-only screening endpoint."""

    asset_class: str = ""
    sector: str = ""
    trend: str = "any"
    coverage: str = "all"
    min_average_amount: float | None = None
    max_annual_volatility: float | None = None
    active_only: bool = False
    sort: str = "momentum"
    direction: str = "desc"
    limit: int = SCREEN_LIMIT_DEFAULT

    def __post_init__(self) -> None:
        if self.trend not in SCREEN_TRENDS:
            raise ValueError("trend must be any, up, mixed, down, or not_down")
        if self.coverage not in SCREEN_COVERAGE:
            raise ValueError("coverage must be all, ready, or complete")
        if self.sort not in SCREEN_SORT_FIELDS:
            raise ValueError("sort is not supported")
        if self.direction not in {"asc", "desc"}:
            raise ValueError("direction must be asc or desc")
        if (
            isinstance(self.limit, bool)
            or not isinstance(self.limit, int)
            or not SCREEN_LIMIT_MIN <= self.limit <= SCREEN_LIMIT_MAX
        ):
            raise ValueError(
                f"limit must be an integer between {SCREEN_LIMIT_MIN} and "
                f"{SCREEN_LIMIT_MAX}"
            )
        for name, value in (
            ("min_average_amount", self.min_average_amount),
            ("max_annual_volatility", self.max_annual_volatility),
        ):
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be numeric")
            if not math.isfinite(float(value)) or float(value) < 0:
                raise ValueError(f"{name} must be finite and non-negative")
        if not isinstance(self.asset_class, str) or not isinstance(self.sector, str):
            raise ValueError("asset_class and sector must be strings")

    def as_dict(self) -> dict[str, Any]:
        return {
            "asset_class": self.asset_class,
            "sector": self.sector,
            "trend": self.trend,
            "coverage": self.coverage,
            "min_average_amount": self.min_average_amount,
            "max_annual_volatility": self.max_annual_volatility,
            "active_only": self.active_only,
            "sort": self.sort,
            "direction": self.direction,
            "limit": self.limit,
        }


def screen_rows(
    rows: Iterable[dict[str, Any]], filters: ScreeningFilters
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Apply bounded filters and stable ordering to already-derived rows."""

    candidates = [dict(row) for row in rows]
    excluded = 0
    accepted: list[dict[str, Any]] = []
    for row in candidates:
        reasons: list[str] = []
        if filters.asset_class and row.get("asset_class") != filters.asset_class:
            reasons.append("asset_class")
        if filters.sector and row.get("sector") != filters.sector:
            reasons.append("sector")
        if filters.active_only and not row.get("active", False):
            reasons.append("inactive")
        trend = row.get("trend")
        if filters.trend != "any":
            if filters.trend == "not_down":
                matched = trend in {"UP", "MIXED"}
            else:
                matched = trend == filters.trend.upper()
            if not matched:
                reasons.append("trend")
        data_status = row.get("data_status")
        history_ready = bool(row.get("history_ready"))
        if filters.coverage == "ready" and not history_ready:
            reasons.append("history")
        elif filters.coverage == "complete" and data_status != "complete":
            reasons.append("coverage")
        average_amount = _finite(row.get("average_amount"))
        if (
            filters.min_average_amount is not None
            and (average_amount is None or average_amount < filters.min_average_amount)
        ):
            reasons.append("liquidity")
        annual_volatility = _finite(row.get("annual_volatility"))
        if (
            filters.max_annual_volatility is not None
            and (
                annual_volatility is None
                or annual_volatility > filters.max_annual_volatility
            )
        ):
            reasons.append("volatility")
        if reasons:
            excluded += 1
            continue
        row["screen_reasons"] = []
        accepted.append(row)

    accepted = _sort_rows(accepted, filters.sort, filters.direction)
    limited = accepted[: filters.limit]
    return limited, {
        "input": len(candidates),
        "matched": len(accepted),
        "returned": len(limited),
        "excluded": excluded,
        "truncated": max(0, len(accepted) - len(limited)),
    }


def _sort_rows(
    rows: list[dict[str, Any]], field: str, direction: str
) -> list[dict[str, Any]]:
    """Sort comparable values while keeping unavailable values at the end.

    A single ``reverse`` sort reverses the missing-value bucket as well.  Keep
    the two buckets separate, then use stable sorts so ties retain a readable
    symbol order in either direction.
    """

    present: list[tuple[Any, dict[str, Any]]] = []
    missing: list[dict[str, Any]] = []
    for row in rows:
        value = _sort_value(row, field)
        if value is None:
            missing.append(row)
        else:
            present.append((value, row))

    def symbol_key(row: dict[str, Any]) -> str:
        return str(row.get("symbol") or "")

    present.sort(key=lambda item: symbol_key(item[1]))
    present.sort(key=lambda item: item[0], reverse=direction == "desc")
    missing.sort(key=symbol_key)
    return [row for _, row in present] + missing


def _sort_value(row: dict[str, Any], field: str) -> str | float | None:
    if field == "symbol":
        value = str(row.get("symbol") or "")
        return value or None
    elif field == "average_amount":
        value = _finite(row.get("average_amount"))
    elif field == "annual_volatility":
        value = _finite(row.get("annual_volatility"))
    elif field == "latest_close":
        value = _finite(row.get("latest_close"))
    elif field == "coverage":
        value = _finite(row.get("coverage_percent"))
    else:
        value = _finite(row.get("momentum"))
    return value


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None
