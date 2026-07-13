from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from .features import ALLOWED_CONCLUSIONS, build_local_analysis
from .provider import (
    AssistantProviderError,
    OpenAICompatibleProvider,
    ProviderSettings,
)
from .store import AssistantRecordStore


SCHEMA_VERSION = 1
LOCAL_MODEL = "local-deterministic-v1"
SUPPORTED_MODES = {"local", "model"}
_CONCLUSION_AUTHORITY = {
    "REDUCE_RISK": 0,
    "NO_ACTION": 1,
    "WATCH": 2,
    "REVIEW_CANDIDATE": 3,
}
_REVIEW_BUDGET_CAP = {
    "NO_ACTION": 0,
    "WATCH": 25,
    "REVIEW_CANDIDATE": 50,
    "REDUCE_RISK": 0,
}


class AssistantEngine:
    """Deterministic research assistant with an optional model wording layer."""

    def __init__(self, config: Any):
        self.config = config
        self._settings, self._configuration_error = ProviderSettings.from_environment()
        self._provider = (
            OpenAICompatibleProvider(self._settings) if self._settings is not None else None
        )
        self._store = AssistantRecordStore(config.project_root)

    def status(self) -> dict[str, Any]:
        configured = self._provider is not None
        return {
            "schema_version": SCHEMA_VERSION,
            "local_available": True,
            "model_configured": configured,
            "ai_configured": configured,
            "supported_modes": ["local", "model"] if configured else ["local"],
            "provider": "openai-compatible" if configured else None,
            "model": self._settings.model if self._settings is not None else None,
            "configuration_error": self._configuration_error,
            "authority": "research_only",
        }

    def analyze(
        self,
        market: Any,
        symbol: str,
        lookback: int = 180,
        mode: str = "local",
        user_id: str = "local-owner",
    ) -> dict[str, Any]:
        selected_symbol = _validate_symbol(market, symbol)
        selected_lookback = _validate_lookback(lookback)
        selected_mode = _validate_mode(mode)
        if selected_mode == "model" and self._provider is None:
            raise RuntimeError("AI model mode is not configured")

        market_date = market.latest_date()
        bars = list(market.history(selected_symbol, market_date, selected_lookback))
        if len(bars) < 60:
            raise ValueError(
                f"At least 60 completed market bars are required for {selected_symbol}"
            )
        instrument = market.instrument(selected_symbol)
        local = build_local_analysis(bars)
        snapshot = _snapshot(self.config, market, selected_symbol, bars)
        previous = _previous_for_symbol(
            self._store.history(user_id, limit=20), selected_symbol
        )
        created_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
        model = self._settings.model if selected_mode == "model" and self._settings else LOCAL_MODEL
        warnings: list[str] = []
        model_enhanced = False
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        if selected_mode == "model":
            try:
                enhancement, usage = self._provider.enhance(
                    symbol=selected_symbol,
                    data_date=bars[-1].date.isoformat(),
                    diagnosis=local["diagnosis"],
                    assessment=local["assessment"],
                )
                _apply_enhancement(local, enhancement)
                model_enhanced = True
            except AssistantProviderError as exc:
                warnings.append(
                    "Model enhancement was unavailable; deterministic local analysis was used "
                    f"({exc.code})."
                )

        result: dict[str, Any] = {
            "schema_version": SCHEMA_VERSION,
            "analysis_id": uuid4().hex,
            "created_at": created_at,
            "authority": "research_only",
            "order_intent": None,
            "symbol": selected_symbol,
            "name": str(instrument.name),
            "data_date": bars[-1].date.isoformat(),
            "lookback": selected_lookback,
            "mode": selected_mode,
            "model": model,
            "snapshot": snapshot,
            "features": local["features"],
            "diagnosis": local["diagnosis"],
            "assessment": local["assessment"],
            "decision_path": local["decision_path"],
            "chart": local["chart"],
            "comparison": _comparison(previous, local, bars[-1].date.isoformat()),
        }
        errors = _validate_result(result)
        result["validation"] = {
            "valid": not errors,
            "errors": errors,
            "warnings": warnings,
            "model_enhanced": model_enhanced,
            "usage": usage,
        }
        if errors:
            raise RuntimeError("Assistant analysis failed internal validation")
        self._store.save(user_id, result)
        return result

    def history(self, user_id: str, limit: int = 20) -> list[dict[str, Any]]:
        return self._store.history(user_id, limit)


