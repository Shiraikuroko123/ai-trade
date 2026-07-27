from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Mapping, Protocol

from ..assistant.governance import GovernanceSettings, ModelCallGovernance
from ..assistant.provider import (
    MAX_COMPLETION_TOKENS,
    RESEARCH_LOOP_PROMPT_TEMPLATE_VERSION,
    OpenAICompatibleProvider,
    ProviderSettings,
    valid_research_action_shape,
)
from ..config import AppConfig
from ..factor_lab import CustomFactorStore, FactorLabEngine
from ..hypothesis_lab import HypothesisExperimentRunner, HypothesisLabEngine
from ..json_utils import load_unique_json
from ..model_lab import ModelLabEngine
from .ledger import ResearchLoopLedger, ResearchLoopStore
from .schema import (
    RESEARCH_TOOLS,
    TOOL_COST_UNITS,
    json_fingerprint,
    proposal_fingerprint,
    validate_proposal,
    validate_static_plan,
)


DEFAULT_MAX_ROUNDS = 6
DEFAULT_MAX_TOOL_UNITS = 16
MAX_ROUNDS = 12
MAX_TOOL_UNITS = 100
MAX_PLAN_BYTES = 128 * 1024


class ResearchPlanner(Protocol):
    def descriptor(self) -> dict[str, Any]: ...

    def next_action(
        self, context: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]: ...


