from __future__ import annotations

from dataclasses import asdict, dataclass, replace
import math
from typing import Any, Mapping

from ..config import AppConfig
from ..models import RiskSettings, StrategySettings


SCHEMA_VERSION = 1


@dataclass(frozen=True)
class ParameterSpec:
    scope: str
    name: str
    label: str
    type: str
    minimum: float | int | None = None
    maximum: float | int | None = None
    step: float | int | None = None
    unit: str | None = None
    options: tuple[str, ...] = ()

    @property
    def key(self) -> str:
        return f"{self.scope}.{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "name": self.name,
            "label": self.label,
            "type": self.type,
            "min": self.minimum,
            "max": self.maximum,
            "step": self.step,
            "unit": self.unit,
            "options": list(self.options),
        }


PARAMETERS = (
    ParameterSpec(
        "strategy", "rebalance_days", "调仓间隔", "integer", 5, 252, 1, "sessions"
    ),
    ParameterSpec(
        "strategy", "lookback_days", "动量回看期", "integer", 20, 504, 1, "sessions"
    ),
    ParameterSpec(
        "strategy", "skip_days", "动量跳过期", "integer", 0, 63, 1, "sessions"
    ),
    ParameterSpec(
        "strategy", "trend_sma_days", "趋势均线周期", "integer", 20, 504, 1, "sessions"
    ),
    ParameterSpec(
        "strategy", "volatility_days", "波动率窗口", "integer", 10, 252, 1, "sessions"
    ),
    ParameterSpec(
        "strategy", "top_n", "持有标的数量", "integer", 1, 100, 1, "instruments"
    ),
    ParameterSpec(
        "strategy", "minimum_momentum", "最低动量", "number", -1.0, 1.0, 0.01, "ratio"
    ),
    ParameterSpec(
        "strategy",
        "target_annual_volatility",
        "目标年化波动率",
        "number",
        0.0,
        0.5,
        0.01,
        "ratio",
    ),
    ParameterSpec(
        "strategy",
        "minimum_cash_weight",
        "最低现金比例",
        "number",
        0.0,
        0.9,
        0.01,
        "ratio",
    ),
    ParameterSpec(
        "strategy",
        "max_position_weight",
        "单标的最高权重",
        "number",
        0.05,
        1.0,
        0.01,
        "ratio",
    ),
    ParameterSpec(
        "strategy", "covariance_days", "协方差窗口", "integer", 0, 504, 1, "sessions"
    ),
    ParameterSpec(
        "strategy",
        "covariance_shrinkage",
        "协方差收缩系数",
        "number",
        0.0,
        1.0,
        0.01,
        "ratio",
    ),
    ParameterSpec(
        "strategy",
        "minimum_average_amount",
        "最低日均成交额",
        "number",
        0.0,
        1_000_000_000_000.0,
        100_000.0,
        "CNY",
    ),
    ParameterSpec(
        "strategy",
        "minimum_rebalance_weight",
        "最小调仓权重",
        "number",
        0.0,
        0.5,
        0.005,
        "ratio",
    ),
    ParameterSpec(
        "strategy",
        "weighting_method",
        "权重分配方法",
        "choice",
        options=("inverse_volatility", "risk_parity"),
    ),
    ParameterSpec(
        "strategy",
        "risk_model",
        "风险模型",
        "choice",
        options=("conservative_sum", "covariance"),
    ),
    ParameterSpec(
        "strategy",
        "max_asset_class_weight",
        "单资产类别最高权重",
        "number",
        0.05,
        1.0,
        0.01,
        "ratio",
    ),
    ParameterSpec(
        "strategy",
        "max_sector_weight",
        "单行业最高权重",
        "number",
        0.05,
        1.0,
        0.01,
        "ratio",
    ),
    ParameterSpec(
        "strategy",
        "capacity_reference_cash",
        "容量测算资金",
        "number",
        0.0,
        1_000_000_000_000.0,
        10_000.0,
        "CNY",
    ),
    ParameterSpec(
        "strategy",
        "max_average_amount_participation",
        "最高成交参与率",
        "number",
        0.0001,
        1.0,
        0.005,
        "ratio",
    ),
    ParameterSpec(
        "strategy", "capacity_days", "容量执行天数", "integer", 1, 60, 1, "sessions"
    ),
    ParameterSpec(
        "risk",
        "max_portfolio_drawdown",
        "组合最大回撤止损",
        "number",
        0.01,
        0.5,
        0.005,
        "ratio",
    ),
    ParameterSpec(
        "risk",
        "max_daily_loss",
        "单日最大亏损止损",
        "number",
        0.005,
        0.2,
        0.005,
        "ratio",
    ),
    ParameterSpec(
        "risk", "cooldown_days", "风险冷静期", "integer", 0, 252, 1, "sessions"
    ),
)


