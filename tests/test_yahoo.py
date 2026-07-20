from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_trade.data.eastmoney import load_cached_bars
from ai_trade.data.yahoo import (
    YahooDownloadError,
    _download_payload,
    _parse_payload,
    _ticker,
    download_instrument,
    is_transport_failure,
)
from ai_trade.models import Instrument


START = date(2024, 6, 3)
END = date(2024, 6, 4)
INSTRUMENT = Instrument(
    symbol="510300",
    name="CSI 300 ETF",
    market="SH",
    asset="equity",
)


class _Response:
    def __init__(self, body: bytes):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def read(self, limit: int) -> bytes:
        return self.body[:limit]


class YahooProviderTests(unittest.TestCase):
    def test_ticker_mapping_is_market_specific(self):
        self.assertEqual(_ticker(INSTRUMENT), "510300.SS")
        self.assertEqual(
            _ticker(
                Instrument(
                    symbol="000001",
                    name="Ping An Bank",
                    market="SZ",
                    asset="equity",
                )
            ),
            "000001.SZ",
        )

    def test_unadjusted_parser_converts_yahoo_shares_to_domestic_lots(self):
        bars = _parse_payload(
            _payload(),
            INSTRUMENT,
            ticker="510300.SS",
            adjustment="none",
            start=START,
            end=END,
            cutoff=END,
        )
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].date, START)
        self.assertEqual(bars[0].open, 10.0)
        self.assertEqual(bars[0].close, 11.0)
        self.assertAlmostEqual(bars[0].volume, 123.45)
        self.assertAlmostEqual(bars[0].amount, (12.0 + 9.0 + 11.0) / 3 * 12_345)

    def test_forward_parser_scales_ohlc_with_adjusted_close(self):
        bars = _parse_payload(
            _payload(),
            INSTRUMENT,
            ticker="510300.SS",
            adjustment="forward",
            start=START,
            end=END,
            cutoff=END,
        )
        self.assertAlmostEqual(bars[0].open, 5.0)
        self.assertAlmostEqual(bars[0].close, 5.5)
        self.assertAlmostEqual(bars[0].high, 6.0)
        self.assertAlmostEqual(bars[0].low, 4.5)
        self.assertAlmostEqual(bars[0].volume, 123.45)

    def test_parser_rejects_identity_timezone_and_array_length_mismatches(self):
        payload = _payload()
        payload["chart"]["result"][0]["meta"]["symbol"] = "000001.SZ"
        with self.assertRaisesRegex(RuntimeError, "identity"):
            _parse(payload)

        payload = _payload()
        payload["chart"]["result"][0]["meta"]["gmtoffset"] = 0
        with self.assertRaisesRegex(RuntimeError, "timezone"):
            _parse(payload)

        payload = _payload()
        payload["chart"]["result"][0]["indicators"]["quote"][0]["volume"].pop()
        with self.assertRaisesRegex(RuntimeError, "lengths differ"):
            _parse(payload)

    def test_download_writes_valid_csv_and_discloses_estimated_amount(self):
        config = _config()
        metadata: dict[str, object] = {}
        with tempfile.TemporaryDirectory() as temporary, patch(
            "ai_trade.data.yahoo._download_payload", return_value=_payload()
        ):
            output = Path(temporary) / "510300.csv"
            returned = download_instrument(
                config,
                INSTRUMENT,
                output,
                cutoff=END,
                proxy_mode="direct",
                provider_metadata=metadata,
            )
            bars = load_cached_bars(returned)

        self.assertEqual(len(bars), 2)
        self.assertEqual(metadata["source_provider"], "yahoo_chart")
        self.assertEqual(metadata["amount_quality"], "locally_estimated_not_compared")
        self.assertEqual(
            metadata["comparison_fields"],
            ["open", "high", "low", "close", "volume"],
        )

    def test_backward_adjustment_is_rejected_before_network_io(self):
        config = _config(adjustment="backward")
        with tempfile.TemporaryDirectory() as temporary, patch(
            "ai_trade.data.yahoo._download_payload"
        ) as request:
            with self.assertRaisesRegex(RuntimeError, "only none or forward"):
                download_instrument(
                    config,
                    INSTRUMENT,
                    Path(temporary) / "510300.csv",
                    cutoff=END,
                )
        request.assert_not_called()

    def test_retryable_http_failure_is_retried_and_classified(self):
        error = urllib.error.HTTPError(
            "https://query1.finance.yahoo.com/", 429, "rate limited", {}, None
        )
        body = json.dumps(_payload()).encode("utf-8")
        with patch(
            "ai_trade.data.yahoo._open_request",
            side_effect=[error, _Response(body)],
        ) as request, patch("ai_trade.data.yahoo.time_module.sleep") as sleep:
            payload = _download_payload(
                "510300.SS",
                START,
                END,
                timeout=5,
                proxy_mode="system",
                max_attempts=2,
                retry_base=0.0,
                retry_max=0.0,
                retry_jitter=0.0,
            )
        self.assertEqual(payload["chart"]["error"], None)
        self.assertEqual(request.call_count, 2)
        sleep.assert_called_once_with(0.0)
        self.assertTrue(is_transport_failure(YahooDownloadError("failed", [error])))

    def test_malformed_json_and_nonretryable_http_are_not_transport_outages(self):
        with patch(
            "ai_trade.data.yahoo._open_request", return_value=_Response(b'{"a":1,"a":2}')
        ):
            with self.assertRaisesRegex(RuntimeError, "invalid JSON"):
                _download_payload(
                    "510300.SS",
                    START,
                    END,
                    timeout=5,
                    proxy_mode="system",
                    max_attempts=2,
                    retry_base=0.0,
                    retry_max=0.0,
                    retry_jitter=0.0,
                )
        not_found = urllib.error.HTTPError(
            "https://query1.finance.yahoo.com/", 404, "not found", {}, None
        )
        self.assertFalse(is_transport_failure(YahooDownloadError("failed", [not_found])))


def _config(*, adjustment: str = "forward") -> SimpleNamespace:
    return SimpleNamespace(
        raw={
            "data": {
                "start": START.isoformat(),
                "end": END.isoformat(),
                "adjustment": adjustment,
                "timeout_seconds": 5,
                "max_attempts": 2,
                "retry_base_seconds": 0.0,
                "retry_max_seconds": 0.0,
                "retry_jitter_seconds": 0.0,
                "proxy_mode": "system",
            }
        }
    )


def _parse(payload: object):
    return _parse_payload(
        payload,
        INSTRUMENT,
        ticker="510300.SS",
        adjustment="forward",
        start=START,
        end=END,
        cutoff=END,
    )


def _payload() -> dict[str, object]:
    timestamps = [
        int(datetime(2024, 6, 3, 5, 30, tzinfo=timezone.utc).timestamp()),
        int(datetime(2024, 6, 4, 5, 30, tzinfo=timezone.utc).timestamp()),
    ]
    return {
        "chart": {
            "result": [
                {
                    "meta": {
                        "symbol": "510300.SS",
                        "exchangeTimezoneName": "Asia/Shanghai",
                        "gmtoffset": 28_800,
                    },
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [
                            {
                                "open": [10.0, 11.0],
                                "high": [12.0, 13.0],
                                "low": [9.0, 10.0],
                                "close": [11.0, 12.0],
                                "volume": [12_345, 23_456],
                            }
                        ],
                        "adjclose": [{"adjclose": [5.5, 6.0]}],
                    },
                }
            ],
            "error": None,
        }
    }


if __name__ == "__main__":
    unittest.main()
