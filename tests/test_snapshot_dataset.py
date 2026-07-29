from __future__ import annotations

from contextlib import redirect_stdout
from datetime import date, datetime, time, timedelta, timezone
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from ai_trade.cli import build_parser, main
from ai_trade.config import load_config
from ai_trade.factor_lab import FactorLabEngine, FactorLabStore
from ai_trade.factor_lab.library import LIBRARY_VERSION, factor_definition
from ai_trade.feature_store import (
    FeatureSnapshotStore,
    LabelSnapshotStore,
    SnapshotDatasetStore,
    build_snapshot_dataset,
    load_snapshot_dataset,
)
from ai_trade.feature_store.dataset_store import (
    snapshot_dataset_record_fingerprint,
)
from ai_trade.feature_store.labels import (
    LABEL_ENGINE_VERSION,
    LABEL_SAFETY,
    LABEL_SCHEMA_VERSION,
    finalize_label_snapshot,
)
from ai_trade.feature_store.schema import (
    FEATURE_ENGINE_VERSION,
    FEATURE_SAFETY,
    FEATURE_SCHEMA_VERSION,
    finalize_feature_snapshot,
    json_fingerprint,
)
from ai_trade.model_lab import ModelLabEngine, ModelLabStore
from ai_trade.pipeline_cli import fit_model_artifact


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
CHINA_TIMEZONE = timezone(timedelta(hours=8))
SYMBOLS = ("TEST_A", "TEST_B", "TEST_C", "TEST_D")
FACTORS = (
    factor_definition("momentum_60_5"),
    factor_definition("momentum_120_5"),
)


def _feature_snapshot(
    config,
    index: int,
    *,
    factors=FACTORS,
    provider: str = "authorized-fixture",
    historical_reconstruction: bool = False,
    stale_capture: bool = False,
):
    session = date(2026, 1, 1) + timedelta(days=index)
    cutoff = datetime.combine(session, time(15, 30), CHINA_TIMEZONE)
    factor_records = [item.to_dict() for item in factors]
    feature_set_stub = {
        "feature_set_id": "fset_"
        + json_fingerprint(
            {"library_version": LIBRARY_VERSION, "factors": factor_records}
        )[:24],
        "library_version": LIBRARY_VERSION,
        "factors": factor_records,
    }
    feature_set = {
        **feature_set_stub,
        "fingerprint": json_fingerprint(feature_set_stub),
    }
    rows = []
    for symbol_index, symbol in enumerate(SYMBOLS):
        rank = float(symbol_index) - 1.5
        values = {
            "momentum_60_5": rank + index * 0.0001,
            "momentum_120_5": rank * rank + symbol_index * 0.01,
        }
        rows.append(
            {
                "symbol": symbol,
                "session": session.isoformat(),
                "last_bar_session": session.isoformat(),
                "trading_status": "active",
                "tradable": True,
                "input_sha256": json_fingerprint([symbol, session.isoformat()]),
                "values": {item.factor_id: values[item.factor_id] for item in factors},
                "missing": {},
            }
        )
    completed = session + timedelta(days=1) if stale_capture else session
    return finalize_feature_snapshot(
        {
            "schema_version": FEATURE_SCHEMA_VERSION,
            "engine_version": FEATURE_ENGINE_VERSION,
            "created_at": (cutoff + timedelta(minutes=1)).isoformat(),
            "as_of_session": session.isoformat(),
            "knowledge_cutoff": cutoff.isoformat(),
            "historical_reconstruction": historical_reconstruction,
            "feature_set": feature_set,
            "source": {
                "provider": provider,
                "adjustment": "forward",
                "completed_session_cutoff": completed.isoformat(),
                "cache_manifest_sha256": json_fingerprint(["manifest", index]),
                "manifest_snapshot_id": f"fixture-{index}",
                "security_master_sha256": config.security_master.fingerprint(),
                "as_of_market_fingerprint": json_fingerprint(["market", index]),
            },
            "universe": {
                "name": config.universe_name,
                "minimum_listing_days": config.minimum_listing_days,
                "candidate_records": len(SYMBOLS),
                "active_symbols": list(SYMBOLS),
                "excluded": [],
            },
            "rows": rows,
            "safety": dict(FEATURE_SAFETY),
        }
    )


