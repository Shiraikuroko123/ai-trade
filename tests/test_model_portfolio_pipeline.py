from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ai_trade.config import load_config
from ai_trade.feature_store import (
    FeatureSnapshotBuilder,
    FeatureSnapshotStore,
    LabelSnapshotBuilder,
    LabelSnapshotStore,
    training_pairs,
)
from ai_trade.feature_store.schema import json_fingerprint
from ai_trade.model_lab.artifact import ModelArtifactStore, fit_linear_artifact
from ai_trade.model_lab.inference import predict_snapshot
from ai_trade.model_lab.prediction_schema import (
    PREDICTION_ENGINE_VERSION,
    PREDICTION_SCHEMA_VERSION,
    PREDICTION_SAFETY,
    PredictionSnapshotStore,
    finalize_prediction_snapshot,
)
from ai_trade.portfolio import (
    PortfolioConstraints,
    PortfolioPlanStore,
    TransactionCostModel,
    construct_portfolio_plan,
    validate_portfolio_plan,
)
from tests.test_feature_snapshot import _Market


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _qualified_evaluation(factor_ids: list[str]) -> dict:
    best = factor_ids[0]
    validation = {
        "reject_null": True,
        "ci_low": 0.01,
        "positive_subperiods": 3,
        "adjusted_p_value": 0.01,
    }
    return {
        "schema_version": 2,
        "engine_version": 2,
        "evaluation_id": "mdl_" + "a" * 32,
        "record_fingerprint": "b" * 64,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "model": {"model_id": "ridge_v1"},
        "factors": [{"factor_id": factor_id} for factor_id in factor_ids],
        "parameters": {"horizon": 5},
        "evidence": {
            "snapshot": {
                "as_of": "2026-07-24",
                "fingerprint": "c" * 64,
            }
        },
        "results": {
            "best_factor_id": best,
            "model": {"mean_ic": 0.08},
            "model_minus_best_factor_ic": 0.03,
            "statistical_validation": {
                "model_ic": dict(validation),
                "factor_comparisons": [
                    {"factor_id": best, "validation": dict(validation)}
                ],
            },
        },
    }


def _prediction(expected: dict[str, float]) -> dict:
    ranked = {
        symbol: rank
        for rank, (symbol, _value) in enumerate(
            sorted(expected.items(), key=lambda item: (-item[1], item[0])),
            start=1,
        )
    }
    return finalize_prediction_snapshot(
        {
            "schema_version": PREDICTION_SCHEMA_VERSION,
            "engine_version": PREDICTION_ENGINE_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "model_artifact": {
                "model_artifact_id": "ma_" + "1" * 32,
                "artifact_fingerprint": "2" * 64,
                "record_fingerprint": "3" * 64,
            },
            "feature_snapshot": {
                "snapshot_id": "fs_" + "4" * 32,
                "snapshot_fingerprint": "5" * 64,
                "as_of_session": "2026-07-24",
                "knowledge_cutoff": "2026-07-24T15:30:00+08:00",
            },
            "horizon": 20,
            "valid_from_session": "2026-07-27",
            "valid_until_session": "2026-08-24",
            "rows": [
                {
                    "symbol": symbol,
                    "score": value / 10_000.0,
                    "expected_return_bps": value,
                    "uncertainty_bps": 0.0,
                    "rank": ranked[symbol],
                    "rejection_reason": None,
                }
                for symbol, value in sorted(expected.items())
            ],
            "safety": dict(PREDICTION_SAFETY),
        }
    )


def _market_evidence(
    metadata: dict[str, dict[str, object]],
) -> dict[str, object]:
    body = {
        "as_of_session": "2026-07-24",
        "cache_manifest_sha256": "6" * 64,
        "market_snapshot_fingerprint": "7" * 64,
        "instrument_metadata": metadata,
        "input_bindings": {symbol: "8" * 64 for symbol in sorted(metadata)},
    }
    return {**body, "fingerprint": json_fingerprint(body)}


class ModelArtifactPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = load_config(REPOSITORY_ROOT / "config" / "default.json")
        self.market = _Market(self.config)
        self.feature_builder = FeatureSnapshotBuilder(
            self.config, FeatureSnapshotStore(self.root / "features")
        )
        self.label_builder = LabelSnapshotBuilder(
            self.config, LabelSnapshotStore(self.root / "features")
        )

    def test_artifact_round_trip_produces_identical_predictions(self):
        features = []
        labels = []
        for index in (-60, -45, -30):
            feature = self.feature_builder.build(
                self.market, as_of_session=self.market.calendar[index]
            )
            label = self.label_builder.build(feature, self.market, horizon=5)
            features.append(feature)
            labels.append(label)
        pairs = training_pairs(
            features,
            labels,
            training_cutoff=datetime.now(timezone.utc),
        )
        factor_ids = [
            item["factor_id"]
            for item in features[0]["feature_set"]["factors"]
        ]
        artifact_store = ModelArtifactStore(self.root / "model_lab")
        with patch("ai_trade.model_lab.artifact.validate_evaluation"):
            artifact = fit_linear_artifact(
                pairs,
                model_id="ridge_v1",
                evaluation=_qualified_evaluation(factor_ids),
                store=artifact_store,
            )
        loaded = artifact_store.get(artifact["model_artifact_id"])
        out_of_sample = self.feature_builder.build(
            self.market, as_of_session=self.market.calendar[-10]
        )
        prediction_store = PredictionSnapshotStore(self.root / "model_lab")
        first = predict_snapshot(
            loaded,
            out_of_sample,
            valid_from_session=self.market.calendar[-9],
            valid_until_session=self.market.calendar[-5],
            trading_calendar=self.market.calendar,
            store=prediction_store,
        )
        second = predict_snapshot(
            loaded,
            out_of_sample,
            valid_from_session=self.market.calendar[-9],
            valid_until_session=self.market.calendar[-5],
            trading_calendar=self.market.calendar,
            store=prediction_store,
        )
        with self.assertRaisesRegex(ValueError, "validity window"):
            predict_snapshot(
                loaded,
                out_of_sample,
                valid_from_session=self.market.calendar[-8],
                valid_until_session=self.market.calendar[-5],
                trading_calendar=self.market.calendar,
            )
        with self.assertRaisesRegex(ValueError, "full future horizon"):
            predict_snapshot(
                loaded,
                out_of_sample,
                valid_from_session=self.market.calendar[-9],
                valid_until_session=self.market.calendar[-5],
                trading_calendar=self.market.calendar[:-9],
            )

        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["snapshot_fingerprint"], second["snapshot_fingerprint"])
        self.assertEqual(first["rows"], second["rows"])
        self.assertTrue(any(row["rejection_reason"] is None for row in first["rows"]))


class PortfolioConstructionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = load_config(REPOSITORY_ROOT / "config" / "default.json")
        self.cost_model = TransactionCostModel(self.config)

    def _metadata(self, symbols: list[str]) -> dict[str, dict[str, object]]:
        return {
            symbol: {
                "asset_class": f"asset-{index % 2}",
                "sector": f"sector-{index}",
                "average_amount": 1_000_000_000.0,
                "annual_volatility": 0.10,
            }
            for index, symbol in enumerate(symbols)
        }

    def _constraints(self, **overrides) -> PortfolioConstraints:
        values = {
            "minimum_cash_weight": 0.10,
            "max_position_weight": 0.30,
            "max_asset_class_weight": 0.60,
            "max_sector_weight": 0.35,
            "max_turnover": 0.50,
            "target_annual_volatility": 0.12,
            "max_average_amount_participation": 0.05,
            "capacity_days": 1,
            "minimum_net_alpha_bps": 1.0,
            "uncertainty_penalty": 0.0,
        }
        values.update(overrides)
        return PortfolioConstraints(**values)

    def test_high_alpha_plan_satisfies_cost_turnover_and_group_constraints(self):
        symbols = ["510300", "510500", "159915"]
        metadata = self._metadata(symbols)
        prediction = _prediction(
            {"510300": 1000.0, "510500": 800.0, "159915": 600.0}
        )
        plan = construct_portfolio_plan(
            prediction,
            equity=100_000.0,
            current_weights={},
            instrument_metadata=metadata,
            market_evidence=_market_evidence(metadata),
            cost_model=self.cost_model,
            constraints=self._constraints(),
            decision_time=datetime.now(timezone.utc),
            execution_session=datetime(2026, 7, 27).date(),
        )

        validate_portfolio_plan(plan)
        self.assertTrue(plan["trades"])
        self.assertLessEqual(plan["metrics"]["turnover"], 0.50 + 1e-10)
        self.assertGreaterEqual(plan["metrics"]["cash_weight"], 0.10 - 1e-10)
        self.assertTrue(
            all(weight <= 0.30 + 1e-10 for weight in plan["target_weights"].values())
        )
        self.assertGreater(plan["metrics"]["estimated_cost_currency"], 0)
        self.assertTrue(plan["safety"]["creates_no_order"])
        tampered = json.loads(json.dumps(plan))
        tampered["market_evidence"]["instrument_metadata"]["510300"][
            "average_amount"
        ] = 1.0
        with self.assertRaisesRegex(ValueError, "market evidence fingerprint"):
            validate_portfolio_plan(tampered)
        store = PortfolioPlanStore(Path(self.temporary.name) / "portfolio")
        first = store.publish(plan)
        second = store.publish(plan)
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])

    def test_alpha_below_minimum_commission_produces_zero_trade(self):
        metadata = self._metadata(["510300"])
        prediction = _prediction({"510300": 0.1})
        plan = construct_portfolio_plan(
            prediction,
            equity=1_000.0,
            current_weights={},
            instrument_metadata=metadata,
            market_evidence=_market_evidence(metadata),
            cost_model=self.cost_model,
            constraints=self._constraints(max_position_weight=0.50),
            decision_time=datetime.now(timezone.utc),
            execution_session=datetime(2026, 7, 27).date(),
        )

        self.assertEqual(plan["trades"], [])
        self.assertEqual(plan["target_weights"], {})
        self.assertEqual(plan["metrics"]["turnover"], 0.0)
        self.assertTrue(
            any("alpha_does_not_cover_cost" in item for item in plan["diagnostics"]["excluded_symbols"])
        )

    def test_portfolio_decision_cannot_predate_prediction(self):
        prediction = _prediction({"510300": 1000.0})
        prediction_created = datetime.fromisoformat(prediction["created_at"])
        metadata = self._metadata(["510300"])

        with self.assertRaisesRegex(ValueError, "predates its prediction"):
            construct_portfolio_plan(
                prediction,
                equity=100_000.0,
                current_weights={},
                instrument_metadata=metadata,
                market_evidence=_market_evidence(metadata),
                cost_model=self.cost_model,
                constraints=self._constraints(max_position_weight=0.50),
                decision_time=prediction_created - timedelta(microseconds=1),
                execution_session=datetime(2026, 7, 27).date(),
            )


if __name__ == "__main__":
    unittest.main()
