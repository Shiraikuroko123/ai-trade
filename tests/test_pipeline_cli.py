from __future__ import annotations

from datetime import date, datetime, timezone
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from contextlib import redirect_stdout
from unittest.mock import patch

from ai_trade.cli import build_parser, main
from ai_trade.config import _validate_shadow_path_boundaries
from ai_trade.pipeline_cli import (
    append_shadow_event,
    shadow_projection,
    shadow_reconciliation,
)


class PipelineCliTests(unittest.TestCase):
    def test_parser_exposes_research_pipeline_without_execution_flags(self):
        feature = build_parser().parse_args(
            ["feature-build", "--as-of", "2026-07-24"]
        )
        self.assertEqual(feature.as_of, "2026-07-24")
        self.assertTrue(feature.live_capture)
        historical = build_parser().parse_args(
            ["feature-build", "--historical-reconstruction"]
        )
        self.assertFalse(historical.live_capture)
        forward = build_parser().parse_args(
            [
                "feature-forward-run",
                "--factors",
                "momentum_120_5,volatility_60",
                "--horizons",
                "5,20",
            ]
        )
        self.assertEqual(forward.horizons, "5,20")
        self.assertFalse(hasattr(forward, "refresh"))
        self.assertFalse(hasattr(forward, "trade"))

        artifact = build_parser().parse_args(
            [
                "model-artifact-fit",
                "mdl_" + "a" * 32,
                "--training-cutoff",
                "2026-07-24T15:30:00+08:00",
            ]
        )
        self.assertFalse(hasattr(artifact, "approve"))
        self.assertFalse(hasattr(artifact, "activate"))
        self.assertFalse(hasattr(artifact, "trade"))

        plan = build_parser().parse_args(
            [
                "portfolio-plan",
                "ps_" + "b" * 32,
                "--feature-date",
                "2026-07-24",
                "--execution-date",
                "2026-07-27",
                "--equity",
                "100000",
            ]
        )
        self.assertEqual(plan.equity, 100000.0)

    def test_feature_command_dispatches_canonical_date(self):
        output = io.StringIO()
        expected = {
            "snapshot_id": "fs_" + "a" * 32,
            "safety": {"research_only": True},
        }
        config = object()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch(
                "ai_trade.pipeline_cli.build_feature_snapshot",
                return_value=expected,
            ) as build,
            redirect_stdout(output),
        ):
            status = main(
                [
                    "feature-build",
                    "--as-of",
                    "2026-07-24",
                    "--factors",
                    "momentum_60_5,volatility_60",
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue()), expected)
        build.assert_called_once_with(
            config,
            as_of_session=date(2026, 7, 24),
            live_capture=True,
            factor_ids=["momentum_60_5", "volatility_60"],
        )

    def test_forward_command_dispatches_factors_and_horizons(self):
        output = io.StringIO()
        expected = {
            "as_of_session": "2026-07-27",
            "feature": {"genuine_pit": True},
            "safety": {"research_only": True},
        }
        config = object()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch(
                "ai_trade.pipeline_cli.run_forward_evidence",
                return_value=expected,
            ) as run,
            redirect_stdout(output),
        ):
            status = main(
                [
                    "feature-forward-run",
                    "--factors",
                    "momentum_120_5,volatility_60",
                    "--horizons",
                    "5,20",
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue()), expected)
        run.assert_called_once_with(
            config,
            factor_ids=["momentum_120_5", "volatility_60"],
            horizons=(5, 20),
        )

    def test_shadow_cli_helpers_append_project_and_reconcile(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = SimpleNamespace(shadow_ledger_dir=root / "ledger")
            opening = root / "opening.json"
            opening.write_text(
                json.dumps({"cash": "100000", "currency": "CNY"}),
                encoding="utf-8",
            )
            event = append_shadow_event(
                config,
                "test-account",
                "opening_balance",
                occurred_at=datetime(2026, 7, 24, 8, tzinfo=timezone.utc),
                trading_session=date(2026, 7, 24),
                source="fixture",
                external_id="opening-1",
                payload_file=opening,
            )
            self.assertFalse(event["reused"])

            projection = shadow_projection(config, "test-account")
            self.assertEqual(projection["cash"], "100000")
            self.assertFalse(projection["safety"]["execution_enabled"])

            broker = root / "broker.json"
            broker.write_text(
                json.dumps({"cash": "100000", "positions": {}}),
                encoding="utf-8",
            )
            reconciliation = shadow_reconciliation(
                config,
                "test-account",
                broker_snapshot_file=broker,
            )
            self.assertTrue(reconciliation["clean"])
            self.assertFalse(reconciliation["safety"]["execution_enabled"])

            broker.write_text(
                json.dumps(
                    {
                        "cash": "0",
                        "positions": {},
                        "cash_tolerance": "100000",
                    }
                ),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "fields"):
                shadow_reconciliation(
                    config,
                    "test-account",
                    broker_snapshot_file=broker,
                )

    def test_shadow_decimal_expansion_is_rejected_before_persistence(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = SimpleNamespace(shadow_ledger_dir=root / "ledger")
            payload = root / "opening.json"
            payload.write_text(
                json.dumps({"cash": "1e100000000", "currency": "CNY"}),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "decimal"):
                append_shadow_event(
                    config,
                    "oversized-account",
                    "opening_balance",
                    occurred_at=datetime(2026, 7, 24, 8, tzinfo=timezone.utc),
                    trading_session=date(2026, 7, 24),
                    source="fixture",
                    external_id="opening-large",
                    payload_file=payload,
                )
            self.assertFalse((root / "ledger").exists())

    def test_shadow_ledger_cannot_overlap_feature_or_execution_state(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            with self.assertRaisesRegex(ValueError, "feature_store"):
                _validate_shadow_path_boundaries(
                    {
                        "shadow_account": {"ledger_dir": "state/features"},
                        "feature_store": {"root_dir": "state/features"},
                    },
                    project_root=root,
                )
            with self.assertRaisesRegex(ValueError, "broker.orders_file"):
                _validate_shadow_path_boundaries(
                    {
                        "shadow_account": {"ledger_dir": "state/broker"},
                        "broker": {"orders_file": "state/broker/orders.json"},
                    },
                    project_root=root,
                )


if __name__ == "__main__":
    unittest.main()
