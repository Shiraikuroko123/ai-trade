import csv
from datetime import date, datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
import tempfile
import unittest

from ai_trade.research_archive import (
    ResearchArchiveProjection,
    ResearchArchiveQuery,
)
from ai_trade.research_journal import (
    JournalDraft,
    ResearchJournalStore,
)


DAY = date(2026, 7, 17)
ACCOUNT = "paper-account-1"
CONFIG_FINGERPRINT = "a" * 64
SNAPSHOT_ID = "snapshot-20260717"


class ResearchArchiveProjectionTests(unittest.TestCase):
    def test_builds_daily_weekly_and_position_snapshots_from_bound_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ResearchJournalStore(root / "state" / "research_journal")
            _append_journal(store, "alice", "Alice note")
            _write_equity(root)
            _write_report(root)
            projection = _projection(root, store)

            result = _build(
                projection,
                "alice",
                market_calendar=[DAY],
            )

            self.assertTrue(result["available"])
            self.assertEqual(result["status"], "current")
            self.assertEqual(result["account_fingerprint"], _hash(ACCOUNT))
            self.assertNotIn(ACCOUNT, json.dumps(result))
            self.assertEqual(result["daily"][0]["status"], "current")
            self.assertEqual(result["daily"][0]["journal"]["entry_count"], 1)
            self.assertEqual(result["weekly"][0]["included_sessions"], 1)
            self.assertEqual(result["weekly"][0]["expected_sessions"], 1)
            self.assertAlmostEqual(result["weekly"][0]["period_return"], 0.005)
            self.assertEqual(result["monthly"][0]["month_start"], "2026-07-01")
            self.assertEqual(result["monthly"][0]["month_end"], "2026-07-31")
            self.assertEqual(result["monthly"][0]["included_sessions"], 1)
            self.assertEqual(result["monthly"][0]["expected_sessions"], 1)
            self.assertAlmostEqual(result["monthly"][0]["period_return"], 0.005)
            self.assertEqual(result["snapshots"][0]["valuation_status"], "ledger_only")
            self.assertFalse(
                result["snapshots"][0]["price_derived_values_available"]
            )
            self.assertFalse(result["authority"]["execution_authorized"])

    def test_owner_scoping_keeps_private_journal_entries_separate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ResearchJournalStore(root / "state" / "research_journal")
            _append_journal(store, "alice", "Alice private note")
            _append_journal(store, "bob", "Bob private note")
            _write_equity(root)
            _write_report(root)
            projection = _projection(root, store)

            alice = _build(projection, "alice")
            bob = _build(projection, "bob")

            self.assertEqual(
                alice["daily"][0]["journal"]["entries"][0]["title"],
                "Alice private note",
            )
            self.assertEqual(
                bob["daily"][0]["journal"]["entries"][0]["title"],
                "Bob private note",
            )
            self.assertNotIn("Bob private note", json.dumps(alice))
            self.assertNotIn("Alice private note", json.dumps(bob))

    def test_default_archive_includes_journal_notes_after_last_paper_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ResearchJournalStore(root / "state" / "research_journal")
            _append_journal(
                store,
                "alice",
                "Post-run review",
                on_date=date(2026, 7, 18),
            )
            _write_equity(root)
            _write_report(root)

            result = _build(_projection(root, store), "alice")

            self.assertEqual(
                [item["as_of_date"] for item in result["daily"][:2]],
                ["2026-07-18", "2026-07-17"],
            )
            self.assertEqual(result["daily"][0]["status"], "journal_only")
            self.assertEqual(result["daily"][0]["journal"]["entry_count"], 1)

    def test_report_ledger_mismatch_is_visible_and_never_becomes_current(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ResearchJournalStore(root / "state" / "research_journal")
            _write_equity(root)
            _write_report(root, cash=1.0)

            result = _build(_projection(root, store), "alice")

            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["daily"][0]["status"], "evidence_mismatch")
            self.assertIn("cash", result["daily"][0]["status_detail"].lower())
            self.assertEqual(result["daily"][0]["cash"], 80_000.0)

    def test_invalid_journal_preserves_paper_evidence_with_partial_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ResearchJournalStore(root / "state" / "research_journal")
            entries = store.owner_directory("alice") / "entries"
            entries.mkdir(parents=True)
            (entries / f"journal_{'f' * 32}.json").write_text(
                '{"schema_version":1,"schema_version":2}',
                encoding="utf-8",
            )
            _write_equity(root)
            _write_report(root)

            result = _build(_projection(root, store), "alice")

            self.assertTrue(result["available"])
            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["daily"][0]["status"], "current")
            self.assertFalse(result["summary"]["journal_available"])
            self.assertEqual(
                result["errors"][0]["code"], "research_journal_unavailable"
            )

    def test_ambiguous_nested_ledger_json_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ResearchJournalStore(root / "state" / "research_journal")
            _write_equity(root, positions='{"510300":100,"510300":200}')
            _write_report(root)

            result = _build(_projection(root, store), "alice")

            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["errors"][0]["code"], "paper_equity_invalid")
            self.assertEqual(result["daily"][0]["status"], "unbound_report")
            self.assertTrue(result["authority"]["research_only"])

    def test_ledger_must_match_the_active_configuration_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ResearchJournalStore(root / "state" / "research_journal")
            _write_equity(root, fingerprint="d" * 64)
            _write_report(root)

            result = _build(_projection(root, store), "alice")

            self.assertEqual(result["status"], "partial")
            self.assertEqual(result["errors"][0]["code"], "paper_equity_invalid")
            self.assertIn("active configuration", result["errors"][0]["message"])
            self.assertEqual(result["snapshots"], [])

    def test_zero_quantity_positions_fail_closed_in_both_sources(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ResearchJournalStore(root / "state" / "research_journal")
            _write_equity(root, positions='{"510300":0}')
            _write_report(root, positions={"510300": 0})

            result = _build(_projection(root, store), "alice")

            self.assertFalse(result["available"])
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(result["daily"], [])
            self.assertEqual(result["snapshots"], [])
            self.assertEqual(
                {error["code"] for error in result["errors"]},
                {"paper_equity_invalid", "paper_report_invalid"},
            )

    def test_projection_is_read_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ResearchJournalStore(root / "state" / "research_journal")
            _append_journal(store, "alice", "Evidence remains immutable")
            _write_equity(root)
            _write_report(root)
            before = _tree_hashes(root)

            result = _build(_projection(root, store), "alice")

            self.assertTrue(result["available"])
            self.assertEqual(_tree_hashes(root), before)

    def test_query_filters_one_day_and_validates_week_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ResearchJournalStore(root / "state" / "research_journal")
            _write_equity(root)
            _write_report(root)
            projection = _projection(root, store)

            result = _build(
                projection,
                "alice",
                query=ResearchArchiveQuery(kind="daily", on_date=DAY, limit=1),
            )
            self.assertEqual(len(result["daily"]), 1)
            self.assertEqual(result["weekly"], [])
            self.assertEqual(result["monthly"], [])

            monthly = _build(
                projection,
                "alice",
                query=ResearchArchiveQuery(
                    kind="monthly",
                    month_start=date(2026, 7, 1),
                    limit=1,
                ),
            )
            self.assertEqual(monthly["daily"], [])
            self.assertEqual(monthly["weekly"], [])
            self.assertEqual(monthly["monthly"][0]["month_start"], "2026-07-01")

            with self.assertRaisesRegex(ValueError, "Monday"):
                _build(
                    projection,
                    "alice",
                    query=ResearchArchiveQuery(week_start=DAY),
                )
            with self.assertRaisesRegex(ValueError, "date"):
                _build(
                    projection,
                    "alice",
                    query=ResearchArchiveQuery(
                        on_date=datetime(2026, 7, 17, tzinfo=timezone.utc)
                    ),
                )

            empty = _build(
                projection,
                "alice",
                query=ResearchArchiveQuery(
                    kind="daily", on_date=date(2026, 7, 16), limit=1
                ),
            )
            self.assertTrue(empty["available"])
            self.assertEqual(empty["status"], "empty")
            self.assertEqual(empty["daily"], [])

    def test_report_only_evidence_is_not_promoted_to_a_ledger_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ResearchJournalStore(root / "state" / "research_journal")
            _write_report(root)

            result = _build(_projection(root, store), "alice")

            self.assertEqual(result["daily"][0]["status"], "unbound_report")
            self.assertEqual(result["snapshots"], [])
            self.assertEqual(result["weekly"][0]["status"], "partial")
            self.assertEqual(result["weekly"][0]["included_sessions"], 0)

    def test_weekly_status_cannot_be_current_when_same_week_has_unbound_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ResearchJournalStore(root / "state" / "research_journal")
            _write_equity(root)
            _write_report(root)
            _write_report(root, on_date=date(2026, 7, 18))

            result = _build(_projection(root, store), "alice")

            self.assertEqual(result["weekly"][0]["status"], "partial")
            self.assertIn(
                {"date": "2026-07-18", "status": "unbound_report"},
                result["weekly"][0]["daily_statuses"],
            )

    def test_weekly_coverage_discloses_unexpected_non_trading_sessions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ResearchJournalStore(root / "state" / "research_journal")
            saturday = date(2026, 7, 18)
            weekdays = [date(2026, 7, 13 + offset) for offset in range(5)]
            _write_equity(root, on_date=saturday)
            _write_report(root, on_date=saturday)

            result = _build(
                _projection(root, store),
                "alice",
                market_calendar=weekdays,
            )

            week = result["weekly"][0]
            self.assertEqual(week["status"], "partial")
            self.assertEqual(week["included_sessions"], 1)
            self.assertEqual(week["expected_sessions"], 5)
            self.assertEqual(week["unexpected_sessions"], [saturday.isoformat()])

    def test_missing_paper_account_has_actionable_unavailable_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            result = _projection(
                root,
                ResearchJournalStore(root / "state" / "research_journal"),
            ).build(
                "alice",
                account_id=None,
                config_fingerprint=CONFIG_FINGERPRINT,
            )

            self.assertFalse(result["available"])
            self.assertEqual(result["errors"][0]["code"], "paper_account_unavailable")
            self.assertEqual(result["errors"][0]["recovery_action"], "paper-init")


def _projection(root: Path, store: ResearchJournalStore) -> ResearchArchiveProjection:
    return ResearchArchiveProjection(
        root / "reports",
        root / "state" / "paper_equity.csv",
        store,
    )


def _build(
    projection: ResearchArchiveProjection,
    owner: str,
    *,
    account_id: str = ACCOUNT,
    **kwargs: object,
) -> dict[str, object]:
    return projection.build(
        owner,
        account_id=account_id,
        config_fingerprint=CONFIG_FINGERPRINT,
        **kwargs,
    )


def _append_journal(
    store: ResearchJournalStore,
    owner: str,
    title: str,
    *,
    on_date: date = DAY,
) -> None:
    store.append(
        owner,
        JournalDraft(
            research_date=on_date,
            category="observation",
            symbol="510300",
            title=title,
            note="Evidence-bound closing note.",
            decision="watch",
            confidence=60,
        ),
        actor=owner,
        market_evidence={
            "available": True,
            "date": on_date.isoformat(),
            "fingerprint": "b" * 64,
        },
        strategy_evidence={
            "available": True,
            "candidate_id": None,
            "fingerprint": "c" * 64,
            "lifecycle_state": "CONFIGURED",
        },
        now=datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc),
    )


