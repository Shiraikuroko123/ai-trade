from __future__ import annotations

import shutil
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import TestCase

from ai_trade.config import load_config
from ai_trade.data.sentiment import SentimentTiltEngine
from ai_trade.factor_lab import CustomFactorStore
from ai_trade.web.service import DashboardService


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]

LAB_SECTIONS = ("factors", "models", "hypotheses", "sweeps", "sentiment")


class ResearchLabsProjectionTests(TestCase):
    """The research payload must project the deterministic labs read-only."""

    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        root = Path(self.temporary.name)
        (root / "config").mkdir()
        for name in ("default.json", "security_master.json"):
            shutil.copy(
                REPOSITORY_ROOT / "config" / name, root / "config" / name
            )
        self.config = load_config(root / "config" / "default.json")

    def test_research_payload_includes_every_lab_section(self):
        research = DashboardService(self.config).research()
        labs = research["labs"]

        self.assertEqual(labs["schema_version"], 1)
        for name in LAB_SECTIONS:
            self.assertIn(name, labs, name)
        # Empty stores are still readable evidence, not errors.
        self.assertTrue(labs["factors"]["available"])
        self.assertEqual(labs["factors"]["total"], 0)
        self.assertEqual(labs["factors"]["custom_factors"], [])
        self.assertTrue(labs["models"]["available"])
        self.assertEqual(labs["models"]["total"], 0)
        self.assertTrue(labs["hypotheses"]["available"])
        self.assertEqual(labs["hypotheses"]["registered"], 0)
        self.assertEqual(labs["hypotheses"]["runs"], [])
        self.assertTrue(labs["sweeps"]["available"])
        self.assertEqual(labs["sweeps"]["total"], 0)
        # No composed tilt yet: the section fails soft with a reason.
        self.assertFalse(labs["sentiment"]["available"])
        self.assertIn("error", labs["sentiment"])
        self.assertEqual(
            labs["safety"],
            {
                "research_only": True,
                "read_only": True,
                "creates_no_signal": True,
                "orders_created": False,
            },
        )

    def test_custom_factors_and_sentiment_evidence_are_projected(self):
        CustomFactorStore(self.config).define(
            "local-owner",
            name="gap_rev",
            expression="delay(close, 1) / open - 1",
            direction=-1,
            label="隔夜跳空反转",
        )
        SentimentTiltEngine(
            self.config,
            readers={
                "breadth": lambda: {
                    "trade_date": "2026-07-24",
                    "breadth": [
                        {"advancers": 900, "decliners": 300, "unchanged": 10}
                    ],
                },
                "capital_flow": lambda: {
                    "trade_date": "2026-07-24",
                    "summary": {"positive_main_share": 0.75},
                },
                "news_lexicon": lambda: {"error": "not refreshed"},
            },
        ).compose()

        labs = DashboardService(self.config).research()["labs"]

        custom = labs["factors"]["custom_factors"]
        self.assertEqual(len(custom), 1)
        self.assertEqual(custom[0]["name"], "gap_rev")
        self.assertEqual(custom[0]["direction"], -1)
        self.assertEqual(custom[0]["expression"], "delay(close,1)/open-1")
        sentiment = labs["sentiment"]
        self.assertTrue(sentiment["available"])
        self.assertEqual(sentiment["trade_date"], "2026-07-24")
        self.assertEqual(sentiment["available_components"], 2)
        self.assertEqual(sentiment["tilt_label"], "RISK_ON_TILT")

    def test_broken_config_fails_soft_per_section(self):
        service = DashboardService(
            SimpleNamespace(reports_dir=Path(self.temporary.name))
        )
        labs = service.research()["labs"]
        for name in LAB_SECTIONS:
            self.assertFalse(labs[name]["available"], name)
            self.assertTrue(labs[name]["error"], name)


if __name__ == "__main__":
    import unittest

    unittest.main()
