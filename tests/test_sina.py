from __future__ import annotations

from datetime import date, timedelta
import hashlib
import json
import unittest
from unittest.mock import patch

from ai_trade.data.sina import (
    SinaEquivalenceError,
    verify_forward_adjustment_identity,
)
from ai_trade.models import Bar, Instrument


class SinaEquivalenceTests(unittest.TestCase):
    def test_identity_factor_and_full_ohlcv_match_are_accepted(self):
        bars = _bars()
        kline = _kline_bytes(bars)
        factor = _factor_bytes("1")
        with patch(
            "ai_trade.data.sina._download",
            side_effect=[kline, factor],
        ):
            evidence = verify_forward_adjustment_identity(
                _instrument(),
                bars,
                start=date(2024, 1, 1),
                end=date(2024, 1, 3),
                cutoff=date(2024, 1, 3),
                timeout=20,
                proxy_mode="direct",
                max_attempts=2,
                retry_base=0.0,
                retry_max=0.0,
                retry_jitter=0.0,
            )

        self.assertEqual(
            evidence["adjustment_equivalence"],
            "sina_qfq_identity_factor",
        )
        self.assertEqual(evidence["adjustment_evidence_rows"], 2)
        self.assertEqual(
            evidence["adjustment_factor_sha256"],
            hashlib.sha256(factor).hexdigest(),
        )

    def test_non_identity_factor_is_rejected(self):
        bars = _bars()
        with (
            patch(
                "ai_trade.data.sina._download",
                side_effect=[_kline_bytes(bars), _factor_bytes("0.9")],
            ),
            self.assertRaisesRegex(SinaEquivalenceError, "not identity"),
        ):
            verify_forward_adjustment_identity(
                _instrument(),
                bars,
                start=date(2024, 1, 1),
                end=date(2024, 1, 3),
                cutoff=date(2024, 1, 3),
                timeout=20,
                proxy_mode="direct",
                max_attempts=2,
                retry_base=0.0,
                retry_max=0.0,
                retry_jitter=0.0,
            )

    def test_bounded_missing_reference_volume_is_recorded(self):
        bars = _bars()
        with patch(
            "ai_trade.data.sina._download",
            side_effect=[
                _kline_bytes(bars, zero_volume_dates={date(2024, 1, 3)}),
                _factor_bytes("1"),
            ],
        ):
            evidence = verify_forward_adjustment_identity(
                _instrument(),
                bars,
                start=date(2024, 1, 1),
                end=date(2024, 1, 3),
                cutoff=date(2024, 1, 3),
                timeout=20,
                proxy_mode="direct",
                max_attempts=2,
                retry_base=0.0,
                retry_max=0.0,
                retry_jitter=0.0,
            )

        self.assertEqual(
            evidence["adjustment_evidence_missing_volume_sessions"],
            ["2024-01-03"],
        )

    def test_high_coverage_reference_gap_is_recorded(self):
        bars = _many_bars(101)
        reference = bars[:50] + bars[51:]
        with patch(
            "ai_trade.data.sina._download",
            side_effect=[_kline_bytes(reference), _factor_bytes("1")],
        ):
            evidence = verify_forward_adjustment_identity(
                _instrument(),
                bars,
                start=bars[0].date,
                end=bars[-1].date,
                cutoff=bars[-1].date,
                timeout=20,
                proxy_mode="direct",
                max_attempts=2,
                retry_base=0.0,
                retry_max=0.0,
                retry_jitter=0.0,
            )

        self.assertEqual(evidence["adjustment_evidence_rows"], 100)
        self.assertEqual(
            evidence["adjustment_evidence_missing_reference_sessions"],
            [bars[50].date.isoformat()],
        )

    def test_low_reference_coverage_is_rejected(self):
        bars = _many_bars(101)
        reference = bars[:49] + bars[51:]
        with (
            patch(
                "ai_trade.data.sina._download",
                side_effect=[_kline_bytes(reference), _factor_bytes("1")],
            ),
            self.assertRaisesRegex(SinaEquivalenceError, "coverage differs"),
        ):
            verify_forward_adjustment_identity(
                _instrument(),
                bars,
                start=bars[0].date,
                end=bars[-1].date,
                cutoff=bars[-1].date,
                timeout=20,
                proxy_mode="direct",
                max_attempts=2,
                retry_base=0.0,
                retry_max=0.0,
                retry_jitter=0.0,
            )

    def test_ohlcv_mismatch_is_rejected(self):
        bars = _bars()
        mismatched = list(bars)
        mismatched[-1] = Bar(
            date=mismatched[-1].date,
            open=mismatched[-1].open,
            close=mismatched[-1].close + 0.1,
            high=mismatched[-1].high + 0.1,
            low=mismatched[-1].low,
            volume=mismatched[-1].volume,
            amount=mismatched[-1].amount,
        )
        with (
            patch(
                "ai_trade.data.sina._download",
                side_effect=[_kline_bytes(bars), _factor_bytes("1")],
            ),
            self.assertRaisesRegex(SinaEquivalenceError, "OHLCV differs"),
        ):
            verify_forward_adjustment_identity(
                _instrument(),
                mismatched,
                start=date(2024, 1, 1),
                end=date(2024, 1, 3),
                cutoff=date(2024, 1, 3),
                timeout=20,
                proxy_mode="direct",
                max_attempts=2,
                retry_base=0.0,
                retry_max=0.0,
                retry_jitter=0.0,
            )

    def test_single_tick_price_mismatch_is_recorded(self):
        bars = _bars()
        reference = list(bars)
        reference[-1] = Bar(
            date=reference[-1].date,
            open=reference[-1].open,
            close=reference[-1].close - 0.01,
            high=reference[-1].high,
            low=reference[-1].low,
            volume=reference[-1].volume,
            amount=reference[-1].amount,
        )
        with patch(
            "ai_trade.data.sina._download",
            side_effect=[_kline_bytes(reference), _factor_bytes("1")],
        ):
            evidence = verify_forward_adjustment_identity(
                _instrument(),
                bars,
                start=date(2024, 1, 1),
                end=date(2024, 1, 3),
                cutoff=date(2024, 1, 3),
                timeout=20,
                proxy_mode="direct",
                max_attempts=2,
                retry_base=0.0,
                retry_max=0.0,
                retry_jitter=0.0,
            )

        mismatches = evidence["adjustment_evidence_price_mismatches"]
        self.assertEqual(len(mismatches), 1)
        self.assertEqual(mismatches[0]["session"], "2024-01-03")