_BY_KEY = {item.key: item for item in PARAMETERS}


def parameter_schema() -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "parameters": [item.to_dict() for item in PARAMETERS],
    }


def settings_snapshot(
    strategy: StrategySettings, risk: RiskSettings
) -> dict[str, dict[str, Any]]:
    return {"strategy": asdict(strategy), "risk": asdict(risk)}


def apply_changes(
    config: AppConfig,
    baseline: Mapping[str, Mapping[str, Any]],
    changes: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    normalized = normalize_changes(config, baseline, changes)
    strategy_values = {**dict(baseline["strategy"]), **normalized["strategy"]}
    risk_values = {**dict(baseline["risk"]), **normalized["risk"]}
    strategy = replace(config.strategy, **strategy_values)
    risk = replace(config.risk, **risk_values)
    _validate_combination(config, strategy, risk)
    candidate = settings_snapshot(strategy, risk)
    effective = {
        scope: {
            name: value
            for name, value in values.items()
            if candidate[scope][name] != baseline[scope][name]
        }
        for scope, values in normalized.items()
    }
    if not any(effective.values()):
        raise ValueError("Candidate changes must differ from the active baseline")
    return candidate, effective


def normalize_changes(
    config: AppConfig,
    baseline: Mapping[str, Mapping[str, Any]],
    changes: Mapping[str, Any],
) -> dict[str, dict[str, Any]]:
    if not isinstance(changes, Mapping):
        raise ValueError("Candidate changes must be an object")
    flattened: dict[str, Any] = {}
    for key, value in changes.items():
        if key in {"strategy", "risk"}:
            if not isinstance(value, Mapping):
                raise ValueError(f"{key} changes must be an object")
            for name, nested_value in value.items():
                flattened[f"{key}.{name}"] = nested_value
        elif "." in str(key):
            flattened[str(key)] = value
        else:
            raise ValueError(f"Unknown parameter scope: {key!r}")
    if not flattened:
        raise ValueError("At least one parameter change is required")

    output: dict[str, dict[str, Any]] = {"strategy": {}, "risk": {}}
    for key, value in flattened.items():
        spec = _BY_KEY.get(key)
        if spec is None:
            raise ValueError(f"Parameter is not editable: {key}")
        parsed = _coerce(spec, value)
        if spec.name == "top_n" and parsed > len(config.instruments):
            raise ValueError("strategy.top_n exceeds the configured universe size")
        if spec.scope not in baseline or spec.name not in baseline[spec.scope]:
            raise ValueError(f"Parameter is unavailable in this configuration: {key}")
        output[spec.scope][spec.name] = parsed
    return output


def parameter_spec(scope: str, name: str) -> ParameterSpec:
    try:
        return _BY_KEY[f"{scope}.{name}"]
    except KeyError as exc:
        raise ValueError(f"Parameter is not editable: {scope}.{name}") from exc


def clamp_parameter(spec: ParameterSpec, value: float) -> int | float:
    if spec.minimum is not None:
        value = max(float(spec.minimum), value)
    if spec.maximum is not None:
        value = min(float(spec.maximum), value)
    if spec.type == "integer":
        return int(round(value))
    return float(value)


def _coerce(spec: ParameterSpec, value: Any) -> Any:
    if spec.type == "choice":
        if not isinstance(value, str) or value not in spec.options:
            raise ValueError(f"{spec.key} must be one of {list(spec.options)}")
        return value
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{spec.key} must be numeric")
    parsed: int | float
    if spec.type == "integer":
        if isinstance(value, float) and not value.is_integer():
            raise ValueError(f"{spec.key} must be an integer")
        parsed = int(value)
    else:
        parsed = float(value)
    if not math.isfinite(float(parsed)):
        raise ValueError(f"{spec.key} must be finite")
    if spec.minimum is not None and parsed < spec.minimum:
        raise ValueError(f"{spec.key} must be at least {spec.minimum}")
    if spec.maximum is not None and parsed > spec.maximum:
        raise ValueError(f"{spec.key} must be at most {spec.maximum}")
    return parsed


def _validate_combination(
    config: AppConfig, strategy: StrategySettings, risk: RiskSettings
) -> None:
    if strategy.skip_days >= strategy.lookback_days:
        raise ValueError("strategy.skip_days must be below strategy.lookback_days")
    if strategy.top_n > len(config.instruments):
        raise ValueError("strategy.top_n exceeds the configured universe size")
    if strategy.minimum_cash_weight + strategy.max_position_weight <= 0:
        raise ValueError("Strategy allocation settings are invalid")
    if risk.max_daily_loss >= risk.max_portfolio_drawdown:
        raise ValueError(
            "risk.max_daily_loss must be below risk.max_portfolio_drawdown"
        )
