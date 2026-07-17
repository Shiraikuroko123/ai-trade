from __future__ import annotations

from dataclasses import dataclass
import math
from typing import Any, Mapping


MONITORING_OK = "MONITORING_OK"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


@dataclass(frozen=True)
class LifecyclePolicy:
    minimum_sessions: int = 20
    window_sessions: int = 60
    validation_sharpe_tolerance: float = 0.75
    relative_sharpe_tolerance: float = 0.35
    drawdown_tolerance: float = 0.05

    def __post_init__(self) -> None:
        if self.minimum_sessions < 2:
            raise ValueError("minimum_sessions must be at least 2")
        if self.window_sessions < self.minimum_sessions:
            raise ValueError("window_sessions must cover minimum_sessions")
        for name in (
            "validation_sharpe_tolerance",
            "relative_sharpe_tolerance",
            "drawdown_tolerance",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"{name} must be finite and non-negative")

    def public_dict(self) -> dict[str, int | float]:
        return {
            "minimum_sessions": self.minimum_sessions,
            "window_sessions": self.window_sessions,
            "validation_sharpe_tolerance": self.validation_sharpe_tolerance,
            "relative_sharpe_tolerance": self.relative_sharpe_tolerance,
            "drawdown_tolerance": self.drawdown_tolerance,
        }


def evaluate_strategy_decay(
    *,
    session_count: int,
    recent_candidate: Mapping[str, Any] | None,
    recent_parent: Mapping[str, Any] | None,
    validation_candidate: Mapping[str, Any],
    maximum_drawdown: float,
    policy: LifecyclePolicy,
) -> dict[str, Any]:
    if isinstance(session_count, bool) or not isinstance(session_count, int):
        raise ValueError("session_count must be an integer")
    if session_count < 0:
        raise ValueError("session_count must be non-negative")
    hard_drawdown = _finite(maximum_drawdown, "maximum_drawdown")
    if not 0 < hard_drawdown < 1:
        raise ValueError("maximum_drawdown must be between 0 and 1")

    minimum_check = {
        "id": "minimum_sessions",
        "label": "近期样本达到最低观察期",
        "passed": session_count >= policy.minimum_sessions,
        "detail": f"已观察 {session_count} / {policy.minimum_sessions} 个交易日",
    }
    if recent_candidate is None or recent_parent is None:
        if session_count >= policy.minimum_sessions:
            raise ValueError("recent metrics are required after the minimum observation period")
        return {
            "verdict": INSUFFICIENT_DATA,
            "review_required": False,
            "automatic_state_change": False,
            "checks": [minimum_check],
            "failed_checks": [],
            "policy": policy.public_dict(),
        }

    recent = _required_metrics(recent_candidate, "recent_candidate")
    parent = _required_metrics(recent_parent, "recent_parent")
    validation = _required_metrics(validation_candidate, "validation_candidate")
    checks = [
        minimum_check,
        {
            "id": "validation_sharpe",
            "label": "近期 Sharpe 未明显偏离激活时留出集",
            "passed": recent["sharpe"] + policy.validation_sharpe_tolerance
            >= validation["sharpe"],
            "detail": (
                f"近期 {recent['sharpe']:.3f}；激活参考 {validation['sharpe']:.3f}；"
                f"容忍差 {policy.validation_sharpe_tolerance:.2f}"
            ),
        },
        {
            "id": "relative_sharpe",
            "label": "近期表现未明显落后于同窗父基线",
            "passed": recent["sharpe"] + policy.relative_sharpe_tolerance
            >= parent["sharpe"],
            "detail": (
                f"活动版本 {recent['sharpe']:.3f}；父基线 {parent['sharpe']:.3f}；"
                f"容忍差 {policy.relative_sharpe_tolerance:.2f}"
            ),
        },
        {
            "id": "drawdown_limit",
            "label": "近期回撤仍在策略硬限制内",
            "passed": recent["max_drawdown"] >= -hard_drawdown,
            "detail": (
                f"近期 {recent['max_drawdown']:.2%}；硬限制 {-hard_drawdown:.2%}"
            ),
        },
        {
            "id": "drawdown_decay",
            "label": "近期回撤未明显恶化于激活参考",
            "passed": recent["max_drawdown"] + policy.drawdown_tolerance
            >= validation["max_drawdown"],
            "detail": (
                f"近期 {recent['max_drawdown']:.2%}；激活参考 "
                f"{validation['max_drawdown']:.2%}；容忍差 {policy.drawdown_tolerance:.2%}"
            ),
        },
    ]
    failed = [str(item["id"]) for item in checks if not bool(item["passed"])]
    sufficient = bool(minimum_check["passed"])
    verdict = (
        INSUFFICIENT_DATA
        if not sufficient
        else REVIEW_REQUIRED
        if failed
        else MONITORING_OK
    )
    return {
        "verdict": verdict,
        "review_required": verdict == REVIEW_REQUIRED,
        "automatic_state_change": False,
        "checks": checks,
        "failed_checks": failed if sufficient else [],
        "policy": policy.public_dict(),
    }


def _required_metrics(value: Mapping[str, Any], name: str) -> dict[str, float]:
    return {
        key: _finite(value.get(key), f"{name}.{key}")
        for key in ("sharpe", "max_drawdown")
    }


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{name} must be finite")
    return parsed
