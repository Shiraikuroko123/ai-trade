from __future__ import annotations

from dataclasses import dataclass
from typing import Any


LIBRARY_VERSION = 1


@dataclass(frozen=True)
class ModelDefinition:
    """One deterministic, pure-standard-library research model.

    Models rank a factor cross-section; they never emit target weights,
    orders, or signals. Hyperparameters are fixed at registration time so a
    later evaluation cannot quietly tune itself on the data it reports.
    """

    model_id: str
    version: int
    label: str
    kind: str
    formula: str
    hyperparameters: dict[str, float]

    def to_dict(self) -> dict[str, Any]:
        return {
            "model_id": self.model_id,
            "version": self.version,
            "label": self.label,
            "kind": self.kind,
            "formula": self.formula,
            "hyperparameters": dict(self.hyperparameters),
        }


MODELS: tuple[ModelDefinition, ...] = (
    ModelDefinition(
        "ridge_v1",
        1,
        "岭回归（固定 λ=0.1）",
        "ridge",
        "solve((XtX/n + 0.1*I) w = Xty/n) on train-window z-scored factors "
        "and per-date demeaned forward returns",
        {"lambda": 0.1},
    ),
    ModelDefinition(
        "factor_mean_v1",
        1,
        "等权方向因子均值",
        "factor_mean",
        "mean(direction_f * zscore_train(factor_f)) over the selected factors",
        {},
    ),
    ModelDefinition(
        "gbdt_v1",
        1,
        "梯度提升树（固定超参）",
        "gbdt",
        "24 depth-2 least-squares trees, learning rate 0.12, min leaf 20, 8 "
        "deterministic quantile split candidates, refit every 8 evaluated "
        "dates on the most recent 2000 z-scored training rows with per-date "
        "demeaned forward returns",
        {
            "trees": 24,
            "depth": 2,
            "learning_rate": 0.12,
            "min_leaf": 20,
            "split_candidates": 8,
            "refit_interval": 8,
            "max_train_rows": 2000,
        },
    ),
)

_BY_ID = {item.model_id: item for item in MODELS}


def model_definition(model_id: str) -> ModelDefinition:
    try:
        return _BY_ID[model_id]
    except KeyError as exc:
        raise ValueError(f"Unknown model: {model_id!r}") from exc


def model_registry() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "library_version": LIBRARY_VERSION,
        "models": [item.to_dict() for item in MODELS],
        "safety": {
            "research_only": True,
            "creates_no_signal": True,
            "orders_created": False,
        },
    }


__all__ = [
    "LIBRARY_VERSION",
    "MODELS",
    "ModelDefinition",
    "model_definition",
    "model_registry",
]
