from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import math
from typing import Any, Mapping, Sequence
from uuid import uuid4

from ..backtest import BacktestEngine
from ..config import AppConfig
from ..data.market import MarketData
from ..models import CostSettings, RiskSettings, StrategySettings
from ..strategy_lab import StrategyLabEngine
from ..strategy_lab.schema import apply_changes, clamp_parameter, parameter_spec
from .engine import _market_metadata
from .run_schema import (
    RUN_SAFETY,
    RUN_SCHEMA_VERSION,
    RUNNER_VERSION,
    finalize_run_record,
)
from .schema import json_fingerprint
from .store import HypothesisLabStore


MINIMUM_FOLD_SESSIONS = 5

_MULTIPLE_TESTING_NOTE = (
    "Deterministic pre-registered threshold judgments; no p-values are computed "
    "in this run. The three-hypothesis family budget is enforced at registration "
    "and the Holm plan constrains any future statistical evaluation layer."
)

_FOLD_DIRECTION_RULES = {
    "balanced": (
        "sharpe",
        ">=",
        "Per fold: candidate.sharpe - baseline.sharpe must be >= 0",
    ),
    "drawdown": (
        "max_drawdown",
        ">=",
        "Per fold: candidate.max_drawdown - baseline.max_drawdown must be >= 0",
    ),
    "turnover": (
        "turnover",
        "<=",
        "Per fold: candidate.turnover - baseline.turnover must be <= 0",
    ),
}

_FORMULAS = {
    "full.sharpe_delta": "candidate.full.sharpe - baseline.full.sharpe",
    "full.max_drawdown_delta": (
        "candidate.full.max_drawdown - baseline.full.max_drawdown"
    ),
    "full.turnover_ratio": "candidate.full.turnover / baseline.full.turnover",
    "holdout.sharpe_delta": "candidate.holdout.sharpe - baseline.holdout.sharpe",
    "cost_stress.total_return_delta": (
        "candidate.cost_stress[max].total_return - "
        "baseline.cost_stress[max].total_return"
    ),
    "stability.minimum_sharpe_delta": (
        "sensitivity.minimum_sharpe - candidate.full.sharpe"
    ),
}


