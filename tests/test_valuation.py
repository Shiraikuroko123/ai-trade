from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

from ai_trade.data.valuation import (
    ValuationQuery,
    ValuationStore,
    _validate_reference_check,
    refresh_valuation,
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
        instruments=[
            Instrument(
                "600000", "Test", "SH", "equity", instrument_type="STOCK"
            ),
            Instrument("510300", "ETF", "SH", "ETF"),
        ],
        valuation_dir=root / "valuation",
    )


class _Response:
    def __init__(self, value: dict):
        self.value = value

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _maximum: int) -> bytes:
        return json.dumps(self.value).encode("utf-8")


def _payload(symbol: str, *, pe: int = 500, pb: int = 125):
    return {
        "rc": 0,
        "data": {
            "f43": 1234,
            "f57": symbol,
            "f58": "Test Name",
            "f116": 100000000,
            "f117": 90000000,
            "f162": pe,
            "f163": 510,
            "f164": 490,
            "f167": pb,
            "f168": 0,
            "f169": 12,
            "f170": 35,
            "f124": 0,
        },
    }


def _history_payload(symbol: str, count: int = 130):
    start = date(2026, 7, 17)
    return {
        "success": True,
        "code": 0,
        "message": "ok",
        "result": {
            "count": count,
            "data": [
                {
                    "SECURITY_CODE": symbol,
                    "SECURITY_NAME_ABBR": "Test Name",
                    "TRADE_DATE": (
                        start - timedelta(days=index)
                    ).isoformat()
                    + " 00:00:00",
                    "CLOSE_PRICE": 10.0,
                    "PE_TTM": float(count - index),
                    "PE_LAR": float(count - index + 1),
                    "PB_MRQ": float(count - index + 2),
                    "PCF_OCF_TTM": float(count - index + 3),
                    "PS_TTM": float(count - index + 4),
                }
                for index in range(count)
            ],
        },
    }


class ValuationTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = _config(self.root)

    def tearDown(self):
        self.temporary.cleanup()

    def test_refresh_scales_fields_and_computes_stock_only_percentiles(self):
        responses = {
            "1.600000": _Response(_payload("600000")),
            "1.510300": _Response(_payload("510300", pe=0, pb=0)),
        }

        def open_request(request, _timeout, _proxy):
            if "RPT_VALUEANALYSIS_DET" in request.full_url:
                return _Response(_history_payload("600000"))
            return responses[request.full_url.split("secid=")[1].split("&", 1)[0]]

        with patch("ai_trade.data.valuation._open_request", side_effect=open_request):
            result = refresh_valuation(
                self.config,
                as_of=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
            )
        self.assertTrue(result["available"])
        self.assertEqual(result["status"], "current")
        self.assertEqual(result["summary"]["returned_count"], 2)
        first = result["records"][0]
        self.assertEqual(first["price"], 12.34)
        self.assertEqual(first["pe_ttm"], 5.0)
        self.assertEqual(first["pb"], 1.25)
        self.assertIsNone(result["records"][1]["pe_ttm"])
        self.assertEqual(first["valuation_percentiles"]["pb"], 100.0)
        self.assertEqual(first["valuation_history"]["sample_count"], 130)
        self.assertTrue(first["valuation_history"]["available"])
        self.assertEqual(
            result["records"][1]["valuation_history"]["status"],
            "instrument_type_not_supported",
        )
        self.assertFalse(result["authority"]["execution_authorized"])

        visible = ValuationStore(self.config).list(ValuationQuery(symbol="600000"))
        self.assertEqual(len(visible["records"]), 1)
        self.assertEqual(visible["records"][0]["symbol"], "600000")

    def test_partial_provider_failure_is_explicit_and_idempotent(self):
        calls = {"count": 0}

        def open_request(request, _timeout, _proxy):
            calls["count"] += 1
            if "RPT_VALUEANALYSIS_DET" in request.full_url:
                return _Response(_history_payload("600000"))
            if "600000" in request.full_url:
                return _Response(_payload("600000"))
            raise OSError("provider down")

        with patch("ai_trade.data.valuation._open_request", side_effect=open_request):
            first = refresh_valuation(
                self.config,
                as_of=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(first["status"], "partial")
        self.assertEqual(len(first["errors"]), 1)
        self.assertEqual(first["revision"], 1)
        with patch("ai_trade.data.valuation._open_request", side_effect=open_request):
            second = refresh_valuation(
                self.config,
                as_of=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
            )
        self.assertTrue(second["reused"])
        self.assertEqual(second["revision"], 1)

    def test_unavailable_local_read_does_not_contact_network(self):
        result = ValuationStore(self.config).list(
            ValuationQuery(trade_date=date(2026, 7, 17))
        )
        self.assertFalse(result["available"])
        self.assertEqual(result["errors"][0]["code"], "valuation_not_refreshed")

    def test_quote_request_keeps_raw_eastmoney_scaling_contract(self):
        seen = []
        payload = _payload("600000", pe=420, pb=41)
        payload["data"]["f43"] = 901
        payload["data"]["f170"] = 158

        def open_request(request, _timeout, _proxy):
            seen.append(request.full_url)
            if "RPT_VALUEANALYSIS_DET" in request.full_url:
                return _Response(_history_payload("600000"))
            return _Response(payload)

        with patch("ai_trade.data.valuation._open_request", side_effect=open_request):
            result = refresh_valuation(
                self.config,
                symbols=["600000"],
                as_of=datetime(2026, 7, 20, 16, 0, tzinfo=timezone.utc),
            )
        self.assertEqual(result["records"][0]["price"], 9.01)
        self.assertEqual(result["records"][0]["pe_ttm"], 4.2)
        self.assertEqual(result["records"][0]["pb"], 0.41)
        self.assertEqual(result["records"][0]["change_pct"], 1.58)
        quote_url = next(url for url in seen if "secid=" in url)
        self.assertNotIn("fltt=2", quote_url)

    def test_history_page_totals_must_remain_consistent(self):
        first_page = _history_payload("600000", count=600)
        first_page["result"]["data"] = first_page["result"]["data"][:500]
        second_page = _history_payload("600000", count=599)
        second_page["result"]["data"] = second_page["result"]["data"][500:]

        def open_request(request, _timeout, _proxy):
            if "RPT_VALUEANALYSIS_DET" not in request.full_url:
                return _Response(_payload("600000"))
            return _Response(
                first_page if "pageNumber=1" in request.full_url else second_page
            )

        with patch("ai_trade.data.valuation._open_request", side_effect=open_request):
            result = refresh_valuation(
                self.config,
                symbols=["600000"],
                as_of=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
            )

        self.assertEqual(result["status"], "partial")
        self.assertEqual(
            result["errors"][0]["code"], "valuation_history_provider_error"
        )
        self.assertEqual(
            result["records"][0]["valuation_history"]["status"], "unavailable"
        )

    def test_tushare_daily_basic_is_an_independent_reference_only(self):
        reference = {
            "trade_date": "2026-07-17",
            "pe_ttm": 5.0,
            "pb": 1.25,
            "ps_ttm": 134.0,
            "responses": [],
            "response_sha256": "c" * 64,
        }

        def open_request(request, _timeout, _proxy):
            if "RPT_VALUEANALYSIS_DET" in request.full_url:
                return _Response(_history_payload("600000"))
            return _Response(_payload("600000"))

        with (
            patch("ai_trade.data.valuation.token_configured", return_value=True),
            patch(
                "ai_trade.data.valuation.fetch_valuation_reference",
                return_value=reference,
            ),
            patch("ai_trade.data.valuation._open_request", side_effect=open_request),
        ):
            result = refresh_valuation(
                self.config,
                symbols=["600000"],
                as_of=datetime(2026, 7, 20, 8, 0, tzinfo=timezone.utc),
            )
        check = result["records"][0]["independent_check"]
        self.assertEqual(check["status"], "confirmed")
        self.assertEqual(check["comparable_field_count"], 3)
        self.assertEqual(result["source"]["provider"], "eastmoney")

    def test_reference_status_must_match_field_conflicts(self):
        check = {
            "provider": "tushare",
            "status": "confirmed",
            "reason": None,
            "trade_date": "2026-07-17",
            "comparable_field_count": 1,
            "conflict_count": 1,
            "fields": [
                {
                    "field": "pe_ttm",
                    "primary": 8.0,
                    "reference": 12.0,
                    "absolute_difference": 4.0,
                    "allowed_difference": 0.24,
                    "status": "conflict",
                }
            ],
            "response_sha256": "c" * 64,
            "responses": [],
        }
        with self.assertRaisesRegex(RuntimeError, "status is inconsistent"):
            _validate_reference_check(check)


if __name__ == "__main__":
    unittest.main()
