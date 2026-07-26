from __future__ import annotations

import json
import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from ai_trade.config import load_config
from ai_trade.research_report import (
    generate_research_report,
    write_research_report,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ResearchReportTests(TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / "config").mkdir()
        for name in ("default.json", "security_master.json"):
            shutil.copy(
                REPOSITORY_ROOT / "config" / name, self.root / "config" / name
            )
        self.config = load_config(self.root / "config" / "default.json")
        (self.root / "reports").mkdir()
        (self.root / "state").mkdir()

    def _write_fixtures(self) -> None:
        (self.root / "reports" / "backtest_summary.json").write_text(
            json.dumps(
                {
                    "strategy_metrics": {
                        "total_return": 0.25,
                        "cagr": 0.05,
                        "sharpe": 1.21,
                        "max_drawdown": -0.08,
                        "turnover": 12.5,
                    },
                    "benchmark_metrics": {
                        "total_return": 0.10,
                        "cagr": 0.02,
                        "sharpe": 0.40,
                        "max_drawdown": -0.20,
                        "turnover": 0.0,
                    },
                    "metadata": {
                        "start": "2016-01-04",
                        "end": "2026-07-24",
                        "order_rejection_count": 3,
                    },
                }
            ),
            encoding="utf-8",
        )
        (self.root / "reports" / "walk_forward.json").write_text(
            json.dumps(
                {
                    "aggregate": {
                        "segments": 8,
                        "positive_segments": 6,
                        "oos_total_return": 1.18,
                        "oos_cagr": 0.11,
                        "oos_sharpe": 1.21,
                        "oos_max_drawdown": -0.14,
                    },
                    "selection_disclosure": "Development walk-forward report.",
                }
            ),
            encoding="utf-8",
        )
        (self.root / "reports" / "validation_report.json").write_text(
            json.dumps(
                {
                    "research_gates": {
                        "checks": [
                            {"id": "a", "label": "gate a", "passed": True},
                            {"id": "b", "label": "gate b", "passed": False},
                        ]
                    },
                    "bootstrap": {},
                    "cost_stress": {},
                }
            ),
            encoding="utf-8",
        )
        (self.root / "state" / "paper_state.json").write_text(
            json.dumps(
                {
                    "account_id": "a" * 32,
                    "cash": 79928.98,
                    "positions": {"510500": 1400, "159915": 2100},
                    "last_equity": 97858.78,
                    "last_run_date": "2026-07-24",
                    "cooldown_remaining": 0,
                }
            ),
            encoding="utf-8",
        )

    def test_report_projects_available_evidence_with_explicit_gaps(self):
        self._write_fixtures()
        report = generate_research_report(self.config)

        statuses = {item["id"]: item["status"] for item in report["sections"]}
        self.assertEqual(statuses["backtest"], "available")
        self.assertEqual(statuses["walk_forward"], "available")
        self.assertEqual(statuses["validation"], "available")
        self.assertEqual(statuses["paper"], "available")
        # No market cache exists in this workspace: the section must say so
        # instead of fabricating a snapshot.
        self.assertEqual(statuses["market"], "unavailable")
        markdown = report["markdown"]
        self.assertIn("25.00%", markdown)
        self.assertIn("1.210", markdown)
        self.assertIn("研究门禁: 1/2 通过", markdown)
        self.assertIn("gate b", markdown)
        self.assertIn("持仓标的数: 2", markdown)
        self.assertIn("（不可用）", markdown)
        self.assertIn("research_only", markdown)
        self.assertIn("尚无因子评估记录", markdown)

    def test_missing_sources_stay_explicit_and_fingerprint_is_stable(self):
        report = generate_research_report(self.config)
        statuses = {item["id"]: item["status"] for item in report["sections"]}
        for section in ("backtest", "walk_forward", "validation", "paper"):
            self.assertEqual(statuses[section], "unavailable")
        again = generate_research_report(self.config)
        self.assertEqual(
            report["content_fingerprint"], again["content_fingerprint"]
        )

    def test_write_stays_inside_the_workspace(self):
        self._write_fixtures()
        summary = write_research_report(self.config)
        output = Path(summary["output"])
        self.assertTrue(output.is_file())
        self.assertEqual(output.name, "research_report.md")
        self.assertIn(str(self.root), str(output))
        with self.assertRaisesRegex(ValueError, "inside the workspace"):
            write_research_report(self.config, output="../outside.md")


if __name__ == "__main__":
    import unittest

    unittest.main()
