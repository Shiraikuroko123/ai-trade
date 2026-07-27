from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import math
from typing import Any, Sequence

from ..config import AppConfig
from ..data.market import MarketData
from ..factor_lab.library import FACTORS, LIBRARY_VERSION, FactorDefinition
from ..models import Bar
from .schema import (
    FEATURE_ENGINE_VERSION,
    FEATURE_SCHEMA_VERSION,
    FEATURE_SAFETY,
    finalize_feature_snapshot,
    json_fingerprint,
)
from .provenance import actual_snapshot_provider
from .store import FeatureSnapshotStore


CHINA_TIMEZONE = timezone(timedelta(hours=8))


class FeatureSnapshotBuilder:
    """Materialize one completed-session cross-section without future labels."""

    def __init__(
        self,
        config: AppConfig,
        store: FeatureSnapshotStore | None = None,
    ) -> None:
        self.config = config
        self.store = store or FeatureSnapshotStore(config.feature_store_dir)

    def build(
        self,
        market: MarketData,
        *,
        as_of_session: date | None = None,
        definitions: Sequence[FactorDefinition] | None = None,
        historical_reconstruction: bool = True,
        knowledge_cutoff: datetime | None = None,
        publish: bool = True,
    ) -> dict[str, Any]:
        selected = tuple(definitions or FACTORS)
        _validate_definitions(selected)
        as_of = as_of_session or market.latest_common_session
        if (
            as_of not in market.calendar
            or as_of > market.latest_common_session
            or as_of > market.completed_through
        ):
            raise ValueError("Feature snapshot requires a completed common session")
        cutoff = _knowledge_cutoff(
            self.config,
            market,
            as_of,
            historical_reconstruction=historical_reconstruction,
            supplied=knowledge_cutoff,
        )
        metadata_before = market.snapshot_metadata()
        if not isinstance(metadata_before, dict):
            raise RuntimeError("Market snapshot metadata must be an object")
        source_provider = actual_snapshot_provider(metadata_before)
        manifest_sha256 = getattr(market, "manifest_sha256", None)
        if not isinstance(manifest_sha256, str) or len(manifest_sha256) != 64:
            raise RuntimeError("Feature snapshots require a verified cache manifest")

        factor_payload = [item.to_dict() for item in selected]
        feature_set_stub = {
            "feature_set_id": "fset_"
            + json_fingerprint(
                {"library_version": LIBRARY_VERSION, "factors": factor_payload}
            )[:24],
            "library_version": LIBRARY_VERSION,
            "factors": factor_payload,
        }
        feature_set = {
            **feature_set_stub,
            "fingerprint": json_fingerprint(feature_set_stub),
        }

        universe_snapshot = self.config.security_master.snapshot(
            self.config.universe_name,
            as_of,
            self.config.minimum_listing_days,
        )
        active_symbols = sorted(
            str(item["symbol"])
            for item in universe_snapshot["instruments"]
            if item["active"]
        )
        excluded: list[dict[str, Any]] = [
            {
                "symbol": str(item["symbol"]),
                "reasons": sorted(str(reason) for reason in item["eligibility_reasons"]),
            }
            for item in universe_snapshot["instruments"]
            if not item["active"]
        ]
        excluded.sort(key=lambda item: str(item["symbol"]))

        rows: list[dict[str, Any]] = []
        input_bindings: dict[str, str] = {}
        for symbol in active_symbols:
            if symbol not in market.symbols:
                raise RuntimeError(f"Active feature symbol is absent from market data: {symbol}")
            bars = [bar for bar in market.symbols[symbol].bars if bar.date <= as_of]
            input_sha256 = _bars_fingerprint(bars)
            input_bindings[symbol] = input_sha256
            current = market.bar(symbol, as_of)
            status = market.trading_status(symbol, as_of)
            values: dict[str, float] = {}
            missing: dict[str, str] = {}
            for definition in selected:
                if current is None:
                    missing[definition.factor_id] = "no_bar_for_session"
                    continue
                history = market.history(symbol, as_of, definition.minimum_history)
                if len(history) < definition.minimum_history:
                    missing[definition.factor_id] = (
                        f"insufficient_history:{len(history)}/{definition.minimum_history}"
                    )
                    continue
                result = definition.compute(history)
                if result is None:
                    missing[definition.factor_id] = "undefined_by_factor"
                    continue
                if not math.isfinite(float(result)):
                    raise RuntimeError(
                        f"Feature {definition.factor_id} produced a non-finite value"
                    )
                values[definition.factor_id] = float(result)
            rows.append(
                {
                    "symbol": symbol,
                    "session": as_of.isoformat(),
                    "last_bar_session": bars[-1].date.isoformat() if bars else None,
                    "trading_status": status.status,
                    "tradable": status.tradable,
                    "input_sha256": input_sha256,
                    "values": dict(sorted(values.items())),
                    "missing": dict(sorted(missing.items())),
                }
            )

        manifest = metadata_before.get("manifest")
        manifest_snapshot_id = (
            manifest.get("snapshot_id") if isinstance(manifest, dict) else None
        )
        as_of_market_fingerprint = json_fingerprint(
            {
                "as_of_session": as_of.isoformat(),
                "provider": source_provider,
                "adjustment": metadata_before.get("adjustment"),
                "security_master_sha256": self.config.security_master.fingerprint(),
                "inputs": input_bindings,
            }
        )
        record = finalize_feature_snapshot(
            {
                "schema_version": FEATURE_SCHEMA_VERSION,
                "engine_version": FEATURE_ENGINE_VERSION,
                "created_at": _utc_now(),
                "as_of_session": as_of.isoformat(),
                "knowledge_cutoff": cutoff.isoformat(),
                "historical_reconstruction": historical_reconstruction,
                "feature_set": feature_set,
                "source": {
                    "provider": source_provider,
                    "adjustment": str(metadata_before.get("adjustment") or "none"),
                    "completed_session_cutoff": market.completed_through.isoformat(),
                    "cache_manifest_sha256": manifest_sha256,
                    "manifest_snapshot_id": (
                        str(manifest_snapshot_id)
                        if manifest_snapshot_id is not None
                        else None
                    ),
                    "security_master_sha256": self.config.security_master.fingerprint(),
                    "as_of_market_fingerprint": as_of_market_fingerprint,
                },
                "universe": {
                    "name": self.config.universe_name,
                    "minimum_listing_days": self.config.minimum_listing_days,
                    "candidate_records": int(universe_snapshot["candidate_records"]),
                    "active_symbols": active_symbols,
                    "excluded": excluded,
                },
                "rows": rows,
                "safety": dict(FEATURE_SAFETY),
            }
        )
        metadata_after = market.snapshot_metadata()
        if json_fingerprint(metadata_after) != json_fingerprint(metadata_before):
            raise RuntimeError("Market snapshot changed while building features")
        return self.store.publish(record) if publish else record


