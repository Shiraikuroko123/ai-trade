from __future__ import annotations

from datetime import date, datetime, timezone
import math
import statistics
from typing import Any, Mapping
from uuid import uuid4

from .. import __version__
from ..config import AppConfig
from ..data.market import MarketData
from ..numeric import sample_standard_deviation
from ..research_statistics import (
    apply_holm_correction,
    deterministic_seed,
    moving_block_bootstrap_mean,
)
from .custom import resolve_factor
from .library import LIBRARY_VERSION, factor_registry
from .schema import (
    ENGINE_VERSION,
    SAFETY,
    SCHEMA_VERSION,
    finalize_evaluation,
    json_fingerprint,
)
from .store import FactorLabStore


DEFAULT_HORIZONS = (5, 20, 60)
DEFAULT_STEP = 5
MINIMUM_CROSS_SECTION = 4
MINIMUM_EVALUATED_DATES = 24


class FactorLabEngine:
    """Evaluate registered deterministic factors as research evidence only.

    Every evaluation reads the already verified local cache point-in-time:
    factor values use only bars up to each sampled date, forward returns use
    only exact later completed sessions, and the dynamic universe respects
    listing and delisting dates. Results are immutable rank-IC/spread evidence
    with a registered direction hypothesis; they are not signals, weights,
    strategy candidates, or orders.
    """

    def __init__(
        self, config: AppConfig, store: FactorLabStore | None = None
    ) -> None:
        self.config = config
        self.store = store or FactorLabStore(
            config.project_root / "state" / "factor_lab"
        )

    def registry(self) -> dict[str, Any]:
        return factor_registry()

    def evaluate(
        self,
        owner: str,
        market: MarketData,
        factor_id: str,
        *,
        horizons: tuple[int, ...] | list[int] = DEFAULT_HORIZONS,
        step: int = DEFAULT_STEP,
    ) -> dict[str, Any]:
        definition = resolve_factor(self.config, owner, factor_id)
        horizons = _validated_horizons(horizons)
        if isinstance(step, bool) or not isinstance(step, int) or not 1 <= step <= 21:
            raise ValueError("Factor evaluation step must be between 1 and 21")

        metadata_before = _metadata(market)
        snapshot_fingerprint = json_fingerprint(metadata_before)
        as_of = _snapshot_as_of(metadata_before, market)

        start = date.fromisoformat(str(self.config.raw["backtest"]["start"]))
        end = date.fromisoformat(str(self.config.raw["backtest"]["end"]))
        calendar = [day for day in market.calendar if start <= day <= end]
        maximum_horizon = horizons[-1]
        if len(calendar) <= maximum_horizon + definition.minimum_history:
            raise RuntimeError(
                "Factor evaluation history is too short for the requested "
                "horizons and factor lookback"
            )

        last_index = len(calendar) - 1 - maximum_horizon
        sample_indexes = list(
            range(definition.minimum_history, last_index + 1, step)
        )
        evaluated = 0
        skipped = 0
        cross_sections: list[int] = []
        symbol_observations: dict[str, int] = {}
        per_horizon: dict[int, dict[str, list[float]]] = {
            horizon: {"ic": [], "spread": []} for horizon in horizons
        }

        for index in sample_indexes:
            on_date = calendar[index]
            rows: list[tuple[str, float]] = []
            for symbol in market.active_symbols(on_date):
                if market.bar(symbol, on_date) is None:
                    continue
                history = market.history(
                    symbol, on_date, definition.minimum_history
                )
                if len(history) < definition.minimum_history:
                    continue
                value = definition.compute(history)
                if value is None or not _finite(value):
                    continue
                rows.append((symbol, float(value)))
            if len(rows) < MINIMUM_CROSS_SECTION:
                skipped += 1
                continue

            date_used = False
            for horizon in horizons:
                target = calendar[index + horizon]
                pairs: list[tuple[float, float]] = []
                observed_symbols: list[str] = []
                for symbol, value in rows:
                    entry_bar = market.bar(symbol, on_date)
                    exit_bar = market.bar(symbol, target)
                    if (
                        entry_bar is None
                        or exit_bar is None
                        or entry_bar.close <= 0
                        or exit_bar.close <= 0
                    ):
                        continue
                    pairs.append(
                        (value, exit_bar.close / entry_bar.close - 1.0)
                    )
                    observed_symbols.append(symbol)
                if len(pairs) < MINIMUM_CROSS_SECTION:
                    continue
                ic = _spearman(
                    [item[0] for item in pairs], [item[1] for item in pairs]
                )
                if ic is None:
                    continue
                spread = _half_spread(pairs)
                if spread is None:
                    continue
                per_horizon[horizon]["ic"].append(ic)
                per_horizon[horizon]["spread"].append(spread)
                if not date_used:
                    date_used = True
                    cross_sections.append(len(pairs))
                    for symbol in observed_symbols:
                        symbol_observations[symbol] = (
                            symbol_observations.get(symbol, 0) + 1
                        )
            if date_used:
                evaluated += 1
            else:
                skipped += 1

        if evaluated < MINIMUM_EVALUATED_DATES:
            raise RuntimeError(
                "Factor evaluation produced fewer than "
                f"{MINIMUM_EVALUATED_DATES} valid cross-section dates"
            )

        results: list[dict[str, Any]] = []
        validations: list[dict[str, Any]] = []
        for horizon in horizons:
            ics = per_horizon[horizon]["ic"]
            spreads = per_horizon[horizon]["spread"]
            if len(ics) < MINIMUM_EVALUATED_DATES:
                raise RuntimeError(
                    f"Factor evaluation horizon {horizon} has too few valid dates"
                )
            mean_ic = statistics.fmean(ics)
            ic_std = sample_standard_deviation(ics) if len(ics) > 1 else 0.0
            mean_spread = statistics.fmean(spreads)
            spread_std = (
                sample_standard_deviation(spreads) if len(spreads) > 1 else 0.0
            )
            results.append(
                {
                    "horizon": horizon,
                    "dates": len(ics),
                    "mean_ic": mean_ic,
                    "ic_std": ic_std,
                    "ic_ir": (mean_ic / ic_std) if ic_std > 0 else 0.0,
                    "positive_share": sum(value > 0 for value in ics) / len(ics),
                    "direction_hit_rate": (
                        sum(
                            value * definition.direction > 0 for value in ics
                        )
                        / len(ics)
                    ),
                    "mean_spread": mean_spread,
                    "spread_std": spread_std,
                    "direction_adjusted_mean_spread": (
                        mean_spread * definition.direction
                    ),
                }
            )
            validations.append(
                moving_block_bootstrap_mean(
                    [value * definition.direction for value in ics],
                    block_size=min(
                        len(ics), max(1, math.ceil(horizon / step))
                    ),
                    seed=deterministic_seed(
                        "factor-ic",
                        snapshot_fingerprint,
                        definition.factor_id,
                        horizon,
                        step,
                        SCHEMA_VERSION,
                        ENGINE_VERSION,
                    ),
                )
            )

        for result, validation in zip(
            results, apply_holm_correction(validations)
        ):
            result["statistical_validation"] = validation

        metadata_after = _metadata(market)
        if json_fingerprint(metadata_after) != snapshot_fingerprint:
            raise RuntimeError("Market snapshot changed during factor evaluation")

        context_fingerprint = self._context_fingerprint()
        parameters = {
            "start": calendar[0].isoformat(),
            "end": calendar[-1].isoformat(),
            "step": step,
            "horizons": list(horizons),
            "minimum_cross_section": MINIMUM_CROSS_SECTION,
        }
        evaluation_fingerprint = json_fingerprint(
            {
                "factor": definition.to_dict(),
                "parameters": parameters,
                "snapshot_fingerprint": snapshot_fingerprint,
                "config_context_fingerprint": context_fingerprint,
                "schema_version": SCHEMA_VERSION,
                "engine_version": ENGINE_VERSION,
                "library_version": LIBRARY_VERSION,
            }
        )
        record = finalize_evaluation(
            {
                "schema_version": SCHEMA_VERSION,
                "engine_version": ENGINE_VERSION,
                "evaluation_id": f"eval_{uuid4().hex}",
                "owner": self.store.owner_id(owner),
                "created_at": _utc_now(),
                "factor": definition.to_dict(),
                "parameters": parameters,
                "evidence": {
                    "snapshot": {
                        "snapshot_id": "market_" + snapshot_fingerprint[:32],
                        "kind": "market_cache",
                        "as_of": as_of,
                        "provider": str(
                            metadata_before.get("provider") or "local-cache"
                        ),
                        "fingerprint": snapshot_fingerprint,
                    },
                    "universe": {
                        "name": self.config.universe_name,
                        "security_master_sha256": (
                            self.config.security_master.fingerprint()
                        ),
                    },
                    "config_context_fingerprint": context_fingerprint,
                },
                "coverage": {
                    "calendar_sessions": len(calendar),
                    "sampled_dates": len(sample_indexes),
                    "evaluated_dates": evaluated,
                    "skipped_dates": skipped,
                    "average_cross_section": (
                        statistics.fmean(cross_sections)
                        if cross_sections
                        else 0.0
                    ),
                    "symbols": dict(sorted(symbol_observations.items())),
                },
                "results": results,
                "evaluation_fingerprint": evaluation_fingerprint,
                "safety": dict(SAFETY),
            }
        )
        return self.store.publish(owner, record)

    def list(
        self,
        owner: str,
        *,
        limit: int = 50,
        factor_id: str | None = None,
    ) -> dict[str, Any]:
        return self.store.list(owner, limit=limit, factor_id=factor_id)

    def get(self, owner: str, evaluation_id: str) -> dict[str, Any]:
        return self.store.get(owner, evaluation_id)

    def _context_fingerprint(self) -> str:
        return json_fingerprint(
            {
                "app_version": __version__,
                "factor_lab": {
                    "schema_version": SCHEMA_VERSION,
                    "engine_version": ENGINE_VERSION,
                    "library_version": LIBRARY_VERSION,
                },
                "data": self.config.raw.get("data"),
                "backtest": self.config.raw.get("backtest"),
                "universe_name": self.config.universe_name,
                "minimum_listing_days": self.config.minimum_listing_days,
                "security_master_fingerprint": (
                    self.config.security_master.fingerprint()
                ),
            }
        )


