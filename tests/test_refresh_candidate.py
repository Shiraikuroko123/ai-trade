from __future__ import annotations

import csv
from pathlib import Path
import tempfile
import unittest

from ai_trade.data.refresh_candidate import RefreshCandidate


class RefreshCandidateTests(unittest.TestCase):
    def test_unchanged_symbol_is_adopted_across_other_instrument_change(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            original = RefreshCandidate.for_refresh(cache, _identity("2020-01-01"))
            path = original.path_for("AAA")
            _write_csv(path)
            original.record("AAA", path, {"source": "network"})

            changed = RefreshCandidate.for_refresh(cache, _identity("2020-02-01"))
            restored = changed.restore("AAA")

            self.assertIsNotNone(restored)
            assert restored is not None
            self.assertEqual(
                restored[1]["adopted_from_fingerprint"],
                original.fingerprint,
            )

    def test_changed_symbol_or_cutoff_is_not_adopted(self):
        with tempfile.TemporaryDirectory() as temporary:
            cache = Path(temporary)
            original_identity = _identity("2020-01-01")
            original = RefreshCandidate.for_refresh(cache, original_identity)
            path = original.path_for("BBB")
            _write_csv(path)
            original.record("BBB", path, {"source": "network"})

            changed_symbol = RefreshCandidate.for_refresh(
                cache,
                _identity("2020-02-01"),
            )
            self.assertIsNone(changed_symbol.restore("BBB"))

            changed_cutoff_identity = _identity("2020-01-01")
            changed_cutoff_identity["requested_through"] = "2024-01-04"
            changed_cutoff = RefreshCandidate.for_refresh(
                cache,
                changed_cutoff_identity,
            )
            self.assertIsNone(changed_cutoff.restore("BBB"))


def _identity(second_listing: str) -> dict[str, object]:
    return {
        "contract": "daily-adjusted-bars-v1",
        "requested_from": "2024-01-01",
        "requested_through": "2024-01-03",
        "adjustment": "forward",
        "proxy_mode": "direct",
        "instruments": [
            {
                "symbol": "AAA",
                "market": "SH",
                "listing_date": "2010-01-01",
                "delisting_date": None,
            },
            {
                "symbol": "BBB",
                "market": "SZ",
                "listing_date": second_listing,
                "delisting_date": None,
            },
        ],
    }


def _write_csv(path: Path) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "open", "close", "high", "low", "volume", "amount"])
        writer.writerow(["2024-01-02", "1", "1", "1", "1", "1", "1"])


if __name__ == "__main__":
    unittest.main()
