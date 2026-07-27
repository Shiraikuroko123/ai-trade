from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest

from ai_trade.config import load_config
from ai_trade.feature_store import (
    FeatureSnapshotBuilder,
    FeatureSnapshotStore,
    ForwardEvidenceRunner,
    LabelSnapshotBuilder,
    LabelSnapshotStore,
    training_pairs,
)
from ai_trade.feature_store.schema import is_genuine_pit_snapshot
from ai_trade.models import Bar


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class _Market:
    def __init__(self, config) -> None:
        first = date(2026, 1, 1)
        self.calendar = [first + timedelta(days=index) for index in range(205)]
        self.completed_through = self.calendar[-1]
        self.latest_common_session = self.calendar[-1]
        self.manifest_sha256 = "a" * 64
        self.manifest_snapshot_id = "market-fixture-v1"
        self.config = config
        self.symbols = {}
        for symbol_index, instrument in enumerate(config.instruments):
            bars = []
            for index, on_date in enumerate(self.calendar):
                close = 1.0 + symbol_index * 0.01 + index * 0.002
                bars.append(
                    Bar(
                        on_date,
                        close - 0.001,
                        close,
                        close + 0.002,
                        close - 0.002,
                        100_000.0 + index,
                        (100_000.0 + index) * close,
                    )
                )
            self.symbols[instrument.symbol] = SimpleNamespace(bars=bars)

    def bar(self, symbol, on_date):
        return next(
            (bar for bar in self.symbols[symbol].bars if bar.date == on_date),
            None,
        )

    def history(self, symbol, on_date, count):
        values = [
            bar for bar in self.symbols[symbol].bars if bar.date <= on_date
        ]
        return values[-count:]

    def trading_status(self, symbol, on_date):
        return self.config.security_master.trading_status(symbol, on_date)

    def snapshot_metadata(self):
        return {
            "provider": "fixture",
            "adjustment": "forward",
            "completed_session_cutoff": self.completed_through.isoformat(),
            "latest_common_session": self.latest_common_session.isoformat(),
            "latest_benchmark_session": self.calendar[-1].isoformat(),
            "manifest": {"snapshot_id": self.manifest_snapshot_id},
        }

    def append_future_session(self) -> None:
        future = self.calendar[-1] + timedelta(days=1)
        self.calendar.append(future)
        self.completed_through = future
        self.latest_common_session = future
        self.manifest_sha256 = "b" * 64
        self.manifest_snapshot_id = "market-fixture-v2"
        for offset, item in enumerate(self.symbols.values()):
            previous = item.bars[-1]
            close = previous.close + 0.002 + offset * 0.000001
            item.bars.append(
                Bar(
                    future,
                    close - 0.001,
                    close,
                    close + 0.002,
                    close - 0.002,
                    previous.volume + 1,
                    (previous.volume + 1) * close,
                )
            )


class FeatureSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = load_config(REPOSITORY_ROOT / "config" / "default.json")
        self.root = Path(self.temporary.name) / "feature_store"
        self.market = _Market(self.config)
        self.store = FeatureSnapshotStore(self.root)
        self.builder = FeatureSnapshotBuilder(self.config, self.store)
        self.as_of = self.market.calendar[-11]

    def test_snapshot_is_immutable_label_free_and_idempotent(self):
        first = self.builder.build(self.market, as_of_session=self.as_of)
        second = self.builder.build(self.market, as_of_session=self.as_of)

        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["snapshot_id"], second["snapshot_id"])
        self.assertEqual(first["snapshot_fingerprint"], second["snapshot_fingerprint"])
        self.assertTrue(first["historical_reconstruction"])
        self.assertTrue(first["safety"]["contains_no_label"])
        self.assertNotIn("label", first)
        self.assertEqual(
            [row["symbol"] for row in first["rows"]],
            first["universe"]["active_symbols"],
        )
        self.assertTrue(
            all(
                set(row["values"]) | set(row["missing"])
                == {
                    item["factor_id"]
                    for item in first["feature_set"]["factors"]
                }
                for row in first["rows"]
            )
        )

    def test_future_append_does_not_change_the_as_of_identity(self):
        before = self.builder.build(
            self.market, as_of_session=self.as_of, publish=False
        )
        self.market.append_future_session()
        after = self.builder.build(
            self.market, as_of_session=self.as_of, publish=False
        )

        self.assertNotEqual(
            before["source"]["cache_manifest_sha256"],
            after["source"]["cache_manifest_sha256"],
        )
        self.assertEqual(
            before["snapshot_fingerprint"], after["snapshot_fingerprint"]
        )
        self.assertEqual(before["snapshot_id"], after["snapshot_id"])
        self.assertEqual(before["rows"], after["rows"])

    def test_tampered_snapshot_fails_closed(self):
        snapshot = self.builder.build(self.market, as_of_session=self.as_of)
        path = (
            self.store.snapshots_root
            / self.as_of.isoformat()
            / f"{snapshot['snapshot_id']}.json"
        )
        value = json.loads(path.read_text(encoding="utf-8"))
        value["rows"][0]["values"]["momentum_120_5"] = 999.0
        path.write_text(json.dumps(value), encoding="utf-8")

        with self.assertRaisesRegex(RuntimeError, "fingerprint"):
            self.store.get(snapshot["snapshot_id"], on_date=self.as_of)

    def test_nonhistorical_snapshot_requires_same_session_capture(self):
        cutoff = datetime.combine(
            self.market.latest_common_session,
            datetime.min.time(),
            timezone(timedelta(hours=8)),
        ).replace(hour=16)
        snapshot = self.builder.build(
            self.market,
            historical_reconstruction=False,
            knowledge_cutoff=cutoff,
            publish=False,
        )
        self.assertFalse(snapshot["historical_reconstruction"])
        self.assertTrue(is_genuine_pit_snapshot(snapshot))

        with self.assertRaisesRegex(ValueError, "latest common session"):
            self.builder.build(
                self.market,
                as_of_session=self.as_of,
                historical_reconstruction=False,
                knowledge_cutoff=cutoff,
                publish=False,
            )

    def test_nonhistorical_snapshot_rejects_a_stale_latest_common_session(self):
        cutoff = datetime.combine(
            self.market.latest_common_session,
            datetime.min.time(),
            timezone(timedelta(hours=8)),
        ).replace(hour=16)
        self.market.completed_through += timedelta(days=1)

        with self.assertRaisesRegex(ValueError, "current through"):
            self.builder.build(
                self.market,
                historical_reconstruction=False,
                knowledge_cutoff=cutoff,
                publish=False,
            )

    def test_stale_legacy_capture_is_not_genuine_pit(self):
        snapshot = self.builder.build(
            self.market, as_of_session=self.as_of, publish=False
        )
        snapshot["historical_reconstruction"] = False
        snapshot["source"]["completed_session_cutoff"] = (
            self.market.completed_through.isoformat()
        )

        self.assertFalse(is_genuine_pit_snapshot(snapshot))

    def test_snapshot_records_actual_file_provider_instead_of_configured_primary(self):
        original = self.market.snapshot_metadata

        def fallback_metadata():
            value = original()
            value["provider"] = "eastmoney"
            value["manifest"] = {
                "snapshot_id": self.market.manifest_snapshot_id,
                "files": {
                    symbol: {
                        "source": "tencent_network_fallback",
                        "source_provider": "tencent_newfqkline",
                    }
                    for symbol in self.market.symbols
                },
            }
            return value

        self.market.snapshot_metadata = fallback_metadata
        snapshot = self.builder.build(
            self.market, as_of_session=self.as_of, publish=False
        )

        self.assertEqual(snapshot["source"]["provider"], "tencent")

    def test_snapshot_discloses_a_stable_mixed_provider_set(self):
        original = self.market.snapshot_metadata
        first_symbol = next(iter(self.market.symbols))

        def mixed_metadata():
            value = original()
            value["manifest"] = {
                "snapshot_id": self.market.manifest_snapshot_id,
                "files": {
                    symbol: {
                        "source_provider": (
                            "eastmoney_push2his" if symbol == first_symbol
                            else "tencent_newfqkline"
                        )
                    }
                    for symbol in self.market.symbols
                },
            }
            return value

        self.market.snapshot_metadata = mixed_metadata
        snapshot = self.builder.build(
            self.market, as_of_session=self.as_of, publish=False
        )

        self.assertEqual(
            snapshot["source"]["provider"], "mixed:eastmoney,tencent"
        )

    def test_snapshot_rejects_unidentified_file_provenance(self):
        original = self.market.snapshot_metadata

        def unknown_metadata():
            value = original()
            value["manifest"] = {
                "snapshot_id": self.market.manifest_snapshot_id,
                "files": {"159901": {"source": "unidentified"}},
            }
            return value

        self.market.snapshot_metadata = unknown_metadata
        with self.assertRaisesRegex(RuntimeError, "actual provider"):
            self.builder.build(
                self.market, as_of_session=self.as_of, publish=False
            )


class LabelSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = load_config(REPOSITORY_ROOT / "config" / "default.json")
        self.root = Path(self.temporary.name) / "feature_store"
        self.market = _Market(self.config)
        self.feature_builder = FeatureSnapshotBuilder(
            self.config, FeatureSnapshotStore(self.root)
        )
        self.label_builder = LabelSnapshotBuilder(
            self.config, LabelSnapshotStore(self.root)
        )
        self.as_of = self.market.calendar[-11]
        self.feature = self.feature_builder.build(
            self.market, as_of_session=self.as_of
        )

    def test_labels_are_separate_and_respect_training_cutoff(self):
        label = self.label_builder.build(self.feature, self.market, horizon=5)
        realized = datetime.fromisoformat(label["realized_at"])
        created = datetime.fromisoformat(label["created_at"].replace("Z", "+00:00"))

        self.assertEqual(label["feature_snapshot_id"], self.feature["snapshot_id"])
        self.assertTrue(label["safety"]["separate_from_features"])
        self.assertEqual(
            training_pairs(
                [self.feature],
                [label],
                training_cutoff=realized - timedelta(seconds=1),
            ),
            [],
        )
        paired = training_pairs(
            [self.feature], [label], training_cutoff=created
        )
        self.assertEqual(len(paired), 1)
        self.assertEqual(paired[0]["label"]["label_snapshot_id"], label["label_snapshot_id"])
        self.assertLess(realized, created)
        with self.assertRaisesRegex(ValueError, "Historical reconstruction"):
            training_pairs(
                [self.feature],
                [label],
                training_cutoff=created,
                require_genuine_pit=True,
            )

    def test_label_store_reuses_identical_evidence(self):
        first = self.label_builder.build(self.feature, self.market, horizon=5)
        second = self.label_builder.build(self.feature, self.market, horizon=5)
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["snapshot_fingerprint"], second["snapshot_fingerprint"])

    def test_label_records_the_provider_that_supplied_current_market_files(self):
        original = self.market.snapshot_metadata

        def fallback_metadata():
            value = original()
            value["provider"] = "eastmoney"
            value["manifest"] = {
                "snapshot_id": self.market.manifest_snapshot_id,
                "files": {
                    symbol: {"source": "tencent_network_fallback"}
                    for symbol in self.market.symbols
                },
            }
            return value

        self.market.snapshot_metadata = fallback_metadata
        label = self.label_builder.build(
            self.feature, self.market, horizon=5, publish=False
        )

        self.assertEqual(label["source"]["provider"], "tencent")


class ForwardEvidenceRunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.config = load_config(REPOSITORY_ROOT / "config" / "default.json")
        self.root = Path(self.temporary.name) / "feature_store"
        self.market = _Market(self.config)
        self.feature_store = FeatureSnapshotStore(self.root)
        self.label_store = LabelSnapshotStore(self.root)
        self.runner = ForwardEvidenceRunner(
            self.config,
            feature_store=self.feature_store,
            label_store=self.label_store,
        )

    def test_run_is_idempotent_and_materializes_a_newly_mature_label(self):
        first = self.runner.run(self.market, horizons=(1,))
        repeated = self.runner.run(self.market, horizons=(1,))

        self.assertFalse(first["feature"]["reused"])
        self.assertTrue(repeated["feature"]["reused"])
        self.assertEqual(
            first["feature"]["snapshot_id"],
            repeated["feature"]["snapshot_id"],
        )
        self.assertTrue(first["feature"]["genuine_pit"])
        self.assertEqual(first["labels"]["pending_count"], 1)

        self.market.append_future_session()
        matured = self.runner.run(self.market, horizons=(1,))
        labels = self.label_store.list_for_feature(
            str(first["feature"]["snapshot_id"])
        )

        self.assertEqual(matured["labels"]["created_count"], 1)
        self.assertEqual(len(labels), 1)
        self.assertEqual(labels[0]["horizon"], 1)
        self.assertEqual(
            labels[0]["target_session"], self.market.calendar[-1].isoformat()
        )

    def test_run_rejects_a_stale_cache_before_writing(self):
        self.market.completed_through += timedelta(days=1)

        with self.assertRaisesRegex(ValueError, "current through"):
            self.runner.run(self.market, horizons=(5, 20))
        self.assertEqual(self.feature_store.sessions(), [])

    def test_horizons_must_be_unique_and_ascending(self):
        for horizons in ((20, 5), (5, 5), (0,), ()):
            with self.subTest(horizons=horizons):
                with self.assertRaisesRegex(ValueError, "unique ascending"):
                    self.runner.run(self.market, horizons=horizons)


if __name__ == "__main__":
    unittest.main()
