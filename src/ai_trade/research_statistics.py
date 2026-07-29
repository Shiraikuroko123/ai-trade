from __future__ import annotations

from hashlib import sha256
import math
import random
import statistics
from typing import Any, Mapping, Sequence

from .numeric import sample_standard_deviation


DEFAULT_BOOTSTRAP_RESAMPLES = 999
DEFAULT_CONFIDENCE_LEVEL = 0.95
DEFAULT_ALPHA = 0.05


def deterministic_seed(*parts: object) -> int:
    """Derive a stable 64-bit seed from evidence-bound inputs."""

    payload = "\x1f".join(str(part) for part in parts).encode("utf-8")
    return int.from_bytes(sha256(payload).digest()[:8], "big")


def moving_block_bootstrap_mean(
    values: Sequence[float],
    *,
    block_size: int,
    seed: int,
    resamples: int = DEFAULT_BOOTSTRAP_RESAMPLES,
    confidence_level: float = DEFAULT_CONFIDENCE_LEVEL,
    subperiods: int = 3,
) -> dict[str, Any]:
    """Estimate a mean, uncertainty, and one-sided p-value for a time series.

    Circular moving blocks preserve local serial dependence. The p-value tests
    H0: mean <= 0 against H1: mean > 0 using a centered bootstrap null. The
    contiguous subperiod summary exposes chronological instability rather than
    hiding it inside a full-period average.
    """

    series = _series(values)
    observations = len(series)
    if (
        isinstance(block_size, bool)
        or not isinstance(block_size, int)
        or not 1 <= block_size <= observations
    ):
        raise ValueError("block_size must be an integer in 1..len(values)")
    if (
        isinstance(resamples, bool)
        or not isinstance(resamples, int)
        or not 99 <= resamples <= 100_000
    ):
        raise ValueError("resamples must be an integer between 99 and 100000")
    if (
        isinstance(seed, bool)
        or not isinstance(seed, int)
        or not 0 <= seed < 2**64
    ):
        raise ValueError("seed must be a 64-bit unsigned integer")
    if (
        isinstance(confidence_level, bool)
        or not isinstance(confidence_level, (int, float))
        or not math.isfinite(float(confidence_level))
        or not 0.8 <= float(confidence_level) < 1.0
    ):
        raise ValueError("confidence_level must be in [0.8, 1.0)")
    if (
        isinstance(subperiods, bool)
        or not isinstance(subperiods, int)
        or not 2 <= subperiods <= min(10, observations)
    ):
        raise ValueError("subperiods must be an integer in 2..min(10, len(values))")

    estimate = statistics.fmean(series)
    generator = random.Random(seed)
    bootstrap_means = [
        _block_sample_mean(series, block_size, generator)
        for _ in range(resamples)
    ]
    ordered = sorted(bootstrap_means)
    tail = (1.0 - float(confidence_level)) / 2.0
    standard_error = sample_standard_deviation(bootstrap_means)
    null_exceedances = sum(
        bootstrap_mean - estimate >= estimate
        for bootstrap_mean in bootstrap_means
    )
    p_value = (null_exceedances + 1.0) / (resamples + 1.0)
    period_means = _subperiod_means(series, subperiods)
    low_quantile = _quantile(ordered, tail)
    high_quantile = _quantile(ordered, 1.0 - tail)

    return {
        "method": "circular_moving_block_bootstrap",
        "alternative": "greater",
        "observations": observations,
        "block_size": block_size,
        "resamples": resamples,
        "seed": seed,
        "confidence_level": float(confidence_level),
        "effect_size": estimate,
        "standard_error": standard_error,
        # Interpolation can reverse nearly identical bounds by one ULP.
        "ci_low": min(low_quantile, high_quantile),
        "ci_high": max(low_quantile, high_quantile),
        "p_value": p_value,
        "subperiods": subperiods,
        "subperiod_means": period_means,
        "positive_subperiods": sum(value > 0 for value in period_means),
        "minimum_subperiod_mean": min(period_means),
    }


def apply_holm_correction(
    validations: Sequence[Mapping[str, Any]],
    *,
    alpha: float = DEFAULT_ALPHA,
) -> list[dict[str, Any]]:
    """Attach Holm family-wise-error correction to bootstrap validations."""

    if not isinstance(validations, (list, tuple)) or not validations:
        raise ValueError("validations must be a non-empty list or tuple")
    if (
        isinstance(alpha, bool)
        or not isinstance(alpha, (int, float))
        or not math.isfinite(float(alpha))
        or not 0 < float(alpha) <= 0.05
    ):
        raise ValueError("alpha must be in (0, 0.05]")

    p_values: list[float] = []
    for item in validations:
        if not isinstance(item, Mapping):
            raise ValueError("each validation must be an object")
        value = item.get("p_value")
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or not 0 <= float(value) <= 1
        ):
            raise ValueError("each validation p_value must be in [0, 1]")
        p_values.append(float(value))

    family_size = len(p_values)
    adjusted = [0.0] * family_size
    running_maximum = 0.0
    for rank, index in enumerate(
        sorted(range(family_size), key=lambda item: (p_values[item], item))
    ):
        corrected = min(1.0, (family_size - rank) * p_values[index])
        running_maximum = max(running_maximum, corrected)
        adjusted[index] = running_maximum

    results: list[dict[str, Any]] = []
    for index, item in enumerate(validations):
        result = dict(item)
        result.update(
            {
                "alpha": float(alpha),
                "correction": "holm",
                "family_size": family_size,
                "adjusted_p_value": adjusted[index],
                "reject_null": adjusted[index] <= float(alpha),
            }
        )
        results.append(result)
    return results


def _series(values: Sequence[float]) -> list[float]:
    if not isinstance(values, (list, tuple)) or len(values) < 2:
        raise ValueError("values must contain at least two observations")
    result: list[float] = []
    for value in values:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
        ):
            raise ValueError("values must contain only finite numbers")
        result.append(float(value))
    return result


def _block_sample_mean(
    series: Sequence[float], block_size: int, generator: random.Random
) -> float:
    observations = len(series)
    total = 0.0
    sampled = 0
    while sampled < observations:
        start = generator.randrange(observations)
        take = min(block_size, observations - sampled)
        total += math.fsum(
            series[(start + offset) % observations] for offset in range(take)
        )
        sampled += take
    return total / observations


def _subperiod_means(series: Sequence[float], count: int) -> list[float]:
    base, remainder = divmod(len(series), count)
    means: list[float] = []
    cursor = 0
    for index in range(count):
        size = base + (1 if index < remainder else 0)
        period = series[cursor : cursor + size]
        cursor += size
        means.append(statistics.fmean(period))
    return means


def _quantile(ordered: Sequence[float], probability: float) -> float:
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return float(ordered[lower])
    weight = position - lower
    return float(ordered[lower] * (1.0 - weight) + ordered[upper] * weight)


__all__ = [
    "DEFAULT_ALPHA",
    "DEFAULT_BOOTSTRAP_RESAMPLES",
    "DEFAULT_CONFIDENCE_LEVEL",
    "apply_holm_correction",
    "deterministic_seed",
    "moving_block_bootstrap_mean",
]
