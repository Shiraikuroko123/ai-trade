import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

from ai_trade.config import load_config
from ai_trade.research_journal import (
    JournalDraft,
    JournalQuery,
    ResearchJournalStore,
)
from ai_trade.web.service import DashboardService


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
MARKET_EVIDENCE = {
    "available": True,
    "date": "2026-07-17",
    "fingerprint": "a" * 64,
}
STRATEGY_EVIDENCE = {
    "available": True,
    "candidate_id": None,
    "fingerprint": "b" * 64,
    "lifecycle_state": "CONFIGURED",
}


def _draft(**overrides):
    values = {
        "research_date": date(2026, 7, 17),
        "category": "observation",
        "symbol": "510300",
        "title": "Closing review",
        "note": "Trend remains above the audited moving average.",
        "decision": "watch",
        "confidence": 60,
        "correction_of": None,
    }
    values.update(overrides)
    return JournalDraft(**values)


class ResearchJournalStoreTests(unittest.TestCase):
    def test_records_are_write_once_hashed_and_isolated_by_owner(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchJournalStore(temporary)
            entry = store.append(
                "Alice",
                _draft(),
                actor="alice",
                market_evidence=MARKET_EVIDENCE,
                strategy_evidence=STRATEGY_EVIDENCE,
                now=datetime(2026, 7, 17, 9, 30, tzinfo=timezone.utc),
            )

            self.assertRegex(entry["entry_id"], r"\Ajournal_[0-9a-f]{32}\Z")
            self.assertRegex(entry["entry_fingerprint"], r"\A[0-9a-f]{64}\Z")
            self.assertNotIn("owner", entry)
            self.assertEqual(entry["week_start"], "2026-07-13")
            self.assertFalse(entry["authority"]["execution_authorized"])
            self.assertFalse(entry["authority"]["strategy_changed"])

            alice = store.list("alice")
            bob = store.list("bob")
            self.assertEqual(alice["summary"]["total"], 1)
            self.assertEqual(alice["entries"], [entry])
            self.assertEqual(bob["entries"], [])
            self.assertNotIn("Alice", str(store.owner_directory("Alice")))

            path = (
                store.owner_directory("alice")
                / "entries"
                / f"{entry['entry_id']}.json"
            )
            persisted = json.loads(path.read_text(encoding="utf-8"))
            persisted["title"] = "Manually changed"
            path.write_text(json.dumps(persisted), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "fingerprint"):
                store.list("alice")

    def test_filters_weekly_summary_and_append_only_corrections(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchJournalStore(temporary)
            first = store.append(
                "owner",
                _draft(),
                actor="owner",
                market_evidence=MARKET_EVIDENCE,
                strategy_evidence=STRATEGY_EVIDENCE,
            )
            store.append(
                "owner",
                _draft(
                    research_date=date(2026, 7, 10),
                    category="risk",
                    symbol=None,
                    title="Weekly risk review",
                    note="Liquidity evidence needs another completed session.",
                    decision="not_recorded",
                    confidence=None,
                ),
                actor="owner",
                market_evidence=MARKET_EVIDENCE,
                strategy_evidence=STRATEGY_EVIDENCE,
            )
            correction = store.append(
                "owner",
                _draft(
                    title="Correction to closing review",
                    note="The audited line was EMA rather than SMA.",
                    category="decision",
                    correction_of=first["entry_id"],
                ),
                actor="owner",
                market_evidence=MARKET_EVIDENCE,
                strategy_evidence=STRATEGY_EVIDENCE,
            )

            filtered = store.list(
                "owner", JournalQuery(category="decision", query="EMA", limit=10)
            )
            self.assertEqual(filtered["summary"]["total"], 3)
            self.assertEqual(filtered["summary"]["matched"], 1)
            self.assertEqual(filtered["entries"][0]["entry_id"], correction["entry_id"])
            self.assertEqual(
                filtered["entries"][0]["correction_of"], first["entry_id"]
            )
            self.assertEqual(
                filtered["summary"]["by_week"],
                [{"week_start": "2026-07-13", "count": 1}],
            )

            with self.assertRaises(KeyError):
                store.append(
                    "owner",
                    _draft(correction_of="journal_" + "f" * 32),
                    actor="owner",
                    market_evidence=MARKET_EVIDENCE,
                    strategy_evidence=STRATEGY_EVIDENCE,
                )

    def test_dashboard_service_captures_context_without_changing_authority(self):
        source = load_config(REPOSITORY_ROOT / "config/default.json")
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(source, project_root=Path(temporary))
            service = DashboardService(config)
            symbol = config.instruments[0].symbol

            entry = service.append_research_journal(
                owner_id="local-owner",
                actor="local-owner",
                draft=_draft(symbol=symbol),
            )
            result = service.research(owner_id="local-owner")

            self.assertEqual(result["journal"]["entries"][0], entry)
            self.assertFalse(
                entry["evidence"]["market_snapshot"]["available"]
            )
            self.assertTrue(entry["evidence"]["strategy"]["available"])
            self.assertFalse(entry["authority"]["paper_account_changed"])
            self.assertFalse(
                entry["authority"]["broker_permissions_changed"]
            )
            self.assertFalse(config.paper_state_file.exists())
            self.assertFalse(config.paper_trades_file.exists())

    def test_configuration_keeps_journal_under_git_ignored_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(REPOSITORY_ROOT / "config", root / "config")
            path = root / "config" / "default.json"
            baseline = json.loads(path.read_text(encoding="utf-8"))
            invalid = (
                (None, "must be an object"),
                ({"root_dir": ""}, "non-empty path"),
                ({"root_dir": "research-journal"}, "inside the workspace state"),
                ({"root_dir": "state"}, "must be a child"),
            )
            for value, message in invalid:
                with self.subTest(value=value):
                    current = dict(baseline)
                    current["research_journal"] = value
                    path.write_text(json.dumps(current), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, message):
                        load_config(path)

            current = dict(baseline)
            current["research_journal"] = {
                "root_dir": "state/private-research-journal"
            }
            path.write_text(json.dumps(current), encoding="utf-8")
            config = load_config(path)
            self.assertEqual(
                config.research_journal_dir,
                (root / "state" / "private-research-journal").resolve(),
            )


if __name__ == "__main__":
    unittest.main()
