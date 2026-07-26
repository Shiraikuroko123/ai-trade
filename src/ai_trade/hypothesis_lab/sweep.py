from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import math
import re
from typing import Any, Mapping, Sequence
from uuid import uuid4

from ..backtest import BacktestEngine
from ..config import AppConfig
from ..data.evidence_io import atomic_create_json, evidence_store_lock
from ..data.market import MarketData
from ..json_utils import load_unique_json
from ..models import RiskSettings, StrategySettings
from ..strategy_lab import StrategyLabEngine
from ..strategy_lab.schema import (
    PARAMETERS,
    apply_changes,
    clamp_parameter,
)
from .schema import FINGERPRINT, json_fingerprint


SWEEP_SCHEMA_VERSION = 1
SWEEP_ENGINE_VERSION = 1
SWEEP_ID = re.compile(r"sweep_[0-9a-f]{32}\Z")
MAX_SWEEP_RECORD_BYTES = 512 * 1024
MAX_SWEEPS_PER_OWNER = 100
MAX_POINTS_PER_PARAMETER = 8
MAX_TOTAL_VARIANTS = 200
OBJECTIVES = ("sharpe", "max_drawdown", "turnover")

SWEEP_SAFETY = {
    "research_only": True,
    "exploratory_not_confirmatory": True,
    "may_register_hypothesis": False,
    "may_create_candidate": False,
    "may_approve": False,
    "may_activate": False,
    "may_trade": False,
}

_DISCLOSURE = (
    "One-at-a-time exploratory neighborhood sweep. Rankings are inflated by "
    "multiple comparisons and ignore parameter interactions; no variant is a "
    "validated improvement. A selected direction must still be pre-registered "
    "as a hypothesis and survive the deterministic experiment runner's "
    "holdout, cost-stress, sensitivity, and later-snapshot replication before "
    "any human materialization, validation, and approval."
)

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "engine_version",
        "sweep_id",
        "owner",
        "created_at",
        "objective",
        "points_per_parameter",
        "baseline",
        "evidence",
        "parameters_swept",
        "variants",
        "ranking",
        "disclosure",
        "sweep_fingerprint",
        "safety",
        "record_fingerprint",
    }
)


