from __future__ import annotations

from datetime import date
from typing import Any, Sequence

from ..config import AppConfig
from ..data.market import MarketData
from ..factor_lab.library import FactorDefinition
from .builder import FeatureSnapshotBuilder
from .labels import LabelSnapshotBuilder, LabelSnapshotStore
from .schema import is_genuine_pit_snapshot
from .store import FeatureSnapshotStore


DEFAULT_FORWARD_HORIZONS = (5, 20, 60)
MAX_REPORTED_LABELS = 100
FORWARD_EVIDENCE_SAFETY = {
    "research_only": True,
    "refreshes_market_data": False,
    "creates_no_signal": True,
    "may_trade": False,
}


class ForwardEvidenceRunner:
    """Capture one current feature cross-section and mature pending labels."""

    def __init__(
        self,
        config: AppConfig,
        *,
        feature_store: FeatureSnapshotStore | None = None,
        label_store: LabelSnapshotStore | None = None,
    ) -> None:
        self.config = config
        self.feature_store = feature_store or FeatureSnapshotStore(
            config.feature_store_dir
        )
        self.label_store = label_store or LabelSnapshotStore(
            config.feature_store_dir
        )

    def run(
        self,
        market: MarketData,
        *,
        definitions: Sequence[FactorDefinition] | None = None,
        horizons: Sequence[int] = DEFAULT_FORWARD_HORIZONS,
    ) -> dict[str, Any]:
        selected_horizons = _validated_horizons(horizons)
        builder = FeatureSnapshotBuilder(self.config, self.feature_store)
        draft = builder.build(
            market,
            definitions=definitions,
            historical_reconstruction=False,
            publish=False,
        )
        feature = self._publish_or_reuse(draft)
        feature_set_id = str(feature["feature_set"]["feature_set_id"])

        label_builder = LabelSnapshotBuilder(self.config, self.label_store)
        created: list[dict[str, Any]] = []
        created_count = 0
        reused_count = 0
        pending_count = 0
        eligible_count = 0
        ignored_count = 0
        pending_by_horizon = {str(item): 0 for item in selected_horizons}

        calendar_index = {session: index for index, session in enumerate(market.calendar)}
        for session in self.feature_store.sessions():
            candidate = self.feature_store.latest(
                on_or_before=session,
                feature_set_id=feature_set_id,
                historical_reconstruction=False,
            )
            if (
                candidate is None
                or candidate["as_of_session"] != session.isoformat()
                or not is_genuine_pit_snapshot(candidate)
            ):
                ignored_count += 1
                continue
            eligible_count += 1
            start_index = calendar_index.get(session)
            for horizon in selected_horizons:
                if (
                    start_index is None
                    or start_index + horizon >= len(market.calendar)
                    or market.calendar[start_index + horizon] > market.completed_through
                ):
                    pending_count += 1
                    pending_by_horizon[str(horizon)] += 1
                    continue
                label = label_builder.build(candidate, market, horizon=horizon)
                if bool(label["reused"]):
                    reused_count += 1
                    continue
                created_count += 1
                if len(created) < MAX_REPORTED_LABELS:
                    created.append(
                        {
                            "label_snapshot_id": label["label_snapshot_id"],
                            "feature_snapshot_id": label["feature_snapshot_id"],
                            "as_of_session": label["as_of_session"],
                            "target_session": label["target_session"],
                            "horizon": label["horizon"],
                        }
                    )

        return {
            "schema_version": 1,
            "as_of_session": feature["as_of_session"],
            "feature": {
                "snapshot_id": feature["snapshot_id"],
                "feature_set_id": feature_set_id,
                "provider": feature["source"]["provider"],
                "rows": len(feature["rows"]),
                "reused": bool(feature["reused"]),
                "genuine_pit": is_genuine_pit_snapshot(feature),
            },
            "labels": {
                "horizons": list(selected_horizons),
                "eligible_feature_snapshots": eligible_count,
                "ignored_feature_sessions": ignored_count,
                "created_count": created_count,
                "reused_count": reused_count,
                "pending_count": pending_count,
                "pending_by_horizon": pending_by_horizon,
                "created": created,
                "created_truncated": created_count > len(created),
            },
            "safety": dict(FORWARD_EVIDENCE_SAFETY),
        }

    def _publish_or_reuse(self, draft: dict[str, Any]) -> dict[str, Any]:
        on_date = date.fromisoformat(str(draft["as_of_session"]))
        existing = self.feature_store.latest(
            on_or_before=on_date,
            feature_set_id=str(draft["feature_set"]["feature_set_id"]),
            historical_reconstruction=False,
        )
        if (
            existing is not None
            and existing["as_of_session"] == draft["as_of_session"]
            and is_genuine_pit_snapshot(existing)
            and existing["source"]["as_of_market_fingerprint"]
            == draft["source"]["as_of_market_fingerprint"]
        ):
            result = dict(existing)
            result["reused"] = True
            return result
        return self.feature_store.publish(draft)


def _validated_horizons(values: Sequence[int]) -> tuple[int, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError("Forward evidence horizons must be a sequence")
    parsed = tuple(values)
    if (
        not parsed
        or len(parsed) > 16
        or any(type(item) is not int or not 1 <= item <= 250 for item in parsed)
        or list(parsed) != sorted(set(parsed))
    ):
        raise ValueError(
            "Forward evidence horizons must be unique ascending integers from 1 to 250"
        )
    return parsed


__all__ = [
    "DEFAULT_FORWARD_HORIZONS",
    "FORWARD_EVIDENCE_SAFETY",
    "ForwardEvidenceRunner",
    "MAX_REPORTED_LABELS",
]
