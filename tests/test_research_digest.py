import json
import shutil
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import ai_trade.research_digest as research_digest_module
from ai_trade.config import load_config
from ai_trade.research_digest import (
    DigestWriteResult,
    ResearchDigestBatchError,
    ResearchDigestCapacityError,
    ResearchDigestDraft,
    ResearchDigestQuery,
    ResearchDigestStore,
)
from ai_trade.web.service import DashboardService


ACCOUNT = "paper-account-20260718"
CONFIG = "a" * 64
DAY = date(2026, 7, 17)
WEEK = date(2026, 7, 13)
REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class ResearchDigestStoreTests(unittest.TestCase):
    def test_configuration_keeps_digests_under_workspace_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(REPOSITORY_ROOT / "config", root / "config")
            path = root / "config" / "default.json"
            baseline = json.loads(path.read_text(encoding="utf-8"))
            invalid = (
                (None, "must be an object"),
                ({"root_dir": ""}, "non-empty path"),
                ({"root_dir": "research-digests"}, "inside the workspace state"),
                ({"root_dir": "state"}, "must be a child"),
            )
            for value, message in invalid:
                with self.subTest(value=value):
                    current = dict(baseline)
                    current["research_digest"] = value
                    path.write_text(json.dumps(current), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, message):
                        load_config(path)

            current = dict(baseline)
            current["research_digest"] = {
                "root_dir": "state/private-research-digests"
            }
            path.write_text(json.dumps(current), encoding="utf-8")
            config = load_config(path)
            self.assertEqual(
                config.research_digest_dir,
                (root / "state" / "private-research-digests").resolve(),
            )

    def test_dashboard_service_generates_weekly_digest_from_week_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchDigestStore(Path(temporary) / "digests")
            projection = Mock()
            projection.build.return_value = {
                "available": True,
                "status": "current",
                "daily": [],
                "weekly": [_weekly_payload(WEEK)],
                "summary": {},
                "filters": {},
                "errors": [],
            }
            service = DashboardService(SimpleNamespace())
            service._research_account_context = Mock(return_value=(ACCOUNT, CONFIG))
            service._research_archive_projection = Mock(return_value=projection)
            service._research_digest_store = Mock(return_value=store)
            service.market = Mock(side_effect=RuntimeError("market calendar unavailable"))

            result = service.generate_research_digests(
                owner_id="owner",
                kind="weekly",
                week_start=WEEK,
            )

            self.assertEqual(result["summary"]["written"], 1)
            self.assertEqual(result["summary"]["weekly"], 1)
            self.assertEqual(result["writes"][0]["period_start"], WEEK.isoformat())
            self.assertEqual(result["writes"][0]["digest"]["kind"], "weekly")
            self.assertEqual(result["status"], "provisional")
            self.assertEqual(result["writes"][0]["digest"]["status"], "provisional")
            self.assertIn(
                "calendar is unavailable",
                result["writes"][0]["digest"]["payload"]["status_detail"],
            )

    def test_batch_preflight_rejects_invalid_week_before_writing_valid_day(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchDigestStore(temporary)
            daily = ResearchDigestDraft(
                kind="daily",
                period_start=DAY,
                payload=_daily_payload(DAY),
                source=_source(store, ACCOUNT),
                config_fingerprint=CONFIG,
            )
            invalid_week = _weekly_payload(WEEK)
            invalid_week["week_end"] = WEEK.isoformat()
            weekly = ResearchDigestDraft(
                kind="weekly",
                period_start=WEEK,
                payload=invalid_week,
                source=_source(store, ACCOUNT),
                config_fingerprint=CONFIG,
            )

            with self.assertRaisesRegex(ValueError, "week_end"):
                store.append_many_with_results(
                    "owner", ACCOUNT, [daily, weekly]
                )

            self.assertEqual(store.list("owner", ACCOUNT)["digests"], [])

    def test_publish_failure_leaves_no_empty_chain_and_retry_succeeds(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchDigestStore(temporary)
            kwargs = {
                "kind": "daily",
                "period_start": DAY,
                "payload": _daily_payload(DAY),
                "source": _source(store, ACCOUNT),
                "config_fingerprint": CONFIG,
            }
            with (
                patch.object(
                    research_digest_module.os,
                    "rename",
                    side_effect=OSError("publication interrupted"),
                ),
                self.assertRaises(ResearchDigestBatchError) as raised,
            ):
                store.append_with_result("owner", ACCOUNT, **kwargs)

            chain = (
                store.owner_directory("owner", ACCOUNT)
                / "digests"
                / "daily"
                / DAY.isoformat()
            )
            self.assertEqual(len(raised.exception.results), 0)
            self.assertFalse(chain.exists())
            self.assertEqual(store.list("owner", ACCOUNT)["status"], "empty")

            result = store.append_with_result("owner", ACCOUNT, **kwargs)
            self.assertTrue(result.created)
            self.assertEqual(store.list("owner", ACCOUNT)["status"], "current")

    def test_post_publish_read_failure_reports_the_committed_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchDigestStore(temporary)
            with (
                patch.object(
                    research_digest_module,
                    "_read_record",
                    side_effect=RuntimeError("verification interrupted"),
                ),
                self.assertRaises(ResearchDigestBatchError) as raised,
            ):
                store.append(
                    "owner",
                    ACCOUNT,
                    kind="daily",
                    period_start=DAY,
                    payload=_daily_payload(DAY),
                    source=_source(store, ACCOUNT),
                    config_fingerprint=CONFIG,
                )

            self.assertEqual(len(raised.exception.results), 1)
            self.assertTrue(raised.exception.results[0].created)
            self.assertEqual(
                raised.exception.results[0].digest["period_start"], DAY.isoformat()
            )
            self.assertEqual(
                store.list("owner", ACCOUNT)["summary"]["total_revisions"], 1
            )

    def test_post_publish_fsync_failure_reports_the_committed_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchDigestStore(temporary)
            with (
                patch.object(
                    research_digest_module,
                    "_fsync_directory",
                    side_effect=OSError("durability barrier interrupted"),
                ),
                self.assertRaises(ResearchDigestBatchError) as raised,
            ):
                store.append(
                    "owner",
                    ACCOUNT,
                    kind="daily",
                    period_start=DAY,
                    payload=_daily_payload(DAY),
                    source=_source(store, ACCOUNT),
                    config_fingerprint=CONFIG,
                )

            self.assertEqual(len(raised.exception.results), 1)
            self.assertTrue(raised.exception.results[0].created)
            self.assertEqual(
                store.list("owner", ACCOUNT)["summary"]["total_revisions"], 1
            )

    def test_list_status_aggregates_latest_chain_evidence(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchDigestStore(temporary)
            provisional = _weekly_payload(WEEK)
            provisional["status"] = "provisional"
            store.append(
                "owner",
                ACCOUNT,
                kind="weekly",
                period_start=WEEK,
                payload=provisional,
                source=_source(store, ACCOUNT),
                config_fingerprint=CONFIG,
            )
            self.assertEqual(store.list("owner", ACCOUNT)["status"], "provisional")

            mismatched = _daily_payload(DAY)
            mismatched["status"] = "evidence_mismatch"
            store.append(
                "owner",
                ACCOUNT,
                kind="daily",
                period_start=DAY,
                payload=mismatched,
                source=_source(store, ACCOUNT),
                config_fingerprint=CONFIG,
            )
            self.assertEqual(store.list("owner", ACCOUNT)["status"], "partial")

    def test_new_chain_capacity_is_checked_before_account_becomes_unreadable(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchDigestStore(temporary)
            with patch.object(
                research_digest_module, "MAX_CHAINS_PER_ACCOUNT", 1
            ):
                store.append(
                    "owner",
                    ACCOUNT,
                    kind="daily",
                    period_start=DAY,
                    payload=_daily_payload(DAY),
                    source=_source(store, ACCOUNT),
                    config_fingerprint=CONFIG,
                )
                with self.assertRaises(ResearchDigestCapacityError):
                    store.append(
                        "owner",
                        ACCOUNT,
                        kind="daily",
                        period_start=DAY - timedelta(days=1),
                        payload=_daily_payload(DAY - timedelta(days=1)),
                        source=_source(store, ACCOUNT),
                        config_fingerprint=CONFIG,
                    )

                result = store.list("owner", ACCOUNT)
                self.assertEqual(result["summary"]["total_chains"], 1)

    def test_account_epoch_rejects_a_different_configuration_fingerprint(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchDigestStore(temporary)
            store.append(
                "owner",
                ACCOUNT,
                kind="daily",
                period_start=DAY,
                payload=_daily_payload(DAY),
                source=_source(store, ACCOUNT),
                config_fingerprint=CONFIG,
            )
            changed_config = "f" * 64
            source = _source(store, ACCOUNT)
            source["config_fingerprint"] = changed_config

            with self.assertRaisesRegex(ValueError, "configuration"):
                store.append(
                    "owner",
                    ACCOUNT,
                    kind="daily",
                    period_start=DAY,
                    payload=_daily_payload(DAY, note="drifted config"),
                    source=source,
                    config_fingerprint=changed_config,
                )

            self.assertEqual(
                store.list("owner", ACCOUNT)["summary"]["total_revisions"], 1
            )

    def test_future_calendar_dates_do_not_revise_an_older_week(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchDigestStore(Path(temporary) / "digests")
            projection = Mock()
            projection.build.return_value = {
                "available": True,
                "status": "current",
                "daily": [],
                "weekly": [_weekly_payload(WEEK)],
                "summary": {},
                "filters": {},
                "errors": [],
            }
            market = SimpleNamespace(calendar=[WEEK, WEEK + timedelta(days=1)])
            service = DashboardService(SimpleNamespace())
            service._research_account_context = Mock(return_value=(ACCOUNT, CONFIG))
            service._research_archive_projection = Mock(return_value=projection)
            service._research_digest_store = Mock(return_value=store)
            service.market = Mock(return_value=market)

            first = service.generate_research_digests(
                owner_id="owner", kind="weekly", week_start=WEEK
            )
            market.calendar.append(WEEK + timedelta(days=7))
            second = service.generate_research_digests(
                owner_id="owner", kind="weekly", week_start=WEEK
            )

            self.assertEqual(first["summary"]["written"], 1)
            self.assertEqual(second["summary"]["written"], 0)
            self.assertEqual(second["summary"]["reused"], 1)
            self.assertEqual(
                store.list("owner", ACCOUNT)["summary"]["total_revisions"], 1
            )

    def test_first_write_is_immutable_owner_account_scoped_and_authority_bound(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchDigestStore(temporary)
            result = store.append_with_result(
                "Alice",
                ACCOUNT,
                kind="daily",
                period_start=DAY,
                payload=_daily_payload(DAY),
                source=_source(store, ACCOUNT),
                config_fingerprint=CONFIG,
                actor="alice",
                trigger="manual",
                now=datetime(2026, 7, 18, 10, 0, tzinfo=timezone.utc),
            )

            self.assertIsInstance(result, DigestWriteResult)
            self.assertTrue(result.created)
            self.assertFalse(result.reused)
            digest = result.digest
            self.assertRegex(digest["digest_id"], r"\Adigest_[0-9a-f]{32}\Z")
            self.assertRegex(digest["digest_fingerprint"], r"\A[0-9a-f]{64}\Z")
            self.assertEqual(digest["revision"], 1)
            self.assertIsNone(digest["supersedes"])
            self.assertNotIn("owner", digest)
            self.assertNotIn(ACCOUNT, json.dumps(digest))
            self.assertFalse(digest["authority"]["execution_authorized"])

            path = (
                store.owner_directory("alice", ACCOUNT)
                / "digests"
                / "daily"
                / DAY.isoformat()
                / "revision_00000001.json"
            )
            self.assertTrue(path.is_file())
            self.assertEqual(list(path.parent.glob("*.tmp")), [])
            self.assertEqual(
                store.list("alice", ACCOUNT)["digests"],
                [digest],
            )
            store.verify("alice", ACCOUNT)

    def test_identical_evidence_is_reused_without_a_new_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchDigestStore(temporary)
            kwargs = {
                "kind": "daily",
                "period_start": DAY,
                "payload": _daily_payload(DAY),
                "source": _source(store, ACCOUNT),
                "config_fingerprint": CONFIG,
            }
            first = store.append_with_result("owner", ACCOUNT, **kwargs)
            second = store.append_with_result(
                "owner",
                ACCOUNT,
                **kwargs,
                actor="a different actor",
                trigger="scheduled",
                now=datetime(2027, 1, 1, tzinfo=timezone.utc),
            )

            self.assertTrue(first.created)
            self.assertFalse(second.created)
            self.assertTrue(second.reused)
            self.assertEqual(second.digest, first.digest)
            summary = store.list("owner", ACCOUNT)["summary"]
            self.assertEqual(summary["total_revisions"], 1)
            self.assertEqual(summary["total_chains"], 1)

    def test_changed_source_or_payload_appends_revision_and_supersedes_previous(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchDigestStore(temporary)
            first = store.append(
                "owner",
                ACCOUNT,
                kind="daily",
                period_start=DAY,
                payload=_daily_payload(DAY),
                source=_source(store, ACCOUNT, evidence="b" * 64),
                config_fingerprint=CONFIG,
            )
            second_payload = _daily_payload(DAY, note="A later correction")
            second_source = _source(store, ACCOUNT, evidence="c" * 64)
            second = store.append_with_result(
                "owner",
                ACCOUNT,
                kind="daily",
                period_start=DAY,
                payload=second_payload,
                source=second_source,
                config_fingerprint=CONFIG,
            )

            self.assertTrue(second.created)
            self.assertEqual(second.digest["revision"], 2)
            self.assertEqual(second.digest["supersedes"], first["digest_id"])
            self.assertEqual(
                second.digest["supersedes_fingerprint"], first["digest_fingerprint"]
            )
            latest = store.list("owner", ACCOUNT)
            self.assertEqual(latest["summary"]["total_revisions"], 2)
            self.assertEqual(latest["summary"]["latest_count"], 1)
            all_revisions = store.list(
                "owner",
                ACCOUNT,
                ResearchDigestQuery(include_revisions=True),
            )
            self.assertEqual(
                [item["revision"] for item in all_revisions["digests"]], [2, 1]
            )
            self.assertEqual(store.get("owner", ACCOUNT, first["digest_id"]), first)

    def test_daily_and_weekly_period_bindings_are_strict(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchDigestStore(temporary)
            with self.assertRaisesRegex(ValueError, "weekly.*Monday"):
                store.append(
                    "owner",
                    ACCOUNT,
                    kind="weekly",
                    period_start=DAY,
                    payload=_weekly_payload(DAY),
                    source=_source(store, ACCOUNT),
                    config_fingerprint=CONFIG,
                )
            with self.assertRaisesRegex(ValueError, "date does not match"):
                store.append(
                    "owner",
                    ACCOUNT,
                    kind="daily",
                    period_start=DAY,
                    payload=_daily_payload(date(2026, 7, 16)),
                    source=_source(store, ACCOUNT),
                    config_fingerprint=CONFIG,
                )
            weekly = store.append(
                "owner",
                ACCOUNT,
                kind="weekly",
                period_start=WEEK,
                payload=_weekly_payload(WEEK),
                source=_source(store, ACCOUNT),
                config_fingerprint=CONFIG,
            )
            self.assertEqual(weekly["period_end"], "2026-07-19")
            filtered = store.list(
                "owner",
                ACCOUNT,
                ResearchDigestQuery(kind="weekly", period_start=WEEK),
            )
            self.assertEqual(filtered["digests"], [weekly])

    def test_users_and_account_epochs_cannot_see_each_other(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchDigestStore(temporary)
            for owner, account in (
                ("alice", ACCOUNT),
                ("bob", ACCOUNT),
                ("alice", ACCOUNT + "-new"),
            ):
                store.append(
                    owner,
                    account,
                    kind="daily",
                    period_start=DAY,
                    payload=_daily_payload(DAY, note=f"{owner}:{account}"),
                    source=_source(store, account),
                    config_fingerprint=CONFIG,
                )

            alice = store.list("alice", ACCOUNT)
            bob = store.list("bob", ACCOUNT)
            new_epoch = store.list("alice", ACCOUNT + "-new")
            self.assertEqual(alice["summary"]["total_chains"], 1)
            self.assertEqual(bob["summary"]["total_chains"], 1)
            self.assertEqual(new_epoch["summary"]["total_chains"], 1)
            self.assertNotIn("bob:", json.dumps(alice))
            self.assertNotIn("alice:", json.dumps(bob))
            self.assertNotEqual(
                alice["account_fingerprint"], new_epoch["account_fingerprint"]
            )
            users = (Path(temporary) / "users").iterdir()
            self.assertEqual(len(list(users)), 2)
            alice_accounts = (
                store.owner_directory("alice") / "accounts"
            ).iterdir()
            self.assertEqual(len(list(alice_accounts)), 2)

    def test_tampering_duplicate_keys_and_revision_gaps_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchDigestStore(temporary)
            first = store.append(
                "owner",
                ACCOUNT,
                kind="daily",
                period_start=DAY,
                payload=_daily_payload(DAY),
                source=_source(store, ACCOUNT),
                config_fingerprint=CONFIG,
            )
            path = (
                store.owner_directory("owner", ACCOUNT)
                / "digests"
                / "daily"
                / DAY.isoformat()
                / "revision_00000001.json"
            )
            persisted = json.loads(path.read_text(encoding="utf-8"))
            persisted["payload"]["note"] = "tampered"
            path.write_text(json.dumps(persisted), encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "fingerprint"):
                store.verify("owner", ACCOUNT)

            path.write_text('{"schema_version":1,"schema_version":2}', encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "cannot be read"):
                store.list("owner", ACCOUNT)

            # A fresh store demonstrates the contiguous-chain check without
            # relying on a repaired file from the previous tamper scenario.
            other_root = Path(temporary) / "other"
            other = ResearchDigestStore(other_root)
            other.append(
                "owner",
                ACCOUNT,
                kind="daily",
                period_start=DAY,
                payload=_daily_payload(DAY),
                source=_source(other, ACCOUNT),
                config_fingerprint=CONFIG,
            )
            other.append(
                "owner",
                ACCOUNT,
                kind="daily",
                period_start=DAY,
                payload=_daily_payload(DAY, note="revision two"),
                source=_source(other, ACCOUNT, evidence="c" * 64),
                config_fingerprint=CONFIG,
            )
            first_path = (
                other.owner_directory("owner", ACCOUNT)
                / "digests"
                / "daily"
                / DAY.isoformat()
                / "revision_00000001.json"
            )
            first_path.unlink()
            with self.assertRaisesRegex(RuntimeError, "gap"):
                other.verify("owner", ACCOUNT)
            self.assertRegex(first["digest_id"], r"\Adigest_")

    def test_source_manifest_and_payload_authority_are_checked(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchDigestStore(temporary)
            source = _source(store, ACCOUNT)
            source["config_fingerprint"] = "f" * 64
            with self.assertRaisesRegex(ValueError, "does not match"):
                store.append(
                    "owner",
                    ACCOUNT,
                    kind="daily",
                    period_start=DAY,
                    payload=_daily_payload(DAY),
                    source=source,
                    config_fingerprint=CONFIG,
                )
            mismatched_payload = _daily_payload(DAY)
            mismatched_payload["source"] = {"evidence_fingerprint": "e" * 64}
            with self.assertRaisesRegex(ValueError, "payload source fingerprint"):
                store.append(
                    "owner",
                    ACCOUNT,
                    kind="daily",
                    period_start=DAY,
                    payload=mismatched_payload,
                    source=_source(store, ACCOUNT),
                    config_fingerprint=CONFIG,
                )
            with self.assertRaisesRegex(ValueError, "forbidden"):
                store.append(
                    "owner",
                    ACCOUNT,
                    kind="daily",
                    period_start=DAY,
                    payload=_daily_payload(DAY) | {"account_id": ACCOUNT},
                    source=_source(store, ACCOUNT),
                    config_fingerprint=CONFIG,
                )
            with self.assertRaisesRegex(ValueError, "authority"):
                store.append(
                    "owner",
                    ACCOUNT,
                    kind="daily",
                    period_start=DAY,
                    payload=_daily_payload(DAY, authority={"execution_authorized": True}),
                    source=_source(store, ACCOUNT),
                    config_fingerprint=CONFIG,
                )
            with self.assertRaisesRegex(ValueError, "finite"):
                store.append(
                    "owner",
                    ACCOUNT,
                    kind="daily",
                    period_start=DAY,
                    payload=_daily_payload(DAY, equity=float("nan")),
                    source=_source(store, ACCOUNT),
                    config_fingerprint=CONFIG,
                )

    def test_concurrent_identical_appends_commit_one_revision(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchDigestStore(temporary)
            kwargs = {
                "kind": "daily",
                "period_start": DAY,
                "payload": _daily_payload(DAY),
                "source": _source(store, ACCOUNT),
                "config_fingerprint": CONFIG,
            }

            def write_once(_index: int) -> DigestWriteResult:
                return store.append_with_result("owner", ACCOUNT, **kwargs)

            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(write_once, range(8)))

            self.assertEqual(sum(item.created for item in results), 1)
            self.assertEqual(sum(item.reused for item in results), 7)
            self.assertEqual(
                {item.digest["digest_id"] for item in results},
                {results[0].digest["digest_id"]},
            )
            self.assertEqual(store.list("owner", ACCOUNT)["summary"]["total_revisions"], 1)

    def test_empty_or_staged_chain_and_unknown_path_members_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchDigestStore(temporary)
            chain = (
                store.owner_directory("owner", ACCOUNT)
                / "digests"
                / "daily"
                / DAY.isoformat()
            )
            chain.mkdir(parents=True)
            with self.assertRaisesRegex(RuntimeError, "no committed revisions"):
                store.list("owner", ACCOUNT)

        with tempfile.TemporaryDirectory() as temporary:
            store = ResearchDigestStore(temporary)
            store.append(
                "owner",
                ACCOUNT,
                kind="daily",
                period_start=DAY,
                payload=_daily_payload(DAY),
                source=_source(store, ACCOUNT),
                config_fingerprint=CONFIG,
            )
            chain = (
                store.owner_directory("owner", ACCOUNT)
                / "digests"
                / "daily"
                / DAY.isoformat()
            )
            (chain / ".revision_00000002.json.partial.tmp").write_text(
                "staged", encoding="utf-8"
            )
            with self.assertRaisesRegex(RuntimeError, "chain member"):
                store.verify("owner", ACCOUNT)

            (chain / ".revision_00000002.json.partial.tmp").unlink()
            (chain / "unexpected.txt").write_text("x", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "chain member"):
                store.verify("owner", ACCOUNT)


def _source(
    store: ResearchDigestStore,
    account: str,
    *,
    evidence: str = "b" * 64,
) -> dict[str, object]:
    return {
        "fingerprint": "c" * 64,
        "evidence_fingerprints": [evidence],
        "calendar_fingerprint": "d" * 64,
        "config_fingerprint": CONFIG,
        "account_fingerprint": store.account_id(account),
    }


def _daily_payload(
    on_date: date,
    *,
    note: str = "Closing evidence",
    authority: dict[str, object] | None = None,
    equity: float = 100_000.0,
) -> dict[str, object]:
    value: dict[str, object] = {
        "as_of_date": on_date.isoformat(),
        "status": "current",
        "equity": equity,
        "daily_return": 0.01,
        "note": note,
        "source": {"evidence_fingerprint": "c" * 64},
    }
    if authority is not None:
        value["authority"] = authority
    return value


def _weekly_payload(on_date: date, *, note: str = "Weekly review") -> dict[str, object]:
    return {
        "week_start": on_date.isoformat(),
        "week_end": (on_date + timedelta(days=6)).isoformat(),
        "status": "current",
        "period_return": 0.02,
        "note": note,
        "source": {"weekly_fingerprint": "c" * 64},
    }


if __name__ == "__main__":
    unittest.main()