class HypothesisExperimentRunner:
    """Execute one pre-registered hypothesis plan without granting authority.

    The runner reruns the immutable registered design on the verified local
    cache, judges every pre-registered prediction against its falsification
    criterion, and appends one immutable run record. A SUPPORTED or REPLICATED
    verdict is research evidence only: it cannot create, validate, approve, or
    activate a Strategy Lab candidate, and it cannot touch accounting, broker
    configuration, or live-trading gates.
    """

    def __init__(
        self,
        config: AppConfig,
        store: HypothesisLabStore | None = None,
        strategy_lab: StrategyLabEngine | None = None,
    ) -> None:
        self.config = config
        self.store = store or HypothesisLabStore(
            config.project_root / "state" / "hypothesis_lab"
        )
        self.strategy_lab = strategy_lab or StrategyLabEngine(config)

    def execute(
        self, owner: str, hypothesis_id: str, market: MarketData
    ) -> dict[str, Any]:
        record = self.store.get(owner, hypothesis_id)
        plan = record["experiment_plan"]
        registered = record["evidence"]["snapshot"]

        context_fingerprint = self.strategy_lab.config_context_fingerprint()
        if context_fingerprint != record["baseline"]["config_context_fingerprint"]:
            raise RuntimeError(
                "Configuration context changed since hypothesis registration; "
                "the pre-registered plan cannot be executed. Generate a new "
                "hypothesis on the current configuration."
            )

        metadata_before = _market_metadata(market)
        executed_fingerprint = json_fingerprint(metadata_before)
        executed_as_of = _snapshot_as_of(metadata_before, market)
        mode = _resolve_mode(
            executed_fingerprint, executed_as_of, registered
        )

        baseline_snapshot = record["baseline"]["settings"]
        if (
            json_fingerprint(baseline_snapshot)
            != record["baseline"]["settings_fingerprint"]
        ):
            raise RuntimeError("Hypothesis baseline settings fingerprint mismatch")
        try:
            candidate_snapshot, effective = apply_changes(
                self.config, baseline_snapshot, plan["proposed_changes"]
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "Registered hypothesis changes are no longer applicable"
            ) from exc
        if effective != plan["proposed_changes"]:
            raise RuntimeError(
                "Registered hypothesis changes did not reproduce exactly"
            )
        if (
            json_fingerprint(candidate_snapshot)
            != plan["candidate_settings_fingerprint"]
        ):
            raise RuntimeError("Hypothesis candidate settings fingerprint mismatch")

        baseline_strategy, baseline_risk = _settings(baseline_snapshot)
        candidate_strategy, candidate_risk = _settings(candidate_snapshot)

        calendar = self._period_calendar(market, int(plan["minimum_sessions"]))
        start, end = calendar[0], calendar[-1]
        holdout_sessions = max(
            20, int(math.ceil(len(calendar) * float(plan["holdout_fraction"])))
        )
        if holdout_sessions >= len(calendar):
            raise RuntimeError(
                "Hypothesis run history is too short for the registered holdout"
            )
        holdout_start = calendar[-holdout_sessions]

        costs = self.config.costs
        full_baseline = self._run(
            market, baseline_strategy, baseline_risk, costs, start, end
        )
        full_candidate = self._run(
            market, candidate_strategy, candidate_risk, costs, start, end
        )
        holdout_baseline = self._run(
            market, baseline_strategy, baseline_risk, costs, holdout_start, end
        )
        holdout_candidate = self._run(
            market, candidate_strategy, candidate_risk, costs, holdout_start, end
        )

        stress_rows = []
        for multiplier in plan["cost_multipliers"]:
            multiplier = float(multiplier)
            if multiplier <= 1.0:
                continue
            stressed = costs.scaled(multiplier)
            stress_rows.append(
                {
                    "multiplier": multiplier,
                    "baseline": self._run(
                        market, baseline_strategy, baseline_risk, stressed, start, end
                    ),
                    "candidate": self._run(
                        market,
                        candidate_strategy,
                        candidate_risk,
                        stressed,
                        start,
                        end,
                    ),
                }
            )
        if not stress_rows:
            raise RuntimeError(
                "Registered plan has no cost multiplier above 1.0 to stress"
            )

        rolling = self._rolling(
            market,
            calendar,
            int(plan["rolling_folds"]),
            str(record["source"]["objective"]),
            baseline_strategy,
            baseline_risk,
            candidate_strategy,
            candidate_risk,
            costs,
        )
        sensitivity = self._sensitivity(
            market,
            candidate_snapshot,
            plan["proposed_changes"],
            float(plan["sensitivity_fraction"]),
            costs,
            start,
            end,
        )

        metadata_after = _market_metadata(market)
        if json_fingerprint(metadata_after) != executed_fingerprint:
            raise RuntimeError("Market snapshot changed during hypothesis execution")

        results = {
            "full": {"baseline": full_baseline, "candidate": full_candidate},
            "holdout": {
                "baseline": holdout_baseline,
                "candidate": holdout_candidate,
            },
            "cost_stress": stress_rows,
            "rolling": rolling,
            "sensitivity": sensitivity,
        }
        observations = _observations(record["predictions"], results)
        judgments = _judgments(
            record["predictions"], record["falsification_criteria"], observations
        )
        supported = sum(item["outcome"] == "SUPPORTED" for item in judgments)
        positive = supported == len(judgments)
        status = (
            ("SUPPORTED" if positive else "FALSIFIED")
            if mode == "same_snapshot"
            else ("REPLICATED" if positive else "NOT_REPLICATED")
        )

        execution_fingerprint = json_fingerprint(
            {
                "hypothesis_record_fingerprint": record["record_fingerprint"],
                "mode": mode,
                "executed_snapshot_fingerprint": executed_fingerprint,
                "config_context_fingerprint": context_fingerprint,
                "runner_version": RUNNER_VERSION,
            }
        )
        run = finalize_run_record(
            {
                "schema_version": RUN_SCHEMA_VERSION,
                "runner_version": RUNNER_VERSION,
                "run_id": f"run_{uuid4().hex}",
                "owner": self.store.owner_id(owner),
                "created_at": _utc_now(),
                "hypothesis_id": record["hypothesis_id"],
                "hypothesis_record_fingerprint": record["record_fingerprint"],
                "hypothesis_design_fingerprint": record["design_fingerprint"],
                "mode": mode,
                "execution_fingerprint": execution_fingerprint,
                "registered_snapshot": {
                    "snapshot_id": registered["snapshot_id"],
                    "as_of": registered["as_of"],
                    "fingerprint": registered["fingerprint"],
                },
                "executed_snapshot": {
                    "snapshot_id": "market_" + executed_fingerprint[:32],
                    "as_of": executed_as_of,
                    "provider": str(metadata_before.get("provider") or "local-cache"),
                    "fingerprint": executed_fingerprint,
                    "sessions_after_registration": sum(
                        day.isoformat() > str(registered["as_of"])
                        for day in calendar
                    ),
                },
                "config_context_fingerprint": context_fingerprint,
                "baseline_settings_fingerprint": record["baseline"][
                    "settings_fingerprint"
                ],
                "candidate_settings_fingerprint": plan[
                    "candidate_settings_fingerprint"
                ],
                "period": {
                    "start": start.isoformat(),
                    "end": end.isoformat(),
                    "sessions": len(calendar),
                    "holdout_start": holdout_start.isoformat(),
                    "holdout_sessions": holdout_sessions,
                },
                "results": results,
                "observations": observations,
                "judgments": judgments,
                "verdict": {
                    "status": status,
                    "predictions_total": len(judgments),
                    "predictions_supported": supported,
                    "falsified_criteria": [
                        item["criterion_id"]
                        for item in judgments
                        if item["outcome"] == "FALSIFIED"
                    ],
                },
                "multiple_testing": {
                    "family_id": plan["multiple_testing"]["family_id"],
                    "family_position": self.store.family_position(
                        owner, registered["fingerprint"], record["hypothesis_id"]
                    ),
                    "maximum_hypotheses": plan["multiple_testing"][
                        "maximum_hypotheses"
                    ],
                    "alpha": plan["multiple_testing"]["alpha"],
                    "correction": plan["multiple_testing"]["correction"],
                    "note": _MULTIPLE_TESTING_NOTE,
                },
                "safety": dict(RUN_SAFETY),
            }
        )
        return self.store.publish_run(owner, run)

    def list_runs(
        self,
        owner: str,
        *,
        limit: int = 50,
        hypothesis_id: str | None = None,
    ) -> dict[str, Any]:
        return self.store.list_runs(owner, limit=limit, hypothesis_id=hypothesis_id)

    def get_run(self, owner: str, run_id: str) -> dict[str, Any]:
        return self.store.get_run(owner, run_id)

    def _period_calendar(
        self, market: MarketData, minimum_sessions: int
    ) -> list[date]:
        start = date.fromisoformat(str(self.config.raw["backtest"]["start"]))
        end = date.fromisoformat(str(self.config.raw["backtest"]["end"]))
        calendar = [day for day in market.calendar if start <= day <= end]
        if len(calendar) < minimum_sessions:
            raise RuntimeError(
                "Hypothesis run requires at least "
                f"{minimum_sessions} sessions in the configured backtest window"
            )
        return calendar

    def _run(
        self,
        market: MarketData,
        strategy: StrategySettings,
        risk: RiskSettings,
        costs: CostSettings,
        start: date,
        end: date,
    ) -> dict[str, float]:
        run_config = replace(self.config, strategy=strategy, risk=risk, costs=costs)
        result = BacktestEngine(run_config, market, strategy).run(start=start, end=end)
        return _metrics_block(result.metrics)

    def _rolling(
        self,
        market: MarketData,
        calendar: list[date],
        fold_count: int,
        objective: str,
        baseline_strategy: StrategySettings,
        baseline_risk: RiskSettings,
        candidate_strategy: StrategySettings,
        candidate_risk: RiskSettings,
        costs: CostSettings,
    ) -> dict[str, Any]:
        metric, operator, rule = _FOLD_DIRECTION_RULES[objective]
        base_size, remainder = divmod(len(calendar), fold_count)
        if base_size < MINIMUM_FOLD_SESSIONS:
            raise RuntimeError(
                "Hypothesis run history is too short for the registered "
                f"rolling folds (needs {MINIMUM_FOLD_SESSIONS} sessions per fold)"
            )
        folds: list[dict[str, Any]] = []
        consistent = 0
        cursor = 0
        for index in range(1, fold_count + 1):
            size = base_size + (1 if index <= remainder else 0)
            segment = calendar[cursor : cursor + size]
            cursor += size
            fold_start, fold_end = segment[0], segment[-1]
            baseline = self._run(
                market, baseline_strategy, baseline_risk, costs, fold_start, fold_end
            )
            candidate = self._run(
                market,
                candidate_strategy,
                candidate_risk,
                costs,
                fold_start,
                fold_end,
            )
            delta = candidate[metric] - baseline[metric]
            direction = delta >= 0 if operator == ">=" else delta <= 0
            consistent += bool(direction)
            folds.append(
                {
                    "fold": index,
                    "start": fold_start.isoformat(),
                    "end": fold_end.isoformat(),
                    "sessions": len(segment),
                    "baseline_value": baseline[metric],
                    "candidate_value": candidate[metric],
                    "delta": delta,
                    "direction_consistent": direction,
                }
            )
        return {
            "fold_count": fold_count,
            "consistent_folds": consistent,
            "direction_rule": rule,
            "folds": folds,
        }

    def _sensitivity(
        self,
        market: MarketData,
        candidate_snapshot: Mapping[str, Mapping[str, Any]],
        changes: Mapping[str, Mapping[str, Any]],
        fraction: float,
        costs: CostSettings,
        start: date,
        end: date,
    ) -> dict[str, Any]:
        numeric = [
            (scope, name)
            for scope in ("strategy", "risk")
            for name, value in changes[scope].items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        if not numeric:
            numeric = [("strategy", "lookback_days")]
        parameters: list[str] = []
        variants: list[dict[str, Any]] = []
        for scope, name in sorted(numeric):
            spec = parameter_spec(scope, name)
            original = candidate_snapshot[scope][name]
            parameter = f"{scope}.{name}"
            used: set[float] = set()
            fallback_step = float(spec.step or 0)
            if fallback_step <= 0:
                fallback_step = max(abs(float(original)) * fraction, 0.01)
            variant_count = 0
            for direction, factor in ((-1, 1.0 - fraction), (1, 1.0 + fraction)):
                perturbed = clamp_parameter(spec, float(original) * factor)
                if perturbed == original:
                    perturbed = clamp_parameter(
                        spec, float(original) + direction * fallback_step
                    )
                if perturbed == original or float(perturbed) in used:
                    continue
                used.add(float(perturbed))
                try:
                    variant_snapshot, _ = apply_changes(
                        self.config,
                        candidate_snapshot,
                        {scope: {name: perturbed}},
                    )
                except ValueError:
                    continue
                variant_strategy, variant_risk = _settings(variant_snapshot)
                metrics = self._run(
                    market, variant_strategy, variant_risk, costs, start, end
                )
                variants.append(
                    {
                        "parameter": parameter,
                        "value": float(perturbed),
                        "sharpe": metrics["sharpe"],
                    }
                )
                variant_count += 1
            if variant_count == 0:
                raise RuntimeError(
                    f"Could not construct a valid sensitivity variant for {parameter}"
                )
            parameters.append(parameter)
        if not variants:
            raise RuntimeError(
                "Could not construct deterministic sensitivity variants"
            )
        return {
            "fraction": fraction,
            "parameters": parameters,
            "variant_count": len(variants),
            "minimum_sharpe": min(item["sharpe"] for item in variants),
            "variants": variants,
        }


def _resolve_mode(
    executed_fingerprint: str,
    executed_as_of: str,
    registered: Mapping[str, Any],
) -> str:
    if executed_fingerprint == registered["fingerprint"]:
        return "same_snapshot"
    if executed_as_of > str(registered["as_of"]):
        return "independent_replication"
    raise RuntimeError(
        "The verified local cache does not match the registered snapshot and is "
        "not newer; refusing to execute the pre-registered plan on it"
    )


def _observations(
    predictions: list[Mapping[str, Any]],
    results: Mapping[str, Any],
) -> list[dict[str, Any]]:
    values: dict[str, float] = {}
    for prediction in predictions:
        metric = str(prediction["metric"])
        if metric not in values:
            values[metric] = _observe(metric, results)
    return [
        {"metric": metric, "value": value, "formula": _FORMULAS[metric]}
        for metric, value in values.items()
    ]


def _observe(metric: str, results: Mapping[str, Any]) -> float:
    full_baseline = results["full"]["baseline"]
    full_candidate = results["full"]["candidate"]
    if metric == "full.sharpe_delta":
        return full_candidate["sharpe"] - full_baseline["sharpe"]
    if metric == "full.max_drawdown_delta":
        return full_candidate["max_drawdown"] - full_baseline["max_drawdown"]
    if metric == "full.turnover_ratio":
        if full_baseline["turnover"] <= 0:
            raise RuntimeError(
                "Baseline turnover is not positive; the registered turnover "
                "ratio is undefined on this window"
            )
        return full_candidate["turnover"] / full_baseline["turnover"]
    if metric == "holdout.sharpe_delta":
        return (
            results["holdout"]["candidate"]["sharpe"]
            - results["holdout"]["baseline"]["sharpe"]
        )
    if metric == "cost_stress.total_return_delta":
        stressed = max(results["cost_stress"], key=lambda row: row["multiplier"])
        return (
            stressed["candidate"]["total_return"]
            - stressed["baseline"]["total_return"]
        )
    if metric == "stability.minimum_sharpe_delta":
        return (
            results["sensitivity"]["minimum_sharpe"] - full_candidate["sharpe"]
        )
    raise RuntimeError(f"Hypothesis run metric is not executable: {metric}")


def _judgments(
    predictions: Sequence[Mapping[str, Any]],
    criteria: Sequence[Mapping[str, Any]],
    observations: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    observed = {str(item["metric"]): float(item["value"]) for item in observations}
    criterion_by_prediction = {
        str(item["prediction_id"]): item for item in criteria
    }
    judgments: list[dict[str, Any]] = []
    for prediction in predictions:
        prediction_id = str(prediction["prediction_id"])
        criterion = criterion_by_prediction.get(prediction_id)
        if criterion is None:
            raise RuntimeError(
                "Registered hypothesis is missing a falsification criterion"
            )
        metric = str(prediction["metric"])
        operator = str(prediction["operator"])
        threshold = float(prediction["threshold"])
        value = observed[metric]
        holds = value >= threshold if operator == ">=" else value <= threshold
        judgments.append(
            {
                "prediction_id": prediction_id,
                "criterion_id": str(criterion["criterion_id"]),
                "metric": metric,
                "operator": operator,
                "threshold": threshold,
                "observed": value,
                "outcome": "SUPPORTED" if holds else "FALSIFIED",
            }
        )
    return judgments


def _metrics_block(metrics: Mapping[str, Any]) -> dict[str, float]:
    output: dict[str, float] = {}
    for field in (
        "total_return",
        "cagr",
        "sharpe",
        "max_drawdown",
        "turnover",
        "transaction_costs",
    ):
        value = metrics.get(field)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise RuntimeError(f"Hypothesis run metric {field} is unavailable")
        parsed = float(value)
        if not math.isfinite(parsed):
            raise RuntimeError(f"Hypothesis run metric {field} is not finite")
        output[field] = parsed
    return output


def _settings(
    snapshot: Mapping[str, Mapping[str, Any]],
) -> tuple[StrategySettings, RiskSettings]:
    try:
        return (
            StrategySettings(**dict(snapshot["strategy"])),
            RiskSettings(**dict(snapshot["risk"])),
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("Hypothesis settings snapshot is invalid") from exc


def _snapshot_as_of(metadata: Mapping[str, Any], market: MarketData) -> str:
    return str(
        metadata.get("latest_common_session")
        or metadata.get("latest_benchmark_session")
        or market.latest_date().isoformat()
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["HypothesisExperimentRunner", "MINIMUM_FOLD_SESSIONS"]
