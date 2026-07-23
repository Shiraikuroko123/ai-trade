from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ai_trade.data.disclosures import (
    DisclosureProviderError,
    DisclosureQuery,
    DisclosureStore,
    _parse_cninfo,
    refresh_disclosures,
)
from ai_trade.models import Instrument


class _Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _maximum):
        if isinstance(self.value, bytes):
            return self.value
        return json.dumps(self.value).encode("utf-8")


def _config(root: Path):
    return SimpleNamespace(
        raw={
            "data": {
                "market_close_time": "15:30",
                "timeout_seconds": 2,
                "max_attempts": 1,
                "proxy_mode": "direct",
            }
        },
        instruments=[
            Instrument(
                "600000", "Test Bank", "SH", "equity", instrument_type="STOCK"
            ),
            Instrument("159915", "Growth ETF", "SZ", "equity", instrument_type="ETF"),
            Instrument("510300", "SH ETF", "SH", "equity", instrument_type="ETF"),
        ],
        disclosures_dir=root / "disclosures",
    )


def _sse_payload():
    return {
        "pageHelp": {
            "total": 1,
            "data": [
                {
                    "SECURITY_CODE": "600000",
                    "SECURITY_NAME": "Test Bank",
                    "ADDDATE": "2026-07-17 18:30:00",
                    "SSEDATE": "2026-07-18",
                    "TITLE": "Test Bank 解除限售股份上市流通公告",
                    "BULLETIN_TYPE": "Other",
                    "URL": "/disclosure/listedinfo/announcement/test.pdf",
                }
            ],
        }
    }


def _master_payload():
    return {
        "stockList": [
            {
                "code": "159915",
                "orgId": "jjjl0000041",
                "category": "ETF",
            }
        ]
    }


def _cninfo_payload():
    return {
        "totalAnnouncement": 1,
        "totalRecordNum": 1,
        "announcements": [
            {
                "secCode": "159915",
                "secName": "Growth ETF",
                "announcementId": "1225406051",
                "announcementTitle": "ETF official notice",
                "announcementTime": 1783008000000,
                "adjunctUrl": "finalpage/2026-07-03/1225406051.PDF",
                "adjunctType": "PDF",
                "announcementTypeName": "Periodic report",
            }
        ],
    }


class DisclosureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = _config(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def _open_request(self, request, _timeout, _proxy):
        if "queryCompanyBulletin" in request.full_url:
            return _Response(_sse_payload())
        if request.full_url.endswith("fund_stock.json"):
            return _Response(_master_payload())
        if "hisAnnouncement/query" in request.full_url:
            self.assertIn(b"159915%2Cjjjl0000041", request.data)
            return _Response(_cninfo_payload())
        if request.full_url.startswith(
            ("https://static.sse.com.cn/", "https://static.cninfo.com.cn/")
        ):
            return _Response(b"%PDF-1.7\nvalidated test document\n%%EOF")
        raise AssertionError(f"unexpected request: {request.full_url}")

    def test_refresh_separates_official_sources_and_reports_coverage_gaps(self):
        with patch(
            "ai_trade.data.disclosures._open_request",
            side_effect=self._open_request,
        ):
            result = refresh_disclosures(
                self.config,
                as_of=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
            )
        self.assertTrue(result["available"])
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["summary"]["record_count"], 2)
        self.assertEqual(
            {item["source_provider"] for item in result["records"]},
            {"sse", "cninfo"},
        )
        gap = next(item for item in result["coverage"] if item["symbol"] == "510300")
        self.assertEqual(gap["status"], "unavailable")
        self.assertEqual(gap["reason"], "official_market_coverage_unavailable")
        self.assertFalse(result["source"]["document_archived"])
        self.assertEqual(result["source"]["document_hashing"], "complete")
        self.assertEqual(result["summary"]["document_hash"]["hashed"], 2)
        event = next(item for item in result["records"] if item["symbol"] == "600000")
        self.assertIn("lockup_expiration", event["event_types"])
        self.assertEqual(event["document_body"]["status"], "hashed")
        self.assertEqual(len(event["document_body"]["sha256"]), 64)
        self.assertFalse(result["authority"]["execution_authorized"])

        visible = DisclosureStore(self.config).list(
            DisclosureQuery(provider="cninfo", symbol="159915")
        )
        self.assertEqual(len(visible["records"]), 1)
        self.assertEqual(visible["records"][0]["title"], "ETF official notice")

    def test_identical_official_evidence_is_reused(self):
        with patch(
            "ai_trade.data.disclosures._open_request",
            side_effect=self._open_request,
        ):
            first = refresh_disclosures(
                self.config,
                symbols=["600000"],
                as_of=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
            )
        with patch(
            "ai_trade.data.disclosures._open_request",
            side_effect=self._open_request,
        ):
            second = refresh_disclosures(
                self.config,
                symbols=["600000"],
                as_of=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(first["revision"], 1)
        self.assertTrue(second["reused"])

    def test_local_unavailable_read_never_contacts_network(self):
        result = DisclosureStore(self.config).list(
            DisclosureQuery(trade_date=date(2026, 7, 17))
        )
        self.assertFalse(result["available"])
        self.assertEqual(
            result["errors"][0]["code"], "official_disclosures_not_refreshed"
        )

    def test_provider_rows_outside_the_requested_window_are_rejected(self):
        payload = _cninfo_payload()
        payload["announcements"][0]["announcementTime"] = 4_102_444_800_000
        instrument = next(
            item for item in self.config.instruments if item.symbol == "159915"
        )
        with self.assertRaisesRegex(DisclosureProviderError, "requested window"):
            _parse_cninfo(
                payload,
                instrument,
                start=date(2026, 6, 20),
                cutoff=date(2026, 7, 20),
            )


if __name__ == "__main__":
    unittest.main()