def _validate_definitions(definitions: Sequence[FactorDefinition]) -> None:
    if not definitions or len(definitions) > 64:
        raise ValueError("Feature snapshots require between 1 and 64 factors")
    identifiers = [item.factor_id for item in definitions]
    if len(identifiers) != len(set(identifiers)):
        raise ValueError("Feature snapshot factor definitions must be unique")


def _knowledge_cutoff(
    config: AppConfig,
    market: MarketData,
    as_of: date,
    *,
    historical_reconstruction: bool,
    supplied: datetime | None,
) -> datetime:
    market_close = time.fromisoformat(
        str(config.raw["data"].get("market_close_time", "15:30"))
    )
    session_close = datetime.combine(as_of, market_close, CHINA_TIMEZONE)
    cutoff = supplied or (
        session_close if historical_reconstruction else datetime.now(timezone.utc)
    )
    if cutoff.tzinfo is None or cutoff.utcoffset() is None:
        raise ValueError("Feature knowledge_cutoff must include a timezone")
    if cutoff < session_close:
        raise ValueError("Feature knowledge_cutoff precedes the completed session")
    if not historical_reconstruction:
        if as_of != market.latest_common_session:
            raise ValueError(
                "A genuine PIT snapshot must capture the latest common session"
            )
        if market.latest_common_session != market.completed_through:
            raise ValueError(
                "A genuine PIT snapshot requires market data current through "
                "the completed-session cutoff"
            )
    return cutoff


def _bars_fingerprint(bars: Sequence[Bar]) -> str:
    return json_fingerprint(
        [
            [
                bar.date.isoformat(),
                bar.open,
                bar.close,
                bar.high,
                bar.low,
                bar.volume,
                bar.amount,
            ]
            for bar in bars
        ]
    )


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = ["CHINA_TIMEZONE", "FeatureSnapshotBuilder"]
