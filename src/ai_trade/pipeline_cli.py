"""Controlled CLI adapters for immutable research-pipeline evidence."""

from __future__ import annotations

from datetime import date, datetime, timezone
import math
from pathlib import Path
import statistics
from typing import Any, Mapping

from .broker.shadow_ledger import ShadowEventLedger
from .broker.shadow_projection import project_shadow_account
from .broker.shadow_reconciliation import reconcile_shadow_projection
from .config import AppConfig
from .data.market import MarketData
from .factor_lab.custom import resolve_factor
from .feature_store import (
    FeatureSnapshotBuilder,
    FeatureSnapshotStore,
    ForwardEvidenceRunner,
    LabelSnapshotBuilder,
    LabelSnapshotStore,
    SnapshotDatasetStore,
    training_pairs,
)
from .feature_store.schema import is_genuine_pit_snapshot, json_fingerprint
from .json_utils import load_unique_json
from .model_lab import ModelLabEngine
from .model_lab.artifact import (
    ModelArtifactStore,
    evaluation_binding,
    fit_linear_artifact,
)
from .model_lab.inference import predict_snapshot
from .model_lab.prediction_schema import PredictionSnapshotStore
from .numeric import sample_standard_deviation
from .portfolio import (
    PortfolioConstraints,
    PortfolioPlanStore,
    TransactionCostModel,
    construct_portfolio_plan,
)


MAX_CLI_JSON_BYTES = 256 * 1024


def build_feature_snapshot(
    config: AppConfig,
    *,
    as_of_session: date | None,
    live_capture: bool,
    factor_ids: list[str] | None = None,
) -> dict[str, Any]:
    market = MarketData(config, recover_snapshot=False)
    definitions = (
        None
        if factor_ids is None
        else tuple(resolve_factor(config, "local-owner", item) for item in factor_ids)
    )
    return FeatureSnapshotBuilder(config).build(
        market,
        as_of_session=as_of_session,
        definitions=definitions,
        historical_reconstruction=not live_capture,
    )


def run_forward_evidence(
    config: AppConfig,
    *,
    factor_ids: list[str] | None = None,
    horizons: tuple[int, ...] = (5, 20, 60),
) -> dict[str, Any]:
    market = MarketData(config, recover_snapshot=False)
    definitions = (
        None
        if factor_ids is None
        else tuple(resolve_factor(config, "local-owner", item) for item in factor_ids)
    )
    return ForwardEvidenceRunner(config).run(
        market,
        definitions=definitions,
        horizons=horizons,
    )


def show_feature_snapshot(
    config: AppConfig,
    snapshot_id: str,
    *,
    on_date: date,
) -> dict[str, Any]:
    return FeatureSnapshotStore(config.feature_store_dir).get(
        snapshot_id,
        on_date=on_date,
    )


def build_label_snapshot(
    config: AppConfig,
    snapshot_id: str,
    *,
    on_date: date,
    horizon: int,
) -> dict[str, Any]:
    feature = show_feature_snapshot(config, snapshot_id, on_date=on_date)
    market = MarketData(config, recover_snapshot=False)
    return LabelSnapshotBuilder(config).build(feature, market, horizon=horizon)


