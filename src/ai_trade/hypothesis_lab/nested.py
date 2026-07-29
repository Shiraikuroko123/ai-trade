"""Nested walk-forward confirmatory tuning protocol.

Upgrades the exploratory one-at-a-time sweep into fold-honest evidence:
every outer fold selects one parameter candidate strictly inside its own
training region (on inner validation windows), and only the untouched,
embargo-separated test fold measures that choice out-of-fold. The record
stays research evidence - it registers no hypothesis, creates no candidate,
and changes no authority.

Design references (documented in docs/HYPOTHESIS_LAB.md): the three-role
separation of train / selection / untouched test used by rolling protocols
in Qlib-style research stacks, purge-and-embargo gaps, and a deterministic
one-standard-error fallback toward the baseline in the spirit of Masters'
rule. Everything is pure standard library and fully deterministic.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timezone
import math
import re
from typing import Any, List, Mapping, Sequence
from uuid import uuid4

from ..backtest import BacktestEngine
from ..config import AppConfig
from ..data.evidence_io import atomic_create_json, evidence_store_lock
from ..data.market import MarketData
from ..json_utils import load_unique_json
from ..models import RiskSettings, StrategySettings
from ..numeric import sample_standard_deviation
from ..strategy_lab import StrategyLabEngine
from ..strategy_lab.schema import apply_changes
from .schema import FINGERPRINT, json_fingerprint

# The candidate grid reuses the exploratory sweep's own neighborhood
# helpers so both record kinds describe the same parameter space for any
# shared --points value (this engine caps points at 6 where the sweep
# allows 8); both helpers are deterministic and side-effect free.
from .sweep import _candidate_values, _selected_specs


NESTED_SCHEMA_VERSION = 1
NESTED_ENGINE_VERSION = 1
NESTED_ID = re.compile(r"nwf_[0-9a-f]{32}\Z")
MAX_NESTED_RECORD_BYTES = 512 * 1024
MAX_NESTED_PER_OWNER = 100
MAX_POINTS_PER_PARAMETER = 6
MAX_TOTAL_BACKTESTS = 400
MIN_OUTER_FOLDS, MAX_OUTER_FOLDS = 2, 6
MIN_INNER_FOLDS, MAX_INNER_FOLDS = 1, 4
MIN_EMBARGO, MAX_EMBARGO = 0, 21
MIN_TEST_SESSIONS = 30
MIN_TRAIN_SESSIONS = 60
MIN_VALIDATION_SESSIONS = 20
VALIDATION_TAIL_FRACTION = 0.3
OBJECTIVES = ("sharpe", "max_drawdown", "turnover")
SELECTION_RULE = "one_standard_error"

NESTED_SAFETY = {
    "research_only": True,
    "selection_inside_training_only": True,
    "may_register_hypothesis": False,
    "may_create_candidate": False,
    "may_approve": False,
    "may_activate": False,
    "may_trade": False,
}

_DISCLOSURE = (
    "Nested walk-forward tuning evidence. Every parameter choice is made "
    "inside its own fold's training region and measured once on an untouched, "
    "embargo-separated test fold, so the out-of-fold deltas are honest; they "
    "are still estimates from one historical path. No ranking here registers "
    "a hypothesis, creates a candidate, or changes any approval authority: a "
    "selected direction must still pass hypothesis registration, the "
    "deterministic experiment runner, and human materialization gates."
)

_PROTOCOL = {
    "roles": (
        "Each outer fold separates three roles: earlier sessions form the "
        "anchored training region, validation windows on that region's tail "
        "select one candidate, and the fold's own untouched test block "
        "measures it out-of-fold. Later folds' training regions include "
        "earlier folds' test sessions, as in any anchored walk-forward."
    ),
    "selection": (
        "Deterministic argmax of the mean validation objective across inner "
        "windows; a non-baseline winner whose margin over the baseline mean "
        "is within one standard error (std/sqrt(windows)) falls back to the "
        "baseline. Ties keep the earliest candidate, and the baseline is "
        "always candidate zero. With a single inner window the fallback "
        "margin is zero, so any positive margin wins."
    ),
    "embargo": (
        "A configurable number of sessions between the training region and "
        "each test fold is discarded so open positions and trailing "
        "indicators from the selection era cannot leak into measurement."
    ),
}

_TOP_LEVEL_FIELDS = frozenset(
    {
        "schema_version",
        "engine_version",
        "nested_id",
        "owner",
        "created_at",
        "objective",
        "points_per_parameter",
        "outer_folds",
        "inner_folds",
        "embargo_sessions",
        "selection_rule",
        "protocol",
        "baseline",
        "evidence",
        "parameters_swept",
        "candidate_count",
        "folds",
        "aggregate",
        "disclosure",
        "nested_fingerprint",
        "safety",
        "record_fingerprint",
    }
)

_METRIC_FIELDS = frozenset(
    {
        "total_return",
        "cagr",
        "sharpe",
        "max_drawdown",
        "turnover",
        "transaction_costs",
    }
)


class NestedWalkForwardEngine:
    """Fold-honest confirmatory tuning over the allowlisted parameter space.

    The engine reruns the deterministic backtest on sub-windows only: inner
    validation windows drive a per-fold selection, the embargo-separated
    test fold measures it. Records are immutable, fingerprinted, and
    owner-isolated exactly like the exploratory sweep records they upgrade.
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
        points: int = 2,
        outer_folds: int = 4,
        inner_folds: int = 2,
        embargo_sessions: int = 5,
    ) -> dict[str, Any]:
        if objective not in OBJECTIVES:
            raise ValueError(
                "Nested walk-forward objective must be one of: "
                + ", ".join(OBJECTIVES)
            )
        _bounded_int("points", points, 2, MAX_POINTS_PER_PARAMETER)
        _bounded_int("outer_folds", outer_folds, MIN_OUTER_FOLDS, MAX_OUTER_FOLDS)
        _bounded_int("inner_folds", inner_folds, MIN_INNER_FOLDS, MAX_INNER_FOLDS)
        _bounded_int(
            "embargo_sessions", embargo_sessions, MIN_EMBARGO, MAX_EMBARGO
        )

        metadata_before = _metadata(market)
        snapshot_fingerprint = json_fingerprint(metadata_before)
        blueprint = self.strategy_lab.local_proposal_blueprint(owner, "balanced")
        baseline_snapshot = blueprint["baseline"]
        context_fingerprint = self.strategy_lab.config_context_fingerprint()

        specs = _selected_specs(parameters, baseline_snapshot)
        candidates = self._candidates(baseline_snapshot, specs, points)
        swept = sorted({item["parameter"] for item in candidates[1:]})
        if not swept:
            raise RuntimeError(
                "Nested walk-forward could not construct any parameter variant"
            )
        estimated = outer_folds * (len(candidates) * inner_folds + 2)
        if estimated > MAX_TOTAL_BACKTESTS:
            raise ValueError(
                f"Nested walk-forward would run {estimated} window backtests; "
                f"the bound is {MAX_TOTAL_BACKTESTS}. Select fewer parameters, "
                "points, folds, or inner windows."
            )

        calendar = self._calendar(market)
        layout = _fold_layout(
            len(calendar), outer_folds, inner_folds, embargo_sessions
        )

        run_cache: dict[tuple[int, int, int], dict[str, float]] = {}

        def run_window(candidate_index: int, start_index: int, end_index: int):
            key = (candidate_index, start_index, end_index)
            if key not in run_cache:
                run_cache[key] = self._run(
                    market,
                    candidates[candidate_index]["snapshot"],
                    calendar[start_index],
                    calendar[end_index],
                )
            return run_cache[key]

        folds: List[dict[str, Any]] = []
        deltas: List[float] = []
        non_baseline = 0
        regret = 0
        positive = 0
        selection_counts: dict[tuple[str, float], int] = {}
        for fold_number, fold in enumerate(layout, start=1):
            scored: List[dict[str, Any]] = []
            for index, candidate in enumerate(candidates):
                scores = [
                    _objective_score(
                        objective, run_window(index, window[0], window[1])
                    )
                    for window in fold["validation"]
                ]
                mean_score = math.fsum(scores) / len(scores)
                spread = (
                    sample_standard_deviation(scores) if len(scores) > 1 else 0.0
                )
                scored.append(
                    {
                        "index": index,
                        "parameter": candidate["parameter"],
                        "value": candidate["value"],
                        "mean": mean_score,
                        "std": spread,
                    }
                )
            baseline_mean = scored[0]["mean"]
            winner = max(scored, key=lambda item: (item["mean"], -item["index"]))
            if winner["index"] != 0:
                margin = winner["mean"] - baseline_mean
                standard_error = winner["std"] / math.sqrt(len(fold["validation"]))
                if margin <= standard_error:
                    winner = scored[0]
            baseline_test = run_window(0, fold["test"][0], fold["test"][1])
            selected_test = run_window(
                winner["index"], fold["test"][0], fold["test"][1]
            )
            delta = _objective_score(objective, selected_test) - _objective_score(
                objective, baseline_test
            )
            deltas.append(delta)
            if winner["index"] != 0:
                non_baseline += 1
                if delta < 0:
                    regret += 1
                key = (str(winner["parameter"]), float(winner["value"]))
                selection_counts[key] = selection_counts.get(key, 0) + 1
            if delta > 0:
                positive += 1
            ranking = sorted(
                scored, key=lambda item: (-item["mean"], item["index"])
            )[:5]
            folds.append(
                {
                    "fold": fold_number,
                    "train_start": calendar[fold["train"][0]].isoformat(),
                    "train_end": calendar[fold["train"][1]].isoformat(),
                    "validation_windows": [
                        {
                            "start": calendar[window[0]].isoformat(),
                            "end": calendar[window[1]].isoformat(),
                        }
                        for window in fold["validation"]
                    ],
                    "test_start": calendar[fold["test"][0]].isoformat(),
                    "test_end": calendar[fold["test"][1]].isoformat(),
                    "selected": {
                        "parameter": winner["parameter"],
                        "value": winner["value"],
                        "is_baseline": winner["index"] == 0,
                        "mean_validation_score": winner["mean"],
                        "validation_score_std": winner["std"],
                    },
                    "baseline_mean_validation_score": baseline_mean,
                    "validation_top": [
                        {
                            "parameter": item["parameter"],
                            "value": item["value"],
                            "mean_validation_score": item["mean"],
                        }
                        for item in ranking
                    ],
                    "test_metrics": dict(selected_test),
                    "baseline_test_metrics": dict(baseline_test),
                    "objective_delta": delta,
                }
            )

        metadata_after = _metadata(market)
        if json_fingerprint(metadata_after) != snapshot_fingerprint:
            raise RuntimeError(
                "Market snapshot changed during the nested walk-forward"
            )

        as_of = str(
            metadata_before.get("latest_common_session")
            or metadata_before.get("latest_benchmark_session")
            or market.latest_date().isoformat()
        )
        nested_fingerprint = json_fingerprint(
            {
                "objective": objective,
                "points": points,
                "outer_folds": outer_folds,
                "inner_folds": inner_folds,
                "embargo_sessions": embargo_sessions,
                "selection_rule": SELECTION_RULE,
                "parameters": swept,
                "baseline_fingerprint": blueprint["parent_fingerprint"],
                "snapshot_fingerprint": snapshot_fingerprint,
                "config_context_fingerprint": context_fingerprint,
                "engine_version": NESTED_ENGINE_VERSION,
            }
        )
        record = {
            "schema_version": NESTED_SCHEMA_VERSION,
            "engine_version": NESTED_ENGINE_VERSION,
            "nested_id": f"nwf_{uuid4().hex}",
            "owner": self.owner_id(owner),
            "created_at": _utc_now(),
            "objective": objective,
            "points_per_parameter": points,
            "outer_folds": outer_folds,
            "inner_folds": inner_folds,
            "embargo_sessions": embargo_sessions,
            "selection_rule": SELECTION_RULE,
            "protocol": dict(_PROTOCOL),
            "baseline": {
                "settings_fingerprint": blueprint["parent_fingerprint"],
                "config_context_fingerprint": context_fingerprint,
            },
            "evidence": {
                "snapshot_id": "market_" + snapshot_fingerprint[:32],
                "as_of": as_of,
                "provider": str(metadata_before.get("provider") or "local-cache"),
                "fingerprint": snapshot_fingerprint,
            },
            "parameters_swept": swept,
            "candidate_count": len(candidates),
            "folds": folds,
            "aggregate": {
                "folds": len(folds),
                "non_baseline_selections": non_baseline,
                "positive_delta_folds": positive,
                "mean_objective_delta": math.fsum(deltas) / len(deltas),
                "selection_regret_share": (
                    regret / non_baseline if non_baseline else 0.0
                ),
                "selection_counts": [
                    {
                        "parameter": parameter,
                        "value": value,
                        "folds_selected": count,
                    }
                    for (parameter, value), count in sorted(
                        selection_counts.items()
                    )
                ],
            },
            "disclosure": _DISCLOSURE,
            "nested_fingerprint": nested_fingerprint,
            "safety": dict(NESTED_SAFETY),
        }
        record["record_fingerprint"] = _record_fingerprint(record)
        _validate_nested(record)
        return self._publish(owner, record)

    def list(self, owner: str, *, limit: int = 50) -> dict[str, Any]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 100
        ):
            raise ValueError(
                "Nested walk-forward list limit must be between 1 and 100"
            )
        records = self._records(owner)
        ordered = sorted(
            records,
            key=lambda item: (str(item["created_at"]), str(item["nested_id"])),
            reverse=True,
        )
        return {
            "schema_version": 1,
            "nested_walk_forwards": [
                {
                    "nested_id": item["nested_id"],
                    "created_at": item["created_at"],
                    "objective": item["objective"],
                    "as_of": item["evidence"]["as_of"],
                    "outer_folds": item["outer_folds"],
                    "parameters": len(item["parameters_swept"]),
                    "mean_objective_delta": item["aggregate"][
                        "mean_objective_delta"
                    ],
                    "non_baseline_selections": item["aggregate"][
                        "non_baseline_selections"
                    ],
                    "selection_regret_share": item["aggregate"][
                        "selection_regret_share"
                    ],
                }
                for item in ordered[:limit]
            ],
            "summary": {
                "total": len(ordered),
                "returned": min(limit, len(ordered)),
                "limit": limit,
                "maximum": MAX_NESTED_PER_OWNER,
                "truncated": len(ordered) > limit,
            },
            "safety": dict(NESTED_SAFETY),
        }

    def get(self, owner: str, nested_id: str) -> dict[str, Any]:
        if not isinstance(nested_id, str) or NESTED_ID.fullmatch(nested_id) is None:
            raise ValueError("Invalid nested walk-forward id")
        path = self.owner_directory(owner) / "nested" / f"{nested_id}.json"
        if path.is_symlink() or not path.is_file():
            raise KeyError(nested_id)
        record = _read_nested(path)
        if record.get("nested_id") != nested_id:
            raise RuntimeError(
                "Nested walk-forward id does not match its file name"
            )
        if record.get("owner") != self.owner_id(owner):
            raise RuntimeError("Nested walk-forward owner binding is invalid")
        return record

    def _candidates(
        self,
        baseline: Mapping[str, Mapping[str, Any]],
        specs,
        points: int,
    ) -> List[dict[str, Any]]:
        candidates: List[dict[str, Any]] = [
            {
                "parameter": None,
                "value": None,
                "snapshot": baseline,
            }
        ]
        for spec in specs:
            original = baseline[spec.scope][spec.name]
            for value in _candidate_values(spec, float(original), points):
                try:
                    snapshot, _effective = apply_changes(
                        self.config,
                        baseline,
                        {spec.scope: {spec.name: value}},
                    )
                except ValueError:
                    continue
                candidates.append(
                    {
                        "parameter": spec.key,
                        "value": float(value),
                        "snapshot": snapshot,
                    }
                )
        return candidates

    def _calendar(self, market: MarketData) -> List[date]:
        raw = self.config.raw.get("backtest", {})
        start = _config_date(raw.get("start"), date.min)
        end = _config_date(raw.get("end"), date.max)
        calendar = [day for day in market.calendar if start <= day <= end]
        if not calendar:
            raise RuntimeError(
                "Nested walk-forward found no sessions in the backtest window"
            )
        return calendar

    def _publish(self, owner: str, record: dict[str, Any]) -> dict[str, Any]:
        target = (
            self.owner_directory(owner) / "nested" / f"{record['nested_id']}.json"
        )
        with evidence_store_lock(self.root, "Hypothesis lab"):
            records = self._records(owner)
            for existing in records:
                if existing["nested_fingerprint"] == record["nested_fingerprint"]:
                    result = dict(existing)
                    result["reused"] = True
                    return result
            if len(records) >= MAX_NESTED_PER_OWNER:
                raise RuntimeError(
                    "Nested walk-forward owner capacity reached "
                    f"({MAX_NESTED_PER_OWNER}); archive the owner directory first"
                )
            atomic_create_json(
                self.root,
                target,
                record,
                label="nested walk-forward record",
                maximum_bytes=MAX_NESTED_RECORD_BYTES,
            )
        stored = self.get(owner, record["nested_id"])
        stored["reused"] = False
        return stored

    def _records(self, owner: str) -> List[dict[str, Any]]:
        directory = self.owner_directory(owner) / "nested"
        if not directory.exists():
            return []
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("Nested walk-forward owner directory is invalid")
        records: List[dict[str, Any]] = []
        for path in directory.iterdir():
            if (
                path.is_symlink()
                or not path.is_file()
                or path.suffix != ".json"
                or NESTED_ID.fullmatch(path.stem) is None
            ):
                raise RuntimeError("Unexpected nested walk-forward store member")
            record = _read_nested(path)
            if record.get("nested_id") != path.stem:
                raise RuntimeError(
                    "Nested walk-forward id does not match its file name"
                )
            if record.get("owner") != self.owner_id(owner):
                raise RuntimeError(
                    "Nested walk-forward owner binding is invalid"
                )
            records.append(record)
            if len(records) > MAX_NESTED_PER_OWNER:
                raise RuntimeError(
                    "Nested walk-forward store exceeds its capacity"
                )
        return records

    def _run(
        self,
        market: MarketData,
        snapshot: Mapping[str, Mapping[str, Any]],
        start: date,
        end: date,
    ) -> dict[str, float]:
        try:
            strategy = StrategySettings(**dict(snapshot["strategy"]))
            risk = RiskSettings(**dict(snapshot["risk"]))
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                "Nested walk-forward settings snapshot is invalid"
            ) from exc
        run_config = replace(self.config, strategy=strategy, risk=risk)
        result = BacktestEngine(run_config, market, strategy).run(
            start=start, end=end
        )
        output: dict[str, float] = {}
        for field in sorted(_METRIC_FIELDS):
            value = result.metrics.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise RuntimeError(
                    f"Nested walk-forward metric {field} is unavailable"
                )
            parsed = float(value)
            if not math.isfinite(parsed):
                raise RuntimeError(
                    f"Nested walk-forward metric {field} is not finite"
                )
            output[field] = parsed
        return output


