import json
import os
import tempfile
import unittest
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_trade.cloud import (
    CloudIntegrityError,
    CloudSettings,
    R2ObjectStore,
    cloud_dashboard_status,
    save_cloud_dashboard_preferences,
    tracked_r2_store,
)
from ai_trade.cloud_usage import (
    CloudUsageStore,
    billing_period,
    cloud_preferences_path,
    load_cloud_preferences,
    update_cloud_preferences,
    usage_summary,
)


class CloudUsageTests(unittest.TestCase):
    def test_preferences_default_to_local_or_hybrid_and_round_trip(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state/cloud_preferences.json"

            self.assertEqual(
                load_cloud_preferences(path, cloud_configured=False).storage_mode,
                "local",
            )
            self.assertEqual(
                load_cloud_preferences(path, cloud_configured=True).storage_mode,
                "hybrid",
            )
            saved = update_cloud_preferences(
                path,
                {
                    "storage_mode": "local",
                    "storage_limit_gb": 25.5,
                    "class_a_limit": 2_000_000,
                    "class_b_limit": 20_000_000,
                    "billing_cycle_day": 12,
                },
                cloud_configured=True,
            )

            self.assertEqual(saved.storage_limit_bytes, 25_500_000_000)
            self.assertEqual(load_cloud_preferences(path), saved)
            self.assertNotIn("credential", path.read_text(encoding="utf-8").lower())

    def test_preferences_reject_hybrid_without_cloud_and_preserve_file(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state/cloud_preferences.json"
            original = update_cloud_preferences(
                path, {"storage_mode": "local"}, cloud_configured=False
            )
            before = path.read_bytes()

            with self.assertRaisesRegex(ValueError, "configured"):
                update_cloud_preferences(
                    path, {"storage_mode": "hybrid"}, cloud_configured=False
                )

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(load_cloud_preferences(path), original)

    def test_usage_store_tracks_operations_inventory_and_remaining_budget(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            usage = CloudUsageStore(root / "state/cloud_usage.sqlite3")
            usage.record("class_a", 3)
            usage.record("class_b", 4)
            usage.save_inventory(
                object_count=2,
                storage_bytes=1_500_000_000,
                snapshots=[
                    {
                        "snapshot_id": "20260713T120000Z-0123456789ab",
                        "size": 1_500_000_000,
                        "last_modified": "2026-07-13T12:00:00+00:00",
                    }
                ],
            )
            preferences = update_cloud_preferences(
                cloud_preferences_path(root),
                {
                    "storage_mode": "local",
                    "storage_limit_gb": 2,
                    "class_a_limit": 10,
                    "class_b_limit": 20,
                    "billing_cycle_day": 1,
                },
                cloud_configured=False,
            )

            result = usage_summary(preferences, usage)

            self.assertEqual(result["storage_remaining_bytes"], 500_000_000)
            self.assertEqual(result["class_a_remaining"], 7)
            self.assertEqual(result["class_b_remaining"], 16)
            self.assertEqual(result["object_count"], 2)
            self.assertTrue(result["tracking_started_at"])
            self.assertEqual(
                result["measurement_scope"],
                "this_installation_namespace_and_observed_requests",
            )

    def test_inventory_scan_counts_namespace_and_records_class_a(self):
        with tempfile.TemporaryDirectory() as temporary:
            usage = CloudUsageStore(Path(temporary) / "state/cloud_usage.sqlite3")
            settings = _settings()
            snapshot_key = (
                f"{settings.namespace}/snapshots/market-cache/"
                "20260713T120000Z-0123456789ab.zip"
            )
            client = _InventoryClient(
                {
                    snapshot_key: 1200,
                    f"{settings.namespace}/indexes/market-cache/latest.json": 300,
                }
            )
            store = R2ObjectStore(settings, client=client, usage_store=usage)

            inventory = store.refresh_inventory()
            start, end = billing_period(date.today(), 1)
            observed = usage.usage(start, end)

            self.assertEqual(inventory["storage_bytes"], 1500)
            self.assertEqual(inventory["object_count"], 2)
            self.assertEqual(
                inventory["snapshots"][0]["snapshot_id"],
                "20260713T120000Z-0123456789ab",
            )
            self.assertNotIn("object_key", inventory["snapshots"][0])
            self.assertEqual(observed["class_a"], 1)
            self.assertEqual(observed["class_b"], 0)

    def test_dashboard_status_is_allowlisted_and_preferences_are_user_editable(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "data/cache"
            cache.mkdir(parents=True)
            (cache / "sample.csv").write_bytes(b"1234")
            config = SimpleNamespace(project_root=root, cache_dir=cache)
            environment = _environment()

            with patch.dict(os.environ, environment, clear=True), patch(
                "ai_trade.cloud.cloud_dependency_available", return_value=True
            ):
                initial = cloud_dashboard_status(config)
                updated = save_cloud_dashboard_preferences(
                    config,
                    {
                        "storage_mode": "local",
                        "storage_limit_gb": 15,
                        "class_a_limit": 500,
                        "class_b_limit": 800,
                        "billing_cycle_day": 5,
                    },
                )

            rendered = json.dumps(updated, ensure_ascii=False)
            for secret in (
                environment["AI_TRADE_R2_ENDPOINT"],
                environment["AI_TRADE_R2_BUCKET"],
                environment["AI_TRADE_R2_ACCESS_KEY_ID"],
                environment["AI_TRADE_R2_SECRET_ACCESS_KEY"],
                environment["AI_TRADE_CLOUD_INSTALLATION_ID"],
            ):
                self.assertNotIn(secret, rendered)
            self.assertTrue(initial["configured"])
            self.assertFalse(updated["official_account_usage"])
            self.assertEqual(updated["effective_storage_mode"], "local")
            self.assertEqual(updated["preferences"]["class_a_limit"], 500)
            self.assertEqual(updated["local"], {"bytes": 4, "files": 1})

    def test_dashboard_state_is_isolated_for_each_r2_profile(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            cache = root / "data/cache"
            cache.mkdir(parents=True)
            config = SimpleNamespace(project_root=root, cache_dir=cache)
            first = _settings()
            second = replace(
                first,
                bucket="other-test-bucket",
                installation_id="b" * 32,
            )

            with patch(
                "ai_trade.cloud.cloud_dependency_available", return_value=True
            ):
                with patch.dict(os.environ, _environment(first), clear=True):
                    save_cloud_dashboard_preferences(
                        config,
                        {
                            "storage_mode": "hybrid",
                            "storage_limit_gb": 12,
                            "class_a_limit": 11,
                            "class_b_limit": 30,
                            "billing_cycle_day": 3,
                        },
                    )
                    first_store = tracked_r2_store(config, first).usage_store
                    self.assertIsNotNone(first_store)
                    first_store.record("class_a", 4)
                    first_store.save_inventory(
                        object_count=1,
                        storage_bytes=700,
                        snapshots=[
                            {
                                "snapshot_id": "20260713T120000Z-0123456789ab",
                                "size": 700,
                                "last_modified": "2026-07-13T12:00:00+00:00",
                            }
                        ],
                    )

                with patch.dict(os.environ, _environment(second), clear=True):
                    second_status = cloud_dashboard_status(config)
                    save_cloud_dashboard_preferences(
                        config,
                        {
                            "storage_mode": "local",
                            "storage_limit_gb": 20,
                            "class_a_limit": 22,
                            "class_b_limit": 40,
                            "billing_cycle_day": 4,
                        },
                    )

                with patch.dict(os.environ, _environment(first), clear=True):
                    first_status = cloud_dashboard_status(config)

            self.assertEqual(second_status["usage"]["class_a"], 0)
            self.assertEqual(second_status["usage"]["storage_bytes"], 0)
            self.assertEqual(second_status["usage"]["snapshots"], [])
            self.assertEqual(first_status["usage"]["class_a"], 4)
            self.assertEqual(first_status["usage"]["storage_bytes"], 700)
            self.assertEqual(first_status["preferences"]["class_a_limit"], 11)
            self.assertNotIn("profile", json.dumps(first_status))

    def test_profile_fingerprint_normalizes_endpoint_and_region_case(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = SimpleNamespace(project_root=Path(temporary))
            settings = _settings()
            differently_cased = replace(
                settings,
                endpoint=settings.endpoint.upper(),
                region=settings.region.upper(),
            )

            first_path = tracked_r2_store(config, settings).usage_store.path
            second_path = tracked_r2_store(
                config, differently_cased
            ).usage_store.path

            self.assertEqual(first_path, second_path)

    def test_missing_cloud_dependency_keeps_effective_mode_local(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = SimpleNamespace(project_root=root, cache_dir=root / "data/cache")
            with patch.dict(os.environ, _environment(), clear=True), patch(
                "ai_trade.cloud.cloud_dependency_available", return_value=False
            ):
                status = cloud_dashboard_status(config)
                with self.assertRaisesRegex(ValueError, "configured"):
                    save_cloud_dashboard_preferences(
                        config, {"storage_mode": "hybrid"}
                    )

            self.assertTrue(status["configured"])
            self.assertFalse(status["operational"])
            self.assertEqual(status["effective_storage_mode"], "local")
            self.assertEqual(status["preferences"]["storage_mode"], "local")

    def test_inventory_rejects_incomplete_pagination_without_overwriting_cache(self):
        with tempfile.TemporaryDirectory() as temporary:
            usage = CloudUsageStore(Path(temporary) / "state/cloud_usage.sqlite3")
            store = R2ObjectStore(
                _settings(), client=_BrokenPaginationClient(), usage_store=usage
            )

            with self.assertRaisesRegex(CloudIntegrityError, "continuation token"):
                store.refresh_inventory()

            self.assertIsNone(usage.inventory()["scanned_at"])

    def test_snapshot_listing_rejects_repeated_pagination_token(self):
        client = _RepeatedPaginationClient()
        store = R2ObjectStore(_settings(), client=client)

        with self.assertRaisesRegex(CloudIntegrityError, "repeated"):
            store.list_snapshots()

        self.assertEqual(client.calls, 2)

    def test_budget_period_uses_user_selected_utc_reset_day(self):
        self.assertEqual(
            billing_period(date(2026, 7, 13), 11),
            (date(2026, 7, 11), date(2026, 8, 11)),
        )
        self.assertEqual(
            billing_period(date(2026, 1, 3), 11),
            (date(2025, 12, 11), date(2026, 1, 11)),
        )


def _settings() -> CloudSettings:
    return CloudSettings(
        enabled=True,
        endpoint="https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com",
        region="auto",
        bucket="test-bucket",
        access_key_id="test-access-key",
        secret_access_key="test-secret-key",
        prefix="ai-trade",
        installation_id="a" * 32,
    )


def _environment(settings: CloudSettings | None = None) -> dict[str, str]:
    settings = settings or _settings()
    return {
        "AI_TRADE_CLOUD_ENABLED": "1",
        "AI_TRADE_CLOUD_PREFIX": settings.prefix,
        "AI_TRADE_CLOUD_INSTALLATION_ID": settings.installation_id,
        "AI_TRADE_R2_ENDPOINT": settings.endpoint,
        "AI_TRADE_R2_REGION": settings.region,
        "AI_TRADE_R2_BUCKET": settings.bucket,
        "AI_TRADE_R2_ACCESS_KEY_ID": settings.access_key_id,
        "AI_TRADE_R2_SECRET_ACCESS_KEY": settings.secret_access_key,
    }


class _InventoryClient:
    def __init__(self, objects: dict[str, int]):
        self.objects = objects
        self.modified = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)

    def list_objects_v2(self, **request):
        prefix = str(request.get("Prefix", ""))
        return {
            "Contents": [
                {
                    "Key": key,
                    "Size": size,
                    "LastModified": self.modified,
                }
                for key, size in sorted(self.objects.items())
                if key.startswith(prefix)
            ],
            "IsTruncated": False,
        }


class _BrokenPaginationClient:
    def list_objects_v2(self, **request):
        return {"Contents": [], "IsTruncated": True}


class _RepeatedPaginationClient:
    def __init__(self):
        self.calls = 0

    def list_objects_v2(self, **request):
        self.calls += 1
        return {
            "Contents": [],
            "IsTruncated": True,
            "NextContinuationToken": "same-token",
        }


if __name__ == "__main__":
    unittest.main()
