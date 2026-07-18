from __future__ import annotations

import math
import statistics
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


def build_local_analysis(bars: Sequence[Any]) -> dict[str, Any]:
    if not bars:
        raise ValueError("No completed bars are available for assistant analysis")
    _validate_bars(bars)
    closes = [float(bar.close) for bar in bars]
    ema20_values = _ema(closes, 20)
    ema50_values = _ema(closes, 50)
    returns = [closes[index] / closes[index - 1] - 1.0 for index in range(1, len(closes))]
    latest = bars[-1]

    return_1d = _period_return(closes, 1)
    return_5d = _period_return(closes, 5)
    return_20d = _period_return(closes, 20)
    return_60d = _period_return(closes, 60)
    volatility20 = (
        statistics.stdev(returns[-20:]) * math.sqrt(252)
        if len(returns) >= 2
        else None
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
    regime = "INSUFFICIENT" if len(bars) < 51 else "TREND" if trend != "MIXED" else "RANGE"
    volatility = _volatility_label(volatility20, atr14_pct)
    score = _score(trend, return_20d, rsi14, volatility, breakout, len(bars))
    risk_level = _risk_level(volatility, return_1d, atr14_pct)
    conclusion = _conclusion(regime, trend, score, risk_level)
    gate = "STOP" if conclusion in {"NO_ACTION", "REDUCE_RISK"} else "WATCH" if conclusion == "WATCH" else "PROCEED"

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
            "status": "UNAVAILABLE",
            "stance": "NOT_AVAILABLE",
            "summary": "当前快照未包含财务报表、估值或公司行动数据，基本面暂不可评估。",
            "evidence_ids": refs("coverage.fundamentals"),
            "limitation": "接入并校验授权基本面数据源后才可启用该视角。",
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
    rows = [
        ("price.close", "最新收盘", features["close"], "用于确认分析截止价格", "neutral"),
        ("trend.ema20", "EMA20", features["ema20"], "短周期趋势参考", "positive" if trend == "UP" else "negative" if trend == "DOWN" else "neutral"),
        ("trend.ema50", "EMA50", features["ema50"], f"趋势状态为 {_label(trend)}，市场结构为 {_label(regime)}", "positive" if trend == "UP" else "negative" if trend == "DOWN" else "neutral"),
        ("momentum.return20", "20 日收益", features["return_20d"], "仅描述历史动量，不代表未来收益", "positive" if (features["return_20d"] or 0) > 0 else "negative" if (features["return_20d"] or 0) < 0 else "neutral"),
        ("momentum.rsi14", "RSI14", features["rsi14"], "识别短期过热或过弱状态", "warning" if features["rsi14"] is not None and (features["rsi14"] >= 78 or features["rsi14"] <= 25) else "neutral"),
        ("risk.volatility20", "20 日年化波动", features["annualized_volatility_20d"], f"波动分级为 {_label(volatility)}", "warning" if volatility == "HIGH" else "neutral"),
        ("risk.atr14_pct", "ATR14 / 收盘", features["atr14_pct"], "衡量近期单日价格波动尺度", "warning" if (features["atr14_pct"] or 0) >= 0.055 else "neutral"),
        ("structure.support20", "20 日支撑参考", features["support20"], "近期窗口低点，不是保证有效的止损位", "neutral"),
        ("structure.resistance20", "20 日阻力参考", features["resistance20"], "近期窗口高点，不是保证有效的目标价", "neutral"),
        ("structure.last_candle", "最新 K 线结构", features["last_candle"], f"20 日突破状态为 {_label(features['breakout20'])}", "neutral"),
        ("coverage.fundamentals", "基本面数据覆盖", "UNAVAILABLE", "当前快照没有财务报表、估值和公司行动数据", "warning"),
        ("coverage.sentiment", "情绪数据覆盖", "UNAVAILABLE", "当前快照没有新闻、公告、资金流和情绪数据", "warning"),
        ("strategy.gate", "策略研究门禁", gate, "只表示研究复核范围，不是交易授权", "warning" if gate != "PROCEED" else "neutral"),
    ]
    return [
        {
            "evidence_id": evidence_id,
            "label": label,
            "value": value,
            "interpretation": interpretation,
            "tone": tone,
        }
        for evidence_id, label, value, interpretation, tone in rows
    ]


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
        "REVIEW_CANDIDATE": ["trend.ema20", "trend.ema50", "momentum.return20", "risk.atr14_pct"],
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
            "outcome": "PASS" if trend == "UP" else "REVIEW" if trend == "MIXED" else "STOP",
            "evidence_ids": ["trend.ema20", "trend.ema50", "momentum.return20"],
        },
        {
            "step": "assessment",
            "label": "形成研究权限内结论",
            "outcome": conclusion,
            "evidence_ids": ["momentum.rsi14", "structure.support20", "structure.resistance20"],
        },
    ]


def _rounded(value: float | None) -> float | None:
    return round(float(value), 8) if value is not None and math.isfinite(value) else None


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
