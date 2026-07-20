from __future__ import annotations

import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_trade.data.eastmoney import load_cached_bars
from ai_trade.data.tushare import (
    TOKEN_ENV,
    _factor_map,
    _parse_rows,
    _response_rows,
    download_instrument,
)
from ai_trade.models import Instrument


INSTRUMENT = Instrument(
    symbol="510300",
    name="CSI 300 ETF",
    market="SH",
    asset="equity",
    instrument_type="ETF",
)
START = date(2024, 6, 3)
END = date(2024, 6, 4)


class TushareProviderTests(unittest.TestCase):
    def test_response_contract_rejects_field_and_row_mismatches(self):
        payload = {
            "code": 0,
            "data": {"fields": ["ts_code"], "items": [["510300.SH"]]},
        }
        self.assertEqual(
            _response_rows(payload, api_name="fund_daily", expected_fields=("ts_code",)),
            [{"ts_code": "510300.SH"}],
        )
        payload["data"]["fields"] = ["trade_date"]
        with self.assertRaisesRegex(RuntimeError, "fields"):
            _response_rows(payload, api_name="fund_daily", expected_fields=("ts_code",))

    def test_parser_normalizes_units_and_forward_adjustment(self):
        rows = _daily_rows()
        factors = _factor_map(
            _factor_rows(), ts_code="510300.SH", start=START, end=END
        )
        bars = _parse_rows(
            rows,
            ts_code="510300.SH",
            start=START,
            end=END,
            adjustment="forward",
            factors=factors,
        )
        self.assertEqual([item.date for item in bars], [START, END])
        self.assertAlmostEqual(bars[0].open, 5.0)
        self.assertAlmostEqual(bars[0].close, 5.5)
        self.assertEqual(bars[0].volume, 123.0)
        self.assertEqual(bars[0].amount, 456000.0)

    def test_download_requires_environment_token(self):
        config = SimpleNamespace(
            raw={
                "data": {
                    "start": START.isoformat(),
                    "end": END.isoformat(),
                    "adjustment": "none",
                }
            }
        )
        with patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, TOKEN_ENV):
                download_instrument(
                    config,
                    INSTRUMENT,
                    Path("unused.csv"),
                    cutoff=END,
                )

    def test_download_writes_valid_csv_without_persisting_token(self):
        config = SimpleNamespace(
            raw={
                "data": {
                    "start": START.isoformat(),
                    "end": END.isoformat(),
                    "adjustment": "forward",
                    "proxy_mode": "direct",
                }
            }
        )
        calls: list[str] = []

        def request_rows(*args, api_name, **kwargs):
            calls.append(api_name)
            return _daily_rows() if api_name == "fund_daily" else _factor_rows()

        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            "os.environ", {TOKEN_ENV: "secret-token"}, clear=True
        ), patch("ai_trade.data.tushare._request_rows", side_effect=request_rows):
            output = Path(temporary) / "reference.csv"
            metadata: dict[str, object] = {}
            download_instrument(
                config,
                INSTRUMENT,
                output,
                cutoff=END,
                provider_metadata=metadata,
            )
            bars = load_cached_bars(output)
            self.assertEqual(len(bars), 2)
            self.assertEqual(calls, ["fund_daily", "fund_adj"])
            self.assertEqual(metadata["source_provider"], "tushare_pro")
            self.assertEqual(metadata["token_source"], TOKEN_ENV)
            self.assertNotIn("secret-token", json.dumps(metadata))


def _daily_rows():
    return [
        {
            "ts_code": "510300.SH",
            "trade_date": "20240604",
            "open": 12.0,
            "high": 13.0,
            "low": 11.0,
            "close": 12.5,
            "vol": 150.0,
            "amount": 600.0,
        },
        {
            "ts_code": "510300.SH",
            "trade_date": "20240603",
            "open": 10.0,
            "high": 12.0,
            "low": 9.0,
            "close": 11.0,
            "vol": 123.0,
            "amount": 456.0,
        },
    ]


def _factor_rows():
    return [
        {"ts_code": "510300.SH", "trade_date": "20240604", "adj_factor": 2.0},
        {"ts_code": "510300.SH", "trade_date": "20240603", "adj_factor": 1.0},
    ]


if __name__ == "__main__":
    unittest.main()