def _validate_symbol(market: Any, symbol: str) -> str:
    if not isinstance(symbol, str):
        raise TypeError("symbol must be a string")
    selected = symbol.strip()
    if not selected or len(selected) > 64 or any(ord(character) < 32 for character in selected):
        raise ValueError("symbol must be a non-empty identifier of at most 64 characters")
    symbols = getattr(market, "symbols", {})
    if selected not in symbols:
        raise ValueError(f"Unknown market symbol: {selected}")
    return selected


def _validate_lookback(value: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 60 <= value <= 500:
        raise ValueError("lookback must be an integer between 60 and 500")
    return value


def _validate_mode(value: str) -> str:
    if not isinstance(value, str) or value not in SUPPORTED_MODES:
        raise ValueError("mode must be local or model")
    return value


def _snapshot(config: Any, market: Any, symbol: str, bars: list[Any]) -> dict[str, Any]:
    raw_data = getattr(config, "raw", {}).get("data", {})
    canonical_bars = [
        [
            bar.date.isoformat(),
            _canonical_number(bar.open),
            _canonical_number(bar.high),
            _canonical_number(bar.low),
            _canonical_number(bar.close),
            _canonical_number(getattr(bar, "volume", 0.0)),
            _canonical_number(getattr(bar, "amount", 0.0)),
        ]
        for bar in bars
    ]
    payload = {
        "symbol": symbol,
        "provider": raw_data.get("provider"),
        "adjustment": raw_data.get("adjustment", "none"),
        "bars": canonical_bars,
        "source_sha256": _safe_sha256(
            getattr(market, "file_hashes", {}).get(symbol)
        ),
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "ascii"
        )
    ).hexdigest()
    return {
        "snapshot_id": digest[:24],
        "provider": raw_data.get("provider"),
        "adjustment": raw_data.get("adjustment", "none"),
        "completed_session_cutoff": _iso_value(
            getattr(market, "completed_through", None)
        ),
        "latest_common_session": _iso_value(
            getattr(market, "latest_common_session", None)
        ),
        "bar_count": len(bars),
        "start_date": bars[0].date.isoformat(),
        "end_date": bars[-1].date.isoformat(),
        "source_sha256": payload["source_sha256"] or None,
        "window_sha256": digest,
    }


def _apply_enhancement(local: dict[str, Any], enhancement: dict[str, Any]) -> None:
    model_diagnosis = enhancement["diagnosis"]
    model_assessment = enhancement["assessment"]
    local_assessment = local["assessment"]
    local_conclusion = str(local_assessment["conclusion"])
    proposed = str(model_assessment["conclusion"])
    if _CONCLUSION_AUTHORITY[proposed] > _CONCLUSION_AUTHORITY[local_conclusion]:
        proposed = local_conclusion

    model_was_relaxed = proposed != str(model_assessment["conclusion"])
    local["diagnosis"]["summary"] = model_diagnosis["summary"]
    summary = (
        str(local_assessment["summary"])
        if model_was_relaxed
        else model_assessment["summary"]
    )
    invalidation = (
        list(local_assessment["invalidation"])
        if model_was_relaxed
        else model_assessment["invalidation"]
    )
    scenarios = (
        list(local_assessment["scenarios"])
        if model_was_relaxed
        else model_assessment["scenarios"]
    )
    evidence_ids = (
        list(local_assessment["evidence_ids"])
        if model_was_relaxed
        else model_assessment["evidence_ids"]
    )
    local["assessment"].update(
        {
            "conclusion": proposed,
            "summary": summary,
            "risk_level": _stricter_risk(
                str(local_assessment["risk_level"]), str(model_assessment["risk_level"])
            ),
            "risk_budget_pct": min(
                int(local_assessment["risk_budget_pct"]),
                int(model_assessment["risk_budget_pct"]),
                _REVIEW_BUDGET_CAP[proposed],
            ),
            "evidence_ids": evidence_ids,
            "invalidation": invalidation,
            "scenarios": scenarios,
        }
    )
    if proposed in {"NO_ACTION", "REDUCE_RISK"}:
        local["assessment"]["risk_budget_pct"] = 0
    local["decision_path"][-1]["outcome"] = proposed


def _stricter_risk(local: str, model: str) -> str:
    ranks = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}
    return local if ranks[local] >= ranks[model] else model


def _previous_for_symbol(
    records: list[dict[str, Any]], symbol: str
) -> dict[str, Any] | None:
    return next((item for item in records if item.get("symbol") == symbol), None)