def _write_equity(
    root: Path,
    *,
    positions: str | None = None,
    fingerprint: str = CONFIG_FINGERPRINT,
    on_date: date = DAY,
) -> None:
    path = root / "state" / "paper_equity.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
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
                "1" * 24,
                on_date.isoformat(),
                "100000.0",
                "80000.0",
                "-0.01",
                "0.005",
                positions or '{"510300":100}',
                "null",
                fingerprint,
                SNAPSHOT_ID,
            ]
        )


def _write_report(
    root: Path,
    *,
    cash: float = 80_000.0,
    on_date: date = DAY,
    positions: dict[str, int] | None = None,
) -> None:
    path = root / "reports" / f"paper_{on_date.strftime('%Y%m%d')}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "account_id": ACCOUNT,
                "date": on_date.isoformat(),
                "equity": 100_000.0,
                "cash": cash,
                "positions": (
                    positions if positions is not None else {"510300": 100}
                ),
                "pending_targets": None,
                "cooldown_remaining": 0,
                "sessions_since_rebalance": 1,
                "drawdown": -0.01,
                "daily_return": 0.005,
                "market_snapshot_id": SNAPSHOT_ID,
                "trades": [],
                "order_rejections": [],
                "reason": "Hold for the next completed session.",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _tree_hashes(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): sha256(path.read_bytes()).hexdigest()
        for path in root.rglob("*")
        if path.is_file()
    }


def _hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


if __name__ == "__main__":
    unittest.main()
