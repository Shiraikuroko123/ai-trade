import csv
from datetime import date
import json
from pathlib import Path
import tempfile
import unittest

from ai_trade.research_archive import ResearchArchiveQuery
from ai_trade.research_digest import ResearchDigestStore
from ai_trade.research_epochs import ResearchEpochBrowser
from ai_trade.research_journal import JournalDraft, ResearchJournalStore


ACCOUNT = "a" * 32
CONFIG = "b" * 64
DAY = date(2026, 6, 30)


class ResearchEpochBrowserTests(unittest.TestCase):
    def test_lists_and_projects_an_archived_epoch_without_exposing_account_id(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "state" / "archive" / "20260701_120000"
            archive.mkdir(parents=True)
            _write_state(archive)
            _write_equity(archive)
            journal = ResearchJournalStore(root / "state" / "research_journal")
            journal.append(
                "alice",
                JournalDraft(
                    research_date=DAY,
                    category="observation",
                    symbol="510300",
                    title="Active epoch note",
                    note="This same-date note is not bound to the archived account.",
                    decision="watch",
                    confidence=50,
                ),
                actor="alice",
                market_evidence={"available": False, "date": None, "fingerprint": None},
                strategy_evidence={
                    "available": False,
                    "candidate_id": None,
                    "fingerprint": None,
                    "lifecycle_state": None,
                },
            )
            browser = ResearchEpochBrowser(
                root / "state" / "archive",
                journal,
                ResearchDigestStore(root / "state" / "research_digests"),
            )

            listing = browser.list("alice")
            detail = browser.get(
                "alice",
                "20260701_120000",
                query=ResearchArchiveQuery(
                    kind="monthly",
                    month_start=date(2026, 6, 1),
                    limit=1,
                ),
            )

            self.assertEqual(listing["summary"]["total"], 1)
            self.assertEqual(listing["epochs"][0]["status"], "archived")
            self.assertNotIn(ACCOUNT, json.dumps(listing))
            self.assertTrue(detail["available"])
            self.assertEqual(detail["status"], "partial")
            self.assertEqual(detail["archive"]["monthly"][0]["month_start"], "2026-06-01")
            self.assertEqual(detail["archive"]["monthly"][0]["journal_count"], 0)
            self.assertEqual(detail["archive"]["daily"], [])
            self.assertEqual(
                detail["errors"][0]["code"],
                "research_journal_epoch_binding_unavailable",
            )
            self.assertFalse(detail["authority"]["archived_epoch_reactivated"])
            self.assertNotIn(ACCOUNT, json.dumps(detail))

    def test_invalid_state_is_visible_but_cannot_be_opened(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "archive" / "20260701_120000"
            archive.mkdir(parents=True)
            (archive / "paper_state.json").write_text("{}", encoding="utf-8")
            browser = ResearchEpochBrowser(
                root / "archive",
                ResearchJournalStore(root / "journal"),
                ResearchDigestStore(root / "digests"),
            )

            listing = browser.list("alice")
            self.assertEqual(listing["status"], "partial")
            self.assertFalse(listing["epochs"][0]["available"])
            with self.assertRaisesRegex(RuntimeError, "schema"):
                browser.get("alice", "20260701_120000")

    def test_rejects_noncanonical_epoch_id_and_unexpected_archive_members(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive_root = root / "archive"
            archive_root.mkdir()
            (archive_root / "notes.txt").write_text("not an epoch", encoding="utf-8")
            browser = ResearchEpochBrowser(
                archive_root,
                ResearchJournalStore(root / "journal"),
                ResearchDigestStore(root / "digests"),
            )

            listing = browser.list("alice")
            self.assertEqual(listing["status"], "partial")
            self.assertEqual(listing["errors"][0]["code"], "unexpected_epoch_member")
            with self.assertRaisesRegex(ValueError, "id"):
                browser.get("alice", "../active")

    def test_rejects_archived_state_with_invalid_position_quantity(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            archive = root / "archive" / "20260701_120000"
            archive.mkdir(parents=True)
            _write_state(archive)
            state_path = archive / "paper_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["positions"] = {"510300": True}
            state_path.write_text(json.dumps(state), encoding="utf-8")
            browser = ResearchEpochBrowser(
                root / "archive",
                ResearchJournalStore(root / "journal"),
                ResearchDigestStore(root / "digests"),
            )

            listing = browser.list("alice")

            self.assertFalse(listing["epochs"][0]["available"])
            self.assertIn("positive integers", listing["epochs"][0]["error"])


def _write_state(directory: Path) -> None:
    state = {
        "version": 5,
        "account_id": ACCOUNT,
        "config_fingerprint": CONFIG,
        "cash": 80000.0,
        "positions": {"510300": 100},
        "high_water_mark": 100500.0,
        "last_equity": 100500.0,
        "last_run_date": DAY.isoformat(),
        "pending_targets": None,
        "pending_signal_date": None,
        "cooldown_remaining": 0,
        "sessions_since_rebalance": 1,
    }
    (directory / "paper_state.json").write_text(
        json.dumps(state), encoding="utf-8"
    )


def _write_equity(directory: Path) -> None:
    with (directory / "paper_equity.csv").open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "account_id",
                "session_id",
                "date",
                "equity",
                "cash",
                "drawdown",
                "daily_return",
                "positions",
                "pending_targets",
                "config_fingerprint",
                "market_snapshot_id",
            ]
        )
        writer.writerow(
            [
                ACCOUNT,
                "c" * 24,
                DAY.isoformat(),
                "100500.0",
                "80000.0",
                "0.0",
                "0.005",
                '{"510300":100}',
                "null",
                CONFIG,
                "snapshot-20260630",
            ]
        )


if __name__ == "__main__":
    unittest.main()
