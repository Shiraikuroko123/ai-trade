from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest

from ai_trade.data.evidence_io import atomic_create_json, evidence_store_lock


class EvidenceIoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "evidence"

    def tearDown(self):
        self.temporary.cleanup()

    def test_atomic_create_never_replaces_an_existing_revision(self):
        path = self.root / "2026-07-20" / "revision_00000001.json"
        with evidence_store_lock(self.root, "Test evidence"):
            atomic_create_json(
                self.root,
                path,
                {"revision": 1, "value": "first"},
                label="test evidence",
                maximum_bytes=1024,
            )
            with self.assertRaises(FileExistsError):
                atomic_create_json(
                    self.root,
                    path,
                    {"revision": 1, "value": "replacement"},
                    label="test evidence",
                    maximum_bytes=1024,
                )

        self.assertEqual(
            json.loads(path.read_text(encoding="utf-8")),
            {"revision": 1, "value": "first"},
        )

    def test_store_lock_serializes_revision_number_allocation(self):
        def publish(value: int) -> int:
            with evidence_store_lock(self.root, "Test evidence"):
                directory = self.root / "2026-07-20"
                existing = list(directory.glob("revision_*.json")) if directory.exists() else []
                revision = len(existing) + 1
                atomic_create_json(
                    self.root,
                    directory / f"revision_{revision:08d}.json",
                    {"revision": revision, "value": value},
                    label="test evidence",
                    maximum_bytes=1024,
                )
                return revision

        with ThreadPoolExecutor(max_workers=8) as executor:
            revisions = list(executor.map(publish, range(8)))

        self.assertEqual(sorted(revisions), list(range(1, 9)))
        paths = sorted((self.root / "2026-07-20").glob("revision_*.json"))
        self.assertEqual(len(paths), 8)
        self.assertEqual(
            [json.loads(path.read_text(encoding="utf-8"))["revision"] for path in paths],
            list(range(1, 9)),
        )


if __name__ == "__main__":
    unittest.main()
