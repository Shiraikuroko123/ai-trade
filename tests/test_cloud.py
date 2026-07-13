from __future__ import annotations

import hashlib
import io
import json
import os
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from ai_trade.cli import _safe_cloud_error, main
from ai_trade.cloud import (
    DATASET,
    CloudConfigurationError,
    CloudIntegrityError,
    R2ObjectStore,
    create_market_snapshot,
    load_cloud_settings,
)
from ai_trade.config import AppConfig, load_config


_CLOUD_ENVIRONMENT = {
    "AI_TRADE_CLOUD_ENABLED",
    "AI_TRADE_CLOUD_PREFIX",
    "AI_TRADE_CLOUD_INSTALLATION_ID",
    "AI_TRADE_R2_ENDPOINT",
    "AI_TRADE_R2_REGION",
    "AI_TRADE_R2_BUCKET",
    "AI_TRADE_R2_ACCESS_KEY_ID",
    "AI_TRADE_R2_SECRET_ACCESS_KEY",
}


class CloudTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "workspace"
        with redirect_stdout(io.StringIO()):
            result = main(["init", "--directory", str(self.root)])
        self.assertEqual(result, 0)
        self.config = load_config(self.root / "config/default.json")
        self.assertEqual(len(self.config.instruments), 8)
        _write_market_cache(self.config)

    def test_cloud_is_disabled_by_default_and_public_status_hides_configuration(self):
        values = {
            "AI_TRADE_R2_ENDPOINT": "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com",
            "AI_TRADE_R2_BUCKET": "private-bucket-name",
            "AI_TRADE_R2_ACCESS_KEY_ID": "private-access-key",
            "AI_TRADE_R2_SECRET_ACCESS_KEY": "private-secret-key",
            "AI_TRADE_CLOUD_INSTALLATION_ID": "a" * 32,
        }
        with _isolated_cloud_environment(values):
            settings = load_cloud_settings()

        self.assertFalse(settings.enabled)
        self.assertFalse(settings.configured)
        status = settings.public_status()
        rendered = json.dumps(status, sort_keys=True) + repr(settings)
        for secret in (
            values["AI_TRADE_R2_ENDPOINT"],
            values["AI_TRADE_R2_BUCKET"],
            values["AI_TRADE_R2_ACCESS_KEY_ID"],
            values["AI_TRADE_R2_SECRET_ACCESS_KEY"],
        ):
            self.assertNotIn(secret, rendered)
        self.assertIsNone(status["provider"])
        self.assertIsNone(status["credentials_source"])

    def test_endpoint_and_prefix_validation(self):
        valid = _configured_environment()
        valid["AI_TRADE_CLOUD_PREFIX"] = "/owner/ai-trade.backups/"
        valid["AI_TRADE_R2_ENDPOINT"] += "/"
        with _isolated_cloud_environment(valid):
            settings = load_cloud_settings()
        self.assertEqual(
            settings.endpoint,
            "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com",
        )
        self.assertEqual(settings.prefix, "owner/ai-trade.backups")

        for jurisdiction in ("eu", "fedramp"):
            environment = _configured_environment()
            environment["AI_TRADE_R2_ENDPOINT"] = (
                "https://0123456789abcdef0123456789abcdef."
                f"{jurisdiction}.r2.cloudflarestorage.com"
            )
            with (
                self.subTest(jurisdiction=jurisdiction),
                _isolated_cloud_environment(environment),
            ):
                settings = load_cloud_settings()
            self.assertEqual(settings.endpoint, environment["AI_TRADE_R2_ENDPOINT"])

        invalid_values = (
            ("AI_TRADE_R2_ENDPOINT", "http://account.r2.cloudflarestorage.com"),
            ("AI_TRADE_R2_ENDPOINT", "https://r2.example.com"),
            (
                "AI_TRADE_R2_ENDPOINT",
                "https://account.r2.cloudflarestorage.com/path",
            ),
            ("AI_TRADE_CLOUD_PREFIX", "ai-trade/../other-user"),
            ("AI_TRADE_CLOUD_PREFIX", "ai trade"),
        )
        for name, value in invalid_values:
            environment = _configured_environment()
            environment[name] = value
            with (
                self.subTest(name=name, value=value),
                _isolated_cloud_environment(environment),
                self.assertRaises(CloudConfigurationError),
            ):
                load_cloud_settings()

    def test_cloud_configuration_error_redacts_provider_coordinates_and_credentials(
        self,
    ):
        environment = _configured_environment()
        object_key = "ai-trade/private-installation/v1/snapshots/private.zip"
        unsafe_message = (
            f"endpoint={environment['AI_TRADE_R2_ENDPOINT']} "
            f"bucket={environment['AI_TRADE_R2_BUCKET']} "
            f"key={object_key} "
            f"access_key={environment['AI_TRADE_R2_ACCESS_KEY_ID']} "
            f"secret_access_key={environment['AI_TRADE_R2_SECRET_ACCESS_KEY']}"
        )
        with _isolated_cloud_environment(environment):
            rendered = _safe_cloud_error(CloudConfigurationError(unsafe_message))

        for sensitive in (
            environment["AI_TRADE_R2_ENDPOINT"],
            environment["AI_TRADE_R2_BUCKET"],
            object_key,
            environment["AI_TRADE_R2_ACCESS_KEY_ID"],
            environment["AI_TRADE_R2_SECRET_ACCESS_KEY"],
        ):
            self.assertNotIn(sensitive, rendered)
        self.assertIn("<redacted endpoint>", rendered)
        self.assertIn("<redacted bucket>", rendered)
        self.assertIn("key=<redacted key>", rendered)

    def test_cloud_provider_error_code_cannot_echo_an_access_key(self):
        environment = _configured_environment()
        provider_error = RuntimeError("provider details are not safe to display")
        provider_error.response = {
            "Error": {"Code": environment["AI_TRADE_R2_ACCESS_KEY_ID"]}
        }

        with _isolated_cloud_environment(environment):
            rendered = _safe_cloud_error(provider_error)

        self.assertNotIn(environment["AI_TRADE_R2_ACCESS_KEY_ID"], rendered)
        self.assertIn("<redacted access key>", rendered)

    def test_cloud_backup_cli_does_not_print_internal_object_key(self):
        result = {
            "snapshot_id": "20260713T120000Z-0123456789ab",
            "object_key": "private-installation/snapshots/market-cache.zip",
            "sha256": "a" * 64,
            "dataset_sha256": "b" * 64,
            "size": 1234,
            "created_at": "2026-07-13T12:00:00+00:00",
            "latest_common_session": "2026-07-13",
            "skipped_duplicate": False,
        }
        output = io.StringIO()
        with (
            _isolated_cloud_environment(_configured_environment()),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cloud.R2ObjectStore"),
            patch("ai_trade.cloud.backup_market_cache", return_value=result),
            redirect_stdout(output),
        ):
            status = main(
                [
                    "--config",
                    str(self.root / "config/default.json"),
                    "cloud-backup",
                ]
            )

        self.assertEqual(status, 0)
        rendered = output.getvalue()
        self.assertNotIn("object_key", rendered)
        self.assertNotIn(result["object_key"], rendered)
        payload = json.loads(rendered)
        self.assertEqual(payload["snapshot_id"], result["snapshot_id"])
        self.assertFalse(payload["skipped_duplicate"])

    def test_snapshot_uses_exact_market_cache_whitelist(self):
        excluded = {
            self.root / "state/beta_users.json": "beta-password-sentinel",
            self.root / "state/live_authorization.json": "live-token-sentinel",
            self.root / "reports/private.html": "report-sentinel",
            self.root / "logs/ai_trade.log": "log-sentinel",
            self.root / ".env": "environment-secret-sentinel",
        }
        for path, content in excluded.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")

        artifact = create_market_snapshot(self.config, self.root / "snapshot.zip")
        expected = {
            "snapshot-manifest.json",
            "data/cache/manifest.json",
            *(f"data/cache/{item.symbol}.csv" for item in self.config.instruments),
        }
        with zipfile.ZipFile(artifact.path) as archive:
            self.assertEqual(set(archive.namelist()), expected)
            archived_content = b"".join(
                archive.read(name) for name in archive.namelist()
            )
            manifest = json.loads(archive.read("snapshot-manifest.json"))

        for content in excluded.values():
            self.assertNotIn(content.encode("utf-8"), archived_content)
        self.assertEqual(
            set(manifest["excluded"]),
            {"state", "logs", "reports", ".env", "broker credentials"},
        )

    def test_snapshot_distinguishes_requested_and_completed_sessions(self):
        manifest_path = self.config.cache_dir / "manifest.json"
        source = json.loads(manifest_path.read_text(encoding="utf-8"))
        source.update(
            {
                "requested_through": "2024-01-03",
                "completed_session_cutoff": "2024-01-03",
                "completed_through": "2024-01-02",
                "latest_common_session": "2024-01-02",
            }
        )
        manifest_path.write_text(json.dumps(source), encoding="utf-8")

        artifact = create_market_snapshot(self.config, self.root / "snapshot.zip")

        market = artifact.manifest["market"]
        self.assertEqual(market["requested_through"], "2024-01-03")
        self.assertEqual(market["completed_session_cutoff"], "2024-01-03")
        self.assertEqual(market["completed_through"], "2024-01-02")
        self.assertEqual(market["latest_common_session"], "2024-01-02")

    def test_snapshot_sanitizes_untrusted_manifest_text(self):
        sentinel = "credential-sentinel-must-not-upload"
        manifest_path = self.config.cache_dir / "manifest.json"
        source = json.loads(manifest_path.read_text(encoding="utf-8"))
        source["arbitrary_debug_field"] = sentinel
        first = next(iter(source["files"].values()))
        first["network_errors"] = [
            f"attempt 1/4: RemoteDisconnected: proxy password {sentinel}"
        ]
        first["fallback_reason"] = f"provider URL contained {sentinel}"
        manifest_path.write_text(json.dumps(source), encoding="utf-8")

        artifact = create_market_snapshot(self.config, self.root / "snapshot.zip")

        with zipfile.ZipFile(artifact.path) as archive:
            content = b"".join(archive.read(name) for name in archive.namelist())
            archived_source = json.loads(archive.read("data/cache/manifest.json"))
        self.assertNotIn(sentinel.encode("utf-8"), content)
        self.assertNotIn("arbitrary_debug_field", archived_source)
        archived_first = next(iter(archived_source["files"].values()))
        self.assertEqual(
            archived_first["network_errors"],
            ["provider_error: RemoteDisconnected"],
        )

    def test_snapshot_rejects_manifest_date_that_disagrees_with_csv(self):
        manifest_path = self.config.cache_dir / "manifest.json"
        source = json.loads(manifest_path.read_text(encoding="utf-8"))
        source["completed_through"] = "2099-12-31"
        source["latest_common_session"] = "2099-12-31"
        manifest_path.write_text(json.dumps(source), encoding="utf-8")

        with self.assertRaisesRegex(CloudIntegrityError, "cached CSV"):
            create_market_snapshot(self.config, self.root / "snapshot.zip")

    def test_fake_r2_upload_creates_latest_pointer_and_list_entry(self):
        artifact = create_market_snapshot(self.config, self.root / "snapshot.zip")
        fake = _FakeR2Client()
        store = _store(fake)

        pointer = store.upload_snapshot(artifact)
        snapshots = store.list_snapshots()

        self.assertEqual(pointer["snapshot_id"], artifact.snapshot_id)
        self.assertEqual(pointer["sha256"], artifact.sha256)
        self.assertEqual(
            [item["snapshot_id"] for item in snapshots], [artifact.snapshot_id]
        )
        self.assertEqual(snapshots[0]["size"], artifact.size)
        latest_key = f"{store.settings.namespace}/indexes/{DATASET}/latest.json"
        latest = json.loads(fake.objects[latest_key]["Body"])
        self.assertEqual(latest, pointer)

    def test_duplicate_upload_reuses_latest_snapshot_without_writing(self):
        artifact = create_market_snapshot(self.config, self.root / "snapshot.zip")
        fake = _FakeR2Client()
        store = _store(fake)

        first = store.upload_snapshot(artifact)
        writes_after_first = list(fake.put_keys)
        second = store.upload_snapshot(artifact)

        self.assertFalse(first["skipped_duplicate"])
        self.assertTrue(second["skipped_duplicate"])
        self.assertEqual(second["snapshot_id"], artifact.snapshot_id)
        self.assertEqual(fake.put_keys, writes_after_first)

    def test_duplicate_upload_recreates_a_missing_snapshot_object(self):
        artifact = create_market_snapshot(self.config, self.root / "snapshot.zip")
        fake = _FakeR2Client()
        store = _store(fake)
        first = store.upload_snapshot(artifact)
        del fake.objects[first["object_key"]]
        writes_before_retry = len(fake.put_keys)

        second = store.upload_snapshot(artifact)

        self.assertFalse(second["skipped_duplicate"])
        self.assertGreater(len(fake.put_keys), writes_before_retry)
        self.assertIn(second["object_key"], fake.objects)

    def test_snapshot_creation_enforces_total_uncompressed_limit(self):
        destination = self.root / "oversized-snapshot.zip"

        with (
            patch("ai_trade.cloud.MAX_ARCHIVE_BYTES", 512),
            self.assertRaisesRegex(CloudIntegrityError, "permitted size"),
        ):
            create_market_snapshot(self.config, destination)

        self.assertFalse(destination.exists())

    def test_download_rejects_sha256_tampering(self):
        artifact = create_market_snapshot(self.config, self.root / "snapshot.zip")
        fake = _FakeR2Client()
        store = _store(fake)
        pointer = store.upload_snapshot(artifact)
        remote = fake.objects[pointer["object_key"]]
        body = bytes(remote["Body"])
        remote["Body"] = bytes([body[0] ^ 1]) + body[1:]

        destination = self.root / "restored-tampered"
        with self.assertRaisesRegex(CloudIntegrityError, "SHA-256"):
            store.restore_snapshot(self.config, artifact.snapshot_id, destination)
        self.assertFalse(destination.exists())

    def test_restore_rejects_zip_path_traversal(self):
        artifact = create_market_snapshot(self.config, self.root / "snapshot.zip")
        fake = _FakeR2Client()
        store = _store(fake)
        pointer = store.upload_snapshot(artifact)
        malicious = io.BytesIO()
        with zipfile.ZipFile(malicious, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("snapshot-manifest.json", "{}")
            archive.writestr("../escaped.txt", "not allowed")
        fake.replace_object(pointer["object_key"], malicious.getvalue())

        destination = self.root / "restored-traversal"
        with self.assertRaisesRegex(CloudIntegrityError, "unsafe member"):
            store.restore_snapshot(self.config, artifact.snapshot_id, destination)
        self.assertFalse(destination.exists())
        self.assertFalse((self.root / "escaped.txt").exists())

    def test_restore_rejects_member_content_not_matching_manifest(self):
        artifact = create_market_snapshot(self.config, self.root / "snapshot.zip")
        fake = _FakeR2Client()
        store = _store(fake)
        pointer = store.upload_snapshot(artifact)
        original = bytes(fake.objects[pointer["object_key"]]["Body"])
        tampered = _replace_first_csv_without_updating_manifest(original)
        fake.replace_object(pointer["object_key"], tampered)

        destination = self.root / "restored-invalid-content"
        with self.assertRaisesRegex(CloudIntegrityError, "checksum mismatch"):
            store.restore_snapshot(self.config, artifact.snapshot_id, destination)
        self.assertFalse(destination.exists())

    def test_restore_rejects_snapshot_id_not_matching_request(self):
        artifact = create_market_snapshot(self.config, self.root / "snapshot.zip")
        fake = _FakeR2Client()
        store = _store(fake)
        pointer = store.upload_snapshot(artifact)
        original = bytes(fake.objects[pointer["object_key"]]["Body"])

        def replace_id(manifest: dict[str, object]) -> None:
            manifest["snapshot_id"] = "20000101T000000Z-000000000000"

        fake.replace_object(
            pointer["object_key"], _rewrite_snapshot_manifest(original, replace_id)
        )

        destination = self.root / "restored-wrong-id"
        with self.assertRaisesRegex(
            CloudIntegrityError, "ID does not match the request"
        ):
            store.restore_snapshot(self.config, artifact.snapshot_id, destination)
        self.assertFalse(destination.exists())

    def test_restore_preflights_declared_member_size_before_extraction(self):
        artifact = create_market_snapshot(self.config, self.root / "snapshot.zip")
        fake = _FakeR2Client()
        store = _store(fake)
        pointer = store.upload_snapshot(artifact)
        original = bytes(fake.objects[pointer["object_key"]]["Body"])

        def increase_size(manifest: dict[str, object]) -> None:
            files = manifest["files"]
            assert isinstance(files, list)
            files[0]["size"] += 1

        fake.replace_object(
            pointer["object_key"], _rewrite_snapshot_manifest(original, increase_size)
        )

        destination = self.root / "restored-wrong-size"
        with self.assertRaisesRegex(CloudIntegrityError, "declared size"):
            store.restore_snapshot(self.config, artifact.snapshot_id, destination)
        self.assertFalse(destination.exists())

    def test_restore_rejects_total_uncompressed_zip_size(self):
        artifact = create_market_snapshot(self.config, self.root / "snapshot.zip")
        fake = _FakeR2Client()
        store = _store(fake)
        pointer = store.upload_snapshot(artifact)
        oversized = io.BytesIO()
        with zipfile.ZipFile(oversized, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("snapshot-manifest.json", "{}")
            archive.writestr("padding.bin", b"A" * 4096)
        self.assertLess(len(oversized.getvalue()), 1024)
        fake.replace_object(pointer["object_key"], oversized.getvalue())

        destination = self.root / "restored-zip-bomb"
        with (
            patch("ai_trade.cloud.MAX_ARCHIVE_BYTES", 1024),
            self.assertRaisesRegex(CloudIntegrityError, "expands beyond"),
        ):
            store.restore_snapshot(self.config, artifact.snapshot_id, destination)
        self.assertFalse(destination.exists())

    def test_restore_rejects_embedded_source_manifest_mismatch(self):
        artifact = create_market_snapshot(self.config, self.root / "snapshot.zip")
        fake = _FakeR2Client()
        store = _store(fake)
        pointer = store.upload_snapshot(artifact)
        original = bytes(fake.objects[pointer["object_key"]]["Body"])
        symbol = self.config.instruments[0].symbol

        def change_source_rows(manifest: dict[str, object]) -> None:
            market = manifest["market"]
            assert isinstance(market, dict)
            market["source_manifest"]["files"][symbol]["rows"] = 2

        fake.replace_object(
            pointer["object_key"],
            _rewrite_snapshot_manifest(original, change_source_rows),
        )

        destination = self.root / "restored-source-mismatch"
        with self.assertRaisesRegex(CloudIntegrityError, "source manifest"):
            store.restore_snapshot(self.config, artifact.snapshot_id, destination)
        self.assertFalse(destination.exists())

    def test_restore_writes_new_staging_directory_without_changing_active_cache(self):
        artifact = create_market_snapshot(self.config, self.root / "snapshot.zip")
        fake = _FakeR2Client()
        store = _store(fake)
        store.upload_snapshot(artifact)
        symbol = self.config.instruments[0].symbol
        active_path = self.config.cache_dir / f"{symbol}.csv"
        archived_bytes = active_path.read_bytes()
        active_path.write_bytes(archived_bytes + b"\n")
        changed_active_bytes = active_path.read_bytes()

        destination = self.root / "cloud-restore" / artifact.snapshot_id
        restored = store.restore_snapshot(
            self.config, artifact.snapshot_id, destination
        )

        self.assertEqual(restored, destination.resolve())
        self.assertEqual(active_path.read_bytes(), changed_active_bytes)
        self.assertEqual(
            (restored / "data/cache" / f"{symbol}.csv").read_bytes(),
            archived_bytes,
        )
        self.assertEqual(
            set(path.name for path in (restored / "data/cache").iterdir()),
            {
                "manifest.json",
                *(f"{item.symbol}.csv" for item in self.config.instruments),
            },
        )
        self.assertFalse((restored / "state").exists())
        self.assertFalse((restored / "reports").exists())
        self.assertFalse((restored / "logs").exists())

    def test_restore_download_uses_random_temporary_directory(self):
        artifact = create_market_snapshot(self.config, self.root / "snapshot.zip")
        fake = _FakeR2Client()
        store = _store(fake)
        store.upload_snapshot(artifact)
        destination = self.root / "cloud-restore" / artifact.snapshot_id
        download_paths: list[Path] = []
        original_download = store._download_verified

        def capture_download(key: str, path: Path) -> None:
            download_paths.append(path)
            original_download(key, path)

        with patch.object(store, "_download_verified", side_effect=capture_download):
            store.restore_snapshot(self.config, artifact.snapshot_id, destination)

        self.assertEqual(len(download_paths), 1)
        self.assertFalse(download_paths[0].is_relative_to(destination))
        self.assertFalse(download_paths[0].exists())


def _write_market_cache(config: AppConfig) -> None:
    config.cache_dir.mkdir(parents=True, exist_ok=True)
    files: dict[str, dict[str, object]] = {}
    for index, instrument in enumerate(config.instruments):
        path = config.cache_dir / f"{instrument.symbol}.csv"
        price = 10 + index
        path.write_text(
            "date,open,close,high,low,volume,amount\n"
            f"2024-01-02,{price},{price + 0.1},{price + 0.2},{price - 0.2},100,1000\n",
            encoding="utf-8",
        )
        files[instrument.symbol] = {
            "rows": 1,
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "source": "test-fixture",
            "latest_session": "2024-01-02",
        }
    (config.cache_dir / "manifest.json").write_text(
        json.dumps(
            {
                "provider": config.raw["data"]["provider"],
                "adjustment": config.raw["data"].get("adjustment", "none"),
                "downloaded_at": "2024-01-02T16:00:00+08:00",
                "latest_common_session": "2024-01-02",
                "files": files,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _configured_environment() -> dict[str, str]:
    return {
        "AI_TRADE_CLOUD_ENABLED": "true",
        "AI_TRADE_CLOUD_PREFIX": "ai-trade",
        "AI_TRADE_CLOUD_INSTALLATION_ID": "a" * 32,
        "AI_TRADE_R2_ENDPOINT": (
            "https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com"
        ),
        "AI_TRADE_R2_REGION": "auto",
        "AI_TRADE_R2_BUCKET": "test-bucket",
        "AI_TRADE_R2_ACCESS_KEY_ID": "fake-access-key",
        "AI_TRADE_R2_SECRET_ACCESS_KEY": "fake-secret-key",
    }


def _isolated_cloud_environment(values: dict[str, str]):
    environment = {
        key: value for key, value in os.environ.items() if key not in _CLOUD_ENVIRONMENT
    }
    environment.update(values)
    return patch.dict(os.environ, environment, clear=True)


def _store(client: "_FakeR2Client") -> R2ObjectStore:
    with _isolated_cloud_environment(_configured_environment()):
        return R2ObjectStore(load_cloud_settings(), client=client)


def _replace_first_csv_without_updating_manifest(original: bytes) -> bytes:
    source = io.BytesIO(original)
    output = io.BytesIO()
    changed = False
    with (
        zipfile.ZipFile(source) as existing,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as rewritten,
    ):
        for name in existing.namelist():
            content = existing.read(name)
            if name.endswith(".csv") and not changed:
                content = bytes([content[0] ^ 1]) + content[1:]
                changed = True
            rewritten.writestr(name, content)
    if not changed:
        raise AssertionError("Fixture archive did not contain a CSV file")
    return output.getvalue()


def _rewrite_snapshot_manifest(original: bytes, mutate) -> bytes:
    source = io.BytesIO(original)
    output = io.BytesIO()
    with (
        zipfile.ZipFile(source) as existing,
        zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as rewritten,
    ):
        for name in existing.namelist():
            content = existing.read(name)
            if name == "snapshot-manifest.json":
                manifest = json.loads(content)
                mutate(manifest)
                content = json.dumps(manifest, sort_keys=True).encode("utf-8")
            rewritten.writestr(name, content)
    return output.getvalue()


class _FakeR2Client:
    def __init__(self) -> None:
        self.objects: dict[str, dict[str, object]] = {}
        self.put_keys: list[str] = []
        self.modified = datetime(2024, 1, 2, 8, 0, tzinfo=timezone.utc)

    def put_object(self, **request):
        body = request["Body"]
        content = body.read() if hasattr(body, "read") else bytes(body)
        self.put_keys.append(request["Key"])
        self.objects[request["Key"]] = {
            "Body": content,
            "Metadata": dict(request.get("Metadata", {})),
            "ContentType": request.get("ContentType"),
            "LastModified": self.modified,
        }
        return {}

    def head_object(self, **request):
        value = self.objects[request["Key"]]
        return {
            "ContentLength": len(value["Body"]),
            "Metadata": dict(value["Metadata"]),
        }

    def get_object(self, **request):
        return {"Body": io.BytesIO(self.objects[request["Key"]]["Body"])}

    def list_objects_v2(self, **request):
        prefix = request.get("Prefix", "")
        contents = [
            {
                "Key": key,
                "Size": len(value["Body"]),
                "LastModified": value["LastModified"],
            }
            for key, value in sorted(self.objects.items())
            if key.startswith(prefix)
        ]
        return {"Contents": contents, "IsTruncated": False}

    def replace_object(self, key: str, body: bytes) -> None:
        value = self.objects[key]
        value["Body"] = body
        value["Metadata"] = {
            **value["Metadata"],
            "sha256": hashlib.sha256(body).hexdigest(),
        }


if __name__ == "__main__":
    unittest.main()