def _label_snapshot(
    feature,
    index: int,
    *,
    target_offset: int = 1,
    realized_delay: int = 0,
):
    session = date.fromisoformat(str(feature["as_of_session"]))
    target = session + timedelta(days=target_offset)
    realized = datetime.combine(
        target + timedelta(days=realized_delay),
        time(15, 30),
        CHINA_TIMEZONE,
    )
    rows = []
    for symbol_index, feature_row in enumerate(feature["rows"]):
        rank = float(symbol_index) - 1.5
        rows.append(
            {
                "symbol": feature_row["symbol"],
                "forward_return": rank * 0.01 + index * 0.00001,
                "missing": None,
                "input_sha256": json_fingerprint(
                    [feature_row["input_sha256"], target.isoformat()]
                ),
            }
        )
    return finalize_label_snapshot(
        {
            "schema_version": LABEL_SCHEMA_VERSION,
            "engine_version": LABEL_ENGINE_VERSION,
            "created_at": (realized + timedelta(minutes=1)).isoformat(),
            "feature_snapshot_id": feature["snapshot_id"],
            "feature_snapshot_fingerprint": feature["snapshot_fingerprint"],
            "horizon": 1,
            "as_of_session": session.isoformat(),
            "target_session": target.isoformat(),
            "realized_at": realized.isoformat(),
            "source": {
                "provider": "authorized-fixture",
                "adjustment": "forward",
                "completed_session_cutoff": target.isoformat(),
                "cache_manifest_sha256": json_fingerprint(
                    ["label-manifest", index, target_offset]
                ),
                "as_of_target_market_fingerprint": json_fingerprint(
                    ["label-market", index, target_offset]
                ),
            },
            "rows": rows,
            "safety": dict(LABEL_SAFETY),
        }
    )


def _evidence(
    config,
    count: int = 50,
    *,
    target_offset: int = 1,
    realized_delay: int = 0,
):
    features = [_feature_snapshot(config, index) for index in range(count)]
    labels = [
        _label_snapshot(
            feature,
            index,
            target_offset=target_offset,
            realized_delay=realized_delay,
        )
        for index, feature in enumerate(features)
    ]
    return features, labels


class SnapshotDatasetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = load_config(REPOSITORY_ROOT / "config" / "default.json")

    def test_dataset_identity_is_deterministic_and_manifest_preserves_sources(self):
        features, labels = _evidence(self.config, count=4)
        first = build_snapshot_dataset(features, labels, horizons=(1,))
        second = build_snapshot_dataset(
            list(reversed(features)), list(reversed(labels)), horizons=(1,)
        )

        self.assertEqual(first.dataset_id, second.dataset_id)
        self.assertEqual(first.fingerprint, second.fingerprint)
        self.assertEqual(first.factor_ids, tuple(item.factor_id for item in FACTORS))
        self.assertEqual(
            first.source_snapshot_ids()["features"],
            list(first.feature_snapshot_ids),
        )
        store = SnapshotDatasetStore(self.root / "features")
        manifest = store.publish(first)
        repeated = store.publish(second)
        self.assertFalse(manifest["reused"])
        self.assertTrue(repeated["reused"])
        self.assertEqual(
            manifest["source_snapshots"]["labels"], list(first.label_snapshot_ids)
        )

    def test_reconstruction_stale_capture_and_changed_feature_set_are_rejected(self):
        genuine = _feature_snapshot(self.config, 0)
        reconstructed = _feature_snapshot(
            self.config, 1, historical_reconstruction=True
        )
        stale = _feature_snapshot(self.config, 1, stale_capture=True)
        with self.assertRaisesRegex(ValueError, "Historical reconstruction"):
            build_snapshot_dataset(
                [genuine, reconstructed],
                [],
                horizons=(1,),
            )
        with self.assertRaisesRegex(ValueError, "Stale feature capture"):
            build_snapshot_dataset([genuine, stale], [], horizons=(1,))

        reordered = _feature_snapshot(self.config, 1, factors=tuple(reversed(FACTORS)))
        with self.assertRaisesRegex(ValueError, "one ordered feature set"):
            build_snapshot_dataset([genuine, reordered], [], horizons=(1,))

    def test_label_symbol_mismatch_and_duplicate_binding_are_rejected(self):
        feature = _feature_snapshot(self.config, 0)
        label = _label_snapshot(feature, 0)
        duplicate = json.loads(json.dumps(label))
        with self.assertRaisesRegex(ValueError, "multiple LabelSnapshots"):
            build_snapshot_dataset(
                [feature], [label, duplicate], horizons=(1,)
            )

        draft = json.loads(json.dumps(label))
        for field in ("label_snapshot_id", "snapshot_fingerprint", "record_fingerprint"):
            draft.pop(field)
        draft["rows"][0]["symbol"] = "TEST_0"
        mismatched = finalize_label_snapshot(draft)
        with self.assertRaisesRegex(ValueError, "symbols do not match"):
            build_snapshot_dataset([feature], [mismatched], horizons=(1,))

    def test_store_loader_selects_the_matching_genuine_feature_set(self):
        features, labels = _evidence(self.config, count=3)
        feature_store = FeatureSnapshotStore(self.root / "features")
        label_store = LabelSnapshotStore(self.root / "features")
        for feature in features:
            feature_store.publish(feature)
        for label in labels:
            label_store.publish(label)
        feature_store.publish(
            _feature_snapshot(
                self.config, 3, historical_reconstruction=True
            )
        )

        dataset = load_snapshot_dataset(
            feature_store,
            label_store,
            horizons=(1,),
            required_factor_ids=[item.factor_id for item in FACTORS],
            exact_factor_set=True,
        )
        self.assertEqual(len(dataset.sessions), 3)
        self.assertTrue(dataset.genuine_pit_required)
        self.assertEqual(len(dataset.observations), 3)

    def test_manifest_rejects_a_rebound_source_id(self):
        features, labels = _evidence(self.config, count=2)
        dataset = build_snapshot_dataset(features, labels, horizons=(1,))
        store = SnapshotDatasetStore(self.root / "features")
        store.publish(dataset)
        path = store.datasets_root / f"{dataset.dataset_id}.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        value["source_snapshots"]["features"][0] = "fs_" + "f" * 32
        value["record_fingerprint"] = snapshot_dataset_record_fingerprint(value)
        path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "fingerprint does not match"):
            store.get(dataset.dataset_id)


class SnapshotLabTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.config = load_config(REPOSITORY_ROOT / "config" / "default.json")
        features, labels = _evidence(self.config)
        self.dataset = build_snapshot_dataset(features, labels, horizons=(1,))

    def test_factor_lab_consumes_materialized_values(self):
        engine = FactorLabEngine(
            self.config, FactorLabStore(self.root / "factor_lab")
        )
        record = engine.evaluate_snapshots(
            "alice",
            self.dataset,
            "momentum_60_5",
            horizons=(1,),
            step=1,
        )

        self.assertEqual(
            record["evidence"]["snapshot"]["kind"],
            "feature_snapshot_dataset",
        )
        self.assertEqual(
            record["evidence"]["snapshot"]["snapshot_id"],
            self.dataset.dataset_id,
        )
        self.assertAlmostEqual(record["results"][0]["mean_ic"], 1.0)

    def test_model_lab_walks_forward_on_realized_target_sessions(self):
        engine = ModelLabEngine(
            self.config, ModelLabStore(self.root / "model_lab")
        )
        record = engine.evaluate_snapshots(
            "alice",
            self.dataset,
            "ridge_v1",
            horizon=1,
            step=1,
        )

        self.assertEqual(
            record["evidence"]["snapshot"]["kind"],
            "feature_snapshot_dataset",
        )
        self.assertGreaterEqual(record["coverage"]["warmup_dates"], 12)
        self.assertGreaterEqual(record["results"]["model"]["dates"], 24)
        self.assertIn("target_session", record["protocol"]["leakage_guard"])

        features, labels = _evidence(self.config, target_offset=100)
        unavailable = build_snapshot_dataset(features, labels, horizons=(1,))
        with self.assertRaisesRegex(RuntimeError, "fewer than 24"):
            engine.evaluate_snapshots(
                "alice", unavailable, "ridge_v1", horizon=1, step=1
            )

        features, labels = _evidence(self.config, realized_delay=100)
        unrealized = build_snapshot_dataset(features, labels, horizons=(1,))
        with self.assertRaisesRegex(RuntimeError, "fewer than 24"):
            engine.evaluate_snapshots(
                "alice", unrealized, "ridge_v1", horizon=1, step=1
            )


