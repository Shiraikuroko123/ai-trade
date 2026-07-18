from __future__ import annotations

import http.client
import json
from pathlib import Path
import threading
import unittest
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import patch

from ai_trade.config import load_config
from ai_trade.models import Bar
from ai_trade.web.screener import ScreeningFilters, screen_rows
from ai_trade.web.server import DashboardServer, _handler_factory, _parse_universe_screen_query
from ai_trade.web.service import DashboardService


REPOSITORY_ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]


class ScreeningHelperTests(unittest.TestCase):
    def test_empty_query_uses_bounded_defaults(self):
        selected, filters = _parse_universe_screen_query("")
        self.assertIsNone(selected)
        self.assertEqual(filters, ScreeningFilters())

    def test_filters_sort_stably_and_put_missing_values_last(self):
        rows = [
            {
                "symbol": "B",
                "asset_class": "equity",
                "trend": "UP",
                "momentum": None,
                "active": True,
                "history_ready": True,
                "data_status": "complete",
            },
            {
                "symbol": "A",
                "asset_class": "equity",
                "trend": "UP",
                "momentum": 0.2,
                "active": True,
                "history_ready": True,
                "data_status": "complete",
            },
            {
                "symbol": "D",
                "asset_class": "equity",
                "trend": "MIXED",
                "momentum": -0.1,
                "active": True,
                "history_ready": True,
                "data_status": "complete",
            },
            {
                "symbol": "C",
                "asset_class": "commodity",
                "trend": "UP",
                "momentum": 0.4,
                "active": False,
                "history_ready": True,
                "data_status": "complete",
            },
        ]
        selected, counts = screen_rows(
            rows,
            ScreeningFilters(
                asset_class="equity",
                trend="not_down",
                active_only=True,
                sort="momentum",
                direction="desc",
            ),
        )
        self.assertEqual([row["symbol"] for row in selected], ["A", "D", "B"])
        self.assertEqual(
            counts,
            {
                "input": 4,
                "matched": 3,
                "returned": 3,
                "excluded": 1,
                "truncated": 0,
            },
        )

        ascending, _ = screen_rows(
            rows,
            ScreeningFilters(
                asset_class="equity",
                trend="not_down",
                active_only=True,
                sort="momentum",
                direction="asc",
            ),
        )
        self.assertEqual(
            [row["symbol"] for row in ascending], ["D", "A", "B"]
        )

    def test_query_parser_is_strict_and_bounded(self):
        selected, filters = _parse_universe_screen_query(
            "date=2026-07-17&asset_class=equity&trend=up&coverage=ready"
            "&min_average_amount=1000000&max_annual_volatility=0.35"
            "&active_only=true&sort=average_amount&direction=asc&limit=20"
        )
        self.assertEqual(selected, date(2026, 7, 17))
        self.assertEqual(filters.asset_class, "equity")
        self.assertEqual(filters.trend, "up")
        self.assertTrue(filters.active_only)
        self.assertEqual(filters.limit, 20)
        for query in (
            "trend=sideways",
            "asset_class=equity%20bad",
            "min_average_amount=-1",
            "limit=501",
            "active_only=yes",
            "sort=unknown",
            "date=2026-02-31",
            "trend=up&trend=down",
            "extra=true",
        ):
            with self.subTest(query=query), self.assertRaises(ValueError):
                _parse_universe_screen_query(query)


