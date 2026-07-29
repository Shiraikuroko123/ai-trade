from __future__ import annotations

import math
from collections.abc import Sequence


def sample_standard_deviation(values: Sequence[float]) -> float:
    """Return the sample deviation for an already validated float series.

    ``statistics.stdev`` preserves arbitrary numeric types through exact
    fractions. Market and model inputs are validated floats, so a compensated
    two-pass calculation provides the same estimator without that conversion
    cost on every rolling window.
    """

    return _standard_deviation(values, correction=1)


def population_standard_deviation(values: Sequence[float]) -> float:
    """Return the population deviation for an already validated float series."""

    return _standard_deviation(values, correction=0)


def _standard_deviation(values: Sequence[float], *, correction: int) -> float:
    count = len(values)
    if count <= correction:
        label = "sample" if correction else "population"
        raise ValueError(f"{label} standard deviation requires more data points")

    mean = math.fsum(values) / count
    squared_error = math.fsum((value - mean) ** 2 for value in values)
    # The correction term protects the two-pass calculation when the computed
    # mean is not exactly representable as a binary float.
    residual = math.fsum(value - mean for value in values)
    numerator = squared_error - residual * residual / count
    return math.sqrt(max(0.0, numerator) / (count - correction))
