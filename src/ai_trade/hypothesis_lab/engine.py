from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import math
from typing import Any, Mapping
from uuid import uuid4

from ..backtest import BacktestEngine
from ..config import AppConfig
from ..data.market import MarketData
from ..models import RiskSettings, StrategySettings
from ..strategy_lab import StrategyLabEngine
from .schema import (
    ENGINE_VERSION,
    OBJECTIVES,
    SAFETY,
    SCHEMA_VERSION,
    TEMPLATE_VERSION,
    finalize_record,
    json_fingerprint,
)
from .store import HypothesisLabStore


_OBJECTIVE_TITLES = {
    "balanced": "Risk-adjusted parameter-neighborhood hypothesis",
    "drawdown": "Drawdown-control parameter-neighborhood hypothesis",
    "turnover": "Turnover-control parameter-neighborhood hypothesis",
}

_MECHANISMS = {
    "balanced": (
        "A modest volatility reduction and a wider rebalance band may suppress "
        "marginal trades without discarding the strategy's momentum ranking. The "
        "candidate should improve full-sample Sharpe while remaining stable out of "
        "sample and under higher transaction costs."
    ),
    "drawdown": (
        "Lower target volatility, lower single-position concentration, and a larger "
        "cash floor may reduce loss amplification during adverse regimes. If this is "
        "causal rather than sample-specific, drawdown improvement should persist in "
        "holdout and rolling out-of-sample windows."
    ),
    "turnover": (
        "A longer rebalance interval and a wider minimum rebalance band may filter "
        "small allocation changes. The mechanism predicts lower turnover and cost "
        "sensitivity without a material collapse in holdout Sharpe."
    ),
}


