from __future__ import annotations

from datetime import date
import http.client
import json
from pathlib import Path
import shutil
from types import SimpleNamespace
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from ai_trade.data.disclosures import DisclosureQuery
from ai_trade.data.fundamentals import FundamentalQuery
from ai_trade.data.order_book import OrderBookQuery
from ai_trade.config import load_config
from ai_trade.web.server import (
    DashboardServer,
    _handler_factory,
    _parse_disclosures_query,
    _parse_fundamentals_query,
    _parse_order_book_query,
)
from ai_trade.web.service import DashboardService


class EvidenceV015QueryTests(unittest.TestCase):
    def test_valid_queries_are_bounded(self):
        self.assertEqual(_parse_fundamentals_query(""), FundamentalQuery())
        self.assertEqual(
            _parse_fundamentals_query(
                "date=2026-07-17&symbol=600000&limit=20&revisions=1"
            ),
            FundamentalQuery(date(2026, 7, 17), "600000", 20, True),
        )
        self.assertEqual(_parse_disclosures_query(""), DisclosureQuery())
        self.assertEqual(
            _parse_disclosures_query(
                "date=2026-07-17&symbol=600000&provider=sse"
                "&q=report&limit=20&revisions=1"
            ),
            DisclosureQuery(
                date(2026, 7, 17), "600000", "sse", "report", 20, True
            ),
        )
        self.assertEqual(_parse_order_book_query(""), OrderBookQuery())
        self.assertEqual(
            _parse_order_book_query(
                "date=2026-07-17&symbol=600000&limit=20&revisions=1"
            ),
            OrderBookQuery(date(2026, 7, 17), "600000", 20, True),
        )

    def test_invalid_queries_are_rejected(self):
        invalid = (
            (_parse_fundamentals_query, "limit=501"),
            (_parse_fundamentals_query, "symbol=60000A"),
            (_parse_disclosures_query, "provider=eastmoney"),
            (_parse_disclosures_query, "q="),
            (_parse_order_book_query, "revisions=yes"),
            (_parse_order_book_query, "unknown=1"),
        )
        for parser, query in invalid:
            with self.subTest(parser=parser.__name__, query=query), self.assertRaises(
                ValueError
            ):
                parser(query)


class EvidenceV015ConfigTests(unittest.TestCase):
    def test_new_evidence_roots_are_bounded_state_children(self):
        repository = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            shutil.copytree(repository / "config", root / "config")
            path = root / "config" / "default.json"
            config = load_config(path)
            self.assertEqual(config.fundamentals_dir, root / "state" / "fundamentals")
            self.assertEqual(config.disclosures_dir, root / "state" / "disclosures")
            self.assertEqual(config.order_book_dir, root / "state" / "order_book")

            baseline = json.loads(path.read_text(encoding="utf-8"))
            for section in ("fundamentals", "disclosures", "order_book"):
                with self.subTest(section=section):
                    current = dict(baseline)
                    current[section] = {"root_dir": "outside-state"}
                    path.write_text(json.dumps(current), encoding="utf-8")
                    with self.assertRaisesRegex(ValueError, "workspace state"):
                        load_config(path)


class EvidenceV015ServiceTests(unittest.TestCase):
    def test_service_reads_only_local_stores(self):
        service = DashboardService(SimpleNamespace(cache_dir=Path("unused")))
        fundamentals = Mock()
        fundamentals.list.return_value = {"available": True, "records": []}
        disclosures = Mock()
        disclosures.list.return_value = {"available": True, "records": []}
        order_book = Mock()
        order_book.list.return_value = {"available": True, "records": []}
        queries = (
            FundamentalQuery(symbol="600000"),
            DisclosureQuery(symbol="600000"),
            OrderBookQuery(symbol="600000"),
        )
        with patch(
            "ai_trade.web.service.FundamentalStore", return_value=fundamentals
        ), patch(
            "ai_trade.web.service.DisclosureStore", return_value=disclosures
        ), patch(
            "ai_trade.web.service.OrderBookStore", return_value=order_book
        ), patch(
            "urllib.request.urlopen",
            side_effect=AssertionError("read-only service must not use network"),
        ) as urlopen:
            results = (
                service.fundamentals(queries[0]),
                service.disclosures(queries[1]),
                service.order_book(queries[2]),
            )
        self.assertTrue(all("generated_at" in item for item in results))
        fundamentals.list.assert_called_once_with(queries[0])
        disclosures.list.assert_called_once_with(queries[1])
        order_book.list.assert_called_once_with(queries[2])
        urlopen.assert_not_called()


class EvidenceV015HttpTests(unittest.TestCase):
    def test_read_only_routes_apply_filters(self):
        service = _HttpService()
        with _RunningServer(service) as port:
            paths = (
                "/api/fundamentals?symbol=600000&limit=20",
                "/api/disclosures?symbol=600000&provider=sse&limit=20",
                "/api/order-book?symbol=600000&limit=20",
            )
            for path in paths:
                status, payload = _request_json(port, path)
                self.assertEqual(status, 200)
                self.assertTrue(payload["available"])
            for path in (
                "/api/fundamentals?limit=501",
                "/api/disclosures?provider=eastmoney",
                "/api/order-book?limit=501",
            ):
                status, payload = _request_json(port, path)
                self.assertEqual(status, 400)
                self.assertTrue(payload["error"])
        self.assertEqual(
            service.fundamental_queries,
            [FundamentalQuery(symbol="600000", limit=20)],
        )
        self.assertEqual(
            service.disclosure_queries,
            [DisclosureQuery(symbol="600000", provider="sse", limit=20)],
        )
        self.assertEqual(
            service.order_book_queries,
            [OrderBookQuery(symbol="600000", limit=20)],
        )


class _HttpService:
    config = SimpleNamespace(reports_dir=Path("unused"))

    def __init__(self):
        self.fundamental_queries = []
        self.disclosure_queries = []
        self.order_book_queries = []

    def fundamentals(self, query):
        self.fundamental_queries.append(query)
        return {"available": True, "records": []}

    def disclosures(self, query):
        self.disclosure_queries.append(query)
        return {"available": True, "records": []}

    def order_book(self, query):
        self.order_book_queries.append(query)
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
