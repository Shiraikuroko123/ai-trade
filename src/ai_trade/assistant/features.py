from __future__ import annotations

import math
import statistics
from datetime import date
from typing import Any, Sequence


ALLOWED_CONCLUSIONS = {
    "NO_ACTION",
    "WATCH",
    "REVIEW_CANDIDATE",
    "REDUCE_RISK",
}

RESEARCH_PERSPECTIVE_KEYS = (
    "technical",
    "risk",
    "fundamental_coverage",
    "sentiment_coverage",
    "strategy_gate",
)

PERSPECTIVE_AUDIT_METHOD = "deterministic-perspective-audit-v1"


def build_local_analysis(
    bars: Sequence[Any],
    research_evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not bars:
        raise ValueError("No completed bars are available for assistant analysis")
    _validate_bars(bars)
    closes = [float(bar.close) for bar in bars]
    ema20_values = _ema(closes, 20)
    ema50_values = _ema(closes, 50)
    returns = [
        closes[index] / closes[index - 1] - 1.0 for index in range(1, len(closes))
    ]
    latest = bars[-1]

    return_1d = _period_return(closes, 1)
    return_5d = _period_return(closes, 5)
    return_20d = _period_return(closes, 20)
    return_60d = _period_return(closes, 60)
    volatility20 = (
        statistics.stdev(returns[-20:]) * math.sqrt(252) if len(returns) >= 2 else None
    )
    atr14 = _atr(bars, 14)
    atr14_pct = atr14 / closes[-1] if atr14 is not None and closes[-1] else None
    rsi14 = _rsi(closes, 14)
    level_window = bars[-min(20, len(bars)) :]
    support20 = min(float(bar.low) for bar in level_window)
    resistance20 = max(float(bar.high) for bar in level_window)
    candle = _candle_structure(latest)
    previous = bars[-21:-1]
    breakout = "NONE"
    if previous:
        if closes[-1] > max(float(bar.high) for bar in previous):
            breakout = "UP"
        elif closes[-1] < min(float(bar.low) for bar in previous):
            breakout = "DOWN"

    trend = _trend(closes[-1], ema20_values[-1], ema50_values[-1], return_20d)
    regime = (
        "INSUFFICIENT" if len(bars) < 51 else "TREND" if trend != "MIXED" else "RANGE"
    )
    volatility = _volatility_label(volatility20, atr14_pct)
    score = _score(trend, return_20d, rsi14, volatility, breakout, len(bars))
    risk_level = _risk_level(volatility, return_1d, atr14_pct)
    conclusion = _conclusion(regime, trend, score, risk_level)
    gate = (
        "STOP"
        if conclusion in {"NO_ACTION", "REDUCE_RISK"}
        else "WATCH"
        if conclusion == "WATCH"
        else "PROCEED"
    )

    features = {
        "close": _rounded(closes[-1]),
        "ema20": _rounded(ema20_values[-1]),
        "ema50": _rounded(ema50_values[-1]),
        "return_1d": _rounded(return_1d),
        "return_5d": _rounded(return_5d),
        "return_20d": _rounded(return_20d),
        "return_60d": _rounded(return_60d),
        "annualized_volatility_20d": _rounded(volatility20),
        "atr14": _rounded(atr14),
        "atr14_pct": _rounded(atr14_pct),
        "rsi14": _rounded(rsi14),
        "support20": _rounded(support20),
        "resistance20": _rounded(resistance20),
        "last_candle": candle,
        "breakout20": breakout,
        "bar_count": len(bars),
        "fundamental": _fundamental_features(research_evidence),
    }
    evidence = _evidence(features, trend, regime, volatility, gate)
    diagnosis_refs = [item["evidence_id"] for item in evidence]
    diagnosis = {
        "stage": "market_diagnosis",
        "trend": trend,
        "regime": regime,
        "volatility": volatility,
        "score": score,
        "summary": _diagnosis_summary(trend, regime, volatility, score, breakout),
        "gate": gate,
        "evidence": evidence,
    }
    assessment = {
        "stage": "risk_assessment",
        "conclusion": conclusion,
        "summary": _assessment_summary(conclusion, risk_level, trend),
        "risk_level": risk_level,
        "risk_budget_pct": _risk_budget(conclusion),
        "evidence_ids": _assessment_refs(conclusion, diagnosis_refs),
        "invalidation": _invalidation(features, conclusion),
        "scenarios": _scenarios(features),
    }
    perspectives = build_research_perspectives(features, diagnosis, assessment)
    decision_path = _decision_path(
        regime=regime,
        risk_level=risk_level,
        trend=trend,
        score=score,
        conclusion=conclusion,
    )
    chart = {
        "points": [
            {
                "date": bar.date.isoformat(),
                "close": _rounded(float(bar.close)),
                "ema20": _rounded(ema20_values[index]),
                "ema50": _rounded(ema50_values[index]),
            }
            for index, bar in enumerate(bars)
        ],
        "series": [
            {"key": "close", "label": "收盘价", "role": "price"},
            {"key": "ema20", "label": "EMA20", "role": "fast_trend"},
            {"key": "ema50", "label": "EMA50", "role": "slow_trend"},
        ],
        "levels": {
            "support20": _rounded(support20),
            "resistance20": _rounded(resistance20),
        },
    }
    return {
        "features": features,
        "diagnosis": diagnosis,
        "assessment": assessment,
        "perspectives": perspectives,
        "decision_path": decision_path,
        "chart": chart,
    }


def build_research_perspectives(
    features: dict[str, Any],
    diagnosis: dict[str, Any],
    assessment: dict[str, Any],
) -> list[dict[str, Any]]:
    """Build deterministic, evidence-bound research views.

    The coverage views deliberately report unavailable data instead of filling
    gaps with model prose. This keeps the role-based presentation useful while
    preserving the research-only authority boundary.
    """
    evidence = diagnosis.get("evidence") if isinstance(diagnosis, dict) else []
    allowed = {
        item.get("evidence_id")
        for item in evidence
        if isinstance(item, dict) and isinstance(item.get("evidence_id"), str)
    }
    trend = str(diagnosis.get("trend", "MIXED"))
    risk_level = str(assessment.get("risk_level", "HIGH"))
    conclusion = str(assessment.get("conclusion", "NO_ACTION"))
    score = diagnosis.get("score")

    def refs(*values: str) -> list[str]:
        return [value for value in values if value in allowed]

    technical_stance = (
        "SUPPORTIVE"
        if trend == "UP" and isinstance(score, int) and score >= 60
        else "ADVERSE"
        if trend == "DOWN"
        else "MIXED"
    )
    risk_stance = {
        "LOW": "SUPPORTIVE",
        "MEDIUM": "CAUTION",
        "HIGH": "ADVERSE",
    }.get(risk_level, "CAUTION")
    strategy_stance = {
        "REVIEW_CANDIDATE": "REVIEW",
        "WATCH": "CAUTION",
        "REDUCE_RISK": "ADVERSE",
        "NO_ACTION": "CAUTION",
    }.get(conclusion, "CAUTION")
    fundamental = (
        features.get("fundamental")
        if isinstance(features.get("fundamental"), dict)
        else {}
    )
    fundamental_available = fundamental.get("available") is True
    fundamental_stance = (
        str(fundamental.get("stance") or "MIXED")
        if fundamental_available
        else "NOT_AVAILABLE"
    )
    fundamental_refs = refs(
        "coverage.fundamentals",
        "fundamental.report_date",
        "fundamental.weighted_roe_pct",
        "fundamental.revenue_yoy_pct",
        "fundamental.net_profit_yoy_pct",
        "fundamental.operating_cash_flow_per_share",
        "valuation.pe_ttm",
        "valuation.pb",
        "valuation.percentile.pe_ttm",
        "valuation.percentile.pb",
        "valuation.percentile.cash_flow",
        "valuation.percentile.ps_ttm",
        "fundamental.independent_check",
        "valuation.independent_check",
    )

    return [
        {
            "key": "technical",
            "label": "技术面",
            "status": "AVAILABLE",
            "stance": technical_stance,
            "summary": (
                f"基于 EMA、20 日动量、RSI 和突破状态，当前趋势为{_label(trend)}。"
            ),
            "evidence_ids": refs(
                "trend.ema20",
                "trend.ema50",
                "momentum.return20",
                "momentum.rsi14",
                "structure.last_candle",
            ),
            "limitation": "只使用已完成交易日 OHLCV，不代表盘中或未来价格。",
        },
        {
            "key": "risk",
            "label": "风险面",
            "status": "AVAILABLE",
            "stance": risk_stance,
            "summary": (
                f"近期波动风险为{_label(risk_level)}，ATR 和波动率用于决定复核强度。"
            ),
            "evidence_ids": refs(
                "risk.volatility20",
                "risk.atr14_pct",
                "structure.support20",
            ),
            "limitation": "风险视角不是止损价，也不会生成仓位或订单。",
        },
        {
            "key": "fundamental_coverage",
            "label": "基本面覆盖",
            "status": "AVAILABLE" if fundamental_available else "UNAVAILABLE",
            "stance": fundamental_stance,
            "summary": (
                _fundamental_summary(fundamental)
                if fundamental_available
                else "当前 K 线日期没有匹配的股票点时基本面快照，基本面暂不可评估。"
            ),
            "evidence_ids": fundamental_refs,
            "limitation": (
                "只使用同一交易日已落盘的第三方财务与估值证据，不改变确定性交易结论。"
                if fundamental_available
                else str(
                    fundamental.get("limitation")
                    or "需先刷新并校验同一交易日的股票基本面快照。"
                )
            ),
        },
        {
            "key": "sentiment_coverage",
            "label": "情绪覆盖",
            "status": "UNAVAILABLE",
            "stance": "NOT_AVAILABLE",
            "summary": "当前快照未包含新闻、公告、资金流或情绪数据，情绪暂不可评估。",
            "evidence_ids": refs("coverage.sentiment"),
            "limitation": "不会用模型语言猜测市场情绪；需要可追溯的外部数据源。",
        },
        {
            "key": "strategy_gate",
            "label": "策略门禁",
            "status": "AVAILABLE",
            "stance": strategy_stance,
            "summary": (
                f"确定性研究结论为{_label(conclusion)}；该状态只决定研究复核范围。"
            ),
            "evidence_ids": refs(
                "strategy.gate",
                *(
                    assessment.get("evidence_ids", [])
                    if isinstance(assessment.get("evidence_ids"), list)
                    else []
                ),
            ),
            "limitation": "策略门禁不等同于买卖指令，也不会解锁模拟或真实交易权限。",
        },
    ]


def build_perspective_conflict_audit(
    perspectives: Sequence[dict[str, Any]],
    assessment: dict[str, Any],
    *,
    model_review: dict[str, Any],
) -> dict[str, Any]:
    """Compare research views without turning them into trading votes."""
    by_key = {
        str(item.get("key")): item
        for item in perspectives
        if isinstance(item, dict) and item.get("key") in RESEARCH_PERSPECTIVE_KEYS
    }
    if set(by_key) != set(RESEARCH_PERSPECTIVE_KEYS):
        raise ValueError("Research perspective coverage is incomplete")

    conflicts: list[dict[str, Any]] = []

    def evidence_for(*keys: str) -> list[str]:
        values = {
            evidence_id
            for key in keys
            for evidence_id in by_key[key].get("evidence_ids", [])
            if isinstance(evidence_id, str)
        }
        return sorted(values)

    def append_conflict(
        conflict_id: str,
        *,
        title: str,
        summary: str,
        perspective_keys: list[str],
        resolution: str,
    ) -> None:
        conflicts.append(
            {
                "conflict_id": conflict_id,
                "severity": "WARNING",
                "title": title,
                "summary": summary,
                "perspective_keys": perspective_keys,
                "evidence_ids": evidence_for(*perspective_keys),
                "resolution": resolution,
            }
        )

    technical = str(by_key["technical"].get("stance"))
    risk = str(by_key["risk"].get("stance"))
    fundamental = str(by_key["fundamental_coverage"].get("stance"))
    strategy = str(by_key["strategy_gate"].get("stance"))
    if (technical, risk) in {
        ("SUPPORTIVE", "ADVERSE"),
        ("ADVERSE", "SUPPORTIVE"),
    }:
        append_conflict(
            "direction_risk_divergence",
            title="方向与风险证据分歧",
            summary=(
                "技术方向和近期波动风险给出相反侧重，不能只依据其中一个视角推进复核。"
            ),
            perspective_keys=["technical", "risk"],
            resolution="等待新收盘证据，或由人工同时核对趋势、波动和组合暴露。",
        )
    if (technical, fundamental) in {
        ("SUPPORTIVE", "ADVERSE"),
        ("ADVERSE", "SUPPORTIVE"),
    }:
        append_conflict(
            "technical_fundamental_divergence",
            title="技术面与基本面证据分歧",
            summary="同一快照中的价格趋势与已披露财务、估值证据方向相反，不能只依据单一视角推进复核。",
            perspective_keys=["technical", "fundamental_coverage"],
            resolution="保留分歧并由人工核对披露期、估值分位和价格窗口；基本面角色不直接改写交易结论。",
        )
    if strategy == "REVIEW" and technical != "SUPPORTIVE":
        append_conflict(
            "strategy_technical_divergence",
            title="策略门禁与技术证据分歧",
            summary="策略门禁进入候选复核，但技术面没有形成支持性一致证据。",
            perspective_keys=["technical", "strategy_gate"],
            resolution="保持研究状态，重新核对门禁来源和技术证据后再由人工判断。",
        )
    if strategy == "REVIEW" and risk in {"CAUTION", "ADVERSE"}:
        append_conflict(
            "strategy_risk_divergence",
            title="策略门禁与风险证据分歧",
            summary="策略门禁进入候选复核，但风险面仍要求谨慎或显示不利。",
            perspective_keys=["risk", "strategy_gate"],
            resolution="不得扩大研究风险预算；先核对波动和风险约束。",
        )
    if strategy == "ADVERSE" and technical == "SUPPORTIVE":
        append_conflict(
            "risk_override",
            title="风险结论覆盖支持性技术信号",
            summary="技术面偏支持，但确定性风险结论更严格，当前不能据此扩大复核权限。",
            perspective_keys=["technical", "risk", "strategy_gate"],
            resolution="保留更严格的风险结论，并由人工复核现有暴露。",
        )
    if model_review.get("relaxation_blocked") is True:
        append_conflict(
            "model_authority_guard",
            title="模型放宽结论已被阻断",
            summary="模型建议的研究结论比确定性本地结论更宽松，权限守卫已保留本地结论。",
            perspective_keys=["strategy_gate"],
            resolution="模型输出只作文字复核，不得提升候选级别或研究风险预算。",
        )

    coverage_gaps = [
        {
            "perspective_key": key,
            "label": str(by_key[key].get("label") or key),
            "summary": str(by_key[key].get("summary") or "该视角数据不可用。"),
            "evidence_ids": [
                value
                for value in by_key[key].get("evidence_ids", [])
                if isinstance(value, str)
            ],
            "resolution": str(
                by_key[key].get("limitation") or "接入并校验对应数据源后再评估。"
            ),
        }
        for key in RESEARCH_PERSPECTIVE_KEYS
        if by_key[key].get("status") == "UNAVAILABLE"
    ]
    conflict_count = len(conflicts)
    gap_count = len(coverage_gaps)
    status = (
        "REVIEW_REQUIRED"
        if conflict_count
        else "INCOMPLETE"
        if gap_count
        else "ALIGNED"
    )
    if conflict_count:
        summary = (
            f"发现 {conflict_count} 项需要人工复核的视角分歧；"
            f"另有 {gap_count} 个数据覆盖缺口。"
        )
    elif gap_count:
        summary = (
            f"已覆盖视角未发现实质分歧；仍有 {gap_count} 个数据覆盖缺口，"
            "不能视为完整共识。"
        )
    else:
        summary = "全部已登记视角均有数据，当前未发现实质分歧。"

    normalized_review = {
        "mode": str(model_review.get("mode") or "local"),
        "attempted": model_review.get("attempted") is True,
        "applied": model_review.get("applied") is True,
        "deterministic_conclusion": model_review.get("deterministic_conclusion"),
        "proposed_conclusion": model_review.get("proposed_conclusion"),
        "effective_conclusion": model_review.get(
            "effective_conclusion", assessment.get("conclusion")
        ),
        "relaxation_blocked": model_review.get("relaxation_blocked") is True,
        "tightened": model_review.get("tightened") is True,
    }
    return {
        "method": PERSPECTIVE_AUDIT_METHOD,
        "status": status,
        "summary": summary,
        "conflict_count": conflict_count,
        "coverage_gap_count": gap_count,
        "conflicts": conflicts,
        "coverage_gaps": coverage_gaps,
        "model_review": normalized_review,
        "authority": "research_only",
        "execution_authorized": False,
    }


def _validate_bars(bars: Sequence[Any]) -> None:
    previous_date = None
    for index, bar in enumerate(bars):
        values = [float(bar.open), float(bar.close), float(bar.high), float(bar.low)]
        if not all(math.isfinite(value) and value > 0 for value in values):
            raise ValueError(f"Bar {index} contains a non-positive or non-finite price")
        if float(bar.high) < max(float(bar.open), float(bar.close), float(bar.low)):
            raise ValueError(f"Bar {index} high is inconsistent with OHLC prices")
        if float(bar.low) > min(float(bar.open), float(bar.close), float(bar.high)):
            raise ValueError(f"Bar {index} low is inconsistent with OHLC prices")
        if previous_date is not None and bar.date <= previous_date:
            raise ValueError("Assistant bars must be strictly increasing by date")
        previous_date = bar.date


def _ema(values: list[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def _period_return(closes: list[float], sessions: int) -> float | None:
    return closes[-1] / closes[-sessions - 1] - 1.0 if len(closes) > sessions else None


def _atr(bars: Sequence[Any], period: int) -> float | None:
    if len(bars) < 2:
        return None
    true_ranges = []
    for previous, current in zip(bars, bars[1:]):
        true_ranges.append(
            max(
                float(current.high) - float(current.low),
                abs(float(current.high) - float(previous.close)),
                abs(float(current.low) - float(previous.close)),
            )
        )
    return statistics.fmean(true_ranges[-period:]) if true_ranges else None


def _rsi(closes: list[float], period: int) -> float | None:
    if len(closes) <= period:
        return None
    changes = [closes[index] - closes[index - 1] for index in range(1, len(closes))]
    selected = changes[-period:]
    average_gain = statistics.fmean(max(value, 0.0) for value in selected)
    average_loss = statistics.fmean(max(-value, 0.0) for value in selected)
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + average_gain / average_loss)


def _candle_structure(bar: Any) -> str:
    span = float(bar.high) - float(bar.low)
    if span <= 0:
        return "FLAT"
    body_ratio = abs(float(bar.close) - float(bar.open)) / span
    if body_ratio <= 0.1:
        return "DOJI"
    return "BULLISH" if float(bar.close) > float(bar.open) else "BEARISH"


def _trend(close: float, ema20: float, ema50: float, return20: float | None) -> str:
    if close > ema20 > ema50 and (return20 is None or return20 > 0):
        return "UP"
    if close < ema20 < ema50 and (return20 is None or return20 < 0):
        return "DOWN"
    return "MIXED"


def _volatility_label(volatility: float | None, atr_pct: float | None) -> str:
    if volatility is None or atr_pct is None:
        return "UNKNOWN"
    if volatility >= 0.45 or atr_pct >= 0.055:
        return "HIGH"
    if volatility <= 0.16 and atr_pct <= 0.022:
        return "LOW"
    return "NORMAL"


def _score(
    trend: str,
    return20: float | None,
    rsi: float | None,
    volatility: str,
    breakout: str,
    bar_count: int,
) -> int:
    if bar_count < 20:
        return 0
    value = 50
    value += 20 if trend == "UP" else -20 if trend == "DOWN" else 0
    if return20 is not None:
        value += 12 if return20 > 0.03 else -12 if return20 < -0.03 else 0
    if rsi is not None:
        value += 6 if 45 <= rsi <= 68 else -8 if rsi >= 78 or rsi <= 25 else 0
    value -= 15 if volatility == "HIGH" else 0
    value += 7 if breakout == "UP" else -7 if breakout == "DOWN" else 0
    return max(0, min(100, value))


def _risk_level(volatility: str, return1: float | None, atr_pct: float | None) -> str:
    if volatility == "HIGH" or (return1 is not None and return1 <= -0.05):
        return "HIGH"
    if volatility == "UNKNOWN" or (atr_pct is not None and atr_pct >= 0.035):
        return "MEDIUM"
    return "LOW"


def _conclusion(regime: str, trend: str, score: int, risk_level: str) -> str:
    if regime == "INSUFFICIENT":
        return "NO_ACTION"
    if risk_level == "HIGH":
        return "REDUCE_RISK"
    if trend == "UP" and regime == "TREND" and score >= 65:
        return "REVIEW_CANDIDATE"
    if score >= 45:
        return "WATCH"
    return "NO_ACTION"


def _risk_budget(conclusion: str) -> int:
    return {
        "NO_ACTION": 0,
        "WATCH": 25,
        "REVIEW_CANDIDATE": 50,
        "REDUCE_RISK": 0,
    }[conclusion]


def _evidence(
    features: dict[str, Any],
    trend: str,
    regime: str,
    volatility: str,
    gate: str,
) -> list[dict[str, Any]]:
    fundamental = features["fundamental"]
    fundamental_available = fundamental.get("available") is True
    valuation_available = fundamental.get("valuation_available") is True
    rows = [
        (
            "price.close",
            "最新收盘",
            features["close"],
            "用于确认分析截止价格",
            "neutral",
        ),
        (
            "trend.ema20",
            "EMA20",
            features["ema20"],
            "短周期趋势参考",
            "positive"
            if trend == "UP"
            else "negative"
            if trend == "DOWN"
            else "neutral",
        ),
        (
            "trend.ema50",
            "EMA50",
            features["ema50"],
            f"趋势状态为 {_label(trend)}，市场结构为 {_label(regime)}",
            "positive"
            if trend == "UP"
            else "negative"
            if trend == "DOWN"
            else "neutral",
        ),
        (
            "momentum.return20",
            "20 日收益",
            features["return_20d"],
            "仅描述历史动量，不代表未来收益",
            "positive"
            if (features["return_20d"] or 0) > 0
            else "negative"
            if (features["return_20d"] or 0) < 0
            else "neutral",
        ),
        (
            "momentum.rsi14",
            "RSI14",
            features["rsi14"],
            "识别短期过热或过弱状态",
            "warning"
            if features["rsi14"] is not None
            and (features["rsi14"] >= 78 or features["rsi14"] <= 25)
            else "neutral",
        ),
        (
            "risk.volatility20",
            "20 日年化波动",
            features["annualized_volatility_20d"],
            f"波动分级为 {_label(volatility)}",
            "warning" if volatility == "HIGH" else "neutral",
        ),
        (
            "risk.atr14_pct",
            "ATR14 / 收盘",
            features["atr14_pct"],
            "衡量近期单日价格波动尺度",
            "warning" if (features["atr14_pct"] or 0) >= 0.055 else "neutral",
        ),
        (
            "structure.support20",
            "20 日支撑参考",
            features["support20"],
            "近期窗口低点，不是保证有效的止损位",
            "neutral",
        ),
        (
            "structure.resistance20",
            "20 日阻力参考",
            features["resistance20"],
            "近期窗口高点，不是保证有效的目标价",
            "neutral",
        ),
        (
            "structure.last_candle",
            "最新 K 线结构",
            features["last_candle"],
            f"20 日突破状态为 {_label(features['breakout20'])}",
            "neutral",
        ),
        (
            "coverage.fundamentals",
            "基本面数据覆盖",
            "AVAILABLE" if fundamental_available else "UNAVAILABLE",
            (
                "已绑定同一交易日的点时财务证据"
                + ("和估值分位" if valuation_available else "；同日估值证据不可用")
                if fundamental_available
                else str(
                    fundamental.get("limitation")
                    or "当前快照没有同一交易日的股票点时基本面证据"
                )
            ),
            "neutral" if fundamental_available else "warning",
        ),
        *_fundamental_evidence_rows(fundamental),
        (
            "coverage.sentiment",
            "情绪数据覆盖",
            "UNAVAILABLE",
            "当前快照没有新闻、公告、资金流和情绪数据",
            "warning",
        ),
        (
            "strategy.gate",
            "策略研究门禁",
            gate,
            "只表示研究复核范围，不是交易授权",
            "warning" if gate != "PROCEED" else "neutral",
        ),
    ]
    result = [
        {
            "evidence_id": evidence_id,
            "label": label,
            "value": value,
            "interpretation": interpretation,
            "tone": tone,
        }
        for evidence_id, label, value, interpretation, tone in rows
    ]
    for item in result:
        evidence_id = str(item["evidence_id"])
        if evidence_id == "coverage.fundamentals" or evidence_id.startswith(
            ("fundamental.", "valuation.")
        ):
            item["provenance"] = _fundamental_provenance(
                fundamental,
                valuation=evidence_id.startswith("valuation."),
            )
    return result


def _fundamental_features(research_evidence: dict[str, Any] | None) -> dict[str, Any]:
    source = research_evidence if isinstance(research_evidence, dict) else {}
    fundamental_snapshot = (
        source.get("fundamentals")
        if isinstance(source.get("fundamentals"), dict)
        else {}
    )
    valuation_snapshot = (
        source.get("valuation")
        if isinstance(source.get("valuation"), dict)
        else {}
    )
    fundamental_record = _single_evidence_record(fundamental_snapshot)
    valuation_record = _single_evidence_record(valuation_snapshot)
    period = {}
    periods = fundamental_record.get("periods")
    if isinstance(periods, list) and periods and isinstance(periods[0], dict):
        period = periods[0]

    metrics = {
        "report_date": _bounded_date_text(period.get("report_date")),
        "weighted_roe_pct": _finite_number(period.get("weighted_roe_pct")),
        "revenue_yoy_pct": _finite_number(period.get("revenue_yoy_pct")),
        "net_profit_yoy_pct": _finite_number(period.get("net_profit_yoy_pct")),
        "operating_cash_flow_per_share": _finite_number(
            period.get("operating_cash_flow_per_share")
        ),
        "pe_ttm": _finite_number(valuation_record.get("pe_ttm")),
        "pb": _finite_number(valuation_record.get("pb")),
    }
    raw_percentiles = valuation_record.get("valuation_percentiles")
    percentiles = {
        key: _finite_number(raw_percentiles.get(key))
        if isinstance(raw_percentiles, dict)
        else None
        for key in ("pe_ttm", "pb", "cash_flow", "ps_ttm")
    }
    fundamental_available = bool(fundamental_record)
    valuation_available = bool(valuation_record)
    available = fundamental_available or valuation_available
    fundamental_check = _independent_check(fundamental_record)
    valuation_check = _independent_check(valuation_record)
    independent_conflict = any(
        item.get("status") == "conflict"
        for item in (fundamental_check, valuation_check)
    )
    positive, adverse = _fundamental_direction_counts(metrics, percentiles)
    directional_count = positive + adverse
    if not available:
        stance = "NOT_AVAILABLE"
        abstention_reason = "matching_snapshot_unavailable"
    elif independent_conflict:
        stance = "MIXED"
        abstention_reason = "independent_source_conflict"
    elif directional_count < 2:
        stance = "MIXED"
        abstention_reason = "insufficient_directional_evidence"
    elif positive and adverse:
        stance = "MIXED"
        abstention_reason = "conflicting_directional_evidence"
    elif positive:
        stance = "SUPPORTIVE"
        abstention_reason = None
    else:
        stance = "ADVERSE"
        abstention_reason = None

    return {
        "available": available,
        "fundamentals_available": fundamental_available,
        "valuation_available": valuation_available,
        "stance": stance,
        "abstained": abstention_reason is not None,
        "abstention_reason": abstention_reason,
        "directional_evidence_count": directional_count,
        "positive_evidence_count": positive,
        "adverse_evidence_count": adverse,
        **metrics,
        "valuation_percentiles": percentiles,
        "fundamental_independent_check": fundamental_check,
        "valuation_independent_check": valuation_check,
        "independent_source_conflict": independent_conflict,
        "fundamental_provenance": (
            _evidence_provenance(fundamental_snapshot)
            if fundamental_available
            else None
        ),
        "valuation_provenance": (
            _evidence_provenance(valuation_snapshot) if valuation_available else None
        ),
        "snapshot_binding": {
            "fundamentals_record_fingerprint": (
                fundamental_snapshot.get("record_fingerprint")
                if fundamental_available
                else None
            ),
            "valuation_record_fingerprint": (
                valuation_snapshot.get("record_fingerprint")
                if valuation_available
                else None
            ),
        },
        "limitation": str(
            source.get("limitation")
            or "当前 K 线日期没有匹配且可验证的股票基本面或估值快照。"
        ),
    }


def _single_evidence_record(snapshot: dict[str, Any]) -> dict[str, Any]:
    if (
        snapshot.get("available") is not True
        or snapshot.get("status") not in {"current", "partial"}
    ):
        return {}
    records = snapshot.get("records")
    if not isinstance(records, list) or len(records) != 1:
        return {}
    return records[0] if isinstance(records[0], dict) else {}


def _evidence_provenance(snapshot: dict[str, Any]) -> dict[str, Any] | None:
    if snapshot.get("available") is not True:
        return None
    source = snapshot.get("source") if isinstance(snapshot.get("source"), dict) else {}
    return {
        "dataset": snapshot.get("dataset"),
        "trade_date": snapshot.get("trade_date"),
        "revision_id": snapshot.get("revision_id"),
        "revision": snapshot.get("revision"),
        "evidence_fingerprint": snapshot.get("evidence_fingerprint"),
        "record_fingerprint": snapshot.get("record_fingerprint"),
        "provider": source.get("provider"),
        "certification": source.get("certification"),
    }


def _fundamental_direction_counts(
    metrics: dict[str, Any], percentiles: dict[str, float | None]
) -> tuple[int, int]:
    positive = 0
    adverse = 0
    roe = metrics["weighted_roe_pct"]
    if roe is not None:
        positive += roe >= 8.0
        adverse += roe <= 0.0
    for name in ("revenue_yoy_pct", "net_profit_yoy_pct"):
        value = metrics[name]
        if value is not None:
            positive += value > 0.0
            adverse += value < 0.0
    cash_flow = metrics["operating_cash_flow_per_share"]
    if cash_flow is not None:
        positive += cash_flow > 0.0
        adverse += cash_flow < 0.0

    available_percentiles = [
        value for value in percentiles.values() if value is not None
    ]
    low_valuation = any(value <= 25.0 for value in available_percentiles)
    high_valuation = any(value >= 75.0 for value in available_percentiles)
    if low_valuation and not high_valuation:
        positive += 1
    elif high_valuation and not low_valuation:
        adverse += 1
    elif low_valuation and high_valuation:
        positive += 1
        adverse += 1
    return int(positive), int(adverse)


def _fundamental_evidence_rows(
    fundamental: dict[str, Any],
) -> list[tuple[str, str, Any, str, str]]:
    rows: list[tuple[str, str, Any, str, str]] = []

    def append(
        evidence_id: str,
        label: str,
        value: Any,
        interpretation: str,
        tone: str = "neutral",
    ) -> None:
        if value is not None:
            rows.append((evidence_id, label, value, interpretation, tone))

    append(
        "fundamental.report_date",
        "最新已披露报告期",
        fundamental.get("report_date"),
        "报告期、公告日和更新日均不晚于分析交易日。",
    )
    append(
        "fundamental.weighted_roe_pct",
        "加权净资产收益率",
        fundamental.get("weighted_roe_pct"),
        "第三方规范化的已披露百分比，仅用于研究比较。",
    )
    append(
        "fundamental.revenue_yoy_pct",
        "营业收入同比",
        fundamental.get("revenue_yoy_pct"),
        "正负方向用于基本面角色判断，不外推未来增长。",
    )
    append(
        "fundamental.net_profit_yoy_pct",
        "归母净利润同比",
        fundamental.get("net_profit_yoy_pct"),
        "正负方向用于基本面角色判断，不外推未来利润。",
    )
    append(
        "fundamental.operating_cash_flow_per_share",
        "每股经营现金流",
        fundamental.get("operating_cash_flow_per_share"),
        "已披露期间值，不替代现金流量表复核。",
    )
    append(
        "valuation.pe_ttm",
        "市盈率 TTM",
        fundamental.get("pe_ttm"),
        "当前第三方估值快照；单独数值不判断高低。",
    )
    append(
        "valuation.pb",
        "市净率",
        fundamental.get("pb"),
        "当前第三方估值快照；单独数值不判断高低。",
    )
    for evidence_id, label, key in (
        (
            "fundamental.independent_check",
            "Tushare 基本面校验状态",
            "fundamental_independent_check",
        ),
        (
            "valuation.independent_check",
            "Tushare 估值校验状态",
            "valuation_independent_check",
        ),
    ):
        check = fundamental.get(key)
        if isinstance(check, dict) and check.get("status"):
            append(
                evidence_id,
                label,
                check.get("status"),
                "只记录独立参考源的字段级对账结果；冲突时强制弃权，不覆盖主数据。",
                "warning" if check.get("status") == "conflict" else "neutral",
            )
    percentiles = fundamental.get("valuation_percentiles")
    if not isinstance(percentiles, dict):
        percentiles = {}
    percentile_labels = {
        "pe_ttm": "市盈率历史分位",
        "pb": "市净率历史分位",
        "cash_flow": "市现率历史分位",
        "ps_ttm": "市销率历史分位",
    }
    for key, label in percentile_labels.items():
        append(
            f"valuation.percentile.{key}",
            label,
            percentiles.get(key),
            "基于至少 120 个正有限历史样本的经验分位，越高表示相对历史越高。",
        )
    return rows


def _fundamental_provenance(
    fundamental: dict[str, Any], *, valuation: bool
) -> dict[str, Any] | None:
    if valuation:
        value = fundamental.get("valuation_provenance")
        return dict(value) if isinstance(value, dict) else None
    financial = fundamental.get("fundamental_provenance")
    valuation_value = fundamental.get("valuation_provenance")
    if not isinstance(financial, dict) and not isinstance(valuation_value, dict):
        return None
    return {
        "fundamentals": dict(financial) if isinstance(financial, dict) else None,
        "valuation": (
            dict(valuation_value) if isinstance(valuation_value, dict) else None
        ),
    }


def _independent_check(record: dict[str, Any]) -> dict[str, Any]:
    value = record.get("independent_check")
    if not isinstance(value, dict):
        return {
            "provider": "tushare",
            "status": "unavailable",
            "reason": "not_recorded",
            "comparable_field_count": 0,
            "conflict_count": 0,
            "fields": [],
        }
    return dict(value)


def _fundamental_summary(fundamental: dict[str, Any]) -> str:
    available_parts = []
    if fundamental.get("fundamentals_available") is True:
        available_parts.append("点时财务")
    if fundamental.get("valuation_available") is True:
        available_parts.append("估值")
    scope = "和".join(available_parts) or "基本面"
    reason = fundamental.get("abstention_reason")
    if reason == "insufficient_directional_evidence":
        return f"已绑定同日{scope}证据，但方向性指标不足两个，基本面角色明确弃权。"
    if reason == "conflicting_directional_evidence":
        return (
            f"已绑定同日{scope}证据，但支持与不利信号并存，基本面角色明确弃权。"
        )
    stance = fundamental.get("stance")
    label = "偏支持" if stance == "SUPPORTIVE" else "偏不利"
    return (
        f"同日{scope}证据{label}；结论引用已登记证据，且不改变确定性交易门禁。"
    )


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return round(result, 8) if math.isfinite(result) else None


def _bounded_date_text(value: Any) -> str | None:
    if not isinstance(value, str) or len(value) != 10:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    if not 1900 <= parsed.year <= 2200:
        return None
    return value


def _diagnosis_summary(
    trend: str, regime: str, volatility: str, score: int, breakout: str
) -> str:
    return (
        f"确定性指标显示趋势{_label(trend)}、结构{_label(regime)}、波动{_label(volatility)}，"
        f"研究评分 {score}/100，20 日突破状态为{_label(breakout)}。"
    )


def _assessment_summary(conclusion: str, risk_level: str, trend: str) -> str:
    messages = {
        "NO_ACTION": "当前证据不足以进入候选复核，保持无操作并等待新数据。",
        "WATCH": "当前信号存在分歧，加入观察但不形成订单意图。",
        "REVIEW_CANDIDATE": "趋势证据达到候选复核条件，仍需结合组合约束和独立研究。",
        "REDUCE_RISK": "近期波动或下行风险偏高，应优先复核风险暴露而非增加仓位。",
    }
    return f"{messages[conclusion]} 风险级别为{_label(risk_level)}，趋势状态为{_label(trend)}。"


def _assessment_refs(conclusion: str, all_refs: list[str]) -> list[str]:
    selected = {
        "NO_ACTION": ["trend.ema20", "trend.ema50", "momentum.return20"],
        "WATCH": ["trend.ema20", "trend.ema50", "momentum.rsi14", "risk.volatility20"],
        "REVIEW_CANDIDATE": [
            "trend.ema20",
            "trend.ema50",
            "momentum.return20",
            "risk.atr14_pct",
        ],
        "REDUCE_RISK": ["risk.volatility20", "risk.atr14_pct", "momentum.return20"],
    }[conclusion]
    return [value for value in selected if value in all_refs]


def _invalidation(features: dict[str, Any], conclusion: str) -> list[str]:
    if conclusion == "REVIEW_CANDIDATE":
        return [
            f"收盘价跌破 EMA20（{features['ema20']}）后，候选复核条件失效。",
            f"收盘价跌破近期支撑参考（{features['support20']}）时重新评估风险。",
        ]
    if conclusion == "WATCH":
        return ["趋势与 EMA 排列继续背离时，移出观察候选。"]
    return ["只有新完成交易日数据改变当前门禁结果时才重新评估。"]


def _scenarios(features: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "name": "上行情景",
            "trigger": f"收盘有效高于近期阻力参考 {features['resistance20']}",
            "implication": "重新运行研究并检查突破后的波动和流动性，不自动追价。",
        },
        {
            "name": "中性情景",
            "trigger": "价格继续位于 EMA20 与近期区间内",
            "implication": "保持观察，等待确定性指标形成一致方向。",
        },
        {
            "name": "下行情景",
            "trigger": f"收盘低于近期支撑参考 {features['support20']}",
            "implication": "优先复核风险暴露，任何行动仍由组合与交易门禁决定。",
        },
    ]


def _decision_path(
    *, regime: str, risk_level: str, trend: str, score: int, conclusion: str
) -> list[dict[str, Any]]:
    return [
        {
            "step": "data_gate",
            "label": "历史数据是否足够",
            "outcome": "PASS" if regime != "INSUFFICIENT" else "STOP",
            "evidence_ids": ["price.close", "trend.ema50"],
        },
        {
            "step": "risk_gate",
            "label": "近期波动风险是否可进入复核",
            "outcome": "STOP" if risk_level == "HIGH" else "PASS",
            "evidence_ids": ["risk.volatility20", "risk.atr14_pct"],
        },
        {
            "step": "trend_gate",
            "label": "趋势指标是否一致",
            "outcome": "PASS"
            if trend == "UP"
            else "REVIEW"
            if trend == "MIXED"
            else "STOP",
            "evidence_ids": ["trend.ema20", "trend.ema50", "momentum.return20"],
        },
        {
            "step": "assessment",
            "label": "形成研究权限内结论",
            "outcome": conclusion,
            "evidence_ids": [
                "momentum.rsi14",
                "structure.support20",
                "structure.resistance20",
            ],
        },
    ]


def _rounded(value: float | None) -> float | None:
    return (
        round(float(value), 8) if value is not None and math.isfinite(value) else None
    )


def _label(value: str) -> str:
    return {
        "UP": "上行",
        "DOWN": "下行",
        "MIXED": "方向混合",
        "TREND": "趋势",
        "RANGE": "区间",
        "INSUFFICIENT": "数据不足",
        "HIGH": "高",
        "NORMAL": "常态",
        "LOW": "低",
        "UNKNOWN": "未知",
        "NONE": "无",
    }.get(value, value)
