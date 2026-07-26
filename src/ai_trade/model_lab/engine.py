from __future__ import annotations

from datetime import date, datetime, timezone
import statistics
from typing import Any, Mapping, Sequence
from uuid import uuid4

from .. import __version__
from ..config import AppConfig
from ..data.market import MarketData
from .gbdt import fit_gbdt
from .library import LIBRARY_VERSION, model_definition, model_registry
from .schema import (
    ENGINE_VERSION,
    SAFETY,
    SCHEMA_VERSION,
    finalize_evaluation,
    json_fingerprint,
)
from .store import ModelLabStore


DEFAULT_HORIZON = 20
DEFAULT_STEP = 5
MINIMUM_CROSS_SECTION = 4
MINIMUM_EVALUATED_DATES = 24
MINIMUM_TRAIN_DATES = 12
MINIMUM_TRAIN_OBSERVATIONS = 48

_PROTOCOL = {
    "target": (
        "Forward close-to-close return over the horizon, demeaned within each "
        "training date's cross-section"
    ),
    "standardization": (
        "Per-feature z-score fitted on the training window only; a constant "
        "training feature contributes zero"
    ),
    "training": (
        "Walk-forward refit at every evaluated date on all observations whose "
        "forward-return window has fully completed"
    ),
    "leakage_guard": (
        "A training observation requires feature_index + horizon <= "
        "evaluation_index; warm-up dates below the training minimums are "
        "reported, never silently backfilled"
    ),
}


