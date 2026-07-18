import json
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ai_trade.models import Bar, Instrument
from ai_trade.monitoring import (
    MonitoringCapacityError,
    MonitoringConflictError,
    MonitoringEngine,
)


SYMBOL = "AAA"


def _rewrite_fingerprint(path: Path, **changes):
    record = json.loads(path.read_text(encoding="utf-8"))
    record.update(changes)
    record["fingerprint"] = _fingerprint_for(record)
    path.write_text(json.dumps(record), encoding="utf-8")


def _fingerprint_for(record):
    payload = {key: value for key, value in record.items() if key != "fingerprint"}
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return sha256(encoded).hexdigest()


class _Market:
    """Small deterministic market double implementing the monitoring contract."""

    def __init__(self, closes, *, start=date(2026, 7, 1), version=None):
        self._closes = list(closes)
        self._start = start
        self.calendar = [start + timedelta(days=index) for index in range(len(closes))]
        self.latest_common_session = self.calendar[-1]
        self.completed_through = self.latest_common_session
        self.symbols = {SYMBOL: SimpleNamespace()}
        self.file_hashes = {SYMBOL: "a" * 64}
        self.manifest = {
            "files": {SYMBOL: {"source_provider": "synthetic"}},
        }
        self.version = version if version is not None else self.latest_common_session.isoformat()

    def latest_date(self):
        return self.latest_common_session

    def history(self, symbol, on_date, count):
        if symbol != SYMBOL:
            raise KeyError(symbol)
        bars = self._bars()
        return [bar for bar in bars if bar.date <= on_date][-count:]

    def snapshot_metadata(self):
        return {
            "provider": "synthetic",
            "latest_common_session": self.latest_common_session.isoformat(),
            "version": self.version,
        }

    def _bars(self):
        result = []
        for index, close in enumerate(self._closes):
            close = float(close)
            previous = float(self._closes[index - 1]) if index else close
            result.append(
                Bar(
                    self._start + timedelta(days=index),
                    previous,
                    close,
                    max(previous, close) * 1.01,
                    min(previous, close) * 0.99,
                    1_000_000.0 + index,
                    close * (1_000_000.0 + index),
                )
            )
        return result


class _UnavailableMarket:
    latest_common_session = None

    def latest_date(self):
        raise RuntimeError("synthetic market is unavailable")


class _TwoSymbolMarket(_Market):
    """Market double that can make a previously missing symbol available."""

    def __init__(self, closes, symbols, *, start=date(2026, 7, 1), version=None):
        super().__init__(closes, start=start, version=version)
        self._available_symbols = set(symbols)
        self.symbols = {symbol: SimpleNamespace() for symbol in self._available_symbols}
        self.manifest = {
            "files": {
                symbol: {"source_provider": "synthetic"}
                for symbol in self._available_symbols
            }
        }

    def history(self, symbol, on_date, count):
        if symbol not in self._available_symbols:
            raise KeyError(symbol)
        return super().history(SYMBOL, on_date, count)


class _WeekendGapMarket:
    """A Friday bar with a Monday cutoff; Saturday/Sunday are not sessions."""

    def __init__(self):
        friday = date(2026, 7, 17)
        monday = date(2026, 7, 20)
        self._bars = [
            Bar(friday, 10.0, 10.0, 10.1, 9.9, 1_000_000.0, 10_000_000.0)
        ]
        self.calendar = [friday, monday]
        self.latest_common_session = friday
        self.completed_through = monday
        self.symbols = {SYMBOL: SimpleNamespace()}
        self.file_hashes = {SYMBOL: "b" * 64}
        self.manifest = {"files": {SYMBOL: {"source_provider": "synthetic"}}}

    def latest_date(self):
        return self.latest_common_session

    def history(self, symbol, on_date, count):
        if symbol != SYMBOL:
            raise KeyError(symbol)
        return [bar for bar in self._bars if bar.date <= on_date][-count:]

    def snapshot_metadata(self):
        return {
            "provider": "synthetic",
            "latest_common_session": self.latest_common_session.isoformat(),
            "completed_through": self.completed_through.isoformat(),
        }


