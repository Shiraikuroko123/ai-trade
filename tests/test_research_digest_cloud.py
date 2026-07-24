import io
import json
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
import tempfile
import unittest
import zipfile

from ai_trade.cloud import CloudIntegrityError, CloudSettings, R2ObjectStore
from ai_trade.research_digest import ResearchDigestStore
from ai_trade.research_digest_cloud import (
    _dataset_fingerprint,
    _extract_verified,
    create_research_digest_snapshot,
    list_research_digest_snapshots,
    restore_research_digest_snapshot,
    upload_research_digest_snapshot,
)


ACCOUNT = "paper-account-20260724"
CONFIG = "a" * 64
DAY = date(2026, 7, 24)


class ResearchDigestCloudTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.store = ResearchDigestStore(self.root / "d")
        self.store.append(
            "alice",
            ACCOUNT,
            kind="daily",
            period_start=DAY,
            payload={
                "as_of_date": DAY.isoformat(),
                "status": "current",
                "equity": 100000.0,
                "daily_return": 0.01,
                "note": "Closing evidence",
                "source": {"evidence_fingerprint": "c" * 64},
            },
            source={
                "fingerprint": "c" * 64,
                "evidence_fingerprints": ["b" * 64],
                "calendar_fingerprint": "d" * 64,
                "config_fingerprint": CONFIG,
                "account_fingerprint": self.store.account_id(ACCOUNT),
            },
            config_fingerprint=CONFIG,
        )
        self.client = _FakeR2Client()
        settings = CloudSettings(
            enabled=True,
            endpoint="https://example.r2.cloudflarestorage.com",
            region="auto",
            bucket="example-bucket",
            access_key_id="access",
            secret_access_key="secret",
            prefix="ai-trade",
            installation_id="1" * 32,
        )
        self.r2 = R2ObjectStore(settings, client=self.client)

    def tearDown(self):
        self.temporary.cleanup()

    def test_snapshot_contains_only_verified_hashed_digest_paths(self):
        artifact = create_research_digest_snapshot(
            self.store,
            "alice",
            ACCOUNT,
            self.root / "snapshot.zip",
        )

        with zipfile.ZipFile(artifact.path) as archive:
            names = archive.namelist()
            manifest = json.loads(archive.read("research-digest-manifest.json"))
        self.assertEqual(manifest["dataset"], "research-digests")
        self.assertEqual(manifest["dataset_sha256"], artifact.dataset_sha256)
        self.assertTrue(any(name.endswith("revision_00000001.json") for name in names))
        self.assertNotIn("alice", json.dumps(manifest))
        self.assertNotIn(ACCOUNT, json.dumps(manifest))
        self.assertFalse(manifest["authority"]["active_state_included"])

    def test_upload_is_idempotent_and_restore_stays_in_new_staging_directory(self):
        artifact = create_research_digest_snapshot(
            self.store,
            "alice",
            ACCOUNT,
            self.root / "snapshot.zip",
        )
        first = upload_research_digest_snapshot(self.r2, artifact)
        second = upload_research_digest_snapshot(self.r2, artifact)
        snapshots = list_research_digest_snapshots(self.r2)
        destination = self.root / "restored"
        restored = restore_research_digest_snapshot(
            self.r2, artifact.snapshot_id, destination
        )

        self.assertFalse(first["skipped_duplicate"])
        self.assertTrue(second["skipped_duplicate"])
        self.assertEqual([item["snapshot_id"] for item in snapshots], [artifact.snapshot_id])
        self.assertEqual(restored, destination.resolve())
        self.assertTrue((restored / "research-digest-manifest.json").is_file())
        self.assertTrue(any(restored.rglob("revision_00000001.json")))
        self.assertTrue((self.root / "d").is_dir())

    def test_restore_rejects_remote_content_tampering(self):
        artifact = create_research_digest_snapshot(
            self.store,
            "alice",
            ACCOUNT,
            self.root / "snapshot.zip",
        )
        result = upload_research_digest_snapshot(self.r2, artifact)
        key = result["object_key"]
        original = self.client.objects[key]["Body"]
        tampered_bytes = bytearray(original)
        tampered_bytes[len(tampered_bytes) // 2] ^= 0x01
        tampered = bytes(tampered_bytes)
        self.client.objects[key]["Body"] = tampered
        self.client.objects[key]["Metadata"]["sha256"] = sha256(tampered).hexdigest()

        with self.assertRaises(CloudIntegrityError):
            restore_research_digest_snapshot(
                self.r2, artifact.snapshot_id, self.root / "tampered"
            )
        self.assertFalse((self.root / "tampered").exists())

    def test_restore_rejects_self_consistent_archive_with_invalid_digest_record(self):
        artifact = create_research_digest_snapshot(
            self.store,
            "alice",
            ACCOUNT,
            self.root / "snapshot.zip",
        )
        with zipfile.ZipFile(artifact.path) as source:
            manifest = json.loads(source.read("research-digest-manifest.json"))
            revision_name = next(iter(manifest["files"]))
            record = json.loads(source.read(revision_name))
        record["actor"] = "tampered-actor"
        revision_body = json.dumps(
            record, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        manifest["files"][revision_name] = {
            "sha256": sha256(revision_body).hexdigest(),
            "size": len(revision_body),
        }
        manifest["dataset_sha256"] = _dataset_fingerprint(manifest["files"])
        snapshot_id = "20260724T080000Z-" + manifest["dataset_sha256"][:12]
        manifest["snapshot_id"] = snapshot_id
        malicious = self.root / "self-consistent.zip"
        with zipfile.ZipFile(malicious, "x", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(
                "research-digest-manifest.json",
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
            )
            archive.writestr(revision_name, revision_body)

        with self.assertRaises(CloudIntegrityError):
            _extract_verified(
                malicious,
                self.root / "invalid-record",
                expected_snapshot_id=snapshot_id,
            )


class _FakeR2Client:
    def __init__(self):
        self.objects = {}
        self.modified = datetime(2026, 7, 24, tzinfo=timezone.utc)

    def put_object(self, **request):
        body = request["Body"]
        content = body.read() if hasattr(body, "read") else bytes(body)
        self.objects[request["Key"]] = {
            "Body": content,
            "Metadata": dict(request.get("Metadata", {})),
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
        return {
            "Contents": [
                {
                    "Key": key,
                    "Size": len(value["Body"]),
                    "LastModified": value["LastModified"],
                }
                for key, value in sorted(self.objects.items())
                if key.startswith(prefix)
            ],
            "IsTruncated": False,
        }


if __name__ == "__main__":
    unittest.main()
