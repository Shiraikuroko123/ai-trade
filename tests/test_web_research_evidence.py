from __future__ import annotations

from datetime import date
import http.client
import json
from pathlib import Path
from types import SimpleNamespace
import threading
import unittest
from unittest.mock import Mock, patch

from ai_trade.data.intraday import IntradayQuery
from ai_trade.data.news import NewsQuery
from ai_trade.data.valuation import ValuationQuery
from ai_trade.web.server import (
    DashboardServer,
    _handler_factory,
    _parse_intraday_query,
    _parse_news_query,
    _parse_valuation_query,
)
from ai_trade.web.service import DashboardService


class ResearchEvidenceQueryTests(unittest.TestCase):
    def test_valid_queries_are_parsed_to_bounded_contracts(self):
        self.assertEqual(
            _parse_intraday_query(
                "symbol=600000&date=2026-07-17&interval=5&limit=120&revisions=1"
            ),
            IntradayQuery("600000", date(2026, 7, 17), 5, 120, True),
        )
        self.assertEqual(_parse_valuation_query(""), ValuationQuery())
        self.assertEqual(
            _parse_valuation_query(
                "date=2026-07-17&symbol=600000&limit=20&revisions=1"
            ),
            ValuationQuery(date(2026, 7, 17), "600000", 20, True),
        )
        self.assertEqual(_parse_news_query(""), NewsQuery())
        self.assertEqual(
            _parse_news_query(
                "date=2026-07-17&symbol=600000&kind=announcement"
                "&q=%E9%A3%8E%E9%99%A9&limit=20&revisions=1"
            ),
            NewsQuery(
                date(2026, 7, 17),
                "600000",
                "announcement",
                "风险",
                20,
                True,
            ),
        )

    def test_queries_reject_missing_ambiguous_and_unbounded_values(self):
        invalid_intraday = (
            "",
            "symbol=60000",
            "symbol=600000&interval=2",
            "symbol=600000&limit=1501",
            "symbol=600000&symbol=000001",
            "symbol=600000&unknown=1",
        )
        invalid_valuation = (
            "symbol=60000A",
            "limit=501",
            "revisions=yes",
            "date=2026-02-30",
            "symbol=600000&symbol=000001",
        )
        invalid_news = (
            "kind=sentiment",
            "q=",
            "limit=2001",
            "symbol=60000",
            "q=a&q=b",
            "unknown=1",
        )
        for query in invalid_intraday:
            with self.subTest(dataset="intraday", query=query), self.assertRaises(
                ValueError
            ):
                _parse_intraday_query(query)
        for query in invalid_valuation:
            with self.subTest(dataset="valuation", query=query), self.assertRaises(
                ValueError
            ):
                _parse_valuation_query(query)
        for query in invalid_news:
            with self.subTest(dataset="news", query=query), self.assertRaises(
                ValueError
            ):
                _parse_news_query(query)


class ResearchEvidenceServiceTests(unittest.TestCase):
    def setUp(self):
        self.config = SimpleNamespace(cache_dir=Path("unused"))
        self.service = DashboardService(self.config)

    def test_service_reads_local_stores_without_provider_access(self):
        intraday = Mock()
        intraday.list.return_value = {"available": True, "bars": []}
        valuation = Mock()
        valuation.list.return_value = {"available": True, "records": []}
        news = Mock()
        news.list.return_value = {"available": True, "records": []}
        intraday_query = IntradayQuery("600000", interval=5)
        valuation_query = ValuationQuery(symbol="600000")
        news_query = NewsQuery(symbol="600000", kind="announcement")

        with patch(
            "ai_trade.web.service.IntradayStore", return_value=intraday
        ), patch(
            "ai_trade.web.service.ValuationStore", return_value=valuation
        ), patch("ai_trade.web.service.NewsStore", return_value=news), patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("read-only service must not use network"),
        ) as urlopen:
            results = (
                self.service.intraday(intraday_query),
                self.service.valuation(valuation_query),
                self.service.news(news_query),
            )

        self.assertTrue(all("generated_at" in item for item in results))
        intraday.list.assert_called_once_with(intraday_query)
        valuation.list.assert_called_once_with(valuation_query)
        news.list.assert_called_once_with(news_query)
        urlopen.assert_not_called()


class ResearchEvidenceHttpTests(unittest.TestCase):
    def test_get_routes_apply_filters_and_reject_bad_queries(self):
        service = _HttpService()
        with _RunningServer(service) as port:
            paths = (
                "/api/intraday?symbol=600000&date=2026-07-17&interval=5&limit=120",
                "/api/valuation?symbol=600000&limit=20",
                "/api/news?symbol=600000&kind=announcement&q=%E9%A3%8E%E9%99%A9&limit=20",
            )
            for path in paths:
                status, payload = _request_json(port, path)
                self.assertEqual(status, 200)
                self.assertTrue(payload["available"])
            for path in (
                "/api/intraday?interval=5",
                "/api/valuation?limit=501",
                "/api/news?kind=sentiment",
            ):
                status, payload = _request_json(port, path)
                self.assertEqual(status, 400)
                self.assertTrue(payload["error"])

        self.assertEqual(
            service.intraday_queries,
            [IntradayQuery("600000", date(2026, 7, 17), 5, 120)],
        )
        self.assertEqual(
            service.valuation_queries, [ValuationQuery(symbol="600000", limit=20)]
        )
        self.assertEqual(
            service.news_queries,
            [NewsQuery(symbol="600000", kind="announcement", q="风险", limit=20)],
        )


class _HttpService:
    config = SimpleNamespace(reports_dir=Path("unused"))

    def __init__(self):
        self.intraday_queries = []
        self.valuation_queries = []
        self.news_queries = []

    def intraday(self, query):
        self.intraday_queries.append(query)
        return {"available": True, "bars": []}

    def valuation(self, query):
        self.valuation_queries.append(query)
        return {"available": True, "records": []}

    def news(self, query):
        self.news_queries.append(query)
        return {"available": True, "records": []}


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
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )

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
