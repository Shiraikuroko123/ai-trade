from __future__ import annotations

from datetime import date, datetime, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ai_trade.data.intraday import (
    IntradayProviderError,
    IntradayQuery,
    IntradayStore,
    refresh_intraday,
)
from ai_trade.models import Instrument


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
        instruments=[Instrument("600000", "Test", "SH", "equity")],
        intraday_dir=root / "intraday",
    )


def _payload(*rows: str, pre_price: float = 10.0) -> dict:
    return {
        "rc": 0,
        "data": {
            "prePrice": pre_price,
            "trendsTotal": len(rows),
            "trends": list(rows),
        },
    }


class _Response:
    def __init__(self, value: dict):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _maximum: int) -> bytes:
        return json.dumps(self.value).encode("utf-8")


class IntradayTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = _config(self.root)
        self.rows = (
            "2026-07-17 09:30,10.10,10.20,10.30,10.10,100,1020,10.20",
            "2026-07-17 09:31,10.20,10.25,10.40,10.20,200,2050,10.25",
            "2026-07-17 09:32,10.25,10.30,10.35,10.25,300,3090,10.30",
            "2026-07-17 13:00,10.30,10.35,10.40,10.30,400,4140,10.35",
        )

    def tearDown(self):
        self.temporary.cleanup()

    def test_refresh_publishes_validated_immutable_revision(self):
        response = _Response(_payload(*self.rows))
        as_of = datetime(2026, 7, 20, 16, 0, tzinfo=timezone.utc)
        with patch("ai_trade.data.intraday._open_request", return_value=response):
            first = refresh_intraday(
                self.config,
                "600000",
                trade_date=date(2026, 7, 17),
                interval=5,
                as_of=as_of,
            )
        self.assertTrue(first["available"])
        self.assertEqual(first["revision"], 1)
        self.assertEqual(first["summary"]["bar_count"], 2)
        self.assertEqual(first["bars"][0]["time"], "09:30")
        self.assertEqual(first["bars"][1]["time"], "13:00")
        self.assertEqual(first["bars"][0]["open"], 10.10)
        self.assertFalse(first["bars"][0]["open_derived"])
        self.assertFalse(first["authority"]["execution_authorized"])
        with patch("ai_trade.data.intraday._open_request", return_value=response):
            second = refresh_intraday(
                self.config,
                "600000",
                trade_date=date(2026, 7, 17),
                interval=5,
                as_of=as_of,
            )
        self.assertTrue(second["reused"])
        self.assertEqual(second["revision"], 1)
        files = list((self.root / "intraday").rglob("revision_*.json"))
        self.assertEqual(len(files), 1)

        visible = IntradayStore(self.config).list(
            IntradayQuery("600000", date(2026, 7, 17), interval=5)
        )
        self.assertEqual(len(visible["bars"]), 2)

    def test_unknown_or_future_dates_fail_closed(self):
        with self.assertRaisesRegex(ValueError, "must not be after"):
            refresh_intraday(
                self.config,
                "600000",
                trade_date=date(2026, 7, 20),
                as_of=datetime(2026, 7, 20, 2, 0, tzinfo=timezone.utc),
            )
        unavailable = IntradayStore(self.config).list(
            IntradayQuery("600000", date(2026, 7, 17))
        )
        self.assertFalse(unavailable["available"])
        self.assertEqual(unavailable["errors"][0]["code"], "intraday_not_refreshed")

    def test_wider_interval_can_be_derived_from_published_one_minute_evidence(self):
        response = _Response(_payload(*self.rows))
        as_of = datetime(2026, 7, 20, 16, 0, tzinfo=timezone.utc)
        with patch("ai_trade.data.intraday._open_request", return_value=response):
            refresh_intraday(
                self.config,
                "600000",
                trade_date=date(2026, 7, 17),
                interval=1,
                as_of=as_of,
            )

        visible = IntradayStore(self.config).list(
            IntradayQuery("600000", date(2026, 7, 17), interval=5)
        )
        self.assertTrue(visible["derived_view"])
        self.assertEqual(visible["base_interval_minutes"], 1)
        self.assertEqual(visible["interval_minutes"], 5)
        self.assertEqual(visible["summary"]["bar_count"], 2)
        self.assertEqual(visible["bars"][0]["open"], 10.10)
        self.assertEqual(visible["bars"][0]["close"], 10.30)
        self.assertEqual(visible["bars"][0]["average"], 10.30)

    def test_malformed_provider_rows_are_rejected(self):
        response = _Response(
            _payload("2026-07-17 09:30,10.20,10.20,10.10,10.30,100,1020,10.20")
        )
        with patch("ai_trade.data.intraday._open_request", return_value=response):
            with self.assertRaises(IntradayProviderError):
                refresh_intraday(
                    self.config,
                    "600000",
                    trade_date=date(2026, 7, 17),
                    as_of=datetime(2026, 7, 20, 16, 0, tzinfo=timezone.utc),
                )


if __name__ == "__main__":
    unittest.main()
