from __future__ import annotations

import http.client
import json
import tempfile
import threading
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from ai_trade.data.market_intelligence import DragonTigerQuery
from ai_trade.web.server import (
    DashboardServer,
    _handler_factory,
    _parse_market_intelligence_query,
)
from ai_trade.web.service import DashboardService


class MarketIntelligenceQueryTests(unittest.TestCase):
    def test_empty_and_filtered_queries_are_parsed_to_bounded_contract(self):
        self.assertEqual(_parse_market_intelligence_query(""), DragonTigerQuery())
        self.assertEqual(
            _parse_market_intelligence_query(
                "date=2026-07-17&symbol=600519&market=SH&q=%E4%B9%B0%E5%85%A5&limit=25"
            ),
            DragonTigerQuery(
                trade_date=date(2026, 7, 17),
                symbol="600519",
                market="SH",
                q="买入",
                limit=25,
            ),
        )

    def test_query_parser_rejects_malformed_unbounded_and_ambiguous_values(self):
        for query in (
            "date=2026-02-30",
            "date=2026-7-17",
            "symbol=60051",
            "symbol=60051A",
            "symbol=%EF%BC%96%EF%BC%90%EF%BC%90%EF%BC%95%EF%BC%91%EF%BC%99",
            "market=sh",
            "market=HK",
            "q=",
            f"q={'x' * 101}",
            "limit=0",
            "limit=501",
            "limit=1.5",
            "extra=true",
            "market=SH&market=SZ",
            "symbol=600519&symbol=000001",
            "date=2026-07-17&",
        ):
            with self.subTest(query=query), self.assertRaises(ValueError):
                _parse_market_intelligence_query(query)

        with self.assertRaisesRegex(ValueError, "too long"):
            _parse_market_intelligence_query("q=" + "x" * 1023)


class DashboardMarketIntelligenceServiceTests(unittest.TestCase):
    def test_service_passes_completed_cutoff_and_requests_revision_history(self):
        config = SimpleNamespace(cache_dir=Path("unused"))
        query = DragonTigerQuery(symbol="600519", limit=5)
        market = SimpleNamespace(completed_through=date(2026, 7, 17))
        service = DashboardService(config)
        service.market = Mock(return_value=market)
        store = Mock()
        store.list.return_value = {"available": True, "records": []}

        with patch(
            "ai_trade.web.service.DragonTigerStore", return_value=store
        ) as store_type:
            result = service.market_intelligence(query)

        self.assertTrue(result["available"])
        service.market.assert_called_once_with(recover_snapshot=False)
        store_type.assert_called_once_with(config)
        store.list.assert_called_once_with(
            DragonTigerQuery(
                symbol="600519",
                limit=5,
                include_revisions=True,
            ),
            completed_session_cutoff=date(2026, 7, 17),
        )

    def test_local_snapshot_remains_readable_when_market_cache_is_unavailable(self):
        config = SimpleNamespace(cache_dir=Path("unused"))
        query = DragonTigerQuery()
        snapshot = {
            "available": True,
            "status": "current",
            "records": [{"symbol": "600519", "trade_date": "2026-07-17"}],
        }
        service = DashboardService(config)
        service.market = Mock(side_effect=RuntimeError("ordinary cache unavailable"))
        store = Mock()
        store.list.return_value = snapshot

        with patch("ai_trade.web.service.DragonTigerStore", return_value=store):
            result = service.market_intelligence(query)

        self.assertEqual(result, snapshot)
        store.list.assert_called_once_with(
            DragonTigerQuery(include_revisions=True),
            completed_session_cutoff=None,
        )


class DashboardMarketIntelligenceHttpTests(unittest.TestCase):
    def test_real_service_get_uses_local_snapshot_when_market_cache_is_missing(self):
        snapshot = {
            "available": True,
            "status": "unknown_freshness",
            "records": [{"symbol": "600519", "trade_date": "2026-07-17"}],
        }
        store = Mock()
        store.list.return_value = snapshot
        with tempfile.TemporaryDirectory() as temporary:
            config = SimpleNamespace(cache_dir=Path(temporary))
            service = DashboardService(config)
            with (
                patch(
                    "ai_trade.web.service.MarketData",
                    side_effect=FileNotFoundError("market cache missing"),
                ),
                patch(
                    "ai_trade.web.service.DragonTigerStore", return_value=store
                ),
                patch(
                    "urllib.request.urlopen",
                    side_effect=AssertionError(
                        "read-only GET must not access the network"
                    ),
                ) as urlopen,
                _RunningServer(service) as port,
            ):
                status, payload = _request_json(port, "/api/market-intelligence")

        self.assertEqual(status, 200)
        self.assertEqual(payload, snapshot)
        urlopen.assert_not_called()
        store.list.assert_called_once_with(
            DragonTigerQuery(include_revisions=True), completed_session_cutoff=None
        )

    def test_get_supports_defaults_and_filters_without_network_access(self):
        service = _HttpService()
        with _RunningServer(service) as port, patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("read-only GET must not access the network"),
        ) as urlopen:
            status, payload = _request_json(port, "/api/market-intelligence")
            self.assertEqual(status, 200)
            self.assertEqual(payload["records"], [])

            status, payload = _request_json(
                port,
                "/api/market-intelligence?date=2026-07-17&symbol=600519"
                "&market=SH&q=%E8%8C%85%E5%8F%B0&limit=10",
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["filters"]["symbol"], "600519")

        urlopen.assert_not_called()
        self.assertEqual(service.queries[0], DragonTigerQuery())
        self.assertEqual(
            service.queries[1],
            DragonTigerQuery(
                trade_date=date(2026, 7, 17),
                symbol="600519",
                market="SH",
                q="茅台",
                limit=10,
            ),
        )

    def test_unknown_and_duplicate_parameters_return_400_before_service(self):
        service = _HttpService()
        with _RunningServer(service) as port:
            for query in ("unknown=1", "market=SH&market=SZ", "limit=501"):
                with self.subTest(query=query):
                    status, payload = _request_json(
                        port, f"/api/market-intelligence?{query}"
                    )
                    self.assertEqual(status, 400)
                    self.assertTrue(payload["error"])

        self.assertEqual(service.queries, [])


class _HttpService:
    config = SimpleNamespace(reports_dir=Path("unused"))

    def __init__(self):
        self.queries: list[DragonTigerQuery] = []

    def market_intelligence(self, query: DragonTigerQuery):
        self.queries.append(query)
        return {
            "available": True,
            "filters": {
                "date": query.trade_date.isoformat() if query.trade_date else None,
                "symbol": query.symbol,
                "market": query.market,
                "q": query.q,
                "limit": query.limit,
            },
            "records": [],
        }


class _Jobs:
    def close(self):
        pass

    def list(self):
        return []

    def get(self, _job_id):
        return None


class _RunningServer:
    def __init__(self, service):
        self.jobs = _Jobs()
        handler = _handler_factory(service, self.jobs, "local-token", None, 0)
        self.server = DashboardServer(("127.0.0.1", 0), handler, self.jobs)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self.server.server_port

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _request_json(port: int, path: str) -> tuple[int, dict[str, object]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request("GET", path, headers={"Accept": "application/json"})
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    return response.status, json.loads(raw)


if __name__ == "__main__":
    unittest.main()
