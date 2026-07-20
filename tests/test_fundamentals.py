from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ai_trade.data.fundamentals import (
    FundamentalQuery,
    FundamentalStore,
    refresh_fundamentals,
)
from ai_trade.models import Instrument


class _Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, maximum):
        return json.dumps(self.value).encode("utf-8")[:maximum]


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
            Instrument("510300", "ETF", "SH", "equity", instrument_type="ETF"),
        ],
        fundamentals_dir=root / "fundamentals",
    )


def _payload():
    return {
        "success": True,
        "code": 0,
        "message": "ok",
        "result": {
            "count": 4,
            "data": [
                _row("2026-03-31", "2026-04-25", eps=2.2),
                _row("2025-12-31", "2026-04-17", eps=6.5),
                _row("2026-06-30", "2026-08-30", eps=4.0),
                _row("2026-12-31", "2026-05-01", eps=9.0),
            ],
        },
    }


def _row(report_date: str, notice_date: str, *, eps: float):
    return {
        "SECURITY_CODE": "600000",
        "SECURITY_NAME_ABBR": "Test Bank",
        "REPORTDATE": report_date + " 00:00:00",
        "NOTICE_DATE": notice_date + " 00:00:00",
        "UPDATE_DATE": notice_date + " 00:00:00",
        "DATATYPE": "report",
        "BASIC_EPS": eps,
        "TOTAL_OPERATE_INCOME": 1000.0,
        "PARENT_NETPROFIT": 200.0,
        "WEIGHTAVG_ROE": 10.0,
        "YSTZ": 5.0,
        "SJLTZ": 6.0,
        "BPS": 12.0,
        "MGJYXJJE": 3.0,
        "XSMLL": 40.0,
    }


class FundamentalTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = _config(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_refresh_filters_future_disclosures_and_marks_etf_unsupported(self):
        with patch(
            "ai_trade.data.fundamentals._open_request",
            return_value=_Response(_payload()),
        ):
            result = refresh_fundamentals(
                self.config,
                as_of=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(result["status"], "partial")
        self.assertEqual(len(result["records"]), 1)
        self.assertEqual(len(result["records"][0]["periods"]), 2)
        self.assertEqual(result["records"][0]["periods"][0]["basic_eps"], 2.2)
        self.assertEqual(result["errors"][0]["code"], "instrument_type_not_supported")
        self.assertFalse(result["authority"]["execution_authorized"])

        visible = FundamentalStore(self.config).list(
            FundamentalQuery(symbol="600000", include_revisions=True)
        )
        self.assertEqual(visible["records"][0]["symbol"], "600000")
        self.assertEqual(len(visible["revisions"]), 1)

    def test_identical_evidence_is_reused(self):
        with patch(
            "ai_trade.data.fundamentals._open_request",
            return_value=_Response(_payload()),
        ):
            first = refresh_fundamentals(
                self.config,
                symbols=["600000"],
                as_of=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
            )
        with patch(
            "ai_trade.data.fundamentals._open_request",
            return_value=_Response(_payload()),
        ):
            second = refresh_fundamentals(
                self.config,
                symbols=["600000"],
                as_of=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(first["revision"], 1)
        self.assertTrue(second["reused"])

    def test_local_unavailable_read_never_contacts_network(self):
        result = FundamentalStore(self.config).list(
            FundamentalQuery(trade_date=date(2026, 7, 17))
        )
        self.assertFalse(result["available"])
        self.assertEqual(result["errors"][0]["code"], "fundamentals_not_refreshed")


if __name__ == "__main__":
    unittest.main()