def _fold_layout(
    sessions: int,
    outer_folds: int,
    inner_folds: int,
    embargo_sessions: int,
) -> list[dict[str, Any]]:
    """Partition the calendar into an initial block plus outer test folds.

    Index layout per fold (all inclusive index pairs into the calendar):
    train = [0, test_start - embargo - 1]; validation windows are the tail
    of the train region; test = the fold's own block. Fails closed when any
    region would be shorter than its minimum.
    """
    blocks = outer_folds + 1
    base, remainder = divmod(sessions, blocks)
    sizes = [base + (1 if index < remainder else 0) for index in range(blocks)]
    if min(sizes[1:]) < MIN_TEST_SESSIONS:
        raise ValueError(
            "Nested walk-forward needs at least "
            f"{MIN_TEST_SESSIONS} sessions per test fold; "
            f"{sessions} sessions across {outer_folds} folds is too short"
        )
    layout: list[dict[str, Any]] = []
    cursor = sizes[0]
    for fold_index in range(1, blocks):
        test_start = cursor
        test_end = cursor + sizes[fold_index] - 1
        cursor = test_end + 1
        train_end = test_start - embargo_sessions - 1
        train_length = train_end + 1
        if train_length < MIN_TRAIN_SESSIONS:
            raise ValueError(
                "Nested walk-forward training region has "
                f"{max(train_length, 0)} sessions before fold "
                f"{fold_index}; at least {MIN_TRAIN_SESSIONS} are required "
                "(reduce folds or the embargo)"
            )
        tail = max(
            MIN_VALIDATION_SESSIONS * inner_folds,
            int(train_length * VALIDATION_TAIL_FRACTION),
        )
        tail = min(tail, train_length)
        if tail < MIN_VALIDATION_SESSIONS * inner_folds:
            raise ValueError(
                "Nested walk-forward validation tail has "
                f"{tail} sessions before fold {fold_index}; at least "
                f"{MIN_VALIDATION_SESSIONS * inner_folds} are required"
            )
        validation_start = train_end - tail + 1
        window_base, window_remainder = divmod(tail, inner_folds)
        windows: list[tuple[int, int]] = []
        window_cursor = validation_start
        for window_index in range(inner_folds):
            length = window_base + (1 if window_index < window_remainder else 0)
            windows.append((window_cursor, window_cursor + length - 1))
            window_cursor += length
        layout.append(
            {
                "train": (0, train_end),
                "validation": windows,
                "test": (test_start, test_end),
            }
        )
    return layout


