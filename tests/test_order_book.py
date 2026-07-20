from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ai_trade.data.order_book import (
    OrderBookQuery,
    OrderBookStore,
    refresh_order_book,
)
from ai_trade.models import Instrument


CHINA_TIMEZONE = timezone(timedelta(hours=8))
OBSERVED = datetime(2026, 7, 20, 11, 49, tzinfo=CHINA_TIMEZONE)


class _Response:
    def __init__(self, value):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _maximum):
        return json.dumps(self.value).encode("utf-8")


def _config(root: Path):
    return SimpleNamespace(
        raw={
            "data": {
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
        order_book_dir=root / "order_book",
    )


def _payload(symbol: str, observed: datetime = OBSERVED):
    data = {
        "f43": 10.0,
        "f57": symbol,
        "f58": "Test Name",
        "f60": 9.9,
        "f86": int(observed.timestamp()),
    }
    for index, (price_field, volume_field) in enumerate(
        (("f19", "f20"), ("f17", "f18"), ("f15", "f16"), ("f13", "f14"), ("f11", "f12"))
    ):
        data[price_field] = 10.0 - index * 0.01
        data[volume_field] = index + 1
    for index, (price_field, volume_field) in enumerate(
        (("f39", "f40"), ("f37", "f38"), ("f35", "f36"), ("f33", "f34"), ("f31", "f32"))
    ):
        data[price_field] = 10.01 + index * 0.01
        data[volume_field] = index + 2
    return {"rc": 0, "data": data}


class OrderBookTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = _config(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def _open_request(self, request, _timeout, _proxy):
        self.assertIn("fltt=2", request.full_url)
        self.assertIn("invt=2", request.full_url)
        symbol = "600000" if "1.600000" in request.full_url else "510300"
        return _Response(_payload(symbol))

    def test_refresh_normalizes_five_level_prices_and_share_volumes(self):
        with patch(
            "ai_trade.data.order_book._open_request",
            side_effect=self._open_request,
        ):
            result = refresh_order_book(
                self.config,
                as_of=datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc),
            )
        self.assertTrue(result["available"])
        self.assertEqual(result["status"], "current")
        self.assertEqual(result["summary"]["complete_depth_count"], 2)
        first = result["records"][0]
        self.assertEqual(first["best_bid"], 10.0)
        self.assertEqual(first["best_ask"], 10.01)
        self.assertEqual(first["buy_levels"][0]["volume_lots"], 1.0)
        self.assertEqual(first["buy_levels"][0]["volume_shares"], 100)
        self.assertEqual(first["complete_level_count"], 10)
        self.assertFalse(result["authority"]["execution_authorized"])

        visible = OrderBookStore(self.config).list(
            OrderBookQuery(symbol="510300")
        )
        self.assertEqual(len(visible["records"]), 1)
        self.assertEqual(visible["records"][0]["symbol"], "510300")

    def test_partial_provider_failure_and_identical_evidence_reuse(self):
        def open_request(request, _timeout, _proxy):
            if "510300" in request.full_url:
                raise OSError("provider down")
            return _Response(_payload("600000"))

        with patch(
            "ai_trade.data.order_book._open_request", side_effect=open_request
        ):
            first = refresh_order_book(
                self.config,
                as_of=datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc),
            )
        with patch(
            "ai_trade.data.order_book._open_request", side_effect=open_request
        ):
            second = refresh_order_book(
                self.config,
                as_of=datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(first["status"], "partial")
        self.assertEqual(first["errors"][0]["symbol"], "510300")
        self.assertTrue(second["reused"])

    def test_local_unavailable_read_never_contacts_network(self):
        result = OrderBookStore(self.config).list(
            OrderBookQuery(trade_date=date(2026, 7, 17))
        )
        self.assertFalse(result["available"])
        self.assertEqual(result["errors"][0]["code"], "order_book_not_refreshed")

    def test_batch_excludes_a_stale_provider_date(self):
        def open_request(request, _timeout, _proxy):
            if "510300" in request.full_url:
                return _Response(_payload("510300", OBSERVED - timedelta(days=1)))
            return _Response(_payload("600000"))

        with patch(
            "ai_trade.data.order_book._open_request", side_effect=open_request
        ):
            result = refresh_order_book(
                self.config,
                as_of=datetime(2026, 7, 20, 4, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(result["status"], "partial")
        self.assertEqual([item["symbol"] for item in result["records"]], ["600000"])
        self.assertEqual(result["errors"][0]["code"], "order_book_stale_provider_date")


if __name__ == "__main__":
    unittest.main()
