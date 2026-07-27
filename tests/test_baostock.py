from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_trade.data.baostock import (
    BaoStockDownloadError,
    FIELDS,
    _parse_rows,
    download_instrument,
    is_transport_failure,
)
from ai_trade.data.eastmoney import load_cached_bars
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


class _Result:
    def __init__(
        self,
        rows: list[list[str]] | None = None,
        *,
        error_code: str = "0",
        error_msg: str = "success",
    ):
        self.error_code = error_code
        self.error_msg = error_msg
        self.fields = list(FIELDS)
        self._rows = list(rows or [])
        self._index = 0

    def next(self) -> bool:
        return self._index < len(self._rows)

    def get_row_data(self) -> list[str]:
        row = self._rows[self._index]
        self._index += 1
        return row


class _Client:
    def __init__(self, *, login: _Result | None = None):
        self.login_result = login or _Result()
        self.query_result = _Result(_raw_rows())
        self.query_call: tuple[object, ...] | None = None
        self.query_kwargs: dict[str, object] | None = None
        self.logout_called = False

    def login(self) -> _Result:
        print("login success!")
        return self.login_result

    def query_history_k_data_plus(self, *args: object, **kwargs: object) -> _Result:
        self.query_call = args
        self.query_kwargs = kwargs
        return self.query_result

    def logout(self) -> _Result:
        print("logout success!")
        self.logout_called = True
        return _Result()


class BaoStockProviderTests(unittest.TestCase):
    def test_parser_normalizes_share_volume_and_preserves_provider_amount(self):
        rows = [dict(zip(FIELDS, row, strict=True)) for row in reversed(_raw_rows())]
        bars = _parse_rows(
            rows,
            code="sh.510300",
            start=START,
            end=END,
            cutoff=END,
            adjustflag="2",
        )
        self.assertEqual([bar.date for bar in bars], [START, END])
        self.assertEqual(bars[0].volume, 12_345.0)
        self.assertEqual(bars[0].amount, 6_200_000.25)
        self.assertEqual(bars[1].close, 5.2)

    def test_parser_rejects_wrong_symbol_adjustment_and_duplicates(self):
        rows = [dict(zip(FIELDS, row, strict=True)) for row in _raw_rows()]
        rows[0]["code"] = "sz.510300"
        with self.assertRaisesRegex(RuntimeError, "symbol identity"):
            _parse_rows(
                rows,
                code="sh.510300",
                start=START,
                end=END,
                cutoff=END,
                adjustflag="2",
            )

        rows = [dict(zip(FIELDS, _raw_rows()[0], strict=True))] * 2
        with self.assertRaisesRegex(RuntimeError, "duplicate"):
            _parse_rows(
                rows,
                code="sh.510300",
                start=START,
                end=END,
                cutoff=END,
                adjustflag="2",
            )

    def test_download_uses_anonymous_client_without_leaking_client_prints(self):
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
        client = _Client()
        captured = io.StringIO()
        with tempfile.TemporaryDirectory() as temporary, patch(
            "ai_trade.data.baostock._load_client", return_value=client
        ), redirect_stdout(captured):
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

        self.assertEqual(captured.getvalue(), "")
        self.assertEqual(len(bars), 2)
        self.assertTrue(client.logout_called)
        self.assertEqual(client.query_call, ("sh.510300", ",".join(FIELDS)))
        self.assertEqual(client.query_kwargs["adjustflag"], "2")
        self.assertEqual(metadata["source_provider"], "baostock_public_api")
        self.assertEqual(metadata["usage_scope"], "provisional_reference_only")
        self.assertEqual(
            metadata["authorization_status"],
            "upstream_and_redistribution_unverified",
        )

    def test_transport_result_code_is_classified_and_logout_is_attempted(self):
        config = SimpleNamespace(
            raw={
                "data": {
                    "start": START.isoformat(),
                    "end": END.isoformat(),
                    "adjustment": "none",
                }
            }
        )
        client = _Client(
            login=_Result(error_code="10002007", error_msg="network receive error")
        )
        with patch("ai_trade.data.baostock._load_client", return_value=client):
            with self.assertRaises(BaoStockDownloadError) as raised:
                download_instrument(
                    config,
                    INSTRUMENT,
                    Path("unused.csv"),
                    cutoff=END,
                )
        self.assertTrue(is_transport_failure(raised.exception))
        self.assertTrue(client.logout_called)


def _raw_rows() -> list[list[str]]:
    return [
        [
            "2024-06-03",
            "sh.510300",
            "5.000",
            "5.200",
            "4.900",
            "5.100",
            "1234500",
            "6200000.25",
            "2",
        ],
        [
            "2024-06-04",
            "sh.510300",
            "5.100",
            "5.300",
            "5.000",
            "5.200",
            "2345600",
            "12100000",
            "2",
        ],
    ]


if __name__ == "__main__":
    unittest.main()