class ParameterSweepEngine:
    """Exploratory one-at-a-time parameter sweeps as bounded evidence.

    The sweep reruns the deterministic backtest for allowlisted parameter
    variants around the active Strategy Lab baseline and records every result
    immutably. It is exploration, not confirmation: the record's disclosure
    and safety contract state that no ranking can register a hypothesis,
    create a candidate, or change any authority.
    """

    def __init__(
        self,
        config: AppConfig,
        strategy_lab: StrategyLabEngine | None = None,
    ) -> None:
        self.config = config
        self.strategy_lab = strategy_lab or StrategyLabEngine(config)
        self.root = config.project_root / "state" / "hypothesis_lab"

    def owner_directory(self, owner: str):
        from .store import HypothesisLabStore

        return HypothesisLabStore(self.root).owner_directory(owner)

    def owner_id(self, owner: str) -> str:
        from .store import HypothesisLabStore

        return HypothesisLabStore(self.root).owner_id(owner)

    def execute(
        self,
        owner: str,
        market: MarketData,
        *,
        objective: str = "sharpe",
        parameters: Sequence[str] | None = None,
        points: int = 4,
    ) -> dict[str, Any]:
        if objective not in OBJECTIVES:
            raise ValueError(
                "Sweep objective must be one of: " + ", ".join(OBJECTIVES)
            )
        if (
            isinstance(points, bool)
            or not isinstance(points, int)
            or not 2 <= points <= MAX_POINTS_PER_PARAMETER
        ):
            raise ValueError(
                f"Sweep points must be between 2 and {MAX_POINTS_PER_PARAMETER}"
            )

        metadata_before = _metadata(market)
        snapshot_fingerprint = json_fingerprint(metadata_before)
        blueprint = self.strategy_lab.local_proposal_blueprint(owner, "balanced")
        baseline_snapshot = blueprint["baseline"]
        context_fingerprint = self.strategy_lab.config_context_fingerprint()

        specs = _selected_specs(parameters, baseline_snapshot)
        estimated = sum(points for _ in specs)
        if estimated > MAX_TOTAL_VARIANTS:
            raise ValueError(
                f"Sweep would evaluate {estimated} variants; the bound is "
                f"{MAX_TOTAL_VARIANTS}. Select fewer parameters or points."
            )

        baseline_metrics = self._run(market, baseline_snapshot)
        variants: list[dict[str, Any]] = []
        swept: list[str] = []
        for spec in specs:
            original = baseline_snapshot[spec.scope][spec.name]
            values = _candidate_values(spec, float(original), points)
            produced = 0
            for value in values:
                try:
                    candidate_snapshot, effective = apply_changes(
                        self.config,
                        baseline_snapshot,
                        {spec.scope: {spec.name: value}},
                    )
                except ValueError:
                    continue
                metrics = self._run(market, candidate_snapshot)
                variants.append(
                    {
                        "parameter": spec.key,
                        "value": float(value),
                        "baseline_value": float(original),
                        "metrics": metrics,
                        "objective_delta": _objective_delta(
                            objective, baseline_metrics, metrics
                        ),
                        "changes": effective,
                    }
                )
                produced += 1
            if produced:
                swept.append(spec.key)
        if not variants:
            raise RuntimeError(
                "Sweep could not construct any valid parameter variant"
            )

        metadata_after = _metadata(market)
        if json_fingerprint(metadata_after) != snapshot_fingerprint:
            raise RuntimeError("Market snapshot changed during the parameter sweep")

        ranking = sorted(
            (
                {
                    "parameter": item["parameter"],
                    "value": item["value"],
                    "objective_delta": item["objective_delta"],
                }
                for item in variants
            ),
            key=lambda item: item["objective_delta"],
            reverse=True,
        )[: min(20, len(variants))]

        as_of = str(
            metadata_before.get("latest_common_session")
            or metadata_before.get("latest_benchmark_session")
            or market.latest_date().isoformat()
        )
        sweep_fingerprint = json_fingerprint(
            {
                "objective": objective,
                "points": points,
                "parameters": sorted(swept),
                "baseline_fingerprint": blueprint["parent_fingerprint"],
                "snapshot_fingerprint": snapshot_fingerprint,
                "config_context_fingerprint": context_fingerprint,
                "engine_version": SWEEP_ENGINE_VERSION,
            }
        )
        record = {
            "schema_version": SWEEP_SCHEMA_VERSION,
            "engine_version": SWEEP_ENGINE_VERSION,
            "sweep_id": f"sweep_{uuid4().hex}",
            "owner": self.owner_id(owner),
            "created_at": _utc_now(),
            "objective": objective,
            "points_per_parameter": points,
            "baseline": {
                "settings_fingerprint": blueprint["parent_fingerprint"],
                "config_context_fingerprint": context_fingerprint,
                "metrics": baseline_metrics,
            },
            "evidence": {
                "snapshot_id": "market_" + snapshot_fingerprint[:32],
                "as_of": as_of,
                "provider": str(metadata_before.get("provider") or "local-cache"),
                "fingerprint": snapshot_fingerprint,
            },
            "parameters_swept": swept,
            "variants": variants,
            "ranking": ranking,
            "disclosure": _DISCLOSURE,
            "sweep_fingerprint": sweep_fingerprint,
            "safety": dict(SWEEP_SAFETY),
        }
        record["record_fingerprint"] = _record_fingerprint(record)
        _validate_sweep(record)
        return self._publish(owner, record)

    def list(self, owner: str, *, limit: int = 50) -> dict[str, Any]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError("Sweep list limit must be between 1 and 100")
        records = self._records(owner)
        ordered = sorted(
            records,
            key=lambda item: (str(item["created_at"]), str(item["sweep_id"])),
            reverse=True,
        )
        return {
            "schema_version": 1,
            "sweeps": [
                {
                    "sweep_id": item["sweep_id"],
                    "created_at": item["created_at"],
                    "objective": item["objective"],
                    "as_of": item["evidence"]["as_of"],
                    "parameters": len(item["parameters_swept"]),
                    "variants": len(item["variants"]),
                    "top": item["ranking"][:3],
                }
                for item in ordered[:limit]
            ],
            "summary": {
                "total": len(ordered),
                "returned": min(limit, len(ordered)),
                "limit": limit,
                "maximum": MAX_SWEEPS_PER_OWNER,
                "truncated": len(ordered) > limit,
            },
            "safety": dict(SWEEP_SAFETY),
        }

    def get(self, owner: str, sweep_id: str) -> dict[str, Any]:
        if not isinstance(sweep_id, str) or SWEEP_ID.fullmatch(sweep_id) is None:
            raise ValueError("Invalid sweep id")
        path = self.owner_directory(owner) / "sweeps" / f"{sweep_id}.json"
        if path.is_symlink() or not path.is_file():
            raise KeyError(sweep_id)
        record = _read_sweep(path)
        if record.get("sweep_id") != sweep_id:
            raise RuntimeError("Sweep id does not match its file name")
        if record.get("owner") != self.owner_id(owner):
            raise RuntimeError("Sweep owner binding is invalid")
        return record

    def _publish(self, owner: str, record: dict[str, Any]) -> dict[str, Any]:
        target = (
            self.owner_directory(owner) / "sweeps" / f"{record['sweep_id']}.json"
        )
        with evidence_store_lock(self.root, "Hypothesis lab"):
            records = self._records(owner)
            for existing in records:
                if existing["sweep_fingerprint"] == record["sweep_fingerprint"]:
                    result = dict(existing)
                    result["reused"] = True
                    return result
            if len(records) >= MAX_SWEEPS_PER_OWNER:
                raise RuntimeError(
                    "Sweep owner capacity reached "
                    f"({MAX_SWEEPS_PER_OWNER}); archive the owner directory first"
                )
            atomic_create_json(
                self.root,
                target,
                record,
                label="parameter sweep record",
                maximum_bytes=MAX_SWEEP_RECORD_BYTES,
            )
        stored = self.get(owner, record["sweep_id"])
        stored["reused"] = False
        return stored

    def _records(self, owner: str) -> list[dict[str, Any]]:
        directory = self.owner_directory(owner) / "sweeps"
        if not directory.exists():
            return []
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("Sweep owner directory is invalid")
        records: list[dict[str, Any]] = []
        for path in directory.iterdir():
            if (
                path.is_symlink()
                or not path.is_file()
                or path.suffix != ".json"
                or SWEEP_ID.fullmatch(path.stem) is None
            ):
                raise RuntimeError("Unexpected sweep store member")
            record = _read_sweep(path)
            if record.get("sweep_id") != path.stem:
                raise RuntimeError("Sweep id does not match its file name")
            if record.get("owner") != self.owner_id(owner):
                raise RuntimeError("Sweep owner binding is invalid")
            records.append(record)
            if len(records) > MAX_SWEEPS_PER_OWNER:
                raise RuntimeError("Sweep store exceeds its capacity")
        return records

    def _run(
        self, market: MarketData, snapshot: Mapping[str, Mapping[str, Any]]
    ) -> dict[str, float]:
        try:
            strategy = StrategySettings(**dict(snapshot["strategy"]))
            risk = RiskSettings(**dict(snapshot["risk"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Sweep settings snapshot is invalid") from exc
        run_config = replace(self.config, strategy=strategy, risk=risk)
        result = BacktestEngine(run_config, market, strategy).run()
        output: dict[str, float] = {}
        for field in (
            "total_return",
            "cagr",
            "sharpe",
            "max_drawdown",
            "turnover",
            "transaction_costs",
        ):
            value = result.metrics.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RuntimeError(f"Sweep metric {field} is unavailable")
            parsed = float(value)
            if not math.isfinite(parsed):
                raise RuntimeError(f"Sweep metric {field} is not finite")
            output[field] = parsed
        return output


def _selected_specs(
    parameters: Sequence[str] | None,
    baseline: Mapping[str, Mapping[str, Any]],
):
    numeric = [
        spec
        for spec in PARAMETERS
        if spec.type in {"integer", "number"}
        and spec.scope in baseline
        and spec.name in baseline[spec.scope]
    ]
    if parameters is None:
        return tuple(numeric)
    if not isinstance(parameters, (list, tuple)) or not parameters:
        raise ValueError("Sweep parameters must be a non-empty list")
    if len(set(parameters)) != len(parameters):
        raise ValueError("Sweep parameters must be unique")
    by_key = {spec.key: spec for spec in numeric}
    selected = []
    for key in parameters:
        spec = by_key.get(str(key))
        if spec is None:
            raise ValueError(f"Parameter is not sweepable: {key}")
        selected.append(spec)
    return tuple(selected)


def _candidate_values(spec, original: float, points: int) -> list[float]:
    step = float(spec.step or 0.0)
    if step <= 0:
        step = max(abs(original) * 0.05, 0.01)
    magnitude = max(step, abs(original) * 0.05)
    offsets: list[float] = []
    half = points // 2
    for index in range(1, half + 1):
        offsets.extend((-index * magnitude, index * magnitude))
    if points % 2 == 1:
        offsets.append((half + 1) * magnitude)
    values: list[float] = []
    seen: set[float] = set()
    for offset in offsets[:points]:
        value = clamp_parameter(spec, original + offset)
        numeric = float(value)
        if numeric == float(original) or numeric in seen:
            continue
        seen.add(numeric)
        values.append(value)
    return values


def _objective_delta(
    objective: str,
    baseline: Mapping[str, float],
    candidate: Mapping[str, float],
) -> float:
    if objective == "sharpe":
        return candidate["sharpe"] - baseline["sharpe"]
    if objective == "max_drawdown":
        return candidate["max_drawdown"] - baseline["max_drawdown"]
    return baseline["turnover"] - candidate["turnover"]


def _validate_sweep(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != _TOP_LEVEL_FIELDS:
        raise ValueError("Sweep top-level schema fields are invalid")
    if value.get("schema_version") != SWEEP_SCHEMA_VERSION:
        raise ValueError("Sweep schema version is invalid")
    if value.get("engine_version") != SWEEP_ENGINE_VERSION:
        raise ValueError("Sweep engine version is invalid")
    if (
        not isinstance(value.get("sweep_id"), str)
        or SWEEP_ID.fullmatch(value["sweep_id"]) is None
    ):
        raise ValueError("Sweep id is invalid")
    for field in ("owner", "sweep_fingerprint", "record_fingerprint"):
        item = value.get(field)
        if not isinstance(item, str) or FINGERPRINT.fullmatch(item) is None:
            raise ValueError(f"Sweep {field} is invalid")
    _timestamp(value.get("created_at"))
    if value.get("objective") not in OBJECTIVES:
        raise ValueError("Sweep objective is invalid")
    points = value.get("points_per_parameter")
    if (
        isinstance(points, bool)
        or not isinstance(points, int)
        or not 2 <= points <= MAX_POINTS_PER_PARAMETER
    ):
        raise ValueError("Sweep points_per_parameter is invalid")
    baseline = value.get("baseline")
    if (
        not isinstance(baseline, Mapping)
        or set(baseline)
        != {"settings_fingerprint", "config_context_fingerprint", "metrics"}
    ):
        raise ValueError("Sweep baseline is invalid")
    _metrics_block(baseline.get("metrics"))
    evidence = value.get("evidence")
    if (
        not isinstance(evidence, Mapping)
        or set(evidence) != {"snapshot_id", "as_of", "provider", "fingerprint"}
        or not isinstance(evidence.get("fingerprint"), str)
        or FINGERPRINT.fullmatch(evidence["fingerprint"]) is None
    ):
        raise ValueError("Sweep evidence is invalid")
    _iso_date(evidence.get("as_of"))
    swept = value.get("parameters_swept")
    if (
        not isinstance(swept, list)
        or not 1 <= len(swept) <= 40
        or any(not isinstance(item, str) or not item for item in swept)
        or len(set(swept)) != len(swept)
    ):
        raise ValueError("Sweep parameter list is invalid")
    variants = value.get("variants")
    if (
        not isinstance(variants, list)
        or not 1 <= len(variants) <= MAX_TOTAL_VARIANTS
    ):
        raise ValueError("Sweep variants are invalid")
    for item in variants:
        if not isinstance(item, Mapping) or set(item) != {
            "parameter",
            "value",
            "baseline_value",
            "metrics",
            "objective_delta",
            "changes",
        }:
            raise ValueError("Sweep variant schema fields are invalid")
        if item.get("parameter") not in swept:
            raise ValueError("Sweep variant parameter is unknown")
        _finite(item.get("value"))
        _finite(item.get("baseline_value"))
        _finite(item.get("objective_delta"))
        _metrics_block(item.get("metrics"))
    ranking = value.get("ranking")
    if not isinstance(ranking, list) or not 1 <= len(ranking) <= 20:
        raise ValueError("Sweep ranking is invalid")
    deltas = [
        _finite(item.get("objective_delta"))
        for item in ranking
        if isinstance(item, Mapping)
    ]
    if len(deltas) != len(ranking) or deltas != sorted(deltas, reverse=True):
        raise ValueError("Sweep ranking must be sorted by objective delta")
    if value.get("disclosure") != _DISCLOSURE:
        raise ValueError("Sweep disclosure is invalid")
    if value.get("safety") != SWEEP_SAFETY:
        raise ValueError("Sweep safety contract is invalid")
    if value["record_fingerprint"] != _record_fingerprint(value):
        raise ValueError("Sweep record fingerprint does not match content")


def _record_fingerprint(value: Mapping[str, Any]) -> str:
    body = {
        key: item for key, item in value.items() if key != "record_fingerprint"
    }
    body.pop("reused", None)
    return json_fingerprint(body)


def _read_sweep(path) -> dict[str, Any]:
    try:
        value = load_unique_json(path, max_bytes=MAX_SWEEP_RECORD_BYTES)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Invalid sweep record: {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise RuntimeError("Sweep record must be an object")
    try:
        _validate_sweep(value)
    except ValueError as exc:
        raise RuntimeError(f"Invalid sweep record: {path}: {exc}") from exc
    return value


def _metrics_block(value: Any) -> None:
    fields = {
        "total_return",
        "cagr",
        "sharpe",
        "max_drawdown",
        "turnover",
        "transaction_costs",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("Sweep metrics block is invalid")
    for field in fields:
        _finite(value.get(field))


def _finite(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Sweep numeric value is invalid")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Sweep numeric value must be finite")
    return parsed


def _timestamp(value: Any) -> None:
    if not isinstance(value, str):
        raise ValueError("Sweep timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Sweep timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("Sweep timestamp must include a timezone")


def _iso_date(value: Any) -> None:
    if not isinstance(value, str):
        raise ValueError("Sweep date is invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("Sweep date must use YYYY-MM-DD") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError("Sweep date must use YYYY-MM-DD")


def _metadata(market: MarketData) -> dict[str, Any]:
    value = market.snapshot_metadata()
    if not isinstance(value, Mapping):
        raise RuntimeError("Market snapshot metadata must be an object")
    return dict(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "MAX_POINTS_PER_PARAMETER",
    "MAX_SWEEPS_PER_OWNER",
    "MAX_TOTAL_VARIANTS",
    "OBJECTIVES",
    "ParameterSweepEngine",
    "SWEEP_SAFETY",
]
