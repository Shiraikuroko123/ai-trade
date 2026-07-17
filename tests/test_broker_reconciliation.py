from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

from ai_trade.broker.reconciliation import (
    RECONCILIATION_FIELDS,
    ReconciliationIssue,
    append_reconciliation,
    audit_reconciliations,
)


START = date(2026, 7, 1)


def _arguments(**overrides):
    values = {
        "on_date": START,
        "adapter": "sandbox-adapter",
        "account_id": "sandbox-account",
        "config_fingerprint": "active-config",
        "expected_cash": 1000.0,
        "broker_cash": 1000.0,
        "issues": [],
    }
    values.update(overrides)
    return values


def _read_rows(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_rows(path: Path, rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=RECONCILIATION_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


class BrokerReconciliationTests(unittest.TestCase):
    def test_v2_content_fingerprint_detects_cash_tampering(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reconciliation.csv"
            reconciliation_id = append_reconciliation(path, **_arguments())
            self.assertTrue(reconciliation_id.startswith("v2_"))
            rows = _read_rows(path)
            rows[0]["broker_cash"] = "900.000000"
            _write_rows(path, rows)

            audit = audit_reconciliations(
                path,
                "sandbox-adapter",
                "sandbox-account",
                1,
                "active-config",
            )

            self.assertFalse(audit["eligible"])
            self.assertEqual(audit["clean_sessions"], 0)
            self.assertTrue(
                any("content validation" in error for error in audit["errors"])
            )

    def test_legacy_identity_id_is_readable_but_cannot_qualify(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reconciliation.csv"
            raw_id = "|".join(
                [
                    "sandbox-adapter",
                    "sandbox-account",
                    START.isoformat(),
                    "active-config",
                ]
            )
            legacy_id = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:24]
            row = {
                "reconciliation_id": legacy_id,
                "date": START.isoformat(),
                "adapter": "sandbox-adapter",
                "account_id": "sandbox-account",
                "config_fingerprint": "active-config",
                "expected_cash": "1000.000000",
                "broker_cash": "1000.000000",
                "issue_count": "0",
                "issues": "[]",
            }
            _write_rows(path, [row])

            retry_id = append_reconciliation(path, **_arguments())
            audit = audit_reconciliations(
                path,
                "sandbox-adapter",
                "sandbox-account",
                1,
                "active-config",
            )

            self.assertEqual(retry_id, legacy_id)
            self.assertEqual(_read_rows(path), [row])
            self.assertFalse(audit["eligible"])
            self.assertEqual(audit["clean_sessions"], 0)
            self.assertEqual(audit["ignored_legacy_sessions"], 1)

    def test_v2_logical_session_conflict_is_durable_and_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reconciliation.csv"
            append_reconciliation(path, **_arguments())
            conflict = _arguments(
                broker_cash=900.0,
                issues=[ReconciliationIssue("cash", "CNY", 1000.0, 900.0)],
            )

            with self.assertRaisesRegex(RuntimeError, "Conflicting reconciliation"):
                append_reconciliation(path, **conflict)

            rows = _read_rows(path)
            self.assertEqual(len(rows), 2)
            self.assertNotEqual(
                rows[0]["reconciliation_id"], rows[1]["reconciliation_id"]
            )
            self.assertTrue(
                all(row["reconciliation_id"].startswith("v2_") for row in rows)
            )
            audit = audit_reconciliations(
                path,
                "sandbox-adapter",
                "sandbox-account",
                1,
                "active-config",
            )
            self.assertFalse(audit["eligible"])
            self.assertTrue(
                any("conflicting reconciliation session" in error for error in audit["errors"])
            )

    def test_parallel_exact_appends_are_serialized_and_idempotent(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reconciliation.csv"

            with ThreadPoolExecutor(max_workers=8) as executor:
                ids = list(
                    executor.map(
                        lambda _: append_reconciliation(path, **_arguments()),
                        range(24),
                    )
                )

            self.assertEqual(len(set(ids)), 1)
            self.assertEqual(len(_read_rows(path)), 1)
            self.assertTrue(
                audit_reconciliations(
                    path,
                    "sandbox-adapter",
                    "sandbox-account",
                    1,
                    "active-config",
                )["eligible"]
            )

    def test_replace_failure_preserves_previous_complete_ledger(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reconciliation.csv"
            append_reconciliation(path, **_arguments())
            before = path.read_bytes()

            with patch(
                "ai_trade.broker.ledger.os.replace",
                side_effect=OSError("injected reconciliation replace failure"),
            ):
                with self.assertRaisesRegex(OSError, "injected reconciliation"):
                    append_reconciliation(
                        path,
                        **_arguments(on_date=START + timedelta(days=1)),
                    )

            self.assertEqual(path.read_bytes(), before)
            self.assertEqual(
                list(path.parent.glob(".reconciliation.csv.*.tmp")), []
            )
            self.assertTrue(
                audit_reconciliations(
                    path,
                    "sandbox-adapter",
                    "sandbox-account",
                    1,
                    "active-config",
                )["eligible"]
            )

            append_reconciliation(
                path,
                **_arguments(on_date=START + timedelta(days=1)),
            )
            self.assertTrue(
                audit_reconciliations(
                    path,
                    "sandbox-adapter",
                    "sandbox-account",
                    2,
                    "active-config",
                )["eligible"]
            )

    def test_issue_order_is_canonical_for_exact_retries(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reconciliation.csv"
            cash = ReconciliationIssue("cash", "CNY", 1000.0, 900.0)
            position = ReconciliationIssue("position", "510300", 100.0, 0.0)

            first = append_reconciliation(
                path,
                **_arguments(broker_cash=900.0, issues=[position, cash]),
            )
            second = append_reconciliation(
                path,
                **_arguments(broker_cash=900.0, issues=[cash, position]),
            )

            self.assertEqual(first, second)
            self.assertEqual(len(_read_rows(path)), 1)

    def test_invalid_issue_schema_in_another_account_invalidates_the_ledger(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "reconciliation.csv"
            append_reconciliation(path, **_arguments())
            append_reconciliation(
                path,
                **_arguments(
                    account_id="other-account",
                    on_date=START + timedelta(days=1),
                ),
            )
            rows = _read_rows(path)
            rows[1]["issue_count"] = "1"
            rows[1]["issues"] = json.dumps(
                [
                    {
                        "kind": "cash",
                        "key": "CNY",
                        "expected": 1000.0,
                        "actual": 900.0,
                        "unexpected": True,
                    }
                ]
            )
            _write_rows(path, rows)

            audit = audit_reconciliations(
                path,
                "sandbox-adapter",
                "sandbox-account",
                1,
                "active-config",
            )

            self.assertFalse(audit["eligible"])
            self.assertEqual(audit["clean_sessions"], 0)
            self.assertTrue(audit["errors"])


if __name__ == "__main__":
    unittest.main()