def _validated_horizons(value: tuple[int, ...] | list[int]) -> tuple[int, ...]:
    if (
        not isinstance(value, (tuple, list))
        or not 1 <= len(value) <= 4
        or any(
            isinstance(item, bool)
            or not isinstance(item, int)
            or not 1 <= item <= 250
            for item in value
        )
    ):
        raise ValueError("Factor evaluation horizons must be 1 to 4 ints in 1..250")
    ordered = sorted(set(int(item) for item in value))
    if list(value) != ordered:
        raise ValueError("Factor evaluation horizons must be unique and ascending")
    return tuple(ordered)


def _spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) != len(ys) or len(xs) < 3:
        return None
    rx = _average_ranks(xs)
    ry = _average_ranks(ys)
    mean_x = statistics.fmean(rx)
    mean_y = statistics.fmean(ry)
    var_x = sum((value - mean_x) ** 2 for value in rx)
    var_y = sum((value - mean_y) ** 2 for value in ry)
    if var_x <= 0 or var_y <= 0:
        return None
    covariance = sum(
        (rx[index] - mean_x) * (ry[index] - mean_y) for index in range(len(rx))
    )
    return covariance / (var_x**0.5 * var_y**0.5)


def _average_ranks(values: list[float]) -> list[float]:
    indexed = sorted(range(len(values)), key=lambda index: values[index])
    ranks = [0.0] * len(values)
    position = 0
    while position < len(indexed):
        tail = position
        while (
            tail + 1 < len(indexed)
            and values[indexed[tail + 1]] == values[indexed[position]]
        ):
            tail += 1
        rank = (position + tail) / 2.0 + 1.0
        for cursor in range(position, tail + 1):
            ranks[indexed[cursor]] = rank
        position = tail + 1
    return ranks


def _half_spread(pairs: list[tuple[float, float]]) -> float | None:
    ordered = sorted(pairs, key=lambda item: item[0])
    half = len(ordered) // 2
    if half < 1:
        return None
    bottom = ordered[:half]
    top = ordered[-half:]
    return statistics.fmean(item[1] for item in top) - statistics.fmean(
        item[1] for item in bottom
    )


def _metadata(market: MarketData) -> dict[str, Any]:
    value = market.snapshot_metadata()
    if not isinstance(value, Mapping):
        raise RuntimeError("Market snapshot metadata must be an object")
    return dict(value)


def _snapshot_as_of(metadata: Mapping[str, Any], market: MarketData) -> str:
    return str(
        metadata.get("latest_common_session")
        or metadata.get("latest_benchmark_session")
        or market.latest_date().isoformat()
    )


def _finite(value: float) -> bool:
    try:
        return value == value and abs(value) != float("inf")
    except TypeError:
        return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "DEFAULT_HORIZONS",
    "DEFAULT_STEP",
    "FactorLabEngine",
    "MINIMUM_CROSS_SECTION",
    "MINIMUM_EVALUATED_DATES",
]
