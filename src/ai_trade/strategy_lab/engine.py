from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
import hashlib
import json
import math
from typing import Any, Mapping
from uuid import uuid4

from .. import __version__
from ..backtest import BacktestEngine
from ..config import AppConfig
from ..data.market import MarketData
from ..models import CostSettings, RiskSettings, StrategySettings
from .schema import (
    SCHEMA_VERSION,
    apply_changes,
    clamp_parameter,
    parameter_schema,
    parameter_spec,
    settings_snapshot,
)
from .lifecycle import (
    INSUFFICIENT_DATA,
    MONITORING_OK,
    REVIEW_REQUIRED,
    LifecyclePolicy,
    evaluate_strategy_decay,
)
from .store import (
    StrategyLabCapacityError,
    StrategyLabConflictError,
    StrategyLabStore,
)


_ENGINE_VERSION = 2
MAX_CANDIDATES_PER_OWNER = 100
MAX_TRANSITION_EVENTS_PER_OWNER = 1000
MAX_MONITORS_PER_OWNER = 500
SUMMARY_CANDIDATE_LIMIT = 50
SUMMARY_HISTORY_LIMIT = 200
_ACTIVE_LIFECYCLE_STATES = frozenset({"ACTIVE", "SUSPENDED"})


@dataclass(frozen=True)
class ValidationPolicy:
    minimum_sessions: int = 40
    holdout_fraction: float = 0.2
    full_sharpe_tolerance: float = 0.35
    holdout_sharpe_tolerance: float = 0.50
    cost_return_tolerance: float = 0.05
    drawdown_tolerance: float = 0.03
    stability_sharpe_tolerance: float = 0.50
    cost_multiplier: float = 2.0