def _instrument() -> Instrument:
    return Instrument(
        symbol="510300",
        name="ETF",
        market="SH",
        asset="equity",
    )


def _bars() -> list[Bar]:
    return [
        Bar(date(2024, 1, 2), 10.0, 10.1, 10.2, 9.8, 100.0, 1_000.0),
        Bar(date(2024, 1, 3), 10.1, 10.2, 10.3, 10.0, 110.0, 2_000.0),
    ]


def _many_bars(count: int) -> list[Bar]:
    return [
        Bar(
            date(2024, 1, 1) + timedelta(days=index),
            10.0,
            10.1,
            10.2,
            9.8,
            100.0,
            1_000.0,
        )
        for index in range(count)
    ]


def _kline_bytes(
    bars: list[Bar], *, zero_volume_dates: set[date] | None = None
) -> bytes:
    zero_volume_dates = zero_volume_dates or set()
    payload = [
        {
            "day": bar.date.isoformat(),
            "open": str(bar.open),
            "high": str(bar.high),
            "low": str(bar.low),
            "close": str(bar.close),
            "volume": (
                "0" if bar.date in zero_volume_dates else str(bar.volume * 100)
            ),
        }
        for bar in bars
    ]
    return json.dumps(payload).encode("utf-8")


def _factor_bytes(factor: str) -> bytes:
    payload = {
        "total": 1,
        "data": [
            {
                "d": "1900-01-01",
                "f": factor,
                "s": "1.0000000000000000",
                "u": "0.0000000000000000",
            }
        ],
    }
    return (
        f"var sh510300qfq={json.dumps(payload)}\n/* c2lnbmVkcHJvb2Y= */"
    ).encode("utf-8")


if __name__ == "__main__":
    unittest.main()