def _comparison(
    previous: dict[str, Any] | None,
    local: dict[str, Any],
    data_date: str,
) -> dict[str, Any]:
    if previous is None:
        return {
            "available": False,
            "previous_analysis_id": None,
            "previous_data_date": None,
            "data_advanced": False,
            "conclusion_changed": False,
            "feature_changes": {},
        }
    old_features = previous.get("features") if isinstance(previous.get("features"), dict) else {}
    new_features = local["features"]
    changes = {}
    for name in ("close", "return_20d", "annualized_volatility_20d", "rsi14"):
        before = old_features.get(name)
        after = new_features.get(name)
        changes[name] = {
            "previous": before,
            "current": after,
            "change": _difference(after, before),
        }
    old_assessment = (
        previous.get("assessment") if isinstance(previous.get("assessment"), dict) else {}
    )
    current_conclusion = local["assessment"]["conclusion"]
    previous_conclusion = old_assessment.get("conclusion")
    return {
        "available": True,
        "previous_analysis_id": previous.get("analysis_id"),
        "previous_data_date": previous.get("data_date"),
        "data_advanced": str(previous.get("data_date", "")) < data_date,
        "conclusion_changed": previous_conclusion != current_conclusion,
        "previous_conclusion": previous_conclusion,
        "current_conclusion": current_conclusion,
        "feature_changes": changes,
    }


def _validate_result(result: dict[str, Any]) -> list[str]:
    errors = []
    if result.get("schema_version") != SCHEMA_VERSION:
        errors.append("schema_version is invalid")
    if result.get("authority") != "research_only" or result.get("order_intent") is not None:
        errors.append("assistant authority boundary is invalid")
    diagnosis = result.get("diagnosis")
    assessment = result.get("assessment")
    path = result.get("decision_path")
    if not isinstance(diagnosis, dict) or diagnosis.get("stage") != "market_diagnosis":
        errors.append("diagnosis stage is invalid")
        return errors
    evidence = diagnosis.get("evidence")
    if not isinstance(evidence, list) or not evidence:
        errors.append("diagnosis evidence is missing")
        return errors
    evidence_ids = [item.get("evidence_id") for item in evidence if isinstance(item, dict)]
    allowed = set(evidence_ids)
    if len(allowed) != len(evidence_ids) or None in allowed:
        errors.append("evidence identifiers are missing or duplicated")
    if not isinstance(assessment, dict) or assessment.get("stage") != "risk_assessment":
        errors.append("assessment stage is invalid")
        return errors
    if assessment.get("conclusion") not in ALLOWED_CONCLUSIONS:
        errors.append("assessment conclusion is invalid")
    if assessment.get("risk_level") not in {"LOW", "MEDIUM", "HIGH"}:
        errors.append("assessment risk level is invalid")
    risk_budget = assessment.get("risk_budget_pct")
    if isinstance(risk_budget, bool) or not isinstance(risk_budget, int) or not 0 <= risk_budget <= 100:
        errors.append("assessment risk budget is invalid")
    _check_references(assessment.get("evidence_ids"), allowed, "assessment", errors)
    if not isinstance(path, list) or not path:
        errors.append("decision path is missing")
    else:
        for index, step in enumerate(path):
            if not isinstance(step, dict):
                errors.append(f"decision path step {index} is invalid")
                continue
            _check_references(step.get("evidence_ids"), allowed, f"decision path {index}", errors)
    chart = result.get("chart")
    if not isinstance(chart, dict) or not isinstance(chart.get("points"), list):
        errors.append("chart points are invalid")
    return errors


def _check_references(
    references: Any, allowed: set[Any], name: str, errors: list[str]
) -> None:
    if not isinstance(references, list) or not references:
        errors.append(f"{name} evidence references are missing")
        return
    if any(not isinstance(value, str) or value not in allowed for value in references):
        errors.append(f"{name} contains an unknown evidence reference")


def _canonical_number(value: Any) -> str:
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Market snapshot contains a non-finite number")
    return format(parsed, ".12g")


def _difference(current: Any, previous: Any) -> float | None:
    if not isinstance(current, (int, float)) or not isinstance(previous, (int, float)):
        return None
    if isinstance(current, bool) or isinstance(previous, bool):
        return None
    difference = float(current) - float(previous)
    return round(difference, 8) if math.isfinite(difference) else None


def _iso_value(value: Any) -> str | None:
    return value.isoformat() if hasattr(value, "isoformat") else None


def _safe_sha256(value: Any) -> str:
    candidate = str(value or "").lower()
    if len(candidate) == 64 and all(character in "0123456789abcdef" for character in candidate):
        return candidate
    return ""