class StrategyLabEngine:
    def __init__(
        self,
        config: AppConfig,
        store: StrategyLabStore | None = None,
        policy: ValidationPolicy | None = None,
        lifecycle_policy: LifecyclePolicy | None = None,
    ):
        self.config = config
        self.store = store or StrategyLabStore(
            config.project_root / "state" / "strategy_lab"
        )
        self.policy = policy or ValidationPolicy()
        self.lifecycle_policy = lifecycle_policy or LifecyclePolicy()

    def parameter_schema(self) -> dict[str, Any]:
        schema = parameter_schema()
        for item in schema["parameters"]:
            if item["scope"] == "strategy" and item["name"] == "top_n":
                item["max"] = len(self.config.instruments)
        return schema

    def summary(self, owner: str) -> dict[str, Any]:
        baseline = self._active_baseline(owner)
        monitors = self._active_monitors(owner, baseline)
        stored_candidates = sorted(
            self.store.list_candidates(owner),
            key=lambda candidate: (
                str(candidate.get("created_at", "")),
                str(candidate.get("candidate_id", "")),
            ),
            reverse=True,
        )
        visible_candidates = stored_candidates[:SUMMARY_CANDIDATE_LIMIT]
        visible_history, history_total = self.store.recent_events(
            owner, SUMMARY_HISTORY_LIMIT
        )
        candidates = [
            self._compose_candidate(owner, candidate)
            for candidate in visible_candidates
        ]
        return {
            "schema_version": SCHEMA_VERSION,
            "schema": self.parameter_schema(),
            "baseline": {
                "strategy": baseline["snapshot"]["strategy"],
                "risk": baseline["snapshot"]["risk"],
                "fingerprint": baseline["fingerprint"],
                "candidate_id": baseline["candidate_id"],
            },
            "active": self._public_active(baseline),
            "monitoring": {
                "available": baseline["candidate_id"] is not None,
                "latest": monitors[-1] if monitors else None,
                "count": len(monitors),
                "maximum": MAX_MONITORS_PER_OWNER,
                "policy": self.lifecycle_policy.public_dict(),
                "automatic_state_change": False,
            },
            "candidates": candidates,
            "candidate_summary": {
                "total": len(stored_candidates),
                "count": len(candidates),
                "limit": SUMMARY_CANDIDATE_LIMIT,
                "maximum": MAX_CANDIDATES_PER_OWNER,
                "truncated": len(stored_candidates) > len(candidates),
            },
            "history": visible_history,
            "history_total": history_total,
            "history_count": len(visible_history),
            "history_limit": SUMMARY_HISTORY_LIMIT,
            "history_truncated": history_total > len(visible_history),
            "safety": {
                "research_only": True,
                "ai_can_approve": False,
                "ai_can_activate": False,
                "live_trading_enabled": False,
                "broker_configuration_unchanged": True,
                "paper_export_requires_approval": True,
                "automatic_strategy_suspension": False,
                "automatic_strategy_retirement": False,
            },
        }

    def get_candidate(self, owner: str, candidate_id: str) -> dict[str, Any]:
        return self._compose_candidate(
            owner, self.store.read_candidate(owner, candidate_id)
        )

    def create_manual_candidate(
        self,
        owner: str,
        changes: Mapping[str, Any],
        title: str,
        hypothesis: str,
        reason: str,
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        return self._create_candidate(
            owner=owner,
            source="manual",
            changes=changes,
            title=title,
            hypothesis=hypothesis,
            reason=reason,
            actor=actor,
        )

    def propose_local_ai_candidate(
        self,
        owner: str,
        title: str,
        hypothesis: str,
        objective: str = "balanced",
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        baseline = self._active_baseline(owner)["snapshot"]
        changes = self._local_proposal(baseline, objective)
        reason = {
            "balanced": (
                "Local deterministic proposal: reduce volatility exposure and add a "
                "small rebalance band. Validation and human approval remain mandatory."
            ),
            "drawdown": (
                "Local deterministic proposal: lower target volatility and position "
                "concentration while retaining more cash."
            ),
            "turnover": (
                "Local deterministic proposal: trade less frequently and ignore smaller "
                "allocation changes."
            ),
        }.get(objective)
        if reason is None:
            raise ValueError("objective must be balanced, drawdown, or turnover")
        return self._create_candidate(
            owner=owner,
            source="ai_local",
            changes=changes,
            title=title,
            hypothesis=hypothesis,
            reason=reason,
            actor=actor,
            proposal={"provider": "local_deterministic", "objective": objective},
        )

    def validate_candidate(
        self,
        owner: str,
        candidate_id: str,
        market: MarketData,
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        event_actor = "system" if actor is None else actor
        candidate = self._verified_candidate(owner, candidate_id)
        existing = self.store.read_validation(owner, candidate_id)
        if existing is not None:
            self._verified_validation(candidate, existing)
            self._assert_parent_still_active(owner, candidate)
            self._ensure_lifecycle_event(
                owner,
                candidate,
                action="validate",
                actor=event_actor,
                source="system",
                created_at=str(existing["validated_at"]),
            )
            return self._compose_candidate(owner, candidate)
        if self.store.read_approval(owner, candidate_id) is not None:
            raise RuntimeError("Approved candidates cannot be revalidated")

        snapshot_before = _market_snapshot(market)
        baseline_strategy, baseline_risk = _settings_from_snapshot(
            candidate["baseline"]
        )
        candidate_strategy, candidate_risk = _settings_from_snapshot(
            candidate["candidate"]
        )
        start, end, holdout_start = self._validation_period(market)

        baseline_result = self._run(
            market, baseline_strategy, baseline_risk, self.config.costs, start, end
        )
        candidate_result = self._run(
            market, candidate_strategy, candidate_risk, self.config.costs, start, end
        )
        baseline_holdout = self._run(
            market,
            baseline_strategy,
            baseline_risk,
            self.config.costs,
            holdout_start,
            end,
        )
        candidate_holdout = self._run(
            market,
            candidate_strategy,
            candidate_risk,
            self.config.costs,
            holdout_start,
            end,
        )
        stressed_costs = self.config.costs.scaled(self.policy.cost_multiplier)
        baseline_cost = self._run(
            market, baseline_strategy, baseline_risk, stressed_costs, start, end
        )
        candidate_cost = self._run(
            market, candidate_strategy, candidate_risk, stressed_costs, start, end
        )
        stability = self._stability_runs(
            market, candidate, candidate_strategy, candidate_risk, start, end
        )
        snapshot_after = _market_snapshot(market)
        if snapshot_before["id"] != snapshot_after["id"]:
            raise RuntimeError("Market snapshot changed during strategy validation")

        baseline_metrics = _metrics(baseline_result.metrics)
        candidate_metrics = _metrics(candidate_result.metrics)
        baseline_holdout_metrics = _metrics(baseline_holdout.metrics)
        candidate_holdout_metrics = _metrics(candidate_holdout.metrics)
        baseline_cost_metrics = _metrics(baseline_cost.metrics)
        candidate_cost_metrics = _metrics(candidate_cost.metrics)
        gates = self._gates(
            baseline_metrics,
            candidate_metrics,
            baseline_holdout_metrics,
            candidate_holdout_metrics,
            baseline_cost_metrics,
            candidate_cost_metrics,
            stability,
            candidate_risk,
        )
        validation = {
            "schema_version": SCHEMA_VERSION,
            "candidate_id": candidate_id,
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "config_context_fingerprint": candidate["config_context_fingerprint"],
            "parent_candidate_id": candidate["parent_candidate_id"],
            "parent_fingerprint": candidate["parent_fingerprint"],
            "validated_at": _utc_now(),
            "market_snapshot": snapshot_before,
            "period": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "holdout_start": holdout_start.isoformat(),
            },
            "baseline_metrics": baseline_metrics,
            "candidate_metrics": candidate_metrics,
            "holdout": {
                "baseline_metrics": baseline_holdout_metrics,
                "candidate_metrics": candidate_holdout_metrics,
            },
            "cost_stress": {
                "multiplier": self.policy.cost_multiplier,
                "baseline_metrics": baseline_cost_metrics,
                "candidate_metrics": candidate_cost_metrics,
            },
            "stability": stability,
            "gates": gates,
            "live_ready": False,
        }
        candidate = self._verified_candidate(owner, candidate_id)
        self._verified_validation(candidate, validation)
        self.store.write_validation(
            owner,
            candidate_id,
            validation,
            expected_active_fingerprint=candidate["parent_fingerprint"],
            empty_active_fingerprint=self._configured_baseline()["fingerprint"],
        )
        self._ensure_lifecycle_event(
            owner,
            candidate,
            action="validate",
            actor=event_actor,
            source="system",
            created_at=validation["validated_at"],
        )
        return self._compose_candidate(owner, candidate)

    def approve_candidate(
        self,
        owner: str,
        candidate_id: str,
        approved_by: str,
        note: str = "",
    ) -> dict[str, Any]:
        candidate = self._verified_candidate(owner, candidate_id)
        existing = self.store.read_approval(owner, candidate_id)
        if existing is not None:
            validation = self._verified_validation(
                candidate, self.store.read_validation(owner, candidate_id)
            )
            self._verified_approval(candidate, validation, existing)
            self._assert_parent_still_active(owner, candidate)
            self._ensure_lifecycle_event(
                owner,
                candidate,
                action="approve",
                actor=str(existing["approved_by"]),
                source="human",
                created_at=str(existing["approved_at"]),
            )
            return self._compose_candidate(owner, candidate)
        validation = self.store.read_validation(owner, candidate_id)
        if validation is None:
            raise RuntimeError("Candidate must be validated before approval")
        validation = self._verified_validation(candidate, validation)
        if not bool(validation.get("gates", {}).get("eligible")):
            raise RuntimeError(
                "Candidate failed validation gates and cannot be approved"
            )
        approval = {
            "schema_version": SCHEMA_VERSION,
            "candidate_id": candidate_id,
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "config_context_fingerprint": candidate["config_context_fingerprint"],
            "parent_candidate_id": candidate["parent_candidate_id"],
            "parent_fingerprint": candidate["parent_fingerprint"],
            "validation_fingerprint": _fingerprint(validation),
            "approved_at": _utc_now(),
            "approved_by": _text(approved_by, "approved_by", 200, required=True),
            "note": _text(note, "note", 1000),
            "explicit_human_approval": True,
            "live_trading_authorized": False,
        }
        self.store.write_approval(
            owner,
            candidate_id,
            approval,
            expected_active_fingerprint=candidate["parent_fingerprint"],
            empty_active_fingerprint=self._configured_baseline()["fingerprint"],
        )
        self._ensure_lifecycle_event(
            owner,
            candidate,
            action="approve",
            actor=approval["approved_by"],
            source="human",
            created_at=approval["approved_at"],
        )
        return self._compose_candidate(owner, candidate)

    def export_paper_config(
        self,
        owner: str,
        candidate_id: str,
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        event_actor = "system" if actor is None else actor
        candidate = self._verified_candidate(owner, candidate_id)
        approval = self.store.read_approval(owner, candidate_id)
        if approval is None:
            raise RuntimeError(
                "Only an approved candidate can be exported to paper trading"
            )
        validation = self._verified_validation(
            candidate, self.store.read_validation(owner, candidate_id)
        )
        approval = self._verified_approval(candidate, validation, approval)
        existing = self.store.read_export_config(owner, candidate_id)
        if existing is not None:
            exported = self._verified_export(
                owner, candidate, validation, approval, existing
            )
            self._assert_parent_still_active(owner, candidate)
            self._ensure_lifecycle_event(
                owner,
                candidate,
                action="export",
                actor=event_actor,
                source="human",
                created_at=str(exported["exported_at"]),
            )
            return exported

        exported_at = _utc_now()
        raw = self._isolated_paper_config(
            owner, candidate, validation, approval, exported_at
        )
        path = self.store.write_export(
            owner,
            candidate_id,
            raw,
            expected_active_fingerprint=candidate["parent_fingerprint"],
            empty_active_fingerprint=self._configured_baseline()["fingerprint"],
        )
        self._ensure_lifecycle_event(
            owner,
            candidate,
            action="export",
            actor=event_actor,
            source="human",
            created_at=exported_at,
        )
        return {
            "candidate_id": candidate_id,
            "path": str(path),
            "config_fingerprint": raw["_strategy_lab"]["config_fingerprint"],
            "broker_mode": raw["broker"]["mode"],
            "exported_at": exported_at,
        }

    def activate_candidate(
        self,
        owner: str,
        candidate_id: str,
        activated_by: str,
        note: str = "",
    ) -> dict[str, Any]:
        candidate = self._verified_candidate(owner, candidate_id, require_parent=False)
        approval = self.store.read_approval(owner, candidate_id)
        if approval is None:
            raise RuntimeError("Only an approved candidate can become the lab baseline")
        validation = self._verified_validation(
            candidate, self.store.read_validation(owner, candidate_id)
        )
        approval = self._verified_approval(candidate, validation, approval)
        exported = self.store.read_export_config(owner, candidate_id)
        if exported is None:
            raise RuntimeError(
                "Candidate must have a matching paper export before activation"
            )
        self._verified_export(owner, candidate, validation, approval, exported)
        current = self._active_baseline(owner)
        if candidate_id in current["retired_candidates"]:
            raise RuntimeError("Retired candidates cannot be activated again")
        if current["candidate_id"] == candidate_id:
            if (
                current["fingerprint"] != candidate["candidate_fingerprint"]
                or current["snapshot"] != candidate["candidate"]
            ):
                raise RuntimeError(
                    "Active baseline does not match the candidate record"
                )
            return self._public_active(current)
        self._assert_parent_active(candidate, current)
        target = {
            "candidate_id": candidate_id,
            "fingerprint": candidate["candidate_fingerprint"],
            "snapshot": candidate["candidate"],
        }
        actor = _text(activated_by, "activated_by", 200, required=True)

        def transition(
            stored: dict[str, Any] | None,
        ) -> tuple[dict[str, Any], dict[str, Any] | None]:
            current = self._stored_or_configured_baseline(stored)
            if candidate_id in current["retired_candidates"]:
                raise RuntimeError("Retired candidates cannot be activated again")
            if current["candidate_id"] == candidate_id:
                return current, None
            self._assert_parent_active(candidate, current)
            stack = list(current.get("rollback_stack", []))
            stack.append(_stack_entry(current))
            active = {
                **target,
                "activated_at": _utc_now(),
                "activated_by": actor,
                "rollback_stack": stack,
                "lifecycle_state": "ACTIVE",
                "lifecycle_updated_at": _utc_now(),
                "lifecycle_updated_by": actor,
                "retired_candidates": list(current["retired_candidates"]),
            }
            return active, self._activation_event(
                "activate", current, active, actor, note
            )

        try:
            active = self.store.transition_active(
                owner,
                transition,
                expected_active_fingerprint=candidate["parent_fingerprint"],
                empty_active_fingerprint=self._configured_baseline()["fingerprint"],
                max_transition_events=MAX_TRANSITION_EVENTS_PER_OWNER,
            )
        except StrategyLabCapacityError as exc:
            raise StrategyLabCapacityError(
                "每个账号最多允许 "
                f"{MAX_TRANSITION_EVENTS_PER_OWNER} 次策略激活/回滚；已达到上限。"
                "为保护审计记录，系统已停止策略切换。"
            ) from exc
        return self._public_active(active)

    def monitor_active_candidate(
        self,
        owner: str,
        market: MarketData,
        *,
        actor: str | None = None,
    ) -> dict[str, Any]:
        active = self._active_baseline(owner)
        candidate_id = active.get("candidate_id")
        if not isinstance(candidate_id, str):
            raise RuntimeError("Activate an approved paper candidate before monitoring")
        candidate = self._verified_candidate(owner, candidate_id, require_parent=False)
        validation = self._verified_validation(
            candidate, self.store.read_validation(owner, candidate_id)
        )
        snapshot_before = _market_snapshot(market)
        configured_start = date.fromisoformat(str(self.config.raw["backtest"]["start"]))
        configured_end = date.fromisoformat(str(self.config.raw["backtest"]["end"]))
        calendar = [
            day for day in market.calendar if configured_start <= day <= configured_end
        ]
        if not calendar:
            raise ValueError("Strategy monitoring requires completed market sessions")
        calendar = calendar[-self.lifecycle_policy.window_sessions :]
        start, end = calendar[0], calendar[-1]
        recent_candidate: dict[str, float] | None = None
        recent_parent: dict[str, float] | None = None
        if len(calendar) >= 2:
            candidate_strategy, candidate_risk = _settings_from_snapshot(
                candidate["candidate"]
            )
            parent_strategy, parent_risk = _settings_from_snapshot(candidate["baseline"])
            recent_candidate = _metrics(
                self._run(
                    market,
                    candidate_strategy,
                    candidate_risk,
                    self.config.costs,
                    start,
                    end,
                ).metrics
            )
            recent_parent = _metrics(
                self._run(
                    market,
                    parent_strategy,
                    parent_risk,
                    self.config.costs,
                    start,
                    end,
                ).metrics
            )
        reference = validation.get("holdout", {}).get("candidate_metrics")
        if not isinstance(reference, Mapping):
            raise RuntimeError("Active candidate validation reference is missing")
        evidence = evaluate_strategy_decay(
            session_count=len(calendar),
            recent_candidate=recent_candidate,
            recent_parent=recent_parent,
            validation_candidate=reference,
            maximum_drawdown=float(candidate["candidate"]["risk"]["max_portfolio_drawdown"]),
            policy=self.lifecycle_policy,
        )
        snapshot_after = _market_snapshot(market)
        if snapshot_before["id"] != snapshot_after["id"]:
            raise RuntimeError("Market snapshot changed during strategy monitoring")
        created_at = _utc_now()
        monitor = {
            "schema_version": SCHEMA_VERSION,
            "monitor_id": f"monitor_{uuid4().hex}",
            "created_at": created_at,
            "actor": _text(actor or "system", "actor", 200, required=True),
            "candidate_id": candidate_id,
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "active_lifecycle_state": active["lifecycle_state"],
            "market_snapshot": snapshot_before,
            "period": {
                "start": start.isoformat(),
                "end": end.isoformat(),
                "sessions": len(calendar),
            },
            "validation_reference": {
                "market_snapshot": validation["market_snapshot"],
                "period": validation["period"],
                "candidate_metrics": dict(reference),
            },
            "recent_candidate_metrics": recent_candidate,
            "recent_parent_metrics": recent_parent,
            "evidence": evidence,
            "state_changed": False,
            "live_trading_authorized": False,
        }
        monitor["evidence_fingerprint"] = _fingerprint(
            _monitor_evidence_payload(monitor)
        )
        self.store.write_monitor(
            owner,
            monitor,
            expected_active_candidate_id=candidate_id,
            expected_active_fingerprint=str(active["fingerprint"]),
            expected_lifecycle_state=str(active["lifecycle_state"]),
            max_records=MAX_MONITORS_PER_OWNER,
        )
        self._write_monitor_event(owner, monitor)
        return monitor

    def suspend_active_candidate(
        self,
        owner: str,
        *,
        actor: str,
        expected_active_candidate_id: str,
        expected_active_fingerprint: str,
        note: str,
        monitor_id: str | None = None,
    ) -> dict[str, Any]:
        return self._change_lifecycle_state(
            owner,
            action="suspend",
            from_state="ACTIVE",
            to_state="SUSPENDED",
            actor=actor,
            expected_active_candidate_id=expected_active_candidate_id,
            expected_active_fingerprint=expected_active_fingerprint,
            note=note,
            monitor_id=monitor_id,
        )

    def resume_active_candidate(
        self,
        owner: str,
        *,
        actor: str,
        expected_active_candidate_id: str,
        expected_active_fingerprint: str,
        note: str,
        monitor_id: str | None = None,
    ) -> dict[str, Any]:
        return self._change_lifecycle_state(
            owner,
            action="resume",
            from_state="SUSPENDED",
            to_state="ACTIVE",
            actor=actor,
            expected_active_candidate_id=expected_active_candidate_id,
            expected_active_fingerprint=expected_active_fingerprint,
            note=note,
            monitor_id=monitor_id,
        )

    def retire_active_candidate(
        self,
        owner: str,
        *,
        actor: str,
        expected_active_candidate_id: str,
        expected_active_fingerprint: str,
        note: str,
        monitor_id: str | None = None,
    ) -> dict[str, Any]:
        event_actor = _text(actor, "actor", 200, required=True)
        reason = _text(note, "note", 1000, required=True)
        evidence = self._verified_monitor_reference(
            owner,
            monitor_id,
            expected_active_candidate_id,
            expected_active_fingerprint,
        )

        def transition(
            stored: dict[str, Any] | None,
        ) -> tuple[dict[str, Any], dict[str, Any] | None]:
            current = self._stored_or_configured_baseline(stored)
            self._assert_expected_active(
                current,
                expected_active_candidate_id,
                expected_active_fingerprint,
            )
            stack = list(current.get("rollback_stack", []))
            target = stack.pop() if stack else _stack_entry(self._configured_baseline())
            retired = list(current["retired_candidates"])
            if expected_active_candidate_id not in retired:
                retired.append(expected_active_candidate_id)
            target_candidate_id = target.get("candidate_id")
            target_state = str(
                target.get("lifecycle_state")
                or ("ACTIVE" if target_candidate_id else "CONFIGURED")
            )
            active = {
                "candidate_id": target_candidate_id,
                "fingerprint": target["fingerprint"],
                "snapshot": target["snapshot"],
                "activated_at": _utc_now(),
                "activated_by": event_actor,
                "rollback_stack": stack,
                "lifecycle_state": target_state,
                "lifecycle_updated_at": _utc_now(),
                "lifecycle_updated_by": event_actor,
                "retired_candidates": retired,
            }
            return active, self._lifecycle_transition_event(
                "retire", current, active, event_actor, reason, evidence
            )

        active = self.store.transition_active(
            owner,
            transition,
            max_transition_events=MAX_TRANSITION_EVENTS_PER_OWNER,
        )
        return self._public_active(active)

    def rollback(
        self,
        owner: str,
        rolled_back_by: str,
        expected_active_candidate_id: str,
        expected_active_fingerprint: str,
        note: str = "",
    ) -> dict[str, Any]:
        actor = _text(rolled_back_by, "rolled_back_by", 200, required=True)
        expected_candidate_id = _text(
            expected_active_candidate_id,
            "expected_active_candidate_id",
            37,
            required=True,
        )
        expected_fingerprint = _text(
            expected_active_fingerprint,
            "expected_active_fingerprint",
            64,
            required=True,
        )

        def transition(
            stored: dict[str, Any] | None,
        ) -> tuple[dict[str, Any], dict[str, Any] | None]:
            current = self._stored_or_configured_baseline(stored)
            if (
                current.get("candidate_id") != expected_candidate_id
                or current.get("fingerprint") != expected_fingerprint
            ):
                raise StrategyLabConflictError(
                    "活动策略版本已变化；请重新核对后再决定是否回滚"
                )
            stack = list(current.get("rollback_stack", []))
            if not stack:
                raise RuntimeError("No prior strategy-lab baseline is available")
            target = stack.pop()
            target_candidate_id = target.get("candidate_id")
            active = {
                "candidate_id": target_candidate_id,
                "fingerprint": target["fingerprint"],
                "snapshot": target["snapshot"],
                "activated_at": _utc_now(),
                "activated_by": actor,
                "rollback_stack": stack,
                "lifecycle_state": target.get("lifecycle_state")
                or ("ACTIVE" if target_candidate_id else "CONFIGURED"),
                "lifecycle_updated_at": _utc_now(),
                "lifecycle_updated_by": actor,
                "retired_candidates": list(current["retired_candidates"]),
            }
            return active, self._activation_event(
                "rollback", current, active, actor, note
            )

        try:
            active = self.store.transition_active(
                owner,
                transition,
                max_transition_events=MAX_TRANSITION_EVENTS_PER_OWNER,
            )
        except StrategyLabCapacityError as exc:
            raise StrategyLabCapacityError(
                "每个账号最多允许 "
                f"{MAX_TRANSITION_EVENTS_PER_OWNER} 次策略激活/回滚；已达到上限。"
                "为保护审计记录，系统已停止策略切换。"
            ) from exc
        return self._public_active(active)

    def _create_candidate(
        self,
        owner: str,
        source: str,
        changes: Mapping[str, Any],
        title: str,
        hypothesis: str,
        reason: str,
        actor: str | None = None,
        proposal: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        baseline = self._active_baseline(owner)
        candidate_snapshot, effective = apply_changes(
            self.config, baseline["snapshot"], changes
        )
        candidate = {
            "schema_version": SCHEMA_VERSION,
            "candidate_id": f"cand_{uuid4().hex}",
            "owner": self.store.owner_id(owner),
            "source": source,
            "title": _text(title, "title", 120, required=True),
            "hypothesis": _text(hypothesis, "hypothesis", 1000, required=True),
            "reason": _text(reason, "reason", 1000, required=True),
            "created_at": _utc_now(),
            "parent_candidate_id": baseline["candidate_id"],
            "parent_fingerprint": baseline["fingerprint"],
            "config_context_fingerprint": self._config_context_fingerprint(),
            "baseline": baseline["snapshot"],
            "changes": effective,
            "candidate": candidate_snapshot,
            "candidate_fingerprint": _fingerprint(candidate_snapshot),
            "status": "DRAFT",
            "proposal": proposal,
            "safety": {
                "may_place_orders": False,
                "may_change_broker_configuration": False,
                "may_self_approve": False,
            },
        }
        try:
            self.store.write_candidate(
                owner,
                candidate,
                max_records=MAX_CANDIDATES_PER_OWNER,
                expected_active_fingerprint=baseline["fingerprint"],
                empty_active_fingerprint=self._configured_baseline()["fingerprint"],
            )
        except StrategyLabCapacityError as exc:
            raise StrategyLabCapacityError(
                "策略候选已达到每个账号 "
                f"{MAX_CANDIDATES_PER_OWNER} 个的上限，无法继续创建"
            ) from exc
        self._ensure_lifecycle_event(
            owner,
            candidate,
            action="create",
            actor="system" if actor is None else actor,
            source=source,
            created_at=candidate["created_at"],
        )
        return self._compose_candidate(owner, candidate)

    def _compose_candidate(
        self, owner: str, candidate: dict[str, Any]
    ) -> dict[str, Any]:
        candidate_id = candidate["candidate_id"]
        validation = self.store.read_validation(owner, candidate_id)
        approval = self.store.read_approval(owner, candidate_id)
        exported = self.store.read_export(owner, candidate_id)
        if approval is not None:
            status = "APPROVED"
        elif validation is None:
            status = "DRAFT"
        elif validation.get("gates", {}).get("eligible"):
            status = "ELIGIBLE"
        else:
            status = "REJECTED"
        active = self._active_baseline(owner)
        retired = candidate_id in active["retired_candidates"]
        monitors = [
            self._verified_monitor(item)
            for item in self.store.list_monitors(owner)
            if item.get("candidate_id") == candidate_id
            and item.get("candidate_fingerprint")
            == candidate.get("candidate_fingerprint")
        ]
        public_candidate = {
            key: value for key, value in candidate.items() if key != "owner"
        }
        return {
            **public_candidate,
            "baseline_settings": candidate["baseline"],
            "effective_changes": candidate["changes"],
            "candidate_settings": candidate["candidate"],
            "status": status,
            "validation": validation,
            "approval": approval,
            "export": exported,
            "active": active["candidate_id"] == candidate_id,
            "lifecycle": {
                "state": (
                    active["lifecycle_state"]
                    if active["candidate_id"] == candidate_id
                    else "RETIRED"
                    if retired
                    else "INACTIVE"
                ),
                "latest_monitor": monitors[-1] if monitors else None,
                "monitor_count": len(monitors),
            },
        }

    def _active_baseline(self, owner: str) -> dict[str, Any]:
        return self._stored_or_configured_baseline(self.store.read_active(owner))

    def _configured_baseline(self) -> dict[str, Any]:
        return self._stored_or_configured_baseline(None)

    def _config_context_fingerprint(self) -> str:
        return _fingerprint(
            {
                "app_version": __version__,
                "strategy_lab": {
                    "schema_version": SCHEMA_VERSION,
                    "engine_version": _ENGINE_VERSION,
                },
                "raw": self.config.raw,
                "instruments": [vars(item) for item in self.config.instruments],
                "security_master_fingerprint": (
                    self.config.security_master.fingerprint()
                ),
                "universe_name": self.config.universe_name,
                "minimum_listing_days": self.config.minimum_listing_days,
            }
        )

    def _verified_candidate(
        self,
        owner: str,
        candidate_id: str,
        *,
        require_parent: bool = True,
    ) -> dict[str, Any]:
        candidate = self.store.read_candidate(owner, candidate_id)
        if candidate.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError("Candidate schema version mismatch")
        if candidate.get("candidate_id") != candidate_id:
            raise RuntimeError("Candidate id mismatch")
        if candidate.get("owner") != self.store.owner_id(owner):
            raise RuntimeError("Candidate owner mismatch")
        if (
            candidate.get("config_context_fingerprint")
            != self._config_context_fingerprint()
        ):
            raise RuntimeError(
                "Strategy-lab configuration context changed; create a new candidate"
            )

        baseline = candidate.get("baseline")
        snapshot = candidate.get("candidate")
        if not isinstance(baseline, dict) or not isinstance(snapshot, dict):
            raise RuntimeError("Candidate settings record is invalid")
        if _fingerprint(baseline) != candidate.get("parent_fingerprint"):
            raise RuntimeError("Candidate parent fingerprint mismatch")
        if _fingerprint(snapshot) != candidate.get("candidate_fingerprint"):
            raise RuntimeError("Candidate settings fingerprint mismatch")
        try:
            reconstructed, effective = apply_changes(
                self.config, baseline, candidate.get("changes", {})
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Candidate changes are invalid") from exc
        if reconstructed != snapshot or effective != candidate.get("changes"):
            raise RuntimeError(
                "Candidate settings do not match its baseline and recorded changes"
            )
        if require_parent:
            self._assert_parent_still_active(owner, candidate)
        return candidate

    def _assert_parent_still_active(
        self, owner: str, candidate: Mapping[str, Any]
    ) -> None:
        def unchanged(
            stored: dict[str, Any] | None,
        ) -> tuple[dict[str, Any], dict[str, Any] | None]:
            active = self._stored_or_configured_baseline(stored)
            self._assert_parent_active(candidate, active)
            return active, None

        self.store.transition_active(
            owner,
            unchanged,
            expected_active_fingerprint=str(candidate["parent_fingerprint"]),
            empty_active_fingerprint=self._configured_baseline()["fingerprint"],
        )

    @staticmethod
    def _assert_parent_active(
        candidate: Mapping[str, Any], active: Mapping[str, Any]
    ) -> None:
        if active.get("candidate_id") != candidate.get(
            "parent_candidate_id"
        ) or active.get("fingerprint") != candidate.get("parent_fingerprint"):
            raise StrategyLabConflictError(
                "Active strategy-lab baseline changed; create and validate a new candidate"
            )

    def _verified_validation(
        self,
        candidate: Mapping[str, Any],
        validation: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if validation is None:
            raise RuntimeError("Candidate must be validated before this operation")
        self._assert_record_binding(
            validation,
            {
                "schema_version": SCHEMA_VERSION,
                "candidate_id": candidate["candidate_id"],
                "candidate_fingerprint": candidate["candidate_fingerprint"],
                "config_context_fingerprint": candidate["config_context_fingerprint"],
                "parent_candidate_id": candidate["parent_candidate_id"],
                "parent_fingerprint": candidate["parent_fingerprint"],
            },
            "Validation",
        )
        return validation

    def _verified_approval(
        self,
        candidate: Mapping[str, Any],
        validation: Mapping[str, Any],
        approval: dict[str, Any],
    ) -> dict[str, Any]:
        self._assert_record_binding(
            approval,
            {
                "schema_version": SCHEMA_VERSION,
                "candidate_id": candidate["candidate_id"],
                "candidate_fingerprint": candidate["candidate_fingerprint"],
                "config_context_fingerprint": candidate["config_context_fingerprint"],
                "parent_candidate_id": candidate["parent_candidate_id"],
                "parent_fingerprint": candidate["parent_fingerprint"],
                "validation_fingerprint": _fingerprint(validation),
                "explicit_human_approval": True,
                "live_trading_authorized": False,
            },
            "Approval",
        )
        return approval

    def _verified_export(
        self,
        owner: str,
        candidate: Mapping[str, Any],
        validation: Mapping[str, Any],
        approval: Mapping[str, Any],
        raw: dict[str, Any],
    ) -> dict[str, Any]:
        metadata = raw.get("_strategy_lab")
        if not isinstance(metadata, dict):
            raise RuntimeError("Paper export metadata is missing")
        self._assert_record_binding(
            metadata,
            {
                "schema_version": SCHEMA_VERSION,
                "candidate_id": candidate["candidate_id"],
                "candidate_fingerprint": candidate["candidate_fingerprint"],
                "config_context_fingerprint": candidate["config_context_fingerprint"],
                "parent_candidate_id": candidate["parent_candidate_id"],
                "parent_fingerprint": candidate["parent_fingerprint"],
                "validation_fingerprint": _fingerprint(validation),
                "approval_fingerprint": _fingerprint(approval),
            },
            "Paper export",
        )
        body = deepcopy(raw)
        body.pop("_strategy_lab", None)
        if _fingerprint(body) != metadata.get("config_fingerprint"):
            raise RuntimeError("Paper export configuration fingerprint mismatch")
        if body.get("strategy") != candidate["candidate"]["strategy"]:
            raise RuntimeError("Paper export strategy settings mismatch")
        if body.get("risk") != candidate["candidate"]["risk"]:
            raise RuntimeError("Paper export risk settings mismatch")
        broker = body.get("broker")
        if not isinstance(broker, dict) or broker.get("mode") != "disabled":
            raise RuntimeError("Paper export broker mode must remain disabled")
        return {
            "candidate_id": candidate["candidate_id"],
            "path": str(self.store.export_path(owner, candidate["candidate_id"])),
            "config_fingerprint": metadata["config_fingerprint"],
            "broker_mode": broker["mode"],
            "exported_at": metadata.get("exported_at"),
        }

    @staticmethod
    def _assert_record_binding(
        record: Mapping[str, Any],
        expected: Mapping[str, Any],
        label: str,
    ) -> None:
        for name, value in expected.items():
            if record.get(name) != value:
                field = name.replace("_", " ")
                raise RuntimeError(f"{label} {field} mismatch")

    def _stored_or_configured_baseline(
        self, active: dict[str, Any] | None
    ) -> dict[str, Any]:
        if active is not None:
            snapshot = active.get("snapshot")
            if not isinstance(snapshot, dict) or _fingerprint(snapshot) != active.get(
                "fingerprint"
            ):
                raise RuntimeError("Invalid active strategy-lab baseline")
            candidate_id = active.get("candidate_id")
            if candidate_id is not None and not _is_candidate_id(candidate_id):
                raise RuntimeError("Invalid active strategy candidate id")
            lifecycle_state = str(
                active.get("lifecycle_state")
                or ("ACTIVE" if candidate_id else "CONFIGURED")
            )
            allowed_states = (
                _ACTIVE_LIFECYCLE_STATES if candidate_id else frozenset({"CONFIGURED"})
            )
            if lifecycle_state not in allowed_states:
                raise RuntimeError("Invalid active strategy lifecycle state")
            retired = active.get("retired_candidates", [])
            if (
                not isinstance(retired, list)
                or len(retired) != len(set(retired))
                or any(not _is_candidate_id(item) for item in retired)
            ):
                raise RuntimeError("Invalid retired strategy candidate registry")
            if candidate_id in retired:
                raise RuntimeError("Active candidate cannot also be retired")
            return {
                **active,
                "lifecycle_state": lifecycle_state,
                "lifecycle_updated_at": active.get("lifecycle_updated_at"),
                "lifecycle_updated_by": active.get("lifecycle_updated_by"),
                "retired_candidates": retired,
            }
        snapshot = settings_snapshot(self.config.strategy, self.config.risk)
        return {
            "candidate_id": None,
            "fingerprint": _fingerprint(snapshot),
            "snapshot": snapshot,
            "activated_at": None,
            "activated_by": None,
            "rollback_stack": [],
            "lifecycle_state": "CONFIGURED",
            "lifecycle_updated_at": None,
            "lifecycle_updated_by": None,
            "retired_candidates": [],
        }

    def _public_active(self, active: dict[str, Any]) -> dict[str, Any]:
        return {
            "candidate_id": active["candidate_id"],
            "fingerprint": active["fingerprint"],
            "strategy": active["snapshot"]["strategy"],
            "risk": active["snapshot"]["risk"],
            "activated_at": active.get("activated_at"),
            "activated_by": active.get("activated_by"),
            "can_rollback": bool(active.get("rollback_stack")),
            "rollback_depth": len(active.get("rollback_stack", [])),
            "lifecycle_state": active["lifecycle_state"],
            "lifecycle_updated_at": active.get("lifecycle_updated_at"),
            "lifecycle_updated_by": active.get("lifecycle_updated_by"),
            "can_monitor": active["candidate_id"] is not None,
            "can_suspend": active["candidate_id"] is not None
            and active["lifecycle_state"] == "ACTIVE",
            "can_resume": active["candidate_id"] is not None
            and active["lifecycle_state"] == "SUSPENDED",
            "can_retire": active["candidate_id"] is not None,
            "retired_count": len(active["retired_candidates"]),
        }

    def _active_monitors(
        self, owner: str, active: Mapping[str, Any]
    ) -> list[dict[str, Any]]:
        candidate_id = active.get("candidate_id")
        if not isinstance(candidate_id, str):
            return []
        return [
            self._verified_monitor(item)
            for item in self.store.list_monitors(owner)
            if item.get("candidate_id") == candidate_id
            and item.get("candidate_fingerprint") == active.get("fingerprint")
        ]

    def _validation_period(self, market: MarketData) -> tuple[date, date, date]:
        start = date.fromisoformat(str(self.config.raw["backtest"]["start"]))
        configured_end = date.fromisoformat(str(self.config.raw["backtest"]["end"]))
        calendar = [day for day in market.calendar if start <= day <= configured_end]
        if len(calendar) < self.policy.minimum_sessions:
            raise ValueError(
                f"Strategy validation requires at least {self.policy.minimum_sessions} sessions"
            )
        holdout_sessions = max(
            20, int(math.ceil(len(calendar) * self.policy.holdout_fraction))
        )
        if holdout_sessions >= len(calendar):
            raise ValueError("Strategy validation history is too short for a holdout")
        return calendar[0], calendar[-1], calendar[-holdout_sessions]

    def _run(
        self,
        market: MarketData,
        strategy: StrategySettings,
        risk: RiskSettings,
        costs: CostSettings,
        start: date,
        end: date,
    ):
        run_config = replace(self.config, strategy=strategy, risk=risk, costs=costs)
        return BacktestEngine(run_config, market, strategy).run(start=start, end=end)

    def _stability_runs(
        self,
        market: MarketData,
        candidate: dict[str, Any],
        strategy: StrategySettings,
        risk: RiskSettings,
        start: date,
        end: date,
    ) -> dict[str, Any]:
        numeric = [
            (scope, name)
            for scope in ("strategy", "risk")
            for name, value in candidate["changes"][scope].items()
            if isinstance(value, (int, float)) and not isinstance(value, bool)
        ]
        if not numeric:
            numeric = [("strategy", "lookback_days")]
        rows: list[dict[str, Any]] = []
        parameters: list[str] = []
        for scope, name in sorted(numeric):
            spec = parameter_spec(scope, name)
            original = candidate["candidate"][scope][name]
            used: set[int | float] = set()
            parameter = f"{scope}.{name}"
            variant_count = 0
            fallback_step = float(spec.step or 0)
            if fallback_step <= 0:
                fallback_step = max(abs(float(original)) * 0.1, 0.01)
            for direction, factor in ((-1, 0.9), (1, 1.1)):
                perturbed = clamp_parameter(spec, float(original) * factor)
                if perturbed == original:
                    perturbed = clamp_parameter(
                        spec, float(original) + direction * fallback_step
                    )
                if perturbed == original or perturbed in used:
                    continue
                used.add(perturbed)
                try:
                    variant_snapshot, _ = apply_changes(
                        self.config,
                        candidate["candidate"],
                        {scope: {name: perturbed}},
                    )
                except ValueError:
                    continue
                variant_strategy, variant_risk = _settings_from_snapshot(
                    variant_snapshot
                )
                result = self._run(
                    market,
                    variant_strategy,
                    variant_risk,
                    self.config.costs,
                    start,
                    end,
                )
                rows.append(
                    {
                        "parameter": parameter,
                        "changes": {scope: {name: perturbed}},
                        "metrics": _metrics(result.metrics),
                    }
                )
                variant_count += 1
            if variant_count == 0:
                raise RuntimeError(
                    f"Could not construct a valid stability variant for {parameter}"
                )
            parameters.append(parameter)
        if not rows:
            raise RuntimeError("Could not construct deterministic stability variants")
        return {
            "parameters": parameters,
            "variant_count": len(rows),
            "variants": rows,
            "minimum_sharpe": min(row["metrics"]["sharpe"] for row in rows),
        }

    def _gates(
        self,
        baseline: dict[str, float],
        candidate: dict[str, float],
        baseline_holdout: dict[str, float],
        candidate_holdout: dict[str, float],
        baseline_cost: dict[str, float],
        candidate_cost: dict[str, float],
        stability: dict[str, Any],
        risk: RiskSettings,
    ) -> dict[str, Any]:
        checks = [
            {
                "id": "full_sample",
                "label": "完整样本表现未显著劣于基线",
                "passed": candidate["sharpe"] + self.policy.full_sharpe_tolerance
                >= baseline["sharpe"],
                "detail": (
                    f"候选 Sharpe {candidate['sharpe']:.3f}；基线 "
                    f"{baseline['sharpe']:.3f}；容忍差值 {self.policy.full_sharpe_tolerance:.2f}"
                ),
            },
            {
                "id": "holdout",
                "label": "留出集表现未显著劣于基线",
                "passed": candidate_holdout["sharpe"]
                + self.policy.holdout_sharpe_tolerance
                >= baseline_holdout["sharpe"],
                "detail": (
                    f"候选 Sharpe {candidate_holdout['sharpe']:.3f}；基线 "
                    f"{baseline_holdout['sharpe']:.3f}；容忍差值 "
                    f"{self.policy.holdout_sharpe_tolerance:.2f}"
                ),
            },
            {
                "id": "transaction_cost",
                "label": "候选通过确定性交易成本压力测试",
                "passed": candidate_cost["total_return"]
                + self.policy.cost_return_tolerance
                >= baseline_cost["total_return"],
                "detail": (
                    f"{self.policy.cost_multiplier:.1f} 倍成本下：候选收益 "
                    f"{candidate_cost['total_return']:.2%}；基线 "
                    f"{baseline_cost['total_return']:.2%}"
                ),
            },
            {
                "id": "drawdown",
                "label": "回撤保持在配置的风险边界内",
                "passed": candidate["max_drawdown"] >= -risk.max_portfolio_drawdown
                and candidate["max_drawdown"] + self.policy.drawdown_tolerance
                >= baseline["max_drawdown"],
                "detail": (
                    f"候选 {candidate['max_drawdown']:.2%}；基线 "
                    f"{baseline['max_drawdown']:.2%}；硬限制 "
                    f"{-risk.max_portfolio_drawdown:.2%}"
                ),
            },
            {
                "id": "stability",
                "label": "全部参数邻域未出现性能断崖",
                "passed": float(stability["minimum_sharpe"])
                + self.policy.stability_sharpe_tolerance
                >= candidate["sharpe"],
                "detail": (
                    f"邻域最低 Sharpe {float(stability['minimum_sharpe']):.3f}；"
                    f"候选 {candidate['sharpe']:.3f}"
                ),
            },
        ]
        passed = sum(bool(item["passed"]) for item in checks)
        return {
            "checks": checks,
            "passed": passed,
            "total": len(checks),
            "eligible": passed == len(checks),
        }

    def _local_proposal(
        self, baseline: Mapping[str, Mapping[str, Any]], objective: str
    ) -> dict[str, dict[str, Any]]:
        strategy = baseline["strategy"]
        if objective == "balanced":
            return self._ensure_local_difference(
                baseline,
                {
                    "strategy": {
                        "target_annual_volatility": clamp_parameter(
                            parameter_spec("strategy", "target_annual_volatility"),
                            float(strategy["target_annual_volatility"]) * 0.9,
                        ),
                        "minimum_rebalance_weight": clamp_parameter(
                            parameter_spec("strategy", "minimum_rebalance_weight"),
                            float(strategy["minimum_rebalance_weight"]) + 0.005,
                        ),
                    }
                },
            )
        if objective == "drawdown":
            return self._ensure_local_difference(
                baseline,
                {
                    "strategy": {
                        "target_annual_volatility": clamp_parameter(
                            parameter_spec("strategy", "target_annual_volatility"),
                            float(strategy["target_annual_volatility"]) * 0.85,
                        ),
                        "max_position_weight": clamp_parameter(
                            parameter_spec("strategy", "max_position_weight"),
                            float(strategy["max_position_weight"]) * 0.9,
                        ),
                        "minimum_cash_weight": clamp_parameter(
                            parameter_spec("strategy", "minimum_cash_weight"),
                            float(strategy["minimum_cash_weight"]) + 0.03,
                        ),
                    }
                },
            )
        if objective == "turnover":
            return self._ensure_local_difference(
                baseline,
                {
                    "strategy": {
                        "rebalance_days": clamp_parameter(
                            parameter_spec("strategy", "rebalance_days"),
                            float(strategy["rebalance_days"]) * 1.25,
                        ),
                        "minimum_rebalance_weight": clamp_parameter(
                            parameter_spec("strategy", "minimum_rebalance_weight"),
                            float(strategy["minimum_rebalance_weight"]) + 0.01,
                        ),
                    }
                },
            )
        raise ValueError("objective must be balanced, drawdown, or turnover")

    def _ensure_local_difference(
        self,
        baseline: Mapping[str, Mapping[str, Any]],
        changes: dict[str, dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        if any(
            value != baseline[scope][name]
            for scope, values in changes.items()
            for name, value in values.items()
        ):
            return changes
        cooldown = int(baseline["risk"]["cooldown_days"])
        fallback = cooldown + 1 if cooldown < 252 else cooldown - 1
        return {"risk": {"cooldown_days": fallback}}

    def _isolated_paper_config(
        self,
        owner: str,
        candidate: dict[str, Any],
        validation: Mapping[str, Any],
        approval: Mapping[str, Any],
        exported_at: str,
    ) -> dict[str, Any]:
        raw = deepcopy(self.config.raw)
        raw.pop("_strategy_lab", None)
        raw["strategy"] = deepcopy(candidate["candidate"]["strategy"])
        raw["risk"] = deepcopy(candidate["candidate"]["risk"])
        profile = (
            self.store.owner_directory(owner)
            / "paper_profiles"
            / candidate["candidate_id"]
        )
        raw["data"]["cache_dir"] = str(self.config.cache_dir.resolve())
        if "security_master" in raw:
            raw["security_master"]["file"] = str(
                self.config.resolve(str(raw["security_master"]["file"])).resolve()
            )
        raw["reports_dir"] = str((profile / "reports").resolve())
        raw["logs_dir"] = str((profile / "logs").resolve())
        raw.setdefault("paper", {})
        raw["paper"].update(
            {
                "state_file": str((profile / "paper_state.json").resolve()),
                "trades_file": str((profile / "paper_trades.csv").resolve()),
                "equity_file": str((profile / "paper_equity.csv").resolve()),
                "rejections_file": str((profile / "paper_rejections.csv").resolve()),
            }
        )
        raw.setdefault("auth", {})["users_file"] = str(
            self.config.auth_users_file.resolve()
        )
        raw.setdefault("broker", {})
        raw["broker"].update(
            {
                "mode": "disabled",
                "adapter": None,
                "account_id": None,
                "reconciliation_file": str(
                    (profile / "broker_reconciliation.csv").resolve()
                ),
                "orders_file": str((profile / "broker_orders.csv").resolve()),
                "fills_file": str((profile / "broker_fills.csv").resolve()),
                "ledger_scope_file": str(
                    (profile / "broker_ledger_scope.json").resolve()
                ),
                "authorization_file": str(
                    (profile / "live_authorization.json").resolve()
                ),
                "kill_switch_file": str((profile / "LIVE_KILL_SWITCH").resolve()),
            }
        )
        config_fingerprint = _fingerprint(raw)
        raw["_strategy_lab"] = {
            "schema_version": SCHEMA_VERSION,
            "candidate_id": candidate["candidate_id"],
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "config_context_fingerprint": candidate["config_context_fingerprint"],
            "parent_candidate_id": candidate["parent_candidate_id"],
            "parent_fingerprint": candidate["parent_fingerprint"],
            "validation_fingerprint": _fingerprint(validation),
            "approval_fingerprint": _fingerprint(approval),
            "config_fingerprint": config_fingerprint,
            "exported_at": exported_at,
            "research_only": True,
            "live_trading_authorized": False,
        }
        return raw

    def _change_lifecycle_state(
        self,
        owner: str,
        *,
        action: str,
        from_state: str,
        to_state: str,
        actor: str,
        expected_active_candidate_id: str,
        expected_active_fingerprint: str,
        note: str,
        monitor_id: str | None,
    ) -> dict[str, Any]:
        event_actor = _text(actor, "actor", 200, required=True)
        reason = _text(note, "note", 1000, required=True)
        evidence = self._verified_monitor_reference(
            owner,
            monitor_id,
            expected_active_candidate_id,
            expected_active_fingerprint,
        )

        def transition(
            stored: dict[str, Any] | None,
        ) -> tuple[dict[str, Any], dict[str, Any] | None]:
            current = self._stored_or_configured_baseline(stored)
            self._assert_expected_active(
                current,
                expected_active_candidate_id,
                expected_active_fingerprint,
            )
            if current["lifecycle_state"] != from_state:
                raise StrategyLabConflictError(
                    "Active strategy lifecycle changed; refresh before confirming"
                )
            active = {
                **current,
                "lifecycle_state": to_state,
                "lifecycle_updated_at": _utc_now(),
                "lifecycle_updated_by": event_actor,
            }
            return active, self._lifecycle_transition_event(
                action, current, active, event_actor, reason, evidence
            )

        active = self.store.transition_active(
            owner,
            transition,
            max_transition_events=MAX_TRANSITION_EVENTS_PER_OWNER,
        )
        return self._public_active(active)

    @staticmethod
    def _assert_expected_active(
        active: Mapping[str, Any], candidate_id: str, fingerprint: str
    ) -> None:
        if (
            active.get("candidate_id") != candidate_id
            or active.get("fingerprint") != fingerprint
        ):
            raise StrategyLabConflictError(
                "Active strategy version changed; refresh before confirming"
            )

    def _verified_monitor_reference(
        self,
        owner: str,
        monitor_id: str | None,
        candidate_id: str,
        fingerprint: str,
    ) -> dict[str, str] | None:
        if monitor_id is None:
            return None
        monitor = self._verified_monitor(self.store.read_monitor(owner, monitor_id))
        if (
            monitor.get("candidate_id") != candidate_id
            or monitor.get("candidate_fingerprint") != fingerprint
        ):
            raise StrategyLabConflictError(
                "Monitoring evidence does not match the active strategy version"
            )
        evidence_fingerprint = str(monitor["evidence_fingerprint"])
        return {
            "monitor_id": monitor_id,
            "evidence_fingerprint": evidence_fingerprint,
            "verdict": str(monitor.get("evidence", {}).get("verdict", "")),
        }

    @staticmethod
    def _verified_monitor(monitor: dict[str, Any]) -> dict[str, Any]:
        if monitor.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError("Monitoring evidence schema version mismatch")
        monitor_id = monitor.get("monitor_id")
        fingerprint = monitor.get("evidence_fingerprint")
        if (
            not isinstance(monitor_id, str)
            or not monitor_id.startswith("monitor_")
            or len(monitor_id) != 40
            or any(
                character not in "0123456789abcdef" for character in monitor_id[8:]
            )
        ):
            raise RuntimeError("Monitoring evidence id is invalid")
        if (
            not isinstance(fingerprint, str)
            or len(fingerprint) != 64
            or any(character not in "0123456789abcdef" for character in fingerprint)
        ):
            raise RuntimeError("Monitoring evidence fingerprint is invalid")
        if _fingerprint(_monitor_evidence_payload(monitor)) != fingerprint:
            raise RuntimeError("Monitoring evidence fingerprint mismatch")
        if monitor.get("state_changed") is not False:
            raise RuntimeError("Monitoring evidence cannot change strategy state")
        if monitor.get("live_trading_authorized") is not False:
            raise RuntimeError("Monitoring evidence cannot authorize live trading")
        candidate_id = monitor.get("candidate_id")
        candidate_fingerprint = monitor.get("candidate_fingerprint")
        evidence = monitor.get("evidence")
        if not _is_candidate_id(candidate_id):
            raise RuntimeError("Monitoring candidate id is invalid")
        if (
            not isinstance(candidate_fingerprint, str)
            or len(candidate_fingerprint) != 64
            or any(
                character not in "0123456789abcdef"
                for character in candidate_fingerprint
            )
        ):
            raise RuntimeError("Monitoring candidate fingerprint is invalid")
        if (
            not isinstance(evidence, Mapping)
            or evidence.get("verdict")
            not in {MONITORING_OK, REVIEW_REQUIRED, INSUFFICIENT_DATA}
            or evidence.get("automatic_state_change") is not False
        ):
            raise RuntimeError("Monitoring verdict is invalid")
        return monitor

    def _write_monitor_event(
        self, owner: str, monitor: Mapping[str, Any]
    ) -> None:
        monitor_id = str(monitor["monitor_id"])
        event = {
            "schema_version": SCHEMA_VERSION,
            "event_id": f"event_{monitor_id.removeprefix('monitor_')}",
            "action": "monitor",
            "created_at": monitor["created_at"],
            "candidate_id": monitor["candidate_id"],
            "candidate_fingerprint": monitor["candidate_fingerprint"],
            "actor": monitor["actor"],
            "source": "human_requested",
            "monitor_id": monitor_id,
            "evidence_fingerprint": monitor["evidence_fingerprint"],
            "verdict": monitor["evidence"]["verdict"],
            "state_changed": False,
            "affects_broker_configuration": False,
            "live_trading_authorized": False,
        }
        try:
            self.store.write_event(owner, event)
        except FileExistsError:
            pass

    def _lifecycle_transition_event(
        self,
        action: str,
        previous: Mapping[str, Any],
        active: Mapping[str, Any],
        actor: str,
        note: str,
        evidence: Mapping[str, str] | None,
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "event_id": f"event_{uuid4().hex}",
            "action": action,
            "created_at": _utc_now(),
            "candidate_id": previous["candidate_id"],
            "candidate_fingerprint": previous["fingerprint"],
            "actor": actor,
            "source": "human",
            "note": note,
            "from_candidate_id": previous["candidate_id"],
            "from_fingerprint": previous["fingerprint"],
            "from_lifecycle_state": previous["lifecycle_state"],
            "to_candidate_id": active["candidate_id"],
            "to_fingerprint": active["fingerprint"],
            "to_lifecycle_state": active["lifecycle_state"],
            "monitoring_evidence": dict(evidence) if evidence else None,
            "state_changed": True,
            "affects_broker_configuration": False,
            "live_trading_authorized": False,
        }

    def _ensure_lifecycle_event(
        self,
        owner: str,
        candidate: dict[str, Any],
        *,
        action: str,
        actor: str,
        source: str,
        created_at: str,
    ) -> None:
        seed = (
            f"{self.store.owner_id(owner)}|{action}|{candidate['candidate_id']}"
        ).encode("ascii")
        event = {
            "schema_version": SCHEMA_VERSION,
            "event_id": f"event_{hashlib.sha256(seed).hexdigest()[:32]}",
            "action": action,
            "created_at": created_at,
            "candidate_id": candidate["candidate_id"],
            "candidate_fingerprint": candidate["candidate_fingerprint"],
            "actor": _text(actor, "actor", 200, required=True),
            "source": source,
            "affects_broker_configuration": False,
            "live_trading_authorized": False,
        }
        try:
            self.store.write_event(owner, event)
        except FileExistsError:
            pass

    def _activation_event(
        self,
        action: str,
        previous: dict[str, Any],
        active: dict[str, Any],
        actor: str,
        note: str,
    ) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "event_id": f"event_{uuid4().hex}",
            "action": action,
            "created_at": _utc_now(),
            "candidate_id": active["candidate_id"],
            "actor": _text(actor, "actor", 200, required=True),
            "source": "human",
            "note": _text(note, "note", 1000),
            "from_candidate_id": previous["candidate_id"],
            "from_fingerprint": previous["fingerprint"],
            "to_candidate_id": active["candidate_id"],
            "to_fingerprint": active["fingerprint"],
            "from_lifecycle_state": previous["lifecycle_state"],
            "to_lifecycle_state": active["lifecycle_state"],
            "state_changed": True,
            "affects_broker_configuration": False,
            "live_trading_authorized": False,
        }


def _settings_from_snapshot(
    value: Mapping[str, Mapping[str, Any]],
) -> tuple[StrategySettings, RiskSettings]:
    return StrategySettings(**dict(value["strategy"])), RiskSettings(
        **dict(value["risk"])
    )


def _metrics(value: Mapping[str, Any]) -> dict[str, float]:
    output: dict[str, float] = {}
    for name, raw in value.items():
        parsed = float(raw)
        if not math.isfinite(parsed):
            raise RuntimeError(f"Backtest metric is not finite: {name}")
        output[str(name)] = parsed
    return output


def _market_snapshot(market: MarketData) -> dict[str, Any]:
    metadata = market.snapshot_metadata()
    snapshot_id = _fingerprint(metadata)
    snapshot_date = metadata.get("latest_common_session") or metadata.get(
        "latest_benchmark_session"
    )
    if snapshot_date is None and market.calendar:
        snapshot_date = market.calendar[-1].isoformat()
    return {"id": snapshot_id, "date": snapshot_date, "metadata": metadata}


def _monitor_evidence_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    period = value.get("period")
    reference = value.get("validation_reference")
    if not isinstance(period, Mapping) or not isinstance(reference, Mapping):
        raise RuntimeError("Monitoring evidence structure is invalid")
    candidate_metrics = reference.get("candidate_metrics")
    if not isinstance(candidate_metrics, Mapping):
        raise RuntimeError("Monitoring validation reference is invalid")
    return {
        "market_snapshot": value.get("market_snapshot"),
        "period": [period.get("start"), period.get("end"), period.get("sessions")],
        "validation_reference": dict(candidate_metrics),
        "recent_candidate_metrics": value.get("recent_candidate_metrics"),
        "recent_parent_metrics": value.get("recent_parent_metrics"),
        "evidence": value.get("evidence"),
    }


def _fingerprint(value: Any) -> str:
    raw = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
        allow_nan=False,
        default=lambda item: item.isoformat()
        if isinstance(item, (date, datetime))
        else str(item),
    )
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def _stack_entry(active: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "candidate_id": active.get("candidate_id"),
        "fingerprint": active["fingerprint"],
        "snapshot": active["snapshot"],
        "lifecycle_state": active.get("lifecycle_state")
        or ("ACTIVE" if active.get("candidate_id") else "CONFIGURED"),
    }


def _is_candidate_id(value: Any) -> bool:
    return (
        isinstance(value, str)
        and value.startswith("cand_")
        and len(value) == 37
        and all(character in "0123456789abcdef" for character in value[5:])
    )


def _text(value: Any, name: str, maximum: int, required: bool = False) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be text")
    parsed = value.strip()
    if required and not parsed:
        raise ValueError(f"{name} must not be empty")
    if len(parsed) > maximum:
        raise ValueError(f"{name} must be at most {maximum} characters")
    return parsed


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