class ModelLabEngine:
    """Walk-forward research models over registered factors, evidence only.

    Every evaluation reads the verified local cache point-in-time, trains only
    on observations whose forward-return windows have fully completed before
    the evaluated date, and reports out-of-sample rank IC next to every input
    factor evaluated under the identical protocol. Records are immutable
    research evidence: no prediction becomes a signal, weight, candidate, or
    order.
    """

    def __init__(
        self, config: AppConfig, store: ModelLabStore | None = None
    ) -> None:
        self.config = config
        self.store = store or ModelLabStore(
            config.project_root / "state" / "model_lab"
        )

    def registry(self) -> dict[str, Any]:
        return model_registry()

    def evaluate(
        self,
        owner: str,
        market: MarketData,
        model_id: str,
        *,
        factor_ids: Sequence[str] | None = None,
        horizon: int = DEFAULT_HORIZON,
        step: int = DEFAULT_STEP,
    ) -> dict[str, Any]:
        model = model_definition(model_id)
        if isinstance(horizon, bool) or not isinstance(horizon, int) or not 1 <= horizon <= 250:
            raise ValueError("Model evaluation horizon must be between 1 and 250")
        if isinstance(step, bool) or not isinstance(step, int) or not 1 <= step <= 21:
            raise ValueError("Model evaluation step must be between 1 and 21")
        factors = _selected_factors(self.config, owner, factor_ids)

        metadata_before = _metadata(market)
        snapshot_fingerprint = json_fingerprint(metadata_before)
        as_of = _snapshot_as_of(metadata_before, market)

        start = date.fromisoformat(str(self.config.raw["backtest"]["start"]))
        end = date.fromisoformat(str(self.config.raw["backtest"]["end"]))
        calendar = [day for day in market.calendar if start <= day <= end]
        minimum_history = max(item.minimum_history for item in factors)
        if len(calendar) <= horizon + minimum_history:
            raise RuntimeError(
                "Model evaluation history is too short for the requested "
                "horizon and factor lookbacks"
            )

        last_index = len(calendar) - 1 - horizon
        sample_indexes = list(range(minimum_history, last_index + 1, step))
        observations: list[dict[str, Any]] = []
        skipped = 0
        cross_sections: list[int] = []
        symbol_observations: dict[str, int] = {}
        for index in sample_indexes:
            on_date = calendar[index]
            target_date = calendar[index + horizon]
            rows: list[tuple[str, list[float], float]] = []
            for symbol in market.active_symbols(on_date):
                entry_bar = market.bar(symbol, on_date)
                exit_bar = market.bar(symbol, target_date)
                if (
                    entry_bar is None
                    or exit_bar is None
                    or entry_bar.close <= 0
                    or exit_bar.close <= 0
                ):
                    continue
                history = market.history(symbol, on_date, minimum_history)
                if len(history) < minimum_history:
                    continue
                features: list[float] = []
                for definition in factors:
                    value = definition.compute(history)
                    if value is None or not _is_finite(value):
                        break
                    features.append(float(value))
                else:
                    rows.append(
                        (symbol, features, exit_bar.close / entry_bar.close - 1.0)
                    )
            if len(rows) < MINIMUM_CROSS_SECTION:
                skipped += 1
                continue
            observations.append({"index": index, "date": on_date, "rows": rows})

        evaluated = 0
        warmup = 0
        model_ics: list[float] = []
        model_spreads: list[float] = []
        factor_ics: dict[str, list[float]] = {
            item.factor_id: [] for item in factors
        }
        coefficient_history: list[list[float]] = []
        final_train_observations = 0
        predictor = None
        fit_means: list[float] = []
        fit_stds: list[float] = []
        refit_interval = max(1, int(model.hyperparameters.get("refit_interval", 1)))
        max_train_rows = int(model.hyperparameters.get("max_train_rows", 0))
        for position, observation in enumerate(observations):
            train_rows: list[tuple[list[float], float]] = []
            train_dates = 0
            for earlier in observations[:position]:
                if earlier["index"] + horizon <= observation["index"]:
                    train_dates += 1
                    date_mean = statistics.fmean(
                        row[2] for row in earlier["rows"]
                    )
                    for _symbol, features, forward in earlier["rows"]:
                        train_rows.append((features, forward - date_mean))
            if (
                train_dates < MINIMUM_TRAIN_DATES
                or len(train_rows) < MINIMUM_TRAIN_OBSERVATIONS
            ):
                warmup += 1
                continue

            if predictor is None or evaluated % refit_interval == 0:
                train_window = (
                    train_rows[-max_train_rows:] if max_train_rows else train_rows
                )
                fit_means, fit_stds = _feature_stats(train_window, len(factors))
                predictor, disclosure = _fit_model(
                    model, factors, train_window, fit_means, fit_stds
                )
                coefficient_history.append(disclosure)
                final_train_observations = len(train_window)
            predictions: list[float] = []
            realized: list[float] = []
            for _symbol, features, forward in observation["rows"]:
                standardized = _standardize(features, fit_means, fit_stds)
                predictions.append(predictor(standardized))
                realized.append(forward)
            ic = _spearman(predictions, realized)
            if ic is None:
                skipped += 1
                continue
            spread = _half_spread(list(zip(predictions, realized)))
            if spread is None:
                skipped += 1
                continue
            usable = True
            date_factor_ics: dict[str, float] = {}
            for column, definition in enumerate(factors):
                factor_values = [
                    row[1][column] for row in observation["rows"]
                ]
                factor_ic = _spearman(factor_values, realized)
                if factor_ic is None:
                    usable = False
                    break
                date_factor_ics[definition.factor_id] = factor_ic
            if not usable:
                skipped += 1
                continue
            evaluated += 1
            model_ics.append(ic)
            model_spreads.append(spread)
            for factor_id, factor_ic in date_factor_ics.items():
                factor_ics[factor_id].append(factor_ic)
            cross_sections.append(len(observation["rows"]))
            for symbol, _features, _forward in observation["rows"]:
                symbol_observations[symbol] = (
                    symbol_observations.get(symbol, 0) + 1
                )

        if evaluated < MINIMUM_EVALUATED_DATES:
            raise RuntimeError(
                "Model evaluation produced fewer than "
                f"{MINIMUM_EVALUATED_DATES} out-of-sample dates"
            )

        metadata_after = _metadata(market)
        if json_fingerprint(metadata_after) != snapshot_fingerprint:
            raise RuntimeError("Market snapshot changed during model evaluation")

        mean_ic = statistics.fmean(model_ics)
        ic_std = statistics.stdev(model_ics) if len(model_ics) > 1 else 0.0
        mean_spread = statistics.fmean(model_spreads)
        spread_std = (
            statistics.stdev(model_spreads) if len(model_spreads) > 1 else 0.0
        )
        baselines = []
        adjusted: dict[str, float] = {}
        for definition in factors:
            values = factor_ics[definition.factor_id]
            baseline_mean = statistics.fmean(values)
            baseline_std = statistics.stdev(values) if len(values) > 1 else 0.0
            adjusted_value = baseline_mean * definition.direction
            adjusted[definition.factor_id] = adjusted_value
            baselines.append(
                {
                    "factor_id": definition.factor_id,
                    "direction": definition.direction,
                    "mean_ic": baseline_mean,
                    "direction_adjusted_mean_ic": adjusted_value,
                    "ic_ir": (
                        (baseline_mean / baseline_std) if baseline_std > 0 else 0.0
                    ),
                }
            )
        best_factor_id = max(adjusted, key=lambda key: adjusted[key])
        best_value = adjusted[best_factor_id]
        coefficients = []
        for column, definition in enumerate(factors):
            series = [weights[column] for weights in coefficient_history]
            coefficients.append(
                {
                    "factor_id": definition.factor_id,
                    "mean": statistics.fmean(series),
                    "mean_abs": statistics.fmean(abs(value) for value in series),
                    "final": series[-1],
                }
            )

        context_fingerprint = self._context_fingerprint()
        parameters = {
            "horizon": horizon,
            "step": step,
            "start": calendar[0].isoformat(),
            "end": calendar[-1].isoformat(),
            "minimum_cross_section": MINIMUM_CROSS_SECTION,
            "minimum_train_dates": MINIMUM_TRAIN_DATES,
            "minimum_train_observations": MINIMUM_TRAIN_OBSERVATIONS,
        }
        factor_bindings = [
            {
                "factor_id": item.factor_id,
                "version": item.version,
                "direction": item.direction,
            }
            for item in factors
        ]
        evaluation_fingerprint = json_fingerprint(
            {
                "model": model.to_dict(),
                "factors": factor_bindings,
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
                "evaluation_id": f"mdl_{uuid4().hex}",
                "owner": self.store.owner_id(owner),
                "created_at": _utc_now(),
                "model": model.to_dict(),
                "factors": factor_bindings,
                "parameters": parameters,
                "protocol": dict(_PROTOCOL),
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
                    "warmup_dates": warmup,
                    "skipped_dates": len(sample_indexes) - evaluated - warmup,
                    "average_cross_section": (
                        statistics.fmean(cross_sections) if cross_sections else 0.0
                    ),
                    "final_train_observations": final_train_observations,
                    "symbols": dict(sorted(symbol_observations.items())),
                },
                "results": {
                    "model": {
                        "dates": evaluated,
                        "mean_ic": mean_ic,
                        "ic_std": ic_std,
                        "ic_ir": (mean_ic / ic_std) if ic_std > 0 else 0.0,
                        "positive_share": (
                            sum(value > 0 for value in model_ics) / len(model_ics)
                        ),
                        "mean_spread": mean_spread,
                        "spread_std": spread_std,
                    },
                    "factor_baselines": baselines,
                    "best_factor_id": best_factor_id,
                    "best_factor_direction_adjusted_mean_ic": best_value,
                    "model_minus_best_factor_ic": mean_ic - best_value,
                },
                "coefficients": coefficients,
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
        model_id: str | None = None,
    ) -> dict[str, Any]:
        return self.store.list(owner, limit=limit, model_id=model_id)

    def get(self, owner: str, evaluation_id: str) -> dict[str, Any]:
        return self.store.get(owner, evaluation_id)

    def _context_fingerprint(self) -> str:
        return json_fingerprint(
            {
                "app_version": __version__,
                "model_lab": {
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


def _selected_factors(config, owner: str, factor_ids: Sequence[str] | None):
    from ..factor_lab.custom import resolve_factor
    from ..factor_lab.library import FACTORS

    if factor_ids is None:
        return tuple(FACTORS)
    if not isinstance(factor_ids, (list, tuple)) or not 1 <= len(factor_ids) <= 24:
        raise ValueError("Model evaluation factors must be 1 to 24 identifiers")
    if len(set(factor_ids)) != len(factor_ids):
        raise ValueError("Model evaluation factors must be unique")
    return tuple(
        resolve_factor(config, owner, str(item)) for item in factor_ids
    )


def _fit_model(
    model,
    factors,
    train_rows: Sequence[tuple[list[float], float]],
    means: Sequence[float],
    stds: Sequence[float],
):
    """Return (predict(standardized_row) -> float, per-factor disclosure).

    Ridge discloses its fitted weights, the factor-mean baseline its fixed
    weights, and GBDT its normalized split-gain feature importance.
    """
    if model.kind == "ridge":
        weights = _fit_ridge(
            train_rows, means, stds, float(model.hyperparameters["lambda"])
        )

        def predict_linear(row, values=tuple(weights)):
            return sum(values[column] * row[column] for column in range(len(values)))

        return predict_linear, list(weights)
    if model.kind == "factor_mean":
        weights = [item.direction / len(factors) for item in factors]

        def predict_mean(row, values=tuple(weights)):
            return sum(values[column] * row[column] for column in range(len(values)))

        return predict_mean, list(weights)
    if model.kind == "gbdt":
        standardized_rows = [
            _standardize(features, means, stds) for features, _target in train_rows
        ]
        targets = [target for _features, target in train_rows]
        predict, importance = fit_gbdt(
            standardized_rows,
            targets,
            trees=int(model.hyperparameters["trees"]),
            depth=int(model.hyperparameters["depth"]),
            learning_rate=float(model.hyperparameters["learning_rate"]),
            min_leaf=int(model.hyperparameters["min_leaf"]),
            split_candidates=int(model.hyperparameters["split_candidates"]),
        )
        return predict, importance
    raise RuntimeError(f"Model kind is not executable: {model.kind}")


def _feature_stats(
    rows: Sequence[tuple[list[float], float]], columns: int
) -> tuple[list[float], list[float]]:
    means: list[float] = []
    stds: list[float] = []
    for column in range(columns):
        values = [row[0][column] for row in rows]
        mean = statistics.fmean(values)
        std = statistics.stdev(values) if len(values) > 1 else 0.0
        means.append(mean)
        stds.append(std)
    return means, stds


def _standardize(
    features: Sequence[float], means: Sequence[float], stds: Sequence[float]
) -> list[float]:
    return [
        ((features[column] - means[column]) / stds[column])
        if stds[column] > 1e-12
        else 0.0
        for column in range(len(means))
    ]


def _fit_ridge(
    rows: Sequence[tuple[list[float], float]],
    means: Sequence[float],
    stds: Sequence[float],
    ridge_lambda: float,
) -> list[float]:
    columns = len(means)
    count = len(rows)
    gram = [[0.0] * columns for _ in range(columns)]
    moment = [0.0] * columns
    for features, target in rows:
        standardized = _standardize(features, means, stds)
        for i in range(columns):
            moment[i] += standardized[i] * target
            for j in range(i, columns):
                gram[i][j] += standardized[i] * standardized[j]
    matrix = [
        [
            (gram[min(i, j)][max(i, j)] / count)
            + (ridge_lambda if i == j else 0.0)
            for j in range(columns)
        ]
        for i in range(columns)
    ]
    vector = [value / count for value in moment]
    return _solve(matrix, vector)


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [list(matrix[row]) + [vector[row]] for row in range(size)]
    for pivot in range(size):
        best = max(range(pivot, size), key=lambda row: abs(augmented[row][pivot]))
        if abs(augmented[best][pivot]) < 1e-12:
            raise RuntimeError("Model evaluation normal equations are singular")
        augmented[pivot], augmented[best] = augmented[best], augmented[pivot]
        scale = augmented[pivot][pivot]
        for column in range(pivot, size + 1):
            augmented[pivot][column] /= scale
        for row in range(size):
            if row == pivot:
                continue
            factor = augmented[row][pivot]
            if factor == 0.0:
                continue
            for column in range(pivot, size + 1):
                augmented[row][column] -= factor * augmented[pivot][column]
    return [augmented[row][size] for row in range(size)]


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


def _is_finite(value: float) -> bool:
    try:
        return value == value and abs(value) != float("inf")
    except TypeError:
        return False


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "DEFAULT_HORIZON",
    "DEFAULT_STEP",
    "MINIMUM_CROSS_SECTION",
    "MINIMUM_EVALUATED_DATES",
    "MINIMUM_TRAIN_DATES",
    "MINIMUM_TRAIN_OBSERVATIONS",
    "ModelLabEngine",
]
