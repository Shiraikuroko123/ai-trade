from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from ai_trade.security import SecurityMaster
from ai_trade.security_store import SecurityMasterVersionStore


def _master(name: str = "CSI 300 ETF") -> SecurityMaster:
    return SecurityMaster.from_dict(
        {
            "schema_version": 1,
            "as_of": "2026-07-27",
            "selection_method": "test_fixture",
            "provenance": "unit test",
            "instruments": [
                {
                    "symbol": "510300",
                    "name": name,
                    "market": "SH",
                    "asset": "China large cap",
                    "lot_size": 100,
                    "instrument_type": "ETF",
                    "asset_class": "equity",
                    "sector": "china_large_cap",
                    "currency": "CNY",
                    "board": "ETF",
                    "listing_date": "2012-05-28",
                    "delisting_date": None,
                    "price_limit_pct": 0.1,
                    "tick_size": 0.001,
                }
            ],
            "universes": {
                "core_etf": [
                    {
                        "symbol": "510300",
                        "start": "2012-05-28",
                        "end": None,
                    }
                ]
            },
            "status_periods": [],
        }
    )


def _source(
    digest: str = "a" * 64,
    *,
    provider: str = "manual",
    usage_scope: str = "internal_research_only",
    request: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "provider": provider,
        "dataset": "configured_security_master",
        "request": request or {"mode": "fixture"},
        "rows": 1,
        "response_sha256": digest,
        "usage_scope": usage_scope,
    }


class SecurityMasterVersionStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "security_master"
        self.store = SecurityMasterVersionStore(self.root)
        self.first_time = datetime(2026, 7, 27, 8, tzinfo=timezone.utc)
        self.second_time = self.first_time + timedelta(hours=1)

    def test_business_payload_round_trips_without_changing_its_fingerprint(self):
        master = _master()

        restored = SecurityMaster.from_dict(master.to_dict())

        self.assertEqual(restored.to_dict(), master.to_dict())
        self.assertEqual(restored.fingerprint(), master.fingerprint())

    def test_versions_resolve_by_knowledge_time_and_form_a_hash_chain(self):
        first = self.store.publish(
            _master(),
            known_at=self.first_time,
            source_manifest=_source(),
        )
        second = self.store.publish(
            _master("Updated ETF name"),
            known_at=self.second_time,
            source_manifest=_source("b" * 64),
        )

        self.assertIsNone(first.record["previous_version_id"])
        self.assertEqual(second.record["previous_version_id"], first.version_id)
        self.assertEqual(second.record["previous_record_sha256"], first.record_sha256)
        self.assertEqual(
            self.store.resolve(self.first_time + timedelta(minutes=30)).version_id,
            first.version_id,
        )
        resolved = self.store.resolve(self.second_time + timedelta(minutes=1))
        self.assertEqual(resolved.version_id, second.version_id)
        self.assertEqual(
            resolved.master.instruments["510300"].name,
            "Updated ETF name",
        )
        self.assertEqual(
            [item["version_id"] for item in self.store.versions()],
            [first.version_id, second.version_id],
        )

    def test_identical_capture_time_is_idempotent_but_cannot_be_reassigned(self):
        first = self.store.publish(
            _master(),
            known_at=self.first_time,
            source_manifest=_source(),
        )
        reused = self.store.publish(
            _master(),
            known_at=self.first_time,
            source_manifest=_source(),
        )

        self.assertEqual(reused.version_id, first.version_id)
        self.assertTrue(reused.reused)
        with self.assertRaisesRegex(ValueError, "already identifies"):
            self.store.publish(
                _master("Changed"),
                known_at=self.first_time,
                source_manifest=_source("b" * 64),
            )

    def test_store_rejects_backdating_credentials_and_wrong_jqdata_scope(self):
        self.store.publish(
            _master(),
            known_at=self.second_time,
            source_manifest=_source(),
        )
        with self.assertRaisesRegex(ValueError, "must not precede"):
            self.store.publish(
                _master(),
                known_at=self.first_time,
                source_manifest=_source(),
            )

        for request in (
            {"password": "do-not-store"},
            {"account": "13812345678"},
        ):
            with self.subTest(request=request):
                with self.assertRaisesRegex(ValueError, "credential field|unsafe text"):
                    SecurityMasterVersionStore(
                        Path(self.temporary.name) / f"unsafe-{len(request)}"
                    ).publish(
                        _master(),
                        known_at=self.first_time,
                        source_manifest=_source(request=request),
                    )

        with self.assertRaisesRegex(ValueError, "personal research only"):
            SecurityMasterVersionStore(Path(self.temporary.name) / "jqdata").publish(
                _master(),
                known_at=self.first_time,
                source_manifest=_source(
                    provider="jqdata",
                    usage_scope="internal_research_only",
                ),
            )

    def test_cutoff_before_first_version_and_tampering_fail_closed(self):
        first = self.store.publish(
            _master(),
            known_at=self.first_time,
            source_manifest=_source(),
        )
        with self.assertRaisesRegex(KeyError, "known by"):
            self.store.resolve(self.first_time - timedelta(microseconds=1))

        value = json.loads(first.path.read_text(encoding="utf-8"))
        value["security_master"]["instruments"][0]["name"] = "Tampered"
        first.path.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "fingerprint"):
            self.store.resolve(self.second_time)


if __name__ == "__main__":
    unittest.main()