class HypothesisLabEngine:
    """Generate falsifiable local hypotheses without creating strategy candidates."""

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

    def generate_local(
        self,
        owner: str,
        market: MarketData,
        *,
        objective: str = "auto",
        title: str | None = None,
    ) -> dict[str, Any]:
        metadata_before = _market_metadata(market)
        snapshot_fingerprint = json_fingerprint(metadata_before)
        baseline = self._configured_baseline(owner, market)
        resolved, selection_reason = _resolve_objective(
            objective,
            baseline["metrics"],
            baseline["settings"]["risk"],
        )
        blueprint = self.strategy_lab.local_proposal_blueprint(owner, resolved)
        if blueprint["parent_fingerprint"] != baseline["settings_fingerprint"]:
            raise RuntimeError("Strategy baseline changed during hypothesis generation")
        if blueprint["baseline"] != baseline["settings"]:
            raise RuntimeError("Strategy baseline settings changed during generation")
        metadata_after = _market_metadata(market)
        if json_fingerprint(metadata_after) != snapshot_fingerprint:
            raise RuntimeError("Market snapshot changed during hypothesis generation")

        evidence = _evidence(metadata_before, market, snapshot_fingerprint)
        metrics = baseline["metrics"]
        predictions = _predictions(resolved, metrics)
        record = finalize_record(
            {
                "schema_version": SCHEMA_VERSION,
                "engine_version": ENGINE_VERSION,
                "template_version": TEMPLATE_VERSION,
                "hypothesis_id": f"hyp_{uuid4().hex}",
                "owner": self.store.owner_id(owner),
                "created_at": _utc_now(),
                "source": {
                    "kind": "local_deterministic",
                    "objective": resolved,
                    "selection_reason": selection_reason,
                    "model_used": False,
                },
                "title": (title.strip() if title is not None else _OBJECTIVE_TITLES[resolved]),
                "observation": _observation(metrics, evidence["snapshot"]),
                "mechanism": _MECHANISMS[resolved],
                "scope": _scope(self.config, metrics),
                "assumptions": _assumptions(resolved),
                "predictions": predictions,
                "falsification_criteria": _falsification(predictions),
                "competing_explanations": _competing_explanations(),
                "confounds": _confounds(),
                "evidence": evidence,
                "baseline": {
                    "strategy_lab_candidate_id": blueprint["parent_candidate_id"],
                    "settings_fingerprint": blueprint["parent_fingerprint"],
                    "config_context_fingerprint": blueprint[
                        "config_context_fingerprint"
                    ],
                    "settings": blueprint["baseline"],
                    "metrics": metrics,
                },
                "experiment_plan": {
                    "design": "champion_challenger",
                    "proposed_changes": blueprint["changes"],
                    "candidate_settings_fingerprint": blueprint[
                        "candidate_fingerprint"
                    ],
                    "minimum_sessions": max(
                        40, self.strategy_lab.policy.minimum_sessions
                    ),
                    "holdout_fraction": self.strategy_lab.policy.holdout_fraction,
                    "rolling_folds": 3,
                    "cost_multipliers": [1.0, self.strategy_lab.policy.cost_multiplier],
                    "sensitivity_fraction": 0.1,
                    "tests": [
                        "same_snapshot_baseline",
                        "holdout",
                        "rolling_out_of_sample",
                        "cost_stress",
                        "parameter_sensitivity",
                        "independent_replication",
                    ],
                    "multiple_testing": {
                        "family_id": (
                            "family_" + evidence["snapshot"]["fingerprint"][:32]
                        ),
                        "maximum_hypotheses": 3,
                        "alpha": 0.05,
                        "correction": "holm",
                    },
                },
                "quality_assessment": {
                    "testability": "HIGH",
                    "falsifiability": "HIGH",
                    "parsimony": "HIGH",
                    "explanatory_power": "MEDIUM",
                    "scope": "MEDIUM",
                    "consistency": "HIGH",
                    "novelty": "LOW",
                    "distinguishable": True,
                    "limitations": [
                        "This deterministic template explores only the existing allowlisted parameter neighborhood.",
                        "Backtest evidence cannot establish a causal market law without independent replication.",
                    ],
                },
                "safety": dict(SAFETY),
            }
        )
        return self.store.publish(owner, record)

    def list(self, owner: str, *, limit: int = 50) -> dict[str, Any]:
        return self.store.list(owner, limit=limit)

    def get(self, owner: str, hypothesis_id: str) -> dict[str, Any]:
        return self.store.get(owner, hypothesis_id)

    def materialize_candidate(
        self,
        owner: str,
        hypothesis_id: str,
        *,
        confirmed_by: str,
    ) -> dict[str, Any]:
        if not isinstance(confirmed_by, str) or not confirmed_by.strip():
            raise ValueError("confirmed_by must identify the human operator")
        record = self.get(owner, hypothesis_id)
        objective = record["source"]["objective"]
        blueprint = self.strategy_lab.local_proposal_blueprint(owner, objective)
        expected = {
            "parent_candidate_id": record["baseline"]["strategy_lab_candidate_id"],
            "parent_fingerprint": record["baseline"]["settings_fingerprint"],
            "config_context_fingerprint": record["baseline"][
                "config_context_fingerprint"
            ],
            "baseline": record["baseline"]["settings"],
            "changes": record["experiment_plan"]["proposed_changes"],
            "candidate_fingerprint": record["experiment_plan"][
                "candidate_settings_fingerprint"
            ],
        }
        for field, expected_value in expected.items():
            if blueprint.get(field) != expected_value:
                raise RuntimeError(
                    "Hypothesis baseline or configuration changed; generate a new hypothesis"
                )
        mechanism = f"{record['title']}: {record['mechanism']}"
        candidate = self.strategy_lab.create_hypothesis_candidate(
            owner,
            record["experiment_plan"]["proposed_changes"],
            record["title"],
            mechanism[:1000],
            (
                f"Explicit human materialization of {hypothesis_id}. The candidate "
                "remains DRAFT and requires independent validation and approval."
            ),
            hypothesis_id=hypothesis_id,
            hypothesis_fingerprint=record["record_fingerprint"],
            design_fingerprint=record["design_fingerprint"],
            actor=confirmed_by.strip(),
        )
        return {
            "schema_version": 1,
            "hypothesis_id": hypothesis_id,
            "hypothesis_fingerprint": record["record_fingerprint"],
            "candidate": candidate,
            "safety": {
                "explicit_human_materialization": True,
                "candidate_status": candidate.get("status"),
                "validation_completed": candidate.get("validation") is not None,
                "approval_granted": candidate.get("approval") is not None,
                "strategy_activated": candidate.get("active") is True,
                "live_trading_authorized": False,
            },
        }

    def _configured_baseline(
        self, owner: str, market: MarketData
    ) -> dict[str, Any]:
        blueprint = self.strategy_lab.local_proposal_blueprint(owner, "balanced")
        settings = blueprint["baseline"]
        try:
            strategy = StrategySettings(**settings["strategy"])
            risk = RiskSettings(**settings["risk"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Strategy baseline settings are invalid") from exc
        run_config = replace(self.config, strategy=strategy, risk=risk)
        result = BacktestEngine(run_config, market, strategy).run()
        metrics = {
            "start": str(result.metadata["start"]),
            "end": str(result.metadata["end"]),
            **{
                field: _finite_metric(result.metrics, field)
                for field in (
                    "total_return",
                    "cagr",
                    "sharpe",
                    "max_drawdown",
                    "turnover",
                    "transaction_costs",
                )
            },
        }
        return {
            "settings": settings,
            "settings_fingerprint": blueprint["parent_fingerprint"],
            "metrics": metrics,
        }


def _resolve_objective(
    requested: str,
    metrics: Mapping[str, Any],
    risk: Mapping[str, Any],
) -> tuple[str, str]:
    if requested != "auto":
        if requested not in OBJECTIVES:
            raise ValueError("objective must be auto, balanced, drawdown, or turnover")
        if requested == "turnover" and float(metrics["turnover"]) <= 0:
            raise ValueError("turnover objective requires positive baseline turnover")
        return requested, f"Operator pre-registered the {requested} objective."
    drawdown_limit = float(risk["max_portfolio_drawdown"])
    drawdown_pressure = (
        abs(float(metrics["max_drawdown"])) / drawdown_limit
        if drawdown_limit > 0
        else 0.0
    )
    if drawdown_pressure >= 0.75:
        return (
            "drawdown",
            "Deterministic selection: baseline drawdown consumed at least 75% of the configured drawdown limit.",
        )
    if float(metrics["turnover"]) >= 4.0:
        return (
            "turnover",
            "Deterministic selection: baseline notional turnover was at least 4.0 times average equity.",
        )
    return (
        "balanced",
        "Deterministic selection: neither drawdown pressure nor turnover crossed its predeclared threshold.",
    )


def _predictions(
    objective: str, metrics: Mapping[str, Any]
) -> list[dict[str, Any]]:
    objective_prediction = {
        "balanced": {
            "metric": "full.sharpe_delta",
            "operator": ">=",
            "threshold": 0.0,
            "baseline": float(metrics["sharpe"]),
            "window": "full",
            "rationale": "The risk-adjusted return should improve rather than merely shift risk.",
        },
        "drawdown": {
            "metric": "full.max_drawdown_delta",
            "operator": ">=",
            "threshold": 0.01,
            "baseline": float(metrics["max_drawdown"]),
            "window": "full",
            "rationale": "Maximum drawdown should improve by at least one percentage point.",
        },
        "turnover": {
            "metric": "full.turnover_ratio",
            "operator": "<=",
            "threshold": 0.9,
            "baseline": float(metrics["turnover"]),
            "window": "full",
            "rationale": "Notional turnover should fall by at least ten percent.",
        },
    }[objective]
    rows = [
        objective_prediction,
        {
            "metric": "holdout.sharpe_delta",
            "operator": ">=",
            "threshold": -0.5,
            "baseline": 0.0,
            "window": "holdout",
            "rationale": "The candidate must avoid a material holdout Sharpe collapse.",
        },
        {
            "metric": "cost_stress.total_return_delta",
            "operator": ">=",
            "threshold": -0.05,
            "baseline": 0.0,
            "window": "cost_stress",
            "rationale": "Doubled modeled costs must not erase more than five percentage points of relative return.",
        },
        {
            "metric": "stability.minimum_sharpe_delta",
            "operator": ">=",
            "threshold": -0.5,
            "baseline": 0.0,
            "window": "sensitivity",
            "rationale": "Nearby parameter values must not show a sharp performance cliff.",
        },
    ]
    return [
        {"prediction_id": f"pred_{index:02d}", **row}
        for index, row in enumerate(rows, start=1)
    ]


def _falsification(predictions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    opposite = {">=": "<", "<=": ">"}
    return [
        {
            "criterion_id": f"fals_{index:02d}",
            "prediction_id": prediction["prediction_id"],
            "metric": prediction["metric"],
            "operator": opposite[prediction["operator"]],
            "threshold": prediction["threshold"],
            "window": prediction["window"],
        }
        for index, prediction in enumerate(predictions, start=1)
    ]


def _competing_explanations() -> list[dict[str, str]]:
    return [
        {
            "explanation_id": "alt_01",
            "statement": "Any apparent improvement is specific to the sampled market regime rather than the parameter mechanism.",
            "distinguishing_test": "Require consistent direction across rolling out-of-sample folds and a later immutable snapshot.",
        },
        {
            "explanation_id": "alt_02",
            "statement": "The result is an artifact of transaction-cost assumptions.",
            "distinguishing_test": "Repeat the comparison at predeclared 1x and 2x cost multipliers.",
        },
        {
            "explanation_id": "alt_03",
            "statement": "The selected point is an unstable local optimum rather than a robust neighborhood.",
            "distinguishing_test": "Perturb every changed numeric parameter by plus and minus ten percent.",
        },
    ]


def _confounds() -> list[dict[str, str]]:
    return [
        {
            "confound_id": "conf_01",
            "risk": "Future data or revised constituents could leak into the training window.",
            "control": "Use the same point-in-time immutable snapshot and security-master fingerprint for baseline and candidate.",
        },
        {
            "confound_id": "conf_02",
            "risk": "Repeatedly trying parameter variants can create a multiple-testing false positive.",
            "control": "Limit one snapshot family to three objectives and apply Holm correction at alpha 0.05.",
        },
        {
            "confound_id": "conf_03",
            "risk": "Turnover and return differences may come from inconsistent execution costs.",
            "control": "Run baseline and candidate with identical point-in-time cost schedules plus a doubled-cost stress.",
        },
        {
            "confound_id": "conf_04",
            "risk": "A favorable single date split may dominate the conclusion.",
            "control": "Use a fixed holdout plus at least three rolling out-of-sample folds and independent replication.",
        },
    ]


def _assumptions(objective: str) -> list[str]:
    assumptions = [
        "The local cache contains only completed sessions and its fingerprint remains unchanged during generation.",
        "Baseline and candidate will use identical point-in-time universes, prices, and cost schedules.",
        "The predeclared thresholds will not be revised after validation results are observed.",
    ]
    if objective == "turnover":
        assumptions.append("Baseline turnover is positive, so the turnover ratio is defined.")
    return assumptions


def _scope(config: AppConfig, metrics: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "universe": config.universe_name,
        "instrument_types": sorted(
            {str(item.instrument_type) for item in config.instruments}
        ),
        "start": metrics["start"],
        "end": metrics["end"],
        "regime": "Unspecified; conclusions require rolling-window and later-snapshot replication.",
        "exclusions": [
            "Incomplete sessions after the verified market cutoff",
            "Tick, Level-2, live-order, and causal market-impact claims",
        ],
    }


def _observation(
    metrics: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> str:
    return (
        f"On immutable snapshot {snapshot['snapshot_id']} through {snapshot['as_of']}, "
        f"the active baseline produced Sharpe {float(metrics['sharpe']):.4f}, "
        f"maximum drawdown {float(metrics['max_drawdown']):.4f}, turnover "
        f"{float(metrics['turnover']):.4f}, and total return "
        f"{float(metrics['total_return']):.4f}."
    )


def _evidence(
    metadata: Mapping[str, Any],
    market: MarketData,
    snapshot_fingerprint: str,
) -> dict[str, Any]:
    as_of = str(
        metadata.get("latest_common_session")
        or metadata.get("latest_benchmark_session")
        or market.latest_date().isoformat()
    )
    snapshot_id = "market_" + snapshot_fingerprint[:32]
    references: list[dict[str, str]] = [
        {
            "evidence_id": snapshot_id,
            "kind": "market_snapshot",
            "as_of": as_of,
            "fingerprint": snapshot_fingerprint,
        }
    ]
    symbols = metadata.get("symbols")
    if isinstance(symbols, Mapping):
        for symbol, item in sorted(symbols.items()):
            if not isinstance(item, Mapping):
                continue
            digest = item.get("sha256")
            last = str(item.get("last") or as_of)
            if isinstance(digest, str) and len(digest) == 64:
                references.append(
                    {
                        "evidence_id": f"daily_bars:{symbol}",
                        "kind": "daily_bars",
                        "as_of": last,
                        "fingerprint": digest,
                    }
                )
    manifest = metadata.get("manifest")
    if manifest is not None:
        manifest_fingerprint = getattr(market, "manifest_sha256", None)
        if not isinstance(manifest_fingerprint, str) or len(manifest_fingerprint) != 64:
            manifest_fingerprint = json_fingerprint(manifest)
        references.append(
            {
                "evidence_id": "cache_manifest",
                "kind": "cache_manifest",
                "as_of": as_of,
                "fingerprint": manifest_fingerprint,
            }
        )
    universe = metadata.get("universe")
    security_fingerprint = (
        universe.get("security_master_sha256")
        if isinstance(universe, Mapping)
        else None
    )
    if isinstance(security_fingerprint, str) and len(security_fingerprint) == 64:
        references.append(
            {
                "evidence_id": "security_master",
                "kind": "security_master",
                "as_of": as_of,
                "fingerprint": security_fingerprint,
            }
        )
    if len(references) > 250:
        raise ValueError("Market snapshot has too many evidence references")
    return {
        "snapshot": {
            "snapshot_id": snapshot_id,
            "kind": "market_cache",
            "as_of": as_of,
            "provider": str(metadata.get("provider") or "local-cache"),
            "fingerprint": snapshot_fingerprint,
        },
        "references": references,
    }


def _market_metadata(market: MarketData) -> dict[str, Any]:
    value = market.snapshot_metadata()
    if not isinstance(value, Mapping):
        raise RuntimeError("Market snapshot metadata must be an object")
    return dict(value)


def _finite_metric(metrics: Mapping[str, Any], field: str) -> float:
    value = metrics.get(field)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RuntimeError(f"Baseline metric {field} is unavailable")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise RuntimeError(f"Baseline metric {field} is not finite")
    return parsed


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["HypothesisLabEngine"]
