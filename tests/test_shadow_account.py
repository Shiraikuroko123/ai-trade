from __future__ import annotations

import csv
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from ai_trade.broker.shadow import (
    CANONICAL_COLUMNS,
    ShadowAccountConflictError,
    canonical_template,
    import_shadow_csv,
    shadow_account_status,
)
from ai_trade.config import _validate_shadow_account


def _csv_content(*rows: tuple[object, ...]) -> bytes:
    lines = [",".join(CANONICAL_COLUMNS)]
    lines.extend(",".join(str(value) for value in row) for row in rows)
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


FILL_ONE = (
    "fill-1",
    "order-1",
    "510300",
    "BUY",
    100,
    "10.0100",
    "5.00",
    "0",
    "2026-07-15T09:31:00+08:00",
)
FILL_TWO = (
    "fill-2",
    "order-2",
    "510500",
    "SELL",
    200,
    "5.0000",
    "5",
    "1",
    "2026-07-16T10:02:03+08:00",
)


class ShadowAccountTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.fills_path = self.root / "state" / "shadow_fills.csv"
        self.imports_path = self.root / "state" / "shadow_imports.csv"
        self.imported_at = datetime(2026, 7, 17, 8, 0, tzinfo=timezone.utc)

    def tearDown(self):
        self.temporary.cleanup()

    def _import(self, content: bytes, **overrides):
        values = {
            "owner_id": "owner-a",
            "source_label": "qmt-export",
            "account_alias": "模拟账户 A",
            "content": content,
            "imported_at": self.imported_at,
        }
        values.update(overrides)
        return import_shadow_csv(
            self.fills_path,
            self.imports_path,
            **values,
        )

    def test_import_normalizes_fills_and_exact_file_is_idempotent(self):
        content = _csv_content(FILL_ONE, FILL_TWO)

        first = self._import(content)
        second = self._import(content)
        status = shadow_account_status(
            self.fills_path,
            self.imports_path,
            owner_id="owner-a",
        )

        self.assertEqual(first["accepted_count"], 2)
        self.assertEqual(first["duplicate_count"], 0)
        self.assertFalse(first["already_imported"])
        self.assertTrue(second["already_imported"])
        self.assertEqual(status["fill_count"], 2)
        self.assertEqual(status["import_count"], 1)
        self.assertEqual(status["recent_fills"][0]["price"], 5.0)
        self.assertFalse(status["source_files_retained"])
        self.assertEqual(set(self.root.rglob("*")), {self.root / "state", self.fills_path, self.imports_path})
        self.assertEqual(canonical_template(), ",".join(CANONICAL_COLUMNS) + "\r\n")

    def test_overlapping_export_skips_prior_fill_and_appends_only_new_fill(self):
        self._import(_csv_content(FILL_ONE))
        overlapping = self._import(
            _csv_content(FILL_ONE, FILL_TWO),
            imported_at=datetime(2026, 7, 17, 9, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(overlapping["accepted_count"], 1)
        self.assertEqual(overlapping["duplicate_count"], 1)
        status = shadow_account_status(
            self.fills_path,
            self.imports_path,
            owner_id="owner-a",
        )
        self.assertEqual(status["fill_count"], 2)
        self.assertEqual(status["import_count"], 2)
        self.assertEqual(status["integrity_errors"], [])

    def test_conflicting_fill_id_rejects_entire_batch_without_partial_append(self):
        self._import(_csv_content(FILL_ONE))
        before_fills = self.fills_path.read_bytes()
        before_imports = self.imports_path.read_bytes()
        conflicting = list(FILL_ONE)
        conflicting[5] = "10.50"

        with self.assertRaises(ShadowAccountConflictError):
            self._import(_csv_content(FILL_TWO, tuple(conflicting)))

        self.assertEqual(self.fills_path.read_bytes(), before_fills)
        self.assertEqual(self.imports_path.read_bytes(), before_imports)

    def test_exact_retry_fails_if_a_linked_fill_was_deleted(self):
        content = _csv_content(FILL_ONE, FILL_TWO)
        self._import(content)
        with self.fills_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        with self.fills_path.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(rows[:-1])

        with self.assertRaisesRegex(RuntimeError, "accepted_count"):
            self._import(content)

    def test_strict_schema_and_values_fail_before_creating_ledgers(self):
        invalid_cases = (
            b"symbol,side\r\n510300,BUY\r\n",
            _csv_content((*FILL_ONE[:3], "buy", *FILL_ONE[4:])),
            _csv_content((*FILL_ONE[:4], "100.5", *FILL_ONE[5:])),
            _csv_content((*FILL_ONE[:8], "2026-07-15T09:31:00")),
            _csv_content(FILL_ONE, FILL_ONE),
            _csv_content((*FILL_ONE[:8], "2030-01-01T09:31:00+08:00")),
        )
        for content in invalid_cases:
            with self.subTest(content=content), self.assertRaises(ValueError):
                self._import(content)
            self.assertFalse(self.fills_path.exists())
            self.assertFalse(self.imports_path.exists())
        with self.assertRaises(ValueError):
            self._import(_csv_content(FILL_ONE), account_alias="=unsafe")

    def test_shadow_configuration_bounds_and_separates_ledgers(self):
        _validate_shadow_account({})
        _validate_shadow_account({"max_import_bytes": 1024})
        for value in (True, 1023, 5_000_001):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _validate_shadow_account({"max_import_bytes": value})
        with self.assertRaisesRegex(ValueError, "different"):
            _validate_shadow_account(
                {"fills_file": "state/same.csv", "imports_file": "state/same.csv"}
            )

    def test_review_compares_behavior_price_and_trade_allocation(self):
        self._import(_csv_content(FILL_ONE, FILL_TWO))
        expected = [
            {
                "date": "2026-07-15",
                "symbol": "510300",
                "side": "BUY",
                "quantity": "100",
                "price": "10.0000",
            },
            {
                "date": "2026-07-16",
                "symbol": "510500",
                "side": "SELL",
                "quantity": "200",
                "price": "5.0100",
            },
        ]

        status = shadow_account_status(
            self.fills_path,
            self.imports_path,
            owner_id="owner-a",
            expected_trades=expected,
        )

        review = status["review"]
        self.assertEqual(review["verdict"], "CONSISTENT_WITH_MODEL")
        self.assertEqual(review["matched_groups"], 2)
        self.assertEqual(review["unexpected_groups"], 0)
        self.assertEqual(review["missed_groups"], 0)
        self.assertAlmostEqual(review["match_rate"], 1.0)
        self.assertAlmostEqual(review["weighted_adverse_price_bps"], 14.977551, places=5)
        self.assertLess(review["trade_allocation_deviation"], 0.01)
        self.assertFalse(status["qualifying_evidence"])
        self.assertFalse(status["execution_enabled"])

    def test_review_flags_behavior_and_owner_scope_and_detects_tampering(self):
        sell_instead = (
            "fill-wrong-side",
            "order-wrong-side",
            "510300",
            "SELL",
            50,
            "9.90",
            "5",
            "1",
            "2026-07-15T09:32:00+08:00",
        )
        self._import(_csv_content(sell_instead))
        self._import(
            _csv_content(FILL_TWO),
            owner_id="owner-b",
            account_alias="模拟账户 B",
        )
        expected = [
            {
                "date": "2026-07-15",
                "symbol": "510300",
                "side": "BUY",
                "quantity": "100",
                "price": "10",
            }
        ]

        owner_a = shadow_account_status(
            self.fills_path,
            self.imports_path,
            owner_id="owner-a",
            expected_trades=expected,
        )
        owner_b = shadow_account_status(
            self.fills_path,
            self.imports_path,
            owner_id="owner-b",
            expected_trades=expected,
        )
        self.assertEqual(owner_a["fill_count"], 1)
        self.assertEqual(owner_b["fill_count"], 1)
        self.assertEqual(owner_a["review"]["verdict"], "INSUFFICIENT_DATA")
        self.assertEqual(owner_a["review"]["direction_mismatch_groups"], 1)

        with self.fills_path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.reader(handle))
        rows[1][rows[0].index("quantity")] = "999"
        with self.fills_path.open("w", encoding="utf-8", newline="") as handle:
            csv.writer(handle).writerows(rows)
        tampered = shadow_account_status(
            self.fills_path,
            self.imports_path,
            owner_id="owner-a",
        )
        self.assertEqual(tampered["status"], "INTEGRITY_ERROR")
        self.assertTrue(tampered["integrity_errors"])


if __name__ == "__main__":
    unittest.main()