class StaticResearchPlanner:
    """Replay a bounded, immutable JSON plan through the same safety boundary."""

    def __init__(self, actions: list[dict[str, Any]]) -> None:
        self.actions = validate_static_plan(
            {"schema_version": 1, "actions": actions}
        )
        self.plan_fingerprint = json_fingerprint(
            {"schema_version": 1, "actions": self.actions}
        )

    @classmethod
    def from_file(cls, path: str | Path) -> StaticResearchPlanner:
        source = Path(path)
        if source.is_symlink() or not source.is_file():
            raise ValueError("Research plan file is unavailable")
        value = load_unique_json(source, max_bytes=MAX_PLAN_BYTES)
        actions = validate_static_plan(value)
        return cls(actions)

    def descriptor(self) -> dict[str, Any]:
        return {
            "kind": "static_plan",
            "action_count": len(self.actions),
            "plan_fingerprint": self.plan_fingerprint,
            "model_used": False,
        }

    def next_action(
        self, context: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        round_number = int(context["round"])
        if round_number <= len(self.actions):
            action = self.actions[round_number - 1]
        else:
            action = {
                "tool": "stop",
                "arguments": {},
                "rationale": "The static research plan is exhausted.",
            }
        return action, {
            "kind": "static_plan",
            "plan_fingerprint": self.plan_fingerprint,
            "action_index": round_number,
        }


class ModelResearchPlanner:
    """Use the configured model only to select the next allowlisted experiment."""

    def __init__(self, config: AppConfig, owner: str) -> None:
        settings, provider_error = ProviderSettings.from_environment()
        governance_settings, governance_error = GovernanceSettings.from_environment()
        error = provider_error or governance_error
        if error is not None:
            raise RuntimeError(error)
        if settings is None or governance_settings is None:
            raise RuntimeError("AI model mode is not configured")
        self.owner = owner
        self.provider = OpenAICompatibleProvider(settings)
        self.governance = ModelCallGovernance(
            config.project_root,
            governance_settings,
            model=settings.model,
            endpoint=settings.endpoint,
            template_version=RESEARCH_LOOP_PROMPT_TEMPLATE_VERSION,
            maximum_completion_tokens=MAX_COMPLETION_TOKENS,
        )
        self.model = settings.model

    def descriptor(self) -> dict[str, Any]:
        return {
            "kind": "governed_model",
            "model": self.model,
            "template_version": RESEARCH_LOOP_PROMPT_TEMPLATE_VERSION,
            "model_used": True,
            "governance": self.governance.status(),
        }

    def next_action(
        self, context: dict[str, Any]
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        result, usage, audit = self.governance.run_structured(
            user_id=self.owner,
            role="research_loop_planner",
            template_version=RESEARCH_LOOP_PROMPT_TEMPLATE_VERSION,
            request_payload=context,
            evidence=context["snapshot"],
            provider_call=lambda max_retries, audit_hook: self.provider.research_action(
                context=context,
                max_retries=max_retries,
                audit_hook=audit_hook,
            ),
            result_validator=valid_research_action_shape,
        )
        return result, {"kind": "governed_model", "usage": usage, "call": audit}


class ResearchLoopEngine:
    def __init__(
        self,
        config: AppConfig,
        *,
        store: ResearchLoopStore | None = None,
        executor: Callable[[str, dict[str, Any]], Mapping[str, Any]] | None = None,
    ) -> None:
        self.config = config
        self.root = config.project_root / "state" / "research_loop"
        self.store = store or ResearchLoopStore(self.root)
        self._injected_executor = executor

    def run(
        self,
        owner: str,
        market: Any,
        planner: ResearchPlanner,
        *,
        max_rounds: int = DEFAULT_MAX_ROUNDS,
        max_tool_units: int = DEFAULT_MAX_TOOL_UNITS,
    ) -> dict[str, Any]:
        max_rounds = _bounded_int(max_rounds, "max_rounds", 1, MAX_ROUNDS)
        max_tool_units = _bounded_int(
            max_tool_units, "max_tool_units", 1, MAX_TOOL_UNITS
        )
        ledger = ResearchLoopLedger(self.root, owner)
        factor_registry = FactorLabEngine(self.config).registry()
        model_registry = ModelLabEngine(self.config).registry()
        allowed_factors = {
            str(item["factor_id"]) for item in factor_registry["factors"]
        }
        allowed_factors.update(
            str(item["name"]) for item in CustomFactorStore(self.config).list(owner)
        )
        allowed_models = {
            str(item["model_id"]) for item in model_registry["models"]
        }
        produced_model_ids: set[str] = set()
        produced_hypothesis_ids: set[str] = set()
        history: list[dict[str, Any]] = []
        seen_proposals: set[str] = set()
        used_units = 0
        successes = 0
        failures = 0
        status = "max_rounds"
        rounds_completed = 0
        snapshot = _snapshot_summary(market)
        descriptor = _bounded_planner_descriptor(planner.descriptor())
        ledger.append(
            "loop_started",
            {
                "planner": descriptor,
                "snapshot": snapshot,
                "limits": {
                    "max_rounds": max_rounds,
                    "max_tool_units": max_tool_units,
                },
                "available_tools": list(RESEARCH_TOOLS),
            },
        )

        for round_number in range(1, max_rounds + 1):
            rounds_completed = round_number
            context = _planner_context(
                round_number=round_number,
                max_rounds=max_rounds,
                used_units=used_units,
                max_tool_units=max_tool_units,
                snapshot=snapshot,
                allowed_factors=allowed_factors,
                allowed_models=allowed_models,
                produced_model_ids=produced_model_ids,
                produced_hypothesis_ids=produced_hypothesis_ids,
                history=history,
            )
            planner_audit: dict[str, Any] | None = None
            try:
                raw_proposal, planner_audit = planner.next_action(context)
                proposal = validate_proposal(
                    raw_proposal,
                    allowed_factors=allowed_factors,
                    allowed_models=allowed_models,
                    model_evaluation_ids=produced_model_ids,
                    hypothesis_ids=produced_hypothesis_ids,
                )
            except Exception as exc:
                ledger.append(
                    "planner_failed",
                    {
                        "round": round_number,
                        "error_code": type(exc).__name__,
                        "error_message": _safe_error(exc),
                        "audit": _bounded_planner_audit(
                            planner_audit or getattr(exc, "audit", None)
                        ),
                    },
                )
                failures += 1
                status = "planner_failed"
                break

            ledger.append(
                "planner_succeeded",
                {
                    "round": round_number,
                    "proposal": proposal,
                    "proposal_fingerprint": proposal_fingerprint(proposal),
                    "audit": _bounded_planner_audit(planner_audit),
                },
            )
            tool = str(proposal["tool"])
            if tool == "stop":
                status = "stopped"
                history.append(
                    {
                        "round": round_number,
                        "tool": tool,
                        "status": "stopped",
                        "rationale": proposal["rationale"],
                    }
                )
                break

            fingerprint = proposal_fingerprint(
                {"tool": tool, "arguments": proposal["arguments"]}
            )
            if fingerprint in seen_proposals:
                ledger.append(
                    "tool_rejected",
                    {
                        "round": round_number,
                        "tool": tool,
                        "reason": "duplicate_proposal",
                        "tool_units_used": used_units,
                    },
                )
                failures += 1
                history.append(
                    {
                        "round": round_number,
                        "tool": tool,
                        "status": "rejected",
                        "error": "duplicate_proposal",
                    }
                )
                continue
            seen_proposals.add(fingerprint)
            cost = TOOL_COST_UNITS[tool]
            if used_units + cost > max_tool_units:
                ledger.append(
                    "tool_rejected",
                    {
                        "round": round_number,
                        "tool": tool,
                        "reason": "tool_budget_exhausted",
                        "required_units": cost,
                        "remaining_units": max_tool_units - used_units,
                        "tool_units_used": used_units,
                    },
                )
                failures += 1
                status = "budget_exhausted"
                break

            ledger.append(
                "tool_started",
                {
                    "round": round_number,
                    "tool": tool,
                    "arguments": proposal["arguments"],
                    "cost_units": cost,
                    "tool_units_used_before": used_units,
                },
            )
            used_units += cost
            try:
                result = self._execute(owner, market, tool, proposal["arguments"])
                summary = _summarize_result(tool, result)
                if tool == "factor-define":
                    allowed_factors.add(str(proposal["arguments"]["name"]))
                elif tool == "model-evaluate":
                    produced_model_ids.add(str(summary["evaluation_id"]))
                elif tool == "hypothesis-from-model":
                    produced_hypothesis_ids.add(str(summary["hypothesis_id"]))
                ledger.append(
                    "tool_succeeded",
                    {
                        "round": round_number,
                        "tool": tool,
                        "result": summary,
                        "tool_units_used": used_units,
                    },
                )
                successes += 1
                history.append(
                    {
                        "round": round_number,
                        "tool": tool,
                        "status": "succeeded",
                        "result": summary,
                    }
                )
            except Exception as exc:
                ledger.append(
                    "tool_failed",
                    {
                        "round": round_number,
                        "tool": tool,
                        "error_code": type(exc).__name__,
                        "error_message": _safe_error(exc),
                        "tool_units_used": used_units,
                    },
                )
                failures += 1
                history.append(
                    {
                        "round": round_number,
                        "tool": tool,
                        "status": "failed",
                        "error": type(exc).__name__,
                    }
                )
        else:
            status = "max_rounds"

        ledger.append(
            "loop_finished",
            {
                "status": status,
                "rounds_completed": rounds_completed,
                "tool_units_used": used_units,
                "successful_tools": successes,
                "failed_or_rejected_tools": failures,
            },
        )
        return ledger.snapshot()

    def _execute(
        self,
        owner: str,
        market: Any,
        tool: str,
        arguments: dict[str, Any],
    ) -> Mapping[str, Any]:
        if self._injected_executor is not None:
            return self._injected_executor(tool, arguments)
        if tool == "factor-define":
            return CustomFactorStore(self.config).define(
                owner,
                arguments["name"],
                arguments["expression"],
                arguments["direction"],
                label=arguments["label"],
            )
        if tool == "factor-evaluate":
            return FactorLabEngine(self.config).evaluate(
                owner,
                market,
                arguments["factor_id"],
                horizons=arguments["horizons"],
                step=arguments["step"],
            )
        if tool == "model-evaluate":
            return ModelLabEngine(self.config).evaluate(
                owner,
                market,
                arguments["model_id"],
                factor_ids=arguments["factor_ids"],
                horizon=arguments["horizon"],
                step=arguments["step"],
            )
        if tool == "hypothesis-from-model":
            return HypothesisLabEngine(self.config).derive_from_model(
                owner,
                market,
                arguments["evaluation_id"],
            )
        if tool == "hypothesis-run":
            return HypothesisExperimentRunner(self.config).execute(
                owner,
                arguments["hypothesis_id"],
                market,
            )
        raise ValueError("Research loop tool is not allowlisted")


def _planner_context(
    *,
    round_number: int,
    max_rounds: int,
    used_units: int,
    max_tool_units: int,
    snapshot: dict[str, Any],
    allowed_factors: set[str],
    allowed_models: set[str],
    produced_model_ids: set[str],
    produced_hypothesis_ids: set[str],
    history: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "authority": "research_only",
        "execution_authorized": False,
        "round": round_number,
        "limits": {
            "max_rounds": max_rounds,
            "remaining_rounds": max_rounds - round_number + 1,
            "max_tool_units": max_tool_units,
            "used_tool_units": used_units,
            "remaining_tool_units": max_tool_units - used_units,
        },
        "snapshot": snapshot,
        "available_factors": sorted(allowed_factors),
        "available_models": sorted(allowed_models),
        "loop_model_evaluation_ids": sorted(produced_model_ids),
        "loop_hypothesis_ids": sorted(produced_hypothesis_ids),
        "tool_contracts": {
            "factor-define": {
                "cost_units": 1,
                "arguments": ["name", "expression", "direction", "label"],
            },
            "factor-evaluate": {
                "cost_units": 2,
                "arguments": ["factor_id", "horizons", "step"],
            },
            "model-evaluate": {
                "cost_units": 4,
                "arguments": ["model_id", "factor_ids", "horizon", "step"],
            },
            "hypothesis-from-model": {
                "cost_units": 1,
                "arguments": ["evaluation_id"],
                "requires": "loop_model_evaluation_ids",
            },
            "hypothesis-run": {
                "cost_units": 6,
                "arguments": ["hypothesis_id"],
                "requires": "loop_hypothesis_ids",
            },
            "stop": {"cost_units": 0, "arguments": []},
        },
        "history": history[-8:],
    }


def _snapshot_summary(market: Any) -> dict[str, Any]:
    metadata = market.snapshot_metadata()
    if not isinstance(metadata, Mapping):
        raise RuntimeError("Market snapshot metadata must be an object")
    as_of = (
        metadata.get("latest_common_session")
        or metadata.get("latest_benchmark_session")
        or getattr(market, "completed_through", None)
    )
    symbols = getattr(market, "symbols", {})
    return {
        "as_of": str(as_of),
        "provider": str(metadata.get("provider") or "unknown"),
        "fingerprint": json_fingerprint(metadata),
        "symbol_count": len(symbols) if hasattr(symbols, "__len__") else 0,
    }


def _summarize_result(tool: str, result: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(result, Mapping):
        raise RuntimeError("Research tool returned an invalid result")
    reused = bool(result.get("reused", False))
    if tool == "factor-define":
        return {
            "factor_id": _required_text(result, "name"),
            "fingerprint": _required_text(result, "fingerprint"),
            "reused": reused,
        }
    if tool == "factor-evaluate":
        rows = result.get("results")
        if not isinstance(rows, list):
            raise RuntimeError("Factor evaluation results are unavailable")
        statistics = []
        for row in rows:
            if not isinstance(row, Mapping):
                raise RuntimeError("Factor evaluation result row is invalid")
            validation = row.get("statistical_validation")
            if not isinstance(validation, Mapping):
                validation = {}
            statistics.append(
                {
                    "horizon": row.get("horizon"),
                    "mean_ic": row.get("mean_ic"),
                    "ci_low": validation.get("ci_low"),
                    "adjusted_p_value": validation.get("adjusted_p_value"),
                    "reject_null": validation.get("reject_null"),
                    "positive_subperiods": validation.get("positive_subperiods"),
                }
            )
        factor = result.get("factor")
        if not isinstance(factor, Mapping):
            raise RuntimeError("Factor evaluation identity is unavailable")
        return {
            "evaluation_id": _required_text(result, "evaluation_id"),
            "factor_id": _required_text(factor, "factor_id"),
            "statistics": statistics,
            "reused": reused,
        }
    if tool == "model-evaluate":
        model = result.get("model")
        results = result.get("results")
        if not isinstance(model, Mapping) or not isinstance(results, Mapping):
            raise RuntimeError("Model evaluation summary is unavailable")
        model_metrics = results.get("model")
        validation = results.get("statistical_validation")
        if not isinstance(model_metrics, Mapping):
            model_metrics = {}
        if not isinstance(validation, Mapping):
            validation = {}
        model_ic = validation.get("model_ic")
        if not isinstance(model_ic, Mapping):
            model_ic = {}
        return {
            "evaluation_id": _required_text(result, "evaluation_id"),
            "model_id": _required_text(model, "model_id"),
            "mean_ic": model_metrics.get("mean_ic"),
            "model_minus_best_factor_ic": results.get(
                "model_minus_best_factor_ic"
            ),
            "adjusted_p_value": model_ic.get("adjusted_p_value"),
            "reject_null": model_ic.get("reject_null"),
            "positive_subperiods": model_ic.get("positive_subperiods"),
            "reused": reused,
        }
    if tool == "hypothesis-from-model":
        source = result.get("source")
        return {
            "hypothesis_id": _required_text(result, "hypothesis_id"),
            "source_kind": (
                source.get("kind") if isinstance(source, Mapping) else None
            ),
            "reused": reused,
        }
    verdict = result.get("verdict")
    return {
        "run_id": _required_text(result, "run_id"),
        "hypothesis_id": _required_text(result, "hypothesis_id"),
        "verdict": dict(verdict) if isinstance(verdict, Mapping) else None,
        "reused": reused,
    }


def _required_text(value: Mapping[str, Any], key: str) -> str:
    selected = value.get(key)
    if not isinstance(selected, str) or not selected:
        raise RuntimeError(f"Research tool result {key} is unavailable")
    return selected


def _bounded_planner_descriptor(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("Research planner descriptor is invalid")
    result = dict(value)
    encoded = str(result)
    if len(encoded) > 10_000:
        raise ValueError("Research planner descriptor is too large")
    return result


def _bounded_planner_audit(value: Any) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValueError("Research planner audit is invalid")
    result = dict(value)
    if len(str(result)) > 20_000:
        raise ValueError("Research planner audit is too large")
    return result


def _safe_error(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    return (message or type(exc).__name__)[:500]


def _bounded_int(value: Any, label: str, minimum: int, maximum: int) -> int:
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return value


__all__ = [
    "DEFAULT_MAX_ROUNDS",
    "DEFAULT_MAX_TOOL_UNITS",
    "MAX_ROUNDS",
    "MAX_TOOL_UNITS",
    "ModelResearchPlanner",
    "ResearchLoopEngine",
    "ResearchPlanner",
    "StaticResearchPlanner",
]