def _objective_score(objective: str, metrics: Mapping[str, float]) -> float:
    if objective == "sharpe":
        return metrics["sharpe"]
    if objective == "max_drawdown":
        return metrics["max_drawdown"]
    return -metrics["turnover"]


def _bounded_int(name: str, value: Any, minimum: int, maximum: int) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not minimum <= value <= maximum
    ):
        raise ValueError(
            f"Nested walk-forward {name} must be an integer between "
            f"{minimum} and {maximum}"
        )


def _config_date(value: Any, fallback: date) -> date:
    if not isinstance(value, str) or not value:
        return fallback
    try:
        return date.fromisoformat(value)
    except ValueError:
        return fallback


def _validate_nested(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != _TOP_LEVEL_FIELDS:
        raise ValueError("Nested top-level schema fields are invalid")
    if value.get("schema_version") != NESTED_SCHEMA_VERSION:
        raise ValueError("Nested schema version is invalid")
    if value.get("engine_version") != NESTED_ENGINE_VERSION:
        raise ValueError("Nested engine version is invalid")
    if (
        not isinstance(value.get("nested_id"), str)
        or NESTED_ID.fullmatch(value["nested_id"]) is None
    ):
        raise ValueError("Nested id is invalid")
    for field in ("owner", "nested_fingerprint", "record_fingerprint"):
        item = value.get(field)
        if not isinstance(item, str) or FINGERPRINT.fullmatch(item) is None:
            raise ValueError(f"Nested {field} is invalid")
    _timestamp(value.get("created_at"))
    if value.get("objective") not in OBJECTIVES:
        raise ValueError("Nested objective is invalid")
    points = value.get("points_per_parameter")
    if (
        isinstance(points, bool)
        or not isinstance(points, int)
        or not 2 <= points <= MAX_POINTS_PER_PARAMETER
    ):
        raise ValueError("Nested points_per_parameter is invalid")
    for name, minimum, maximum in (
        ("outer_folds", MIN_OUTER_FOLDS, MAX_OUTER_FOLDS),
        ("inner_folds", MIN_INNER_FOLDS, MAX_INNER_FOLDS),
        ("embargo_sessions", MIN_EMBARGO, MAX_EMBARGO),
    ):
        item = value.get(name)
        if (
            isinstance(item, bool)
            or not isinstance(item, int)
            or not minimum <= item <= maximum
        ):
            raise ValueError(f"Nested {name} is invalid")
    if value.get("selection_rule") != SELECTION_RULE:
        raise ValueError("Nested selection rule is invalid")
    if value.get("protocol") != _PROTOCOL:
        raise ValueError("Nested protocol disclosure is invalid")
    baseline = value.get("baseline")
    if (
        not isinstance(baseline, Mapping)
        or set(baseline)
        != {"settings_fingerprint", "config_context_fingerprint"}
    ):
        raise ValueError("Nested baseline is invalid")
    evidence = value.get("evidence")
    if (
        not isinstance(evidence, Mapping)
        or set(evidence) != {"snapshot_id", "as_of", "provider", "fingerprint"}
        or not isinstance(evidence.get("fingerprint"), str)
        or FINGERPRINT.fullmatch(evidence["fingerprint"]) is None
    ):
        raise ValueError("Nested evidence is invalid")
    _iso_date(evidence.get("as_of"))
    swept = value.get("parameters_swept")
    if (
        not isinstance(swept, list)
        or not 1 <= len(swept) <= 40
        or any(not isinstance(item, str) or not item for item in swept)
        or len(set(swept)) != len(swept)
        or swept != sorted(swept)
    ):
        raise ValueError("Nested parameter list is invalid")
    count = value.get("candidate_count")
    if isinstance(count, bool) or not isinstance(count, int) or count < 2:
        raise ValueError("Nested candidate count is invalid")
    folds = value.get("folds")
    outer = value["outer_folds"]
    if not isinstance(folds, list) or len(folds) != outer:
        raise ValueError("Nested folds are invalid")
    fold_fields = {
        "fold",
        "train_start",
        "train_end",
        "validation_windows",
        "test_start",
        "test_end",
        "selected",
        "baseline_mean_validation_score",
        "validation_top",
        "test_metrics",
        "baseline_test_metrics",
        "objective_delta",
    }
    non_baseline = 0
    regret = 0
    positive = 0
    deltas: list[float] = []
    for index, fold in enumerate(folds, start=1):
        if not isinstance(fold, Mapping) or set(fold) != fold_fields:
            raise ValueError("Nested fold schema fields are invalid")
        if fold.get("fold") != index:
            raise ValueError("Nested fold numbering is invalid")
        for field in ("train_start", "train_end", "test_start", "test_end"):
            _iso_date(fold.get(field))
        windows = fold.get("validation_windows")
        if (
            not isinstance(windows, list)
            or len(windows) != value["inner_folds"]
        ):
            raise ValueError("Nested validation windows are invalid")
        for window in windows:
            if not isinstance(window, Mapping) or set(window) != {
                "start",
                "end",
            }:
                raise ValueError("Nested validation window is invalid")
            _iso_date(window.get("start"))
            _iso_date(window.get("end"))
        selected = fold.get("selected")
        if not isinstance(selected, Mapping) or set(selected) != {
            "parameter",
            "value",
            "is_baseline",
            "mean_validation_score",
            "validation_score_std",
        }:
            raise ValueError("Nested selection is invalid")
        if not isinstance(selected.get("is_baseline"), bool):
            raise ValueError("Nested selection baseline flag is invalid")
        if selected["is_baseline"]:
            if selected.get("parameter") is not None or selected.get(
                "value"
            ) is not None:
                raise ValueError("Nested baseline selection must be empty")
        else:
            if not isinstance(selected.get("parameter"), str):
                raise ValueError("Nested selection parameter is invalid")
            _finite(selected.get("value"))
            non_baseline += 1
            if _finite(fold.get("objective_delta")) < 0:
                regret += 1
        _finite(selected.get("mean_validation_score"))
        _finite(selected.get("validation_score_std"))
        _finite(fold.get("baseline_mean_validation_score"))
        top = fold.get("validation_top")
        if not isinstance(top, list) or not 1 <= len(top) <= 5:
            raise ValueError("Nested validation ranking is invalid")
        for item in top:
            if not isinstance(item, Mapping) or set(item) != {
                "parameter",
                "value",
                "mean_validation_score",
            }:
                raise ValueError("Nested validation ranking entry is invalid")
            _finite(item.get("mean_validation_score"))
        _metrics_block(fold.get("test_metrics"))
        _metrics_block(fold.get("baseline_test_metrics"))
        delta = _finite(fold.get("objective_delta"))
        deltas.append(delta)
        if delta > 0:
            positive += 1
    aggregate = value.get("aggregate")
    if not isinstance(aggregate, Mapping) or set(aggregate) != {
        "folds",
        "non_baseline_selections",
        "positive_delta_folds",
        "mean_objective_delta",
        "selection_regret_share",
        "selection_counts",
    }:
        raise ValueError("Nested aggregate is invalid")
    if aggregate.get("folds") != len(folds):
        raise ValueError("Nested aggregate fold count is invalid")
    if aggregate.get("non_baseline_selections") != non_baseline:
        raise ValueError("Nested aggregate selection count is invalid")
    if aggregate.get("positive_delta_folds") != positive:
        raise ValueError("Nested aggregate positive count is invalid")
    expected_mean = math.fsum(deltas) / len(deltas)
    if abs(_finite(aggregate.get("mean_objective_delta")) - expected_mean) > 1e-9:
        raise ValueError("Nested aggregate mean delta is inconsistent")
    expected_regret = regret / non_baseline if non_baseline else 0.0
    if (
        abs(_finite(aggregate.get("selection_regret_share")) - expected_regret)
        > 1e-9
    ):
        raise ValueError("Nested aggregate regret share is inconsistent")
    counts = aggregate.get("selection_counts")
    if not isinstance(counts, list) or len(counts) > 40:
        raise ValueError("Nested selection counts are invalid")
    total_counted = 0
    for item in counts:
        if not isinstance(item, Mapping) or set(item) != {
            "parameter",
            "value",
            "folds_selected",
        }:
            raise ValueError("Nested selection count entry is invalid")
        folds_selected = item.get("folds_selected")
        if (
            isinstance(folds_selected, bool)
            or not isinstance(folds_selected, int)
            or folds_selected < 1
        ):
            raise ValueError("Nested selection count entry is invalid")
        total_counted += folds_selected
    if total_counted != non_baseline:
        raise ValueError("Nested selection counts are inconsistent")
    if value.get("disclosure") != _DISCLOSURE:
        raise ValueError("Nested disclosure is invalid")
    if value.get("safety") != NESTED_SAFETY:
        raise ValueError("Nested safety contract is invalid")
    if value["record_fingerprint"] != _record_fingerprint(value):
        raise ValueError("Nested record fingerprint does not match content")


def _record_fingerprint(value: Mapping[str, Any]) -> str:
    body = {
        key: item for key, item in value.items() if key != "record_fingerprint"
    }
    body.pop("reused", None)
    return json_fingerprint(body)


def _read_nested(path) -> dict[str, Any]:
    try:
        value = load_unique_json(path, max_bytes=MAX_NESTED_RECORD_BYTES)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(
            f"Invalid nested walk-forward record: {path}: {exc}"
        ) from exc
    if not isinstance(value, dict):
        raise RuntimeError("Nested walk-forward record must be an object")
    try:
        _validate_nested(value)
    except ValueError as exc:
        raise RuntimeError(
            f"Invalid nested walk-forward record: {path}: {exc}"
        ) from exc
    return value


def _metrics_block(value: Any) -> None:
    if not isinstance(value, Mapping) or set(value) != _METRIC_FIELDS:
        raise ValueError("Nested metrics block is invalid")
    for field in _METRIC_FIELDS:
        _finite(value.get(field))


def _finite(value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Nested numeric value is invalid")
    parsed = float(value)
    if not math.isfinite(parsed):
        raise ValueError("Nested numeric value must be finite")
    return parsed


def _timestamp(value: Any) -> None:
    if not isinstance(value, str):
        raise ValueError("Nested timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Nested timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ValueError("Nested timestamp must include a timezone")


def _iso_date(value: Any) -> None:
    if not isinstance(value, str):
        raise ValueError("Nested date is invalid")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError as exc:
        raise ValueError("Nested date must use YYYY-MM-DD") from exc
    if parsed.strftime("%Y-%m-%d") != value:
        raise ValueError("Nested date must use YYYY-MM-DD")


def _metadata(market: MarketData) -> dict[str, Any]:
    value = market.snapshot_metadata()
    if not isinstance(value, Mapping):
        raise RuntimeError("Market snapshot metadata must be an object")
    return dict(value)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "MAX_NESTED_PER_OWNER",
    "MAX_POINTS_PER_PARAMETER",
    "MAX_TOTAL_BACKTESTS",
    "NESTED_SAFETY",
    "NestedWalkForwardEngine",
    "OBJECTIVES",
    "SELECTION_RULE",
]