def fit_model_artifact(
    config: AppConfig,
    evaluation_id: str,
    *,
    training_cutoff: datetime,
) -> dict[str, Any]:
    cutoff = _past_timestamp(training_cutoff, "training_cutoff")
    evaluation = ModelLabEngine(config).get("local-owner", evaluation_id)
    # Fail before reading training stores when the statistical deployment gate fails.
    binding = evaluation_binding(evaluation)
    evaluation_created = _record_timestamp(evaluation["created_at"], "evaluation.created_at")
    if cutoff > evaluation_created:
        raise ValueError(
            "training_cutoff cannot follow the qualified evaluation creation time"
        )
    expected_factors = list(binding["factor_ids"])
    horizon = int(evaluation["parameters"]["horizon"])
    evaluation_as_of = date.fromisoformat(
        str(evaluation["evidence"]["snapshot"]["as_of"])
    )
    evaluation_start = date.fromisoformat(str(evaluation["parameters"]["start"]))
    evaluation_snapshot = evaluation["evidence"]["snapshot"]
    expected_provider = str(evaluation_snapshot["provider"])
    expected_universe = str(evaluation["evidence"]["universe"]["name"])
    expected_security_master = str(
        evaluation["evidence"]["universe"]["security_master_sha256"]
    )
    expected_feature_ids: set[str] | None = None
    expected_label_ids: set[str] | None = None
    expected_providers = {expected_provider}
    if evaluation_snapshot["kind"] == "feature_snapshot_dataset":
        manifest = SnapshotDatasetStore(config.feature_store_dir).get(
            str(evaluation_snapshot["snapshot_id"])
        )
        if manifest["dataset_fingerprint"] != evaluation_snapshot["fingerprint"]:
            raise ValueError(
                "Model evaluation snapshot-dataset fingerprint is inconsistent"
            )
        if (
            [item["factor_id"] for item in manifest["feature_set"]["factors"]]
            != expected_factors
            or horizon not in manifest["horizons"]
            or manifest["source"]["universe_name"] != expected_universe
            or manifest["source"]["security_master_sha256"]
            != expected_security_master
        ):
            raise ValueError(
                "Model evaluation snapshot-dataset contract is inconsistent"
            )
        expected_feature_ids = set(manifest["source_snapshots"]["features"])
        expected_label_ids = set(manifest["source_snapshots"]["labels"])
        expected_providers = set(manifest["source"]["feature_providers"])

    feature_store = FeatureSnapshotStore(config.feature_store_dir)
    label_store = LabelSnapshotStore(config.feature_store_dir)
    features: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    for feature in _evaluation_training_features(
        feature_store,
        start=evaluation_start,
        end=evaluation_as_of,
        expected_snapshot_ids=expected_feature_ids,
    ):
        if not is_genuine_pit_snapshot(feature):
            continue
        if (
            str(feature["source"]["provider"]) not in expected_providers
            or str(feature["universe"]["name"]) != expected_universe
            or str(feature["source"]["security_master_sha256"])
            != expected_security_master
        ):
            continue
        if (
            _record_timestamp(feature["created_at"], "feature.created_at") > cutoff
            or _record_timestamp(
                feature["knowledge_cutoff"], "feature.knowledge_cutoff"
            )
            > cutoff
        ):
            continue
        factor_ids = [
            str(item["factor_id"])
            for item in feature["feature_set"]["factors"]
        ]
        if factor_ids != expected_factors:
            continue
        candidates = [
            item
            for item in label_store.list_for_feature(str(feature["snapshot_id"]))
            if int(item["horizon"]) == horizon
            and (
                expected_label_ids is None
                or str(item["label_snapshot_id"]) in expected_label_ids
            )
            and date.fromisoformat(str(item["target_session"]))
            <= evaluation_as_of
            and _record_timestamp(item["created_at"], "label.created_at") <= cutoff
            and _record_timestamp(item["realized_at"], "label.realized_at") <= cutoff
        ]
        if not candidates:
            continue
        label = max(
            candidates,
            key=lambda item: (str(item["created_at"]), str(item["label_snapshot_id"])),
        )
        features.append(feature)
        labels.append(label)

    pairs = training_pairs(
        features,
        labels,
        training_cutoff=cutoff,
        require_genuine_pit=True,
        evidence_cutoff=evaluation_created,
    )
    if not pairs:
        raise ValueError(
            "No mature FeatureSnapshot/LabelSnapshot training pairs match the evaluation"
        )
    root = config.project_root / "state" / "model_lab"
    return fit_linear_artifact(
        pairs,
        model_id=str(evaluation["model"]["model_id"]),
        evaluation=evaluation,
        store=ModelArtifactStore(root),
    )


def _evaluation_training_features(
    store: FeatureSnapshotStore,
    *,
    start: date,
    end: date,
    expected_snapshot_ids: set[str] | None,
) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for session in store.sessions():
        if session < start or session > end:
            continue
        if expected_snapshot_ids is None:
            feature = store.latest(on_or_before=session)
            if (
                feature is not None
                and date.fromisoformat(str(feature["as_of_session"])) == session
            ):
                records.append(feature)
            continue
        records.extend(
            item
            for item in store.list_for_session(session)
            if str(item["snapshot_id"]) in expected_snapshot_ids
        )
    records.sort(
        key=lambda item: (str(item["as_of_session"]), str(item["snapshot_id"]))
    )
    return records


def create_prediction_snapshot(
    config: AppConfig,
    artifact_id: str,
    feature_snapshot_id: str,
    *,
    feature_date: date,
    valid_from_session: date,
    valid_until_session: date,
) -> dict[str, Any]:
    model_root = config.project_root / "state" / "model_lab"
    artifact = ModelArtifactStore(model_root).get(artifact_id)
    feature = show_feature_snapshot(
        config,
        feature_snapshot_id,
        on_date=feature_date,
    )
    market = MarketData(config, recover_snapshot=False)
    return predict_snapshot(
        artifact,
        feature,
        valid_from_session=valid_from_session,
        valid_until_session=valid_until_session,
        trading_calendar=market.calendar,
        store=PredictionSnapshotStore(model_root),
    )


def create_portfolio_plan(
    config: AppConfig,
    prediction_id: str,
    *,
    feature_date: date,
    equity: float,
    execution_session: date,
    decision_time: datetime,
    current_weights_file: str | Path | None,
) -> dict[str, Any]:
    model_root = config.project_root / "state" / "model_lab"
    prediction = PredictionSnapshotStore(model_root).get(
        prediction_id,
        on_date=feature_date,
    )
    current_weights = (
        {}
        if current_weights_file is None
        else _json_object(current_weights_file, "current weights")
    )
    market = MarketData(config, recover_snapshot=False)
    symbols = {
        str(item["symbol"]) for item in prediction["rows"]
    } | {str(symbol) for symbol in current_weights}
    metadata, market_evidence = _instrument_metadata(
        config, market, feature_date, symbols
    )
    return construct_portfolio_plan(
        prediction,
        equity=equity,
        current_weights=current_weights,
        instrument_metadata=metadata,
        market_evidence=market_evidence,
        cost_model=TransactionCostModel(config),
        constraints=PortfolioConstraints.from_config(config),
        decision_time=_past_timestamp(decision_time, "decision_time"),
        execution_session=execution_session,
        store=PortfolioPlanStore(config.project_root / "state" / "portfolio"),
    )


