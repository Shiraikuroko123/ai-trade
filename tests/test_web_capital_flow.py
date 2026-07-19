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

from ai_trade.data.capital_flow import CapitalFlowQuery
from ai_trade.web.server import (
    DashboardServer,
    _handler_factory,
    _parse_capital_flow_query,
)
from ai_trade.web.service import DashboardService


class CapitalFlowQueryTests(unittest.TestCase):
    def test_empty_and_filtered_queries_are_bounded(self):
        self.assertEqual(_parse_capital_flow_query(""), CapitalFlowQuery())
        self.assertEqual(
            _parse_capital_flow_query(
                "date=2026-07-17&q=%E7%94%B5%E5%8A%9B&sort=large_net_inflow"
                "&direction=asc&limit=25"
            ),
            CapitalFlowQuery(
                trade_date=date(2026, 7, 17),
                q="电力",
                sort="large_net_inflow",
                direction="asc",
                limit=25,
            ),
        )

    def test_parser_rejects_ambiguous_and_unbounded_values(self):
        for query in (
            "date=2026-02-30",
            "date=2026-7-17",
            "q=",
            f"q={'x' * 101}",
            "sort=unknown",
            "direction=sideways",
            "limit=0",
            "limit=501",
            "limit=1.5",
            "extra=true",
            "sort=name&sort=change_pct",
            "date=2026-07-17&",
        ):
            with self.subTest(query=query), self.assertRaises(ValueError):
                _parse_capital_flow_query(query)


class DashboardCapitalFlowServiceTests(unittest.TestCase):
    def test_service_passes_cutoff_and_requests_full_revision_history(self):
        config = SimpleNamespace(cache_dir=Path("unused"))
        query = CapitalFlowQuery(q="银行", limit=10)
        service = DashboardService(config)
        service.market = Mock(
            return_value=SimpleNamespace(completed_through=date(2026, 7, 17))
        )
        store = Mock()
        store.list.return_value = {"available": True, "flows": []}

        with patch("ai_trade.web.service.CapitalFlowStore", return_value=store):
            result = service.capital_flow(query)

        self.assertTrue(result["available"])
        self.assertRegex(result["generated_at"], r"^\d{4}-\d{2}-\d{2}T")
        store.list.assert_called_once_with(
            CapitalFlowQuery(q="银行", limit=10, include_revisions=True),
            completed_session_cutoff=date(2026, 7, 17),
        )

    def test_snapshot_remains_readable_without_ordinary_market_cache(self):
        config = SimpleNamespace(cache_dir=Path("unused"))
        service = DashboardService(config)
        service.market = Mock(side_effect=RuntimeError("cache unavailable"))
        store = Mock()
        store.list.return_value = {"available": True, "flows": []}

        with patch("ai_trade.web.service.CapitalFlowStore", return_value=store):
            result = service.capital_flow()

        self.assertTrue(result["available"])
        store.list.assert_called_once_with(
            CapitalFlowQuery(include_revisions=True),
            completed_session_cutoff=None,
        )


class DashboardCapitalFlowHttpTests(unittest.TestCase):
    def test_real_service_get_is_local_and_network_free(self):
        store = Mock()
        store.list.return_value = {
            "available": True,
            "status": "current",
            "trade_date": "2026-07-17",
            "flows": [],
        }
        with tempfile.TemporaryDirectory() as temporary:
            config = SimpleNamespace(cache_dir=Path(temporary))
            service = DashboardService(config)
            with (
                patch(
                    "ai_trade.web.service.MarketData",
                    side_effect=FileNotFoundError("market cache missing"),
                ),
                patch("ai_trade.web.service.CapitalFlowStore", return_value=store),
                patch(
                    "urllib.request.urlopen",
                    side_effect=AssertionError("read-only GET must not access network"),
                ) as urlopen,
                _RunningServer(service) as port,
            ):
                status, payload = _request_json(port, "/api/capital-flow")

        self.assertEqual(status, 200)
        self.assertTrue(payload["available"])
        self.assertIn("generated_at", payload)
        urlopen.assert_not_called()

    def test_get_supports_filters_and_rejects_unknown_parameters(self):
        service = _HttpService()
        with _RunningServer(service) as port:
            status, payload = _request_json(
                port,
                "/api/capital-flow?date=2026-07-17&q=%E9%93%B6%E8%A1%8C"
                "&sort=main_net_inflow_pct&direction=desc&limit=10",
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["filters"]["q"], "银行")
            for query in ("unknown=1", "sort=name&sort=change_pct", "limit=501"):
                bad_status, bad_payload = _request_json(
                    port, f"/api/capital-flow?{query}"
                )
                self.assertEqual(bad_status, 400)
                self.assertTrue(bad_payload["error"])

        self.assertEqual(
            service.queries,
            [
                CapitalFlowQuery(
                    trade_date=date(2026, 7, 17),
                    q="银行",
                    sort="main_net_inflow_pct",
                    direction="desc",
                    limit=10,
                )
            ],
        )


class _HttpService:
    config = SimpleNamespace(reports_dir=Path("unused"))

    def __init__(self):
        self.queries: list[CapitalFlowQuery] = []

    def capital_flow(self, query: CapitalFlowQuery):
        self.queries.append(query)
        return {
            "available": True,
            "filters": {
                "trade_date": (
                    query.trade_date.isoformat() if query.trade_date else None
                ),
                "q": query.q,
                "sort": query.sort,
                "direction": query.direction,
                "limit": query.limit,
            },
            "flows": [],
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
