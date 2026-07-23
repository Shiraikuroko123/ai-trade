from __future__ import annotations

from datetime import date
import json
import os
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_trade.data.tushare import TOKEN_ENV
from ai_trade.data.tushare_reference import (
    TushareReferenceError,
    fetch_fundamental_reference,
    request_table,
)
from ai_trade.models import Instrument


class _Response:
    def __init__(self, payload: object):
        self.raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _maximum: int) -> bytes:
        return self.raw


class TushareReferenceTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(
            raw={
                "data": {
                    "timeout_seconds": 2,
                    "max_attempts": 1,
                    "proxy_mode": "direct",
                }
            }
        )

    def test_request_table_is_bounded_and_does_not_return_the_token(self):
        payload = {
            "code": 0,
            "data": {
                "fields": ["ts_code", "trade_date", "pe_ttm"],
                "items": [["600000.SH", "20260717", 8.2]],
            },
        }
        captured: dict[str, object] = {}

        def open_request(request, timeout, proxy_mode):
            captured.update(json.loads(request.data.decode("ascii")))
            self.assertEqual(timeout, 2)
            self.assertEqual(proxy_mode, "direct")
            return _Response(payload)

        with patch.dict(os.environ, {TOKEN_ENV: "secret-token"}, clear=True), patch(
            "ai_trade.data.tushare_reference._open_request",
            side_effect=open_request,
        ):
            rows, evidence = request_table(
                self.config,
                "daily_basic",
                {"ts_code": "600000.SH", "trade_date": "20260717"},
                ("ts_code", "trade_date", "pe_ttm"),
                maximum_rows=2,
            )

        self.assertEqual(rows[0]["pe_ttm"], 8.2)
        self.assertEqual(captured["token"], "secret-token")
        self.assertNotIn("secret-token", json.dumps(evidence))
        self.assertEqual(evidence["row_count"], 1)

    def test_request_table_rejects_oversized_or_ambiguous_tables(self):
        cases = (
            {
                "code": 0,
                "data": {
                    "fields": ["ts_code", "ts_code"],
                    "items": [["600000.SH", "600000.SH"]],
                },
            },
            {
                "code": 0,
                "data": {
                    "fields": ["ts_code"],
                    "items": [["600000.SH"], ["600001.SH"]],
                },
            },
        )
        for payload in cases:
            with self.subTest(payload=payload), patch.dict(
                os.environ, {TOKEN_ENV: "secret-token"}, clear=True
            ), patch(
                "ai_trade.data.tushare_reference._open_request",
                return_value=_Response(payload),
            ), self.assertRaises(TushareReferenceError):
                request_table(
                    self.config,
                    "daily_basic",
                    {},
                    ("ts_code",),
                    maximum_rows=1,
                )

    def test_fundamental_reference_uses_consolidated_income_and_cutoff(self):
        instrument = Instrument(
            "600000", "Test Bank", "SH", "equity", instrument_type="STOCK"
        )
        calls: list[tuple[str, dict[str, object]]] = []

        def table(_config, api_name, params, _fields, *, maximum_rows):
            calls.append((api_name, dict(params)))
            evidence = {
                "provider": "tushare",
                "api_name": api_name,
                "response_sha256": ("a" if api_name == "fina_indicator" else "b")
                * 64,
                "response_bytes": 100,
                "row_count": 2,
            }
            if api_name == "fina_indicator":
                return [
                    {
                        "ts_code": "600000.SH",
                        "ann_date": "20260420",
                        "end_date": "20260331",
                        "eps": 0.5,
                        "roe": 5.0,
                        "grossprofit_margin": 40.0,
                        "bps": 12.0,
                        "ocfps": 0.7,
                    },
                    {
                        "ts_code": "600000.SH",
                        "ann_date": "20260820",
                        "end_date": "20260630",
                        "eps": 0.8,
                        "roe": 7.0,
                        "grossprofit_margin": 41.0,
                        "bps": 12.5,
                        "ocfps": 0.9,
                    },
                ], evidence
            return [
                {
                    "ts_code": "600000.SH",
                    "ann_date": "20260420",
                    "f_ann_date": "20260421",
                    "end_date": "20260331",
                    "revenue": 1000.0,
                    "n_income_attr_p": 200.0,
                },
                {
                    "ts_code": "600000.SH",
                    "ann_date": "20260820",
                    "f_ann_date": "20260821",
                    "end_date": "20260630",
                    "revenue": 2000.0,
                    "n_income_attr_p": 300.0,
                },
            ], evidence

        with patch(
            "ai_trade.data.tushare_reference.request_table", side_effect=table
        ):
            result = fetch_fundamental_reference(
                self.config,
                instrument,
                cutoff=date(2026, 7, 17),
                limit=4,
            )

        self.assertEqual([item[0] for item in calls], ["fina_indicator", "income"])
        self.assertNotIn("report_type", calls[0][1])
        self.assertEqual(calls[1][1]["report_type"], "1")
        self.assertEqual(len(result["periods"]), 1)
        self.assertEqual(result["periods"][0]["report_date"], "2026-03-31")
        self.assertEqual(result["periods"][0]["notice_date"], "2026-04-21")


if __name__ == "__main__":
    unittest.main()
