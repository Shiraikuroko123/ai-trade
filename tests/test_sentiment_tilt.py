from __future__ import annotations

import json
import shutil
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase

from ai_trade.config import load_config
from ai_trade.data.sentiment import SentimentTiltEngine


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _breadth(trade_date: str, advancers: int, decliners: int):
    return {
        "trade_date": trade_date,
        "breadth": [
            {"advancers": advancers, "decliners": decliners, "unchanged": 10}
        ],
    }


def _capital_flow(trade_date: str, share: float):
    return {"trade_date": trade_date, "summary": {"positive_main_share": share}}


def _news(trade_date: str, scores: list[float]):
    return {
        "trade_date": trade_date,
        "items": [
            {"sentiment_annotation": {"score": value, "method": "lexicon-v1"}}
            for value in scores
        ],
    }


class SentimentTiltTests(TestCase):
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

    def _engine(self, breadth, capital_flow, news):
        return SentimentTiltEngine(
            self.config,
            readers={
                "breadth": lambda: breadth,
                "capital_flow": lambda: capital_flow,
                "news_lexicon": lambda: news,
            },
        )

    def test_composes_bounded_explainable_tilt_from_three_components(self):
        engine = self._engine(
            _breadth("2026-07-24", 900, 300),
            _capital_flow("2026-07-24", 0.75),
            _news("2026-07-24", [0.5] * 6),
        )
        record = engine.compose()

        self.assertFalse(record["reused"])
        self.assertEqual(record["trade_date"], "2026-07-24")
        self.assertEqual(record["available_components"], 3)
        expected = ((2 * 900 / 1200 - 1) + (2 * 0.75 - 1) + 0.5) / 3
        self.assertAlmostEqual(record["tilt_score"], expected)
        self.assertEqual(record["tilt_label"], "RISK_ON_TILT")
        self.assertTrue(record["safety"]["assistant_coverage_unchanged"])
        again = engine.compose()
        self.assertTrue(again["reused"])
        self.assertEqual(again["revision"], 1)

    def test_mismatched_dates_are_excluded_and_two_sources_still_compose(self):
        engine = self._engine(
            _breadth("2026-07-24", 300, 900),
            _capital_flow("2026-07-24", 0.25),
            _news("2026-07-23", [-0.5] * 6),
        )
        record = engine.compose()
        self.assertEqual(record["available_components"], 2)
        news = next(
            item for item in record["components"] if item["name"] == "news_lexicon"
        )
        self.assertFalse(news["available"])
        self.assertIn("剔除", news["detail"])
        self.assertEqual(record["tilt_label"], "RISK_OFF_TILT")

    def test_single_source_fails_closed(self):
        engine = self._engine(
            _breadth("2026-07-24", 500, 500),
            {"error": "not refreshed"},
            _news("2026-07-24", [0.1]),
        )
        with self.assertRaisesRegex(RuntimeError, "单一来源"):
            engine.compose()
        self.assertEqual(
            SentimentTiltEngine(self.config).list()["summary"]["dates"], 0
        )

    def test_changed_evidence_appends_a_superseding_revision(self):
        first = self._engine(
            _breadth("2026-07-24", 600, 600),
            _capital_flow("2026-07-24", 0.5),
            _news("2026-07-24", [0.0] * 6),
        ).compose()
        second = self._engine(
            _breadth("2026-07-24", 800, 400),
            _capital_flow("2026-07-24", 0.5),
            _news("2026-07-24", [0.0] * 6),
        ).compose()
        self.assertEqual(first["revision"], 1)
        self.assertEqual(second["revision"], 2)
        self.assertEqual(second["supersedes"], first["record_fingerprint"])
        self.assertEqual(first["tilt_label"], "NEUTRAL")

    def test_tampered_record_is_rejected_on_read(self):
        engine = self._engine(
            _breadth("2026-07-24", 900, 300),
            _capital_flow("2026-07-24", 0.9),
            _news("2026-07-24", [0.4] * 6),
        )
        record = engine.compose()
        path = (
            self.config.project_root
            / "state"
            / "sentiment"
            / f"tilt_2026-07-24_r{record['revision']:03d}.json"
        )
        value = json.loads(path.read_text(encoding="utf-8"))
        value["tilt_label"] = "NEUTRAL"
        path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Invalid sentiment record"):
            SentimentTiltEngine(self.config).latest(date(2026, 7, 24))


if __name__ == "__main__":
    import unittest

    unittest.main()
