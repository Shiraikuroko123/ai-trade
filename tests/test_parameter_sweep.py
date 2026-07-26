from __future__ import annotations

import io
import json
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase
from unittest.mock import MagicMock, patch

from ai_trade.cli import build_parser, main
from ai_trade.config import load_config
from ai_trade.hypothesis_lab.sweep import (
    MAX_TOTAL_VARIANTS,
    ParameterSweepEngine,
)
from ai_trade.strategy_lab import StrategyLabEngine, StrategyLabStore


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class _Market:
    def __init__(self) -> None:
        self._latest = date(2026, 7, 24)
        self.manifest_sha256 = "b" * 64

    def latest_date(self):
        return self._latest

    def snapshot_metadata(self):
        return {
            "provider": "test-cache",
            "latest_common_session": self._latest.isoformat(),
            "latest_benchmark_session": self._latest.isoformat(),
            "manifest": {"snapshot_id": "sweep-test-snapshot"},
            "universe": {"security_master_sha256": "c" * 64},
            "symbols": {
                "510300": {"last": self._latest.isoformat(), "sha256": "d" * 64}
            },
        }


class _Backtest:
    """Sharpe improves as lookback_days moves above the baseline value."""

    def __init__(self, config, market, strategy_settings=None):
        self.settings = strategy_settings or config.strategy

    def run(self, start=None, end=None, initial_cash=None):
        sharpe = 0.8 + (self.settings.lookback_days - 126) * 0.001
        return SimpleNamespace(
            metadata={"start": "2022-01-04", "end": "2026-07-24"},
            metrics={
                "total_return": 0.25,
                "cagr": 0.05,
                "sharpe": sharpe,
                "max_drawdown": -0.05,
                "turnover": 1.2,
                "transaction_costs": 100.0,
            },
        )


class ParameterSweepTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        self.config = load_config(REPOSITORY_ROOT / "config" / "default.json")
        self.strategy_lab = StrategyLabEngine(
            self.config, StrategyLabStore(root / "strategy_lab")
        )
        self.engine = ParameterSweepEngine(self.config, self.strategy_lab)
        self.engine.root = root / "hypothesis_lab"
        self.market = _Market()

    def _execute(self, **kwargs):
        with patch("ai_trade.hypothesis_lab.sweep.BacktestEngine", _Backtest):
            return self.engine.execute("alice", self.market, **kwargs)

    def test_sweep_ranks_variants_by_objective_and_stays_exploratory(self):
        record = self._execute(
            parameters=["strategy.lookback_days", "strategy.rebalance_days"],
            points=4,
        )

        self.assertFalse(record["reused"])
        self.assertEqual(
            record["parameters_swept"],
            ["strategy.lookback_days", "strategy.rebalance_days"],
        )
        self.assertLessEqual(len(record["variants"]), 8)
        deltas = [item["objective_delta"] for item in record["ranking"]]
        self.assertEqual(deltas, sorted(deltas, reverse=True))
        top = record["ranking"][0]
        self.assertEqual(top["parameter"], "strategy.lookback_days")
        self.assertGreater(top["value"], 126)
        self.assertIn("exploratory", record["disclosure"].lower())
        self.assertEqual(
            record["safety"],
            {
                "research_only": True,
                "exploratory_not_confirmatory": True,
                "may_register_hypothesis": False,
                "may_create_candidate": False,
                "may_approve": False,
                "may_activate": False,
                "may_trade": False,
            },
        )
        self.assertEqual(self.engine.list("bob")["sweeps"], [])

    def test_repeat_sweep_is_idempotent_and_objective_forks(self):
        first = self._execute(parameters=["strategy.lookback_days"], points=4)
        second = self._execute(parameters=["strategy.lookback_days"], points=4)
        third = self._execute(
            parameters=["strategy.lookback_days"], points=4, objective="turnover"
        )
        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["sweep_id"], second["sweep_id"])
        self.assertNotEqual(first["sweep_id"], third["sweep_id"])
        self.assertEqual(self.engine.list("alice")["summary"]["total"], 2)

    def test_variant_budget_and_inputs_fail_closed(self):
        with patch("ai_trade.hypothesis_lab.sweep.MAX_TOTAL_VARIANTS", 5):
            with self.assertRaisesRegex(ValueError, "bound is 5"):
                self._execute(
                    parameters=[
                        "strategy.lookback_days",
                        "strategy.rebalance_days",
                    ],
                    points=4,
                )
        with self.assertRaisesRegex(ValueError, "not sweepable"):
            self._execute(parameters=["strategy.weighting_method"])
        with self.assertRaisesRegex(ValueError, "objective"):
            self._execute(objective="calmar")
        with self.assertRaisesRegex(ValueError, "points"):
            self._execute(points=1)
        self.assertEqual(self.engine.list("alice")["summary"]["total"], 0)

    def test_tampered_sweep_record_is_rejected_on_read(self):
        record = self._execute(parameters=["strategy.lookback_days"], points=4)
        path = (
            self.engine.owner_directory("alice")
            / "sweeps"
            / f"{record['sweep_id']}.json"
        )
        value = json.loads(path.read_text(encoding="utf-8"))
        value["ranking"][0]["objective_delta"] = 99.0
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Invalid sweep record"):
            self.engine.get("alice", record["sweep_id"])


class SweepAndReportCliTests(TestCase):
    def test_parser_exposes_sweep_and_report_commands(self):
        sweep = build_parser().parse_args(
            [
                "parameter-sweep",
                "--objective",
                "max_drawdown",
                "--parameters",
                "strategy.lookback_days",
                "--points",
                "6",
            ]
        )
        self.assertEqual(sweep.objective, "max_drawdown")
        self.assertEqual(sweep.points, 6)
        listed = build_parser().parse_args(["parameter-sweeps", "--limit", "7"])
        self.assertEqual(listed.limit, 7)
        shown = build_parser().parse_args(
            ["parameter-sweep-show", "sweep_" + "a" * 32]
        )
        self.assertEqual(shown.sweep_id, "sweep_" + "a" * 32)
        report = build_parser().parse_args(
            ["research-report", "--output", "reports/custom.md"]
        )
        self.assertEqual(report.output, "reports/custom.md")

    def test_sweep_reads_existing_cache_without_refreshing_provider(self):
        config = object()
        market = object()
        engine = MagicMock()
        engine.execute.return_value = {"sweep_id": "sweep_" + "a" * 32, "reused": False}
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.MarketData", return_value=market) as market_data,
            patch("ai_trade.cli.ParameterSweepEngine", return_value=engine),
            patch("ai_trade.cli._ensure_cache") as ensure_cache,
            redirect_stdout(output),
        ):
            status = main(
                ["parameter-sweep", "--parameters", "strategy.lookback_days"]
            )
        self.assertEqual(status, 0)
        market_data.assert_called_once_with(config, recover_snapshot=False)
        ensure_cache.assert_not_called()
        engine.execute.assert_called_once_with(
            "local-owner",
            market,
            objective="sharpe",
            parameters=["strategy.lookback_days"],
            points=4,
        )
        self.assertEqual(
            json.loads(output.getvalue())["sweep_id"], "sweep_" + "a" * 32
        )

    def test_research_report_dispatch_writes_without_market_requirement(self):
        config = object()
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch(
                "ai_trade.cli.write_research_report",
                return_value={"output": "reports/research_report.md"},
            ) as writer,
            redirect_stdout(output),
        ):
            status = main(["research-report"])
        self.assertEqual(status, 0)
        writer.assert_called_once_with(config, output=None)
        self.assertIn("research_report.md", output.getvalue())


if __name__ == "__main__":
    import unittest

    unittest.main()