class DashboardScreenTests(unittest.TestCase):
    def test_screen_is_bound_to_one_snapshot_and_exposes_metrics(self):
        config = load_config(REPOSITORY_ROOT / "config" / "default.json")
        selected = date(2026, 7, 17)
        bars = [
            Bar(
                selected - timedelta(days=days),
                10.0 + (219 - days) * 0.01,
                10.0 + (219 - days) * 0.01,
                10.2 + (219 - days) * 0.01,
                9.8 + (219 - days) * 0.01,
                100.0,
                1_000_000.0 + days * 1000,
            )
            for days in range(219, -1, -1)
        ]
        market = SimpleNamespace(
            symbols={
                item.symbol: SimpleNamespace(bars=bars)
                for item in config.instruments
            },
            completed_through=selected,
            latest_common_session=selected,
            latest_date=lambda: selected,
            history=lambda symbol, on_date, count: bars[-count:],
            latest_bar_on_or_before=lambda symbol, on_date: bars[-1],
            manifest={
                "files": {
                    item.symbol: {
                        "source": "network",
                        "source_provider": "eastmoney",
                    }
                    for item in config.instruments
                }
            },
            manifest_sha256="m" * 64,
            file_hashes={item.symbol: "f" * 64 for item in config.instruments},
        )
        coverage = {
            item.symbol: {
                "name": item.name,
                "rows": len(bars),
                "first": bars[0].date.isoformat(),
                "last": selected.isoformat(),
            }
            for item in config.instruments
        }
        service = DashboardService(config)
        service.market = lambda **_kwargs: market
        with patch("ai_trade.web.service.diagnose", return_value={"coverage": coverage}):
            result = service.screen_universe(
                selected,
                ScreeningFilters(trend="up", sort="momentum", direction="desc"),
            )
            repeated = service.screen_universe(
                selected,
                ScreeningFilters(trend="up", sort="momentum", direction="desc"),
            )
            changed = service.screen_universe(
                selected,
                ScreeningFilters(trend="up", sort="momentum", direction="asc"),
            )
        self.assertEqual(result["screen"]["status"], "ok")
        self.assertIn("generated_at", result)
        self.assertEqual(result["screen"]["data_date"], selected.isoformat())
        self.assertTrue(result["screen"]["snapshot_id"].startswith("screen-2026-07-17-"))
        self.assertTrue(
            result["screen"]["filter_fingerprint"].startswith("filter-")
        )
        self.assertEqual(
            result["screen"]["filter_fingerprint"],
            repeated["screen"]["filter_fingerprint"],
        )
        self.assertNotEqual(
            result["screen"]["filter_fingerprint"],
            changed["screen"]["filter_fingerprint"],
        )
        self.assertTrue(result["instruments"])
        self.assertTrue(all(item["data_status"] == "complete" for item in result["instruments"]))
        self.assertTrue(all(item["history_ready"] for item in result["instruments"]))
        self.assertIn("momentum", result["instruments"][0])
        self.assertIn("annual_volatility", result["instruments"][0])

    def test_screen_fails_closed_when_market_cache_is_unavailable(self):
        config = load_config(REPOSITORY_ROOT / "config" / "default.json")
        service = DashboardService(config)
        service.market = lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("cache missing"))
        result = service.screen_universe()
        self.assertEqual(result["screen"]["status"], "unavailable")
        self.assertIsNone(result["screen"]["empty_reason"])
        self.assertTrue(result["instruments"])
        self.assertTrue(all(item["data_status"] == "missing" for item in result["instruments"]))

    def test_http_route_passes_the_bounded_screen_contract(self):
        service = SimpleNamespace(
            config=SimpleNamespace(reports_dir=Path("unused")),
            screen_universe=lambda selected, filters: {
                "date": selected.isoformat() if selected else None,
                "screen": {"filters": filters.as_dict()},
                "instruments": [],
            },
        )
        jobs = _Jobs()
        handler = _handler_factory(service, jobs, "local-token", None, 0)
        server = DashboardServer(("127.0.0.1", 0), handler, jobs)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port, timeout=5)
            connection.request(
                "GET",
                "/api/universe/screen?date=2026-07-17&trend=up&limit=5",
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(response.status, 200)
        self.assertEqual(payload["date"], "2026-07-17")
        self.assertEqual(payload["screen"]["filters"]["trend"], "up")
        self.assertEqual(payload["screen"]["filters"]["limit"], 5)


class _Jobs:
    def close(self):
        pass

    def list(self):
        return []

    def get(self, _job_id):
        return None


if __name__ == "__main__":
    unittest.main()
