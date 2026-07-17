import json
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ai_trade.security import SecurityMaster


class SecurityMasterTests(unittest.TestCase):
    def test_security_master_rejects_duplicate_json_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "security-master.json"
            value = {
                "schema_version": 1,
                "instruments": [],
                "universes": {},
                "status_periods": [],
            }
            content = json.dumps(value).replace(
                '"universes": {}',
                '"universes": {}, "universes": {"other": []}',
            )
            path.write_text(content, encoding="utf-8")

            with self.assertRaisesRegex(ValueError, "duplicate JSON object key"):
                SecurityMaster.load(path)

    def test_point_in_time_universe_is_not_limited_to_eight_symbols(self):
        instruments = []
        memberships = []
        for index in range(12):
            symbol = f"{600000 + index:06d}"
            instruments.append(
                {
                    "symbol": symbol,
                    "name": symbol,
                    "market": "SH",
                    "asset": "stock",
                    "instrument_type": "STOCK",
                    "asset_class": "equity",
                    "sector": "test",
                    "listing_date": "2020-01-01",
                }
            )
            memberships.append(
                {"symbol": symbol, "start": "2021-01-01", "end": None}
            )
        master = SecurityMaster.from_dict(
            {
                "schema_version": 1,
                "selection_method": "point_in_time_test",
                "instruments": instruments,
                "universes": {"large": memberships},
                "status_periods": [],
            }
        )
        active = master.active_symbols("large", date(2024, 1, 2), 180)
        self.assertEqual(len(active), 12)

    def test_membership_listing_seasoning_and_status_are_date_effective(self):
        master = SecurityMaster.from_dict(
            {
                "schema_version": 1,
                "instruments": [
                    {
                        "symbol": "600000",
                        "name": "A",
                        "market": "SH",
                        "asset": "stock",
                        "instrument_type": "STOCK",
                        "listing_date": "2024-01-01",
                        "price_limit_pct": 0.10,
                    }
                ],
                "universes": {
                    "demo": [
                        {"symbol": "600000", "start": "2024-02-01", "end": None}
                    ]
                },
                "status_periods": [
                    {
                        "symbol": "600000",
                        "start": "2024-09-01",
                        "end": "2024-09-10",
                        "status": "suspended",
                        "tradable": False,
                    }
                ],
            }
        )
        self.assertIn(
            "listing_seasoning",
            master.eligibility_reasons("demo", "600000", date(2024, 3, 1), 180),
        )
        self.assertEqual(
            master.active_symbols("demo", date(2024, 8, 1), 180), ("600000",)
        )
        self.assertFalse(master.trading_status("600000", date(2024, 9, 5)).tradable)
        self.assertTrue(master.trading_status("600000", date(2024, 9, 11)).tradable)


if __name__ == "__main__":
    unittest.main()