def append_shadow_event(
    config: AppConfig,
    account_reference: str,
    event_type: str,
    *,
    occurred_at: datetime,
    trading_session: date,
    source: str,
    external_id: str,
    payload_file: str | Path,
) -> dict[str, Any]:
    payload = _json_object(payload_file, "shadow payload")
    ledger = ShadowEventLedger(config.shadow_ledger_dir, account_reference)
    return ledger.append(
        event_type,
        occurred_at=_aware_timestamp(occurred_at, "occurred_at"),
        trading_session=trading_session,
        source=source,
        external_id=external_id,
        payload=payload,
    )


def shadow_projection(
    config: AppConfig,
    account_reference: str,
) -> dict[str, Any]:
    return project_shadow_account(
        ShadowEventLedger(config.shadow_ledger_dir, account_reference)
    )


def shadow_reconciliation(
    config: AppConfig,
    account_reference: str,
    *,
    broker_snapshot_file: str | Path,
) -> dict[str, Any]:
    snapshot = _json_object(broker_snapshot_file, "broker snapshot")
    if set(snapshot) != {"cash", "positions"}:
        raise ValueError("Broker snapshot fields are invalid")
    positions = snapshot["positions"]
    if not isinstance(positions, Mapping):
        raise ValueError("Broker snapshot positions must be an object")
    projection = shadow_projection(config, account_reference)
    return reconcile_shadow_projection(
        projection,
        broker_cash=snapshot["cash"],
        broker_positions=positions,
    )


def _instrument_metadata(
    config: AppConfig,
    market: MarketData,
    on_date: date,
    symbols: set[str],
) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    instruments = {item.symbol: item for item in config.instruments}
    result: dict[str, dict[str, Any]] = {}
    input_bindings: dict[str, str] = {}
    for symbol in sorted(symbols):
        instrument = instruments.get(symbol)
        if instrument is None:
            raise ValueError(f"Unknown portfolio instrument: {symbol}")
        history = market.history(symbol, on_date, 61)
        input_bindings[symbol] = json_fingerprint(
            [
                [
                    item.date.isoformat(),
                    item.open,
                    item.close,
                    item.high,
                    item.low,
                    item.volume,
                    item.amount,
                ]
                for item in history
            ]
        )
        amounts = [float(item.amount) for item in history[-20:] if item.amount >= 0]
        returns = [
            history[index].close / history[index - 1].close - 1.0
            for index in range(1, len(history))
            if history[index - 1].close > 0 and history[index].close > 0
        ]
        volatility = (
            sample_standard_deviation(returns) * math.sqrt(252.0)
            if len(returns) > 1
            else 0.0
        )
        result[symbol] = {
            "asset_class": instrument.asset_class,
            "sector": instrument.sector,
            "average_amount": statistics.fmean(amounts) if amounts else 0.0,
            "annual_volatility": volatility,
        }
    snapshot_metadata = market.snapshot_metadata()
    if not isinstance(snapshot_metadata, Mapping):
        raise RuntimeError("Portfolio market snapshot metadata must be an object")
    manifest_sha256 = getattr(market, "manifest_sha256", None)
    if not isinstance(manifest_sha256, str) or len(manifest_sha256) != 64:
        raise RuntimeError("Portfolio plans require a verified cache manifest")
    evidence_body = {
        "as_of_session": on_date.isoformat(),
        "cache_manifest_sha256": manifest_sha256,
        "market_snapshot_fingerprint": json_fingerprint(dict(snapshot_metadata)),
        "instrument_metadata": result,
        "input_bindings": input_bindings,
    }
    return result, {
        **evidence_body,
        "fingerprint": json_fingerprint(evidence_body),
    }


def _json_object(path: str | Path, label: str) -> dict[str, Any]:
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValueError(f"{label} file is unavailable")
    value = load_unique_json(source, max_bytes=MAX_CLI_JSON_BYTES)
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be a JSON object")
    return value


def _aware_timestamp(value: datetime, label: str) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value


def _past_timestamp(value: datetime, label: str) -> datetime:
    parsed = _aware_timestamp(value, label)
    if parsed > datetime.now(timezone.utc):
        raise ValueError(f"{label} cannot be in the future")
    return parsed


def _record_timestamp(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{label} must be an ISO-8601 timestamp") from exc
    return _aware_timestamp(parsed, label)


__all__ = [
    "MAX_CLI_JSON_BYTES",
    "append_shadow_event",
    "build_feature_snapshot",
    "build_label_snapshot",
    "create_portfolio_plan",
    "create_prediction_snapshot",
    "fit_model_artifact",
    "run_forward_evidence",
    "shadow_projection",
    "shadow_reconciliation",
    "show_feature_snapshot",
]
