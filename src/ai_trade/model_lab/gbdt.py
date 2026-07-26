from __future__ import annotations

from typing import Callable, Sequence


"""Pure-standard-library gradient-boosted regression trees.

Deliberately small and deterministic: least-squares depth-limited trees on
quantile split candidates, fixed pre-registered hyperparameters, no
randomness, no third-party dependency. Designed for the model lab's walk
-forward protocol on personal-computer scale cross-sections; it is research
evidence machinery, not an execution component.
"""


class _Leaf:
    __slots__ = ("value",)

    def __init__(self, value: float) -> None:
        self.value = value


class _Split:
    __slots__ = ("feature", "threshold", "left", "right")

    def __init__(self, feature: int, threshold: float, left, right) -> None:
        self.feature = feature
        self.threshold = threshold
        self.left = left
        self.right = right


def fit_gbdt(
    features: Sequence[Sequence[float]],
    targets: Sequence[float],
    *,
    trees: int,
    depth: int,
    learning_rate: float,
    min_leaf: int,
    split_candidates: int,
) -> tuple[Callable[[Sequence[float]], float], list[float]]:
    """Fit an ensemble; return (predict, normalized per-feature importance)."""

    count = len(targets)
    if count == 0:
        raise ValueError("GBDT training set is empty")
    columns = len(features[0])
    base = sum(targets) / count
    predictions = [base] * count
    ensemble: list[_Split | _Leaf] = []
    importance = [0.0] * columns

    for _round in range(trees):
        residuals = [targets[i] - predictions[i] for i in range(count)]
        tree = _fit_tree(
            features,
            residuals,
            list(range(count)),
            depth,
            min_leaf,
            split_candidates,
            importance,
        )
        ensemble.append(tree)
        for i in range(count):
            predictions[i] += learning_rate * _predict_tree(tree, features[i])

    total_gain = sum(importance)
    if total_gain > 0:
        normalized = [value / total_gain for value in importance]
    else:
        normalized = [0.0] * columns

    def predict(row: Sequence[float]) -> float:
        value = base
        for tree in ensemble:
            value += learning_rate * _predict_tree(tree, row)
        return value

    return predict, normalized


def _fit_tree(
    features: Sequence[Sequence[float]],
    residuals: Sequence[float],
    indexes: list[int],
    depth: int,
    min_leaf: int,
    split_candidates: int,
    importance: list[float],
):
    total = sum(residuals[i] for i in indexes)
    count = len(indexes)
    mean = total / count
    if depth <= 0 or count < 2 * min_leaf:
        return _Leaf(mean)
    parent_sse = sum((residuals[i] - mean) ** 2 for i in indexes)

    best_gain = 0.0
    best_feature = -1
    best_threshold = 0.0
    columns = len(features[indexes[0]])
    for feature in range(columns):
        ordered = sorted(indexes, key=lambda i: features[i][feature])
        values = [features[i][feature] for i in ordered]
        if values[0] == values[-1]:
            continue
        prefix = [0.0]
        prefix_sq = [0.0]
        for i in ordered:
            prefix.append(prefix[-1] + residuals[i])
            prefix_sq.append(prefix_sq[-1] + residuals[i] ** 2)
        boundaries = [
            position
            for position in range(min_leaf, count - min_leaf + 1)
            if 0 < position < count and values[position - 1] != values[position]
        ]
        if not boundaries:
            continue
        if len(boundaries) > split_candidates:
            if split_candidates <= 1:
                boundaries = [boundaries[len(boundaries) // 2]]
            else:
                stride = (len(boundaries) - 1) / (split_candidates - 1)
                boundaries = sorted(
                    {
                        boundaries[round(index * stride)]
                        for index in range(split_candidates)
                    }
                )
        for position in boundaries:
            threshold = (values[position - 1] + values[position]) / 2.0
            left_count = position
            right_count = count - position
            left_sum = prefix[position]
            right_sum = total - left_sum
            left_sse = prefix_sq[position] - left_sum**2 / left_count
            right_sse = (
                prefix_sq[count] - prefix_sq[position] - right_sum**2 / right_count
            )
            gain = parent_sse - left_sse - right_sse
            if gain > best_gain + 1e-12:
                best_gain = gain
                best_feature = feature
                best_threshold = threshold
    if best_feature < 0:
        return _Leaf(mean)

    importance[best_feature] += best_gain
    left_indexes = [
        i for i in indexes if features[i][best_feature] <= best_threshold
    ]
    right_indexes = [
        i for i in indexes if features[i][best_feature] > best_threshold
    ]
    if len(left_indexes) < min_leaf or len(right_indexes) < min_leaf:
        return _Leaf(mean)
    return _Split(
        best_feature,
        best_threshold,
        _fit_tree(
            features,
            residuals,
            left_indexes,
            depth - 1,
            min_leaf,
            split_candidates,
            importance,
        ),
        _fit_tree(
            features,
            residuals,
            right_indexes,
            depth - 1,
            min_leaf,
            split_candidates,
            importance,
        ),
    )


def _predict_tree(tree, row: Sequence[float]) -> float:
    while isinstance(tree, _Split):
        tree = tree.left if row[tree.feature] <= tree.threshold else tree.right
    return tree.value


__all__ = ["fit_gbdt"]