def _config(root: Path):
    return SimpleNamespace(
        project_root=root,
        monitoring_dir=root / "monitoring",
        instruments=[Instrument(SYMBOL, "Synthetic", "SH", "equity")],
    )


class MonitoringEngineTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = _config(self.root)
        self.engine = MonitoringEngine(self.config)
        self.store = self.engine.store

    def tearDown(self):
        self.temporary.cleanup()

    def _configured_profile(
        self,
        owner="alice",
        *,
        threshold=10.5,
        cooldown=1,
        rule_type="close_above",
    ):
        profile = self.store.profile(owner)
        config = profile.create_watchlist("Core", actor="tester", expected_revision=0)
        watchlist_id = config["watchlists"][0]["watchlist_id"]
        config = profile.mutate_watchlist(
            watchlist_id,
            action="add_symbol",
            symbol=SYMBOL,
            actor="tester",
            expected_revision=config["revision"],
        )
        config = profile.create_rule(
            {
                "watchlist_id": watchlist_id,
                "symbol": SYMBOL,
                "rule_type": rule_type,
                "threshold": threshold,
                "cooldown_sessions": cooldown,
                "severity": "warning",
            },
            actor="tester",
            expected_revision=config["revision"],
        )
        return profile, config

    def test_owner_isolation_and_fixed_research_authority(self):
        alice, _ = self._configured_profile("alice")
        bob = self.store.profile("bob")

        market = _Market([10.0, 11.0, 12.0])
        alice_status = self.engine.status("alice", market=market)
        bob_status = self.engine.status("bob", market=market)

        self.assertEqual(alice_status["summary"]["watchlist_count"], 1)
        self.assertEqual(bob_status["summary"]["watchlist_count"], 0)
        self.assertEqual(bob_status["configuration"]["revision"], 0)
        self.assertNotIn("owner", alice_status["configuration"])
        self.assertEqual(alice_status["authority"]["research_only"], True)
        self.assertFalse(alice_status["authority"]["execution_authorized"])
        self.assertIsNone(bob.latest_scan())

    def test_configuration_revision_conflict_is_optimistic_and_immutable(self):
        profile = self.store.profile("alice")
        first = profile.create_watchlist("First", actor="tester", expected_revision=0)
        self.assertEqual(first["revision"], 1)
        with self.assertRaises(MonitoringConflictError):
            profile.create_watchlist("Stale", actor="tester", expected_revision=0)

        current = profile.current()
        self.assertEqual(current["revision"], 1)
        self.assertEqual(len(list((profile.directory / "configurations").glob("*.json"))), 1)
        with self.assertRaises(MonitoringConflictError):
            profile.mutate_watchlist(
                first["watchlists"][0]["watchlist_id"],
                action="rename",
                name="Stale rename",
                actor="tester",
                expected_revision=0,
            )

    def test_same_snapshot_scan_is_idempotent(self):
        profile, _ = self._configured_profile()
        market = _Market([10.0, 11.0, 12.0])

        first = self.engine.scan("alice", actor="scheduler", market=market)
        second = self.engine.scan("alice", actor="scheduler", market=market)

        self.assertFalse(first["reused"])
        self.assertTrue(second["reused"])
        self.assertEqual(first["scan_id"], second["scan_id"])
        self.assertEqual(first["sequence"], 1)
        self.assertEqual(first["actor"], "scheduler")
        self.assertEqual(len(first["triggered_alert_ids"]), 1)
        self.assertEqual(len(profile.alerts()), 1)
        self.assertEqual(first["authority"]["execution_authorized"], False)

    def test_trigger_and_cooldown_are_recorded_across_snapshots(self):
        profile, _ = self._configured_profile(cooldown=2)

        # False -> true triggers once; false -> true again inside the cooldown
        # window is suppressed with an explicit reason.
        scans = []
        for closes in (
            [10.0, 11.0, 10.0],
            [10.0, 11.0, 10.0, 12.0],
            [10.0, 11.0, 10.0, 12.0, 10.0],
            [10.0, 11.0, 10.0, 12.0, 10.0, 12.0],
        ):
            scans.append(self.engine.scan("alice", actor="scheduler", market=_Market(closes)))

        self.assertEqual(scans[0]["triggered_alert_ids"], [])
        self.assertEqual(len(scans[1]["triggered_alert_ids"]), 1)
        self.assertEqual(scans[2]["triggered_alert_ids"], [])
        self.assertEqual(scans[3]["triggered_alert_ids"], [])
        self.assertEqual(scans[3]["suppressed"][0]["reason"], "cooldown")
        self.assertEqual([scan["sequence"] for scan in scans], [1, 2, 3, 4])
        self.assertEqual(len(profile.alerts()), 1)

    def test_alert_actions_are_auditable_and_stateful(self):
        profile, _ = self._configured_profile()
        scan = self.engine.scan("alice", actor="scheduler", market=_Market([10.0, 11.0, 12.0]))
        alert_id = scan["triggered_alert_ids"][0]

        snoozed = profile.alert_action(
            alert_id,
            action="snooze",
            actor="alice",
            note="Review after close",
            snooze_until="2026-07-31",
        )
        self.assertEqual(snoozed["status"], "snoozed")
        self.assertEqual(snoozed["last_action"]["note"], "Review after close")
        self.assertEqual(snoozed["last_action"]["sequence"], 1)
        reopened = profile.alert_action(alert_id, action="unsnooze", actor="alice")
        self.assertEqual(reopened["status"], "open")
        acknowledged = profile.alert_action(alert_id, action="acknowledge", actor="alice")
        self.assertEqual(acknowledged["status"], "acknowledged")
        dismissed = profile.alert_action(alert_id, action="dismiss", actor="alice")
        self.assertEqual(dismissed["status"], "dismissed")
        reopened = profile.alert_action(alert_id, action="reopen", actor="alice")
        self.assertEqual(reopened["status"], "open")
        self.assertEqual(reopened["last_action"]["sequence"], 5)
        self.assertEqual(profile.alerts()[0]["last_action"]["action"], "reopen")
        with self.assertRaises(MonitoringConflictError):
            profile.alert_action(alert_id, action="reopen", actor="alice")
        with self.assertRaises(ValueError):
            profile.alert_action(alert_id, action="snooze", actor="alice")

    def test_alert_state_compare_and_swap_rejects_a_stale_fingerprint(self):
        profile, _ = self._configured_profile()
        scan = self.engine.scan(
            "alice", actor="scheduler", market=_Market([10.0, 11.0, 12.0])
        )
        alert_id = scan["triggered_alert_ids"][0]
        state_fingerprint = profile.alerts()[0]["state_fingerprint"]

        acknowledged = profile.alert_action(
            alert_id,
            action="acknowledge",
            actor="alice",
            expected_state_fingerprint=state_fingerprint,
        )
        self.assertEqual(acknowledged["status"], "acknowledged")
        self.assertNotEqual(acknowledged["state_fingerprint"], state_fingerprint)
        with self.assertRaises(MonitoringConflictError):
            profile.alert_action(
                alert_id,
                action="dismiss",
                actor="alice",
                expected_state_fingerprint=state_fingerprint,
            )
        with self.assertRaises(ValueError):
            profile.alert_action(
                alert_id,
                action="dismiss",
                actor="alice",
                expected_state_fingerprint="not-a-fingerprint",
            )
        self.assertEqual(profile.alerts()[0]["status"], "acknowledged")

    def test_rule_evaluation_failure_with_available_snapshot_is_persisted(self):
        profile, _ = self._configured_profile()
        market = _Market([10.0, 11.0, 12.0])
        with patch.object(
            self.engine, "_evaluate_rule", side_effect=RuntimeError("indicator exploded")
        ):
            scan = self.engine.scan("alice", actor="scheduler", market=market)

        self.assertEqual(scan["status"], "failed")
        self.assertEqual(scan["error"]["code"], "rule_evaluation_failed")
        self.assertEqual(scan["error"]["message"], "indicator exploded")
        self.assertTrue(scan["snapshot_id"].startswith("market-2026-07-03-"))
        self.assertEqual(scan["data_date"], "2026-07-03")
        self.assertEqual(scan["rule_states"], {})
        self.assertEqual(scan["triggered_alert_ids"], [])
        self.assertEqual(profile.alerts(), [])
        self.assertEqual(profile.latest_scan()["status"], "failed")

    def test_alert_write_failure_records_failed_scan_without_orphan_alert(self):
        profile, _ = self._configured_profile()
        market = _Market([10.0, 11.0, 12.0])
        with patch.object(
            profile,
            "_write_alert_unlocked",
            side_effect=OSError("alert disk is full"),
        ):
            scan = self.engine.scan_profile(profile, actor="scheduler", market=market)

        self.assertEqual(scan["status"], "failed")
        self.assertEqual(scan["error"]["code"], "alert_write_failed")
        self.assertEqual(scan["triggered_alert_ids"], [])
        self.assertEqual(profile.alerts(), [])
        self.assertEqual(profile.latest_scan()["status"], "failed")

    def test_scan_write_failure_rolls_back_alerts_before_rethrowing(self):
        profile, _ = self._configured_profile()
        market = _Market([10.0, 11.0, 12.0])
        with patch.object(
            profile,
            "_write_scan_unlocked",
            side_effect=OSError("scan disk is full"),
        ):
            with self.assertRaises(OSError):
                self.engine.scan_profile(profile, actor="scheduler", market=market)

        self.assertEqual(profile.alerts(), [])
        self.assertIsNone(profile.latest_scan())
        alert_directory = profile.directory / "alerts"
        self.assertFalse(any(alert_directory.glob("alert_*.json")))

    def test_data_stale_counts_trading_sessions_not_calendar_days(self):
        profile, config = self._configured_profile(
            rule_type="data_stale", threshold=2, cooldown=0
        )
        market = _WeekendGapMarket()

        scan = self.engine.scan("alice", actor="scheduler", market=market)

        rule_id = config["rules"][0]["rule_id"]
        self.assertEqual(scan["status"], "succeeded")
        self.assertEqual(scan["data_date"], "2026-07-17")
        self.assertEqual(scan["completed_session_cutoff"], "2026-07-20")
        self.assertEqual(scan["rule_states"][rule_id]["observed_value"], 1)
        self.assertFalse(scan["rule_states"][rule_id]["triggered"])
        self.assertEqual(scan["triggered_alert_ids"], [])
        self.assertEqual(profile.alerts(), [])

    def test_rule_fingerprint_change_resets_cooldown_for_same_rule_id(self):
        profile, config = self._configured_profile(cooldown=10)
        self.engine.scan("alice", actor="scheduler", market=_Market([10.0, 11.0, 10.0]))
        first_trigger = self.engine.scan(
            "alice", actor="scheduler", market=_Market([10.0, 11.0, 10.0, 12.0])
        )
        self.assertEqual(len(first_trigger["triggered_alert_ids"]), 1)
        self.engine.scan(
            "alice", actor="scheduler", market=_Market([10.0, 11.0, 10.0, 12.0, 10.0])
        )

        rule = config["rules"][0]
        updated = profile.mutate_rule(
            rule["rule_id"],
            action="update",
            patch={"threshold": 11.5},
            actor="tester",
            expected_revision=config["revision"],
        )
        self.assertEqual(updated["rules"][0]["rule_id"], rule["rule_id"])
        changed_trigger = self.engine.scan(
            "alice",
            actor="scheduler",
            market=_Market([10.0, 11.0, 10.0, 12.0, 10.0, 12.0]),
        )

        self.assertEqual(len(changed_trigger["triggered_alert_ids"]), 1)
        alerts = profile.alerts()
        self.assertEqual(len(alerts), 2)
        self.assertEqual({item["rule_id"] for item in alerts}, {rule["rule_id"]})
        self.assertEqual(len({item["rule_fingerprint"] for item in alerts}), 2)

    def test_partial_scan_same_snapshot_can_recover_without_reusing_partial_record(self):
        profile, _ = self._configured_profile()
        market = _Market([10.0, 11.0, 12.0])
        original_evaluate = self.engine._evaluate_rule
        attempts = 0

        def transient_exclusion(rule, current_market, data_date, cutoff):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return {
                    "exclusion": {
                        "code": "history_unavailable",
                        "message": "temporary provider read failure",
                    },
                    "source": "synthetic",
                }
            return original_evaluate(rule, current_market, data_date, cutoff)

        with patch.object(self.engine, "_evaluate_rule", side_effect=transient_exclusion):
            partial = self.engine.scan("alice", actor="scheduler", market=market)
            recovered = self.engine.scan("alice", actor="scheduler", market=market)

        self.assertEqual(partial["status"], "partial")
        self.assertEqual(partial["snapshot_id"], recovered["snapshot_id"])
        self.assertNotEqual(partial["scan_id"], recovered["scan_id"])
        self.assertFalse(recovered["reused"])
        self.assertEqual(recovered["status"], "succeeded")
        self.assertEqual(recovered["sequence"], 2)
        self.assertEqual(len(recovered["triggered_alert_ids"]), 1)
        self.assertEqual(len(profile.alerts()), 1)
        reused = self.engine.scan("alice", actor="scheduler", market=market)
        self.assertTrue(reused["reused"])
        self.assertEqual(reused["scan_id"], recovered["scan_id"])

    def test_interrupted_alert_publication_is_recovered_on_next_read(self):
        profile, _ = self._configured_profile()
        with patch.object(
            profile, "_write_scan_unlocked", side_effect=SystemExit("hard stop")
        ):
            with self.assertRaises(SystemExit):
                self.engine.scan_profile(
                    profile,
                    actor="scheduler",
                    market=_Market([10.0, 11.0, 12.0]),
                )

        transaction = profile.directory / ".scan-transaction.json"
        self.assertTrue(transaction.exists())
        self.assertTrue(any((profile.directory / "alerts").glob("alert_*.json")))
        self.assertEqual(profile.alerts(), [])
        self.assertFalse(transaction.exists())
        self.assertIsNone(profile.latest_scan())

    def test_mutator_recovers_interrupted_transaction_before_writing_config(self):
        profile, _ = self._configured_profile()
        with patch.object(
            profile, "_write_scan_unlocked", side_effect=SystemExit("hard stop")
        ):
            with self.assertRaises(SystemExit):
                self.engine.scan_profile(
                    profile,
                    actor="scheduler",
                    market=_Market([10.0, 11.0, 12.0]),
                )

        # create_watchlist calls _current_unlocked under the owner lock.  It
        # must recover the pending marker before appending revision two.
        updated = profile.create_watchlist(
            "Recovered", actor="tester", expected_revision=3
        )
        self.assertEqual(updated["revision"], 4)
        self.assertFalse((profile.directory / ".scan-transaction.json").exists())
        self.assertEqual(profile.alerts(), [])
        self.assertIsNone(profile.latest_scan())

    def test_incomplete_committed_scan_is_rolled_back_as_a_transaction(self):
        profile, config = self._configured_profile()
        watchlist_id = config["watchlists"][0]["watchlist_id"]
        profile.create_rule(
            {
                "watchlist_id": watchlist_id,
                "symbol": SYMBOL,
                "rule_type": "close_above",
                "threshold": 11.5,
                "cooldown_sessions": 0,
                "severity": "warning",
            },
            actor="tester",
            expected_revision=config["revision"],
        )
        with patch.object(
            profile, "_finish_scan_transaction_unlocked", side_effect=SystemExit("hard stop")
        ):
            with self.assertRaises(SystemExit):
                self.engine.scan_profile(
                    profile,
                    actor="scheduler",
                    market=_Market([10.0, 11.0, 12.0]),
                )

        transaction = profile.directory / ".scan-transaction.json"
        self.assertTrue(transaction.exists())
        alert_paths = list((profile.directory / "alerts").glob("alert_*.json"))
        self.assertEqual(len(alert_paths), 2)
        alert_paths[0].unlink()

        profile.verify_integrity()
        self.assertFalse(transaction.exists())
        self.assertEqual(list((profile.directory / "alerts").glob("alert_*.json")), [])
        self.assertEqual(list((profile.directory / "scans").glob("scan_*.json")), [])

    def test_transaction_scan_directory_fails_closed(self):
        profile, _ = self._configured_profile()
        with patch.object(
            profile, "_write_scan_unlocked", side_effect=SystemExit("hard stop")
        ):
            with self.assertRaises(SystemExit):
                self.engine.scan_profile(
                    profile,
                    actor="scheduler",
                    market=_Market([10.0, 11.0, 12.0]),
                )
        marker = json.loads(
            (profile.directory / ".scan-transaction.json").read_text(encoding="utf-8")
        )
        scan_path = profile.directory / "scans" / f"{marker['scan_id']}.json"
        scan_path.mkdir(parents=True)
        with self.assertRaisesRegex(RuntimeError, "scan is invalid"):
            profile.current()

    def test_staging_residue_is_removed_and_unexpected_entries_fail_closed(self):
        profile, _ = self._configured_profile()
        staging = profile.directory / ".staging"
        residue = staging / ".revision_00000099.json.crashed.tmp"
        residue.write_text("partial", encoding="utf-8")
        self.assertEqual(profile.current()["revision"], 3)
        self.assertFalse(residue.exists())

        unexpected = staging / "not-a-publish-file"
        unexpected.write_text("unexpected", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "Unexpected monitoring staging entry"):
            profile.current()

    def test_write_paths_reject_invalid_envelope_and_semantics_before_publish(self):
        profile, _ = self._configured_profile()
        scan = self.engine.scan(
            "alice", actor="scheduler", market=_Market([10.0, 11.0, 12.0])
        )
        alert_path = next((profile.directory / "alerts").glob("alert_*.json"))
        alert = json.loads(alert_path.read_text(encoding="utf-8"))
        bad_source = dict(alert)
        bad_source["source"] = "x" * 121
        bad_source["fingerprint"] = _fingerprint_for(bad_source)
        with self.store._owner_lock(profile.profile_id):
            with self.assertRaisesRegex(RuntimeError, "source"):
                profile._write_alert_unlocked(alert["alert_id"], bad_source)

        bad_fingerprint = dict(alert)
        bad_fingerprint["fingerprint"] = "0" * 64
        with self.store._owner_lock(profile.profile_id):
            with self.assertRaisesRegex(ValueError, "fingerprint"):
                profile._write_alert_unlocked(alert["alert_id"], bad_fingerprint)

        with self.assertRaisesRegex(ValueError, "failed scans cannot publish"):
            self.engine._failed_scan_record(
                profile,
                profile.current(),
                actor="tester",
                started_at="2026-07-01T00:00:00Z",
                sequence=2,
                error={"code": "test", "message": "invalid"},
                triggered_alert_ids=[scan["triggered_alert_ids"][0]],
            )

    def test_scan_automatically_expires_snooze_at_completed_cutoff(self):
        profile, _ = self._configured_profile(cooldown=0)
        today = date.today()
        tomorrow = today + timedelta(days=1)
        first = self.engine.scan(
            "alice", actor="scheduler", market=_Market([12.0], start=today)
        )
        alert_id = first["triggered_alert_ids"][0]
        profile.alert_action(
            alert_id,
            action="snooze",
            actor="alice",
            snooze_until=tomorrow.isoformat(),
        )
        self.assertEqual(profile.alerts()[0]["status"], "snoozed")

        second = self.engine.scan(
            "alice", actor="scheduler", market=_Market([12.0, 12.0], start=today)
        )
        self.assertEqual(second["status"], "succeeded")
        self.assertEqual(profile.alerts()[0]["status"], "open")
        self.assertEqual(profile.alerts()[0]["last_action"]["action"], "unsnooze")
        self.assertEqual(profile.alerts()[0]["last_action"]["sequence"], 2)
        self.assertEqual(profile.alerts()[0]["last_action"]["actor"], "scheduler")

    def test_configuration_and_action_capacity_limits_are_enforced(self):
        profile = self.store.profile("alice")
        with patch("ai_trade.monitoring.MAX_CONFIG_REVISIONS", 1):
            first = profile.create_watchlist("First", actor="tester", expected_revision=0)
            with self.assertRaises(MonitoringCapacityError):
                profile.create_watchlist(
                    "Second", actor="tester", expected_revision=first["revision"]
                )
            self.assertEqual(profile.current()["revision"], 1)

        profile, _ = self._configured_profile("capacity")
        scan = self.engine.scan(
            "capacity", actor="scheduler", market=_Market([10.0, 11.0, 12.0])
        )
        alert_id = scan["triggered_alert_ids"][0]
        with patch("ai_trade.monitoring.MAX_ACTIONS", 1):
            profile.alert_action(alert_id, action="acknowledge", actor="capacity")
            with self.assertRaises(MonitoringCapacityError):
                profile.alert_action(alert_id, action="dismiss", actor="capacity")
        self.assertEqual(profile.alerts()[0]["status"], "acknowledged")

    def test_scan_all_profiles_isolates_one_profile_failure(self):
        alice, _ = self._configured_profile("alice")
        bob, _ = self._configured_profile("bob")
        original_scan_profile = self.engine.scan_profile

        def fail_alice(profile, *, actor, market):
            if profile.profile_id == alice.profile_id:
                raise RuntimeError("synthetic user failure")
            return original_scan_profile(profile, actor=actor, market=market)

        with patch.object(self.engine, "scan_profile", side_effect=fail_alice):
            results = self.engine.scan_all_profiles(
                actor="scheduler", market=_Market([10.0, 11.0, 12.0])
            )

        self.assertEqual(len(results), 2)
        self.assertEqual(sum(item["status"] == "failed" for item in results), 1)
        self.assertEqual(sum(item["status"] == "succeeded" for item in results), 1)
        failed = next(item for item in results if item["status"] == "failed")
        self.assertEqual(failed["error"]["code"], "profile_scan_failed")
        self.assertIsNone(alice.latest_scan())
        self.assertEqual(bob.latest_scan()["status"], "succeeded")

    def test_removed_security_master_symbol_can_still_be_removed(self):
        from ai_trade.web.service import DashboardService

        service = DashboardService(self.config)
        engine = MonitoringEngine(self.config)
        service._monitoring = engine
        service.monitoring = lambda **_kwargs: {"ok": True}
        profile = engine.store.profile("alice")
        config = profile.create_watchlist("Legacy", actor="tester", expected_revision=0)
        watchlist_id = config["watchlists"][0]["watchlist_id"]
        config = profile.mutate_watchlist(
            watchlist_id,
            action="add_symbol",
            symbol="REMOVED",
            actor="tester",
            expected_revision=config["revision"],
        )

        result = service.monitoring_watchlist_action(
            owner_id="alice",
            actor="tester",
            watchlist_id=watchlist_id,
            action="remove_symbol",
            symbol="REMOVED",
            expected_revision=config["revision"],
        )
        self.assertEqual(result, {"ok": True})
        self.assertEqual(profile.current()["watchlists"][0]["symbols"], [])

    def test_profile_and_users_symlinks_are_rejected(self):
        profile = self.store.profile("alice")
        profile_directory = profile.directory
        original_is_symlink = Path.is_symlink

        def fake_is_symlink(path):
            return path == profile_directory or original_is_symlink(path)

        with patch.object(Path, "is_symlink", fake_is_symlink):
            with self.assertRaises(RuntimeError):
                profile.current()

        users_directory = self.store.root / "users"

        def fake_users_is_symlink(path):
            return path == users_directory or original_is_symlink(path)

        with patch.object(Path, "is_symlink", fake_users_is_symlink):
            with self.assertRaises(RuntimeError):
                self.store.profile_ids()

    def test_tampering_with_immutable_records_is_detected(self):
        profile, _ = self._configured_profile()
        config_path = next((profile.directory / "configurations").glob("revision_*.json"))
        raw = json.loads(config_path.read_text(encoding="utf-8"))
        raw["actor"] = "tampered"
        config_path.write_text(json.dumps(raw), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            profile.current()

        # Build a fresh profile so the alert record can be tested independently.
        self.temporary.cleanup()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = _config(self.root)
        self.engine = MonitoringEngine(self.config)
        self.store = self.engine.store
        profile, _ = self._configured_profile()
        self.engine.scan("alice", actor="scheduler", market=_Market([10.0, 11.0, 12.0]))
        alert_path = next((profile.directory / "alerts").glob("alert_*.json"))
        alert = json.loads(alert_path.read_text(encoding="utf-8"))
        alert["status"] = "dismissed"
        alert_path.write_text(json.dumps(alert), encoding="utf-8")
        with self.assertRaises(RuntimeError):
            profile.alerts()

    def test_semantic_alert_tampering_is_detected_after_outer_refingerprint(self):
        profile, _ = self._configured_profile()
        self.engine.scan(
            "alice", actor="scheduler", market=_Market([10.0, 11.0, 12.0])
        )
        alert_path = next((profile.directory / "alerts").glob("alert_*.json"))
        _rewrite_fingerprint(alert_path, observed_value=999.0)

        with self.assertRaisesRegex(RuntimeError, "rule-state binding"):
            profile.verify_integrity()

    def test_duplicate_scan_sequence_is_detected_after_outer_refingerprint(self):
        profile, _ = self._configured_profile()
        self.engine.scan(
            "alice", actor="scheduler", market=_Market([10.0, 11.0, 10.0])
        )
        second = self.engine.scan(
            "alice", actor="scheduler", market=_Market([10.0, 11.0, 10.0, 12.0])
        )
        second_path = profile.directory / "scans" / f"{second['scan_id']}.json"
        _rewrite_fingerprint(second_path, sequence=1)

        with self.assertRaisesRegex(RuntimeError, "sequence is not contiguous"):
            profile.verify_integrity()

    def test_failed_scan_cannot_retain_committed_alert_ids(self):
        profile, _ = self._configured_profile()
        scan = self.engine.scan(
            "alice", actor="scheduler", market=_Market([10.0, 11.0, 12.0])
        )
        scan_path = profile.directory / "scans" / f"{scan['scan_id']}.json"
        _rewrite_fingerprint(
            scan_path,
            status="failed",
            error={"code": "rule_evaluation_failed", "message": "forged"},
        )

        with self.assertRaisesRegex(RuntimeError, "failed scans cannot publish"):
            profile.verify_integrity()

    def test_unavailable_snapshot_fails_closed_and_records_failed_scan(self):
        profile, _ = self._configured_profile()
        market = _UnavailableMarket()

        status = self.engine.status("alice", market=market)
        self.assertFalse(status["snapshot"]["available"])
        self.assertEqual(status["snapshot"]["error"]["code"], "snapshot_invalid")
        self.assertEqual(status["scan"]["status"], "not_run")
        scan = self.engine.scan("alice", actor="scheduler", market=market)
        self.assertEqual(scan["status"], "failed")
        self.assertEqual(scan["error"]["code"], "snapshot_invalid")
        self.assertEqual(scan["sequence"], 1)
        self.assertEqual(scan["actor"], "scheduler")
        self.assertFalse(scan["authority"]["execution_authorized"])
        latest = profile.latest_scan()
        self.assertIsNotNone(latest)
        self.assertEqual(latest["status"], "failed")

    def test_missing_symbol_is_explicitly_excluded_in_partial_scan(self):
        profile, config = self._configured_profile()
        watchlist_id = config["watchlists"][0]["watchlist_id"]
        config = profile.mutate_watchlist(
            watchlist_id,
            action="add_symbol",
            symbol="BBB",
            actor="tester",
            expected_revision=config["revision"],
        )
        profile.create_rule(
            {
                "watchlist_id": watchlist_id,
                "symbol": "BBB",
                "rule_type": "close_above",
                "threshold": 1.0,
            },
            actor="tester",
            expected_revision=config["revision"],
        )

        scan = self.engine.scan("alice", actor="scheduler", market=_Market([10.0, 11.0, 12.0]))
        self.assertEqual(scan["status"], "partial")
        self.assertEqual(len(scan["triggered_alert_ids"]), 1)
        self.assertTrue(any(item["symbol"] == "BBB" for item in scan["exclusions"]))


if __name__ == "__main__":
    unittest.main()