class SnapshotArtifactPipelineTests(unittest.TestCase):
    def test_artifact_fit_uses_exact_mixed_provider_dataset_sources(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            fixture_config = load_config(
                REPOSITORY_ROOT / "config" / "default.json"
            )
            runtime_config = SimpleNamespace(
                feature_store_dir=root / "features",
                project_root=root,
            )
            features = [
                _feature_snapshot(fixture_config, 0, provider="provider-alpha"),
                _feature_snapshot(fixture_config, 1, provider="provider-beta"),
            ]
            labels = [
                _label_snapshot(feature, index)
                for index, feature in enumerate(features)
            ]
            dataset = build_snapshot_dataset(features, labels, horizons=(1,))

            feature_store = FeatureSnapshotStore(runtime_config.feature_store_dir)
            label_store = LabelSnapshotStore(runtime_config.feature_store_dir)
            for feature in features:
                feature_store.publish(feature)
            for label in labels:
                label_store.publish(label)
            SnapshotDatasetStore(runtime_config.feature_store_dir).publish(dataset)

            unlisted_feature = _feature_snapshot(
                fixture_config, 0, provider="provider-unlisted"
            )
            unlisted_label = _label_snapshot(unlisted_feature, 99)
            unlisted_revision = _label_snapshot(
                features[0], 100, target_offset=2
            )
            feature_store.publish(unlisted_feature)
            label_store.publish(unlisted_label)
            label_store.publish(unlisted_revision)

            evaluation_created = datetime(
                2026, 1, 10, 15, 30, tzinfo=CHINA_TIMEZONE
            )
            evaluation = {
                "created_at": evaluation_created.isoformat(),
                "model": {"model_id": "ridge_v1"},
                "parameters": {
                    "horizon": 1,
                    "start": features[0]["as_of_session"],
                },
                "evidence": {
                    "snapshot": dataset.evidence(),
                    "universe": {
                        "name": fixture_config.universe_name,
                        "security_master_sha256": (
                            fixture_config.security_master.fingerprint()
                        ),
                    },
                },
            }
            expected_result = {"model_artifact_id": "ma_" + "a" * 32}
            binding = {"factor_ids": list(dataset.factor_ids)}
            with (
                patch("ai_trade.pipeline_cli.ModelLabEngine") as engine_class,
                patch(
                    "ai_trade.pipeline_cli.evaluation_binding",
                    return_value=binding,
                ),
                patch(
                    "ai_trade.pipeline_cli.fit_linear_artifact",
                    return_value=expected_result,
                ) as fit,
            ):
                engine_class.return_value.get.return_value = evaluation
                result = fit_model_artifact(
                    runtime_config,
                    "mdl_" + "b" * 32,
                    training_cutoff=datetime(
                        2026, 1, 9, 15, 30, tzinfo=CHINA_TIMEZONE
                    ),
                )

            self.assertEqual(result, expected_result)
            pairs = fit.call_args.args[0]
            self.assertEqual(
                [pair["feature"]["snapshot_id"] for pair in pairs],
                list(dataset.feature_snapshot_ids),
            )
            self.assertEqual(
                [pair["label"]["label_snapshot_id"] for pair in pairs],
                list(dataset.label_snapshot_ids),
            )
            self.assertNotIn(
                unlisted_feature["snapshot_id"],
                {pair["feature"]["snapshot_id"] for pair in pairs},
            )
            self.assertNotIn(
                unlisted_revision["label_snapshot_id"],
                {pair["label"]["label_snapshot_id"] for pair in pairs},
            )


class SnapshotCliTests(unittest.TestCase):
    def test_parser_exposes_explicit_snapshot_input(self):
        factor = build_parser().parse_args(
            [
                "factor-evaluate",
                "--factor",
                "momentum_60_5",
                "--snapshot-input",
                "--feature-set-id",
                "fset_fixture",
            ]
        )
        model = build_parser().parse_args(
            ["model-evaluate", "--snapshot-input", "--horizon", "1"]
        )
        shown = build_parser().parse_args(
            ["feature-dataset-show", "fds_" + "a" * 32]
        )
        self.assertTrue(factor.snapshot_input)
        self.assertEqual(factor.feature_set_id, "fset_fixture")
        self.assertTrue(model.snapshot_input)
        self.assertEqual(shown.dataset_id, "fds_" + "a" * 32)

    def test_snapshot_factor_command_never_opens_market_data(self):
        config = SimpleNamespace(feature_store_dir=Path("fixture-features"))
        dataset = MagicMock()
        engine = MagicMock()
        engine.evaluate_snapshots.return_value = {
            "evaluation_id": "eval_" + "a" * 32,
            "reused": False,
        }
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.FactorLabEngine", return_value=engine),
            patch("ai_trade.cli.MarketData") as market_data,
            patch(
                "ai_trade.feature_store.load_snapshot_dataset",
                return_value=dataset,
            ) as load_dataset,
            patch("ai_trade.feature_store.FeatureSnapshotStore") as feature_store,
            patch("ai_trade.feature_store.LabelSnapshotStore") as label_store,
            patch("ai_trade.feature_store.SnapshotDatasetStore") as dataset_store,
            redirect_stdout(output),
        ):
            status = main(
                [
                    "factor-evaluate",
                    "--factor",
                    "momentum_60_5",
                    "--horizons",
                    "1",
                    "--snapshot-input",
                ]
            )

        self.assertEqual(status, 0)
        market_data.assert_not_called()
        load_dataset.assert_called_once_with(
            feature_store.return_value,
            label_store.return_value,
            horizons=(1,),
            required_factor_ids=["momentum_60_5"],
            feature_set_id=None,
            require_genuine_pit=True,
        )
        dataset_store.return_value.publish.assert_called_once_with(dataset)
        engine.evaluate_snapshots.assert_called_once_with(
            "local-owner",
            dataset,
            "momentum_60_5",
            horizons=(1,),
            step=5,
        )

    def test_snapshot_model_command_never_opens_market_data(self):
        config = SimpleNamespace(feature_store_dir=Path("fixture-features"))
        dataset = MagicMock()
        engine = MagicMock()
        engine.evaluate_snapshots.return_value = {
            "evaluation_id": "mdl_" + "a" * 32,
            "reused": False,
        }
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.ModelLabEngine", return_value=engine),
            patch("ai_trade.cli.MarketData") as market_data,
            patch(
                "ai_trade.feature_store.load_snapshot_dataset",
                return_value=dataset,
            ),
            patch("ai_trade.feature_store.FeatureSnapshotStore"),
            patch("ai_trade.feature_store.LabelSnapshotStore"),
            patch("ai_trade.feature_store.SnapshotDatasetStore"),
            redirect_stdout(io.StringIO()),
        ):
            status = main(
                [
                    "model-evaluate",
                    "--model",
                    "ridge_v1",
                    "--factors",
                    "momentum_60_5,momentum_120_5",
                    "--horizon",
                    "1",
                    "--snapshot-input",
                ]
            )

        self.assertEqual(status, 0)
        market_data.assert_not_called()
        engine.evaluate_snapshots.assert_called_once_with(
            "local-owner",
            dataset,
            "ridge_v1",
            factor_ids=["momentum_60_5", "momentum_120_5"],
            horizon=1,
            step=5,
        )


if __name__ == "__main__":
    unittest.main()
