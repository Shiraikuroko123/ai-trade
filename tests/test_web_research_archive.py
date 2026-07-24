import http.client
import json
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from ai_trade.research_archive import ResearchArchiveQuery
from ai_trade.web.auth import Session
from ai_trade.web.server import (
    DashboardServer,
    _handler_factory,
    _parse_research_epoch_list_query,
    _parse_research_archive_query,
)


class _Jobs:
    def close(self):
        pass

    def list(self):
        return []

    def get(self, _job_id):
        return None


class _Users:
    @staticmethod
    def has_users():
        return True


class _Auth:
    users = _Users()

    def __init__(self, sessions):
        self.sessions = sessions

    def authenticate_session(self, token):
        return self.sessions.get(token)


class _Service:
    config = SimpleNamespace(reports_dir=None)

    def __init__(self):
        self.calls = []
        self.epoch_calls = []

    def research_archive(self, **payload):
        self.calls.append(payload)
        return {
            "schema_version": 1,
            "available": True,
            "status": "current",
            "daily": [],
            "weekly": [],
            "snapshots": [],
            "errors": [],
        }

    def research_epochs(self, **payload):
        self.epoch_calls.append(payload)
        return {
            "schema_version": 1,
            "available": True,
            "status": "current",
            "epochs": [],
            "errors": [],
        }


class ResearchArchiveHttpTests(unittest.TestCase):
    def test_query_is_bounded_and_owner_bound(self):
        service = _Service()
        account = "account-" + "a" * 32
        cookie = "b" * 64
        auth = _Auth(
            {
                cookie: _session("alice", "csrf", account),
            }
        )
        with _running_server(service, auth=auth) as port:
            status, payload = _request_json(
                port,
                "GET",
                "/api/research/archive?kind=weekly&week=2026-07-13&limit=5",
                cookie=cookie,
            )

        self.assertEqual(status, 200)
        self.assertTrue(payload["available"])
        self.assertEqual(service.calls[-1]["owner_id"], account)
        self.assertEqual(
            service.calls[-1]["query"],
            ResearchArchiveQuery(
                kind="weekly",
                week_start=date(2026, 7, 13),
                limit=5,
            ),
        )

    def test_month_query_is_canonical_and_owner_bound(self):
        service = _Service()
        account = "account-" + "a" * 32
        cookie = "b" * 64
        auth = _Auth({cookie: _session("alice", "csrf", account)})
        with _running_server(service, auth=auth) as port:
            status, payload = _request_json(
                port,
                "GET",
                "/api/research/archive?kind=monthly&month=2026-07-01&limit=6",
                cookie=cookie,
            )

        self.assertEqual(status, 200)
        self.assertTrue(payload["available"])
        self.assertEqual(
            service.calls[-1]["query"],
            ResearchArchiveQuery(
                kind="monthly",
                month_start=date(2026, 7, 1),
                limit=6,
            ),
        )

    def test_invalid_query_returns_bad_request_without_service_call(self):
        service = _Service()
        with _running_server(service) as port:
            status, payload = _request_json(
                port,
                "GET",
                "/api/research/archive?week=2026-07-14",
            )
        self.assertEqual(status, 400)
        self.assertIn("Monday", payload["error"])
        self.assertEqual(service.calls, [])

    def test_parser_rejects_ambiguous_and_unsupported_values(self):
        self.assertEqual(_parse_research_archive_query(""), ResearchArchiveQuery())
        for query in (
            "kind=unknown",
            "kind=daily&kind=weekly",
            "date=2026-07-17&week=2026-07-13",
            "month=2026-07-02",
            "month=2026-07-01&week=2026-07-13",
            "kind=weekly&month=2026-07-01",
            "limit=0",
            "limit=53",
            "owner=alice",
            "date=2026-7-17",
        ):
            with self.subTest(query=query), self.assertRaises(ValueError):
                _parse_research_archive_query(query)

    def test_old_epoch_routes_are_owner_bound_and_read_only(self):
        service = _Service()
        principal = "account-" + "a" * 32
        cookie = "b" * 64
        auth = _Auth({cookie: _session("alice", "csrf", principal)})
        with _running_server(service, auth=auth) as port:
            list_status, _ = _request_json(
                port,
                "GET",
                "/api/research/epochs?limit=10",
                cookie=cookie,
            )
            detail_status, _ = _request_json(
                port,
                "GET",
                "/api/research/epochs/20260701_120000?kind=monthly&month=2026-06-01",
                cookie=cookie,
            )

        self.assertEqual(list_status, 200)
        self.assertEqual(detail_status, 200)
        self.assertEqual(
            service.epoch_calls[0],
            {"owner_id": principal, "limit": 10},
        )
        self.assertEqual(service.epoch_calls[1]["owner_id"], principal)
        self.assertEqual(service.epoch_calls[1]["epoch_id"], "20260701_120000")
        self.assertEqual(
            service.epoch_calls[1]["query"],
            ResearchArchiveQuery(
                kind="monthly", month_start=date(2026, 6, 1)
            ),
        )

    def test_old_epoch_list_query_is_strictly_bounded(self):
        self.assertEqual(_parse_research_epoch_list_query(""), 50)
        self.assertEqual(_parse_research_epoch_list_query("limit=200"), 200)
        for query in ("owner=alice", "limit=0", "limit=201", "limit=1&limit=2"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                _parse_research_epoch_list_query(query)

    def test_frontend_exposes_text_status_scroll_regions_and_read_only_boundary(self):
        root = Path(__file__).resolve().parents[1]
        javascript = (root / "src/ai_trade/web/assets/app.js").read_text(
            encoding="utf-8"
        )
        stylesheet = (root / "src/ai_trade/web/assets/app.css").read_text(
            encoding="utf-8"
        )

        self.assertIn('id="research-archive-heading"', javascript)
        self.assertIn("逐日收盘归档表，可横向滚动", javascript)
        self.assertIn("周度研究归档表，可横向滚动", javascript)
        self.assertIn("月度研究归档表，可横向滚动", javascript)
        self.assertIn('id="research-epoch-heading"', javascript)
        self.assertIn("旧账期浏览器只读取归档目录", javascript)
        self.assertIn("归档是账本和日志的只读投影", javascript)
        self.assertIn("缺失值不会补零", javascript)
        self.assertIn("价格与市值不回填", javascript)
        self.assertIn("个非交易日记录", javascript)
        self.assertIn(".archive-layout", stylesheet)
        self.assertIn(".archive-table", stylesheet)
        self.assertIn('region.hasAttribute("aria-label")', javascript)
        archive_start = javascript.index("function researchArchives")
        archive_end = javascript.index("function archiveDailyRow", archive_start)
        archive_source = javascript[archive_start:archive_end]
        self.assertNotIn("/api/jobs", archive_source)
        self.assertNotIn("execution_authorized: true", archive_source)
        research_start = javascript.index("function renderResearch")
        research_source = javascript[research_start:]
        self.assertLess(
            research_source.index("${researchArchives(data.archives)}"),
            research_source.index("${researchJournal(data.journal)}"),
        )


class _RunningServer:
    def __init__(self, service, *, auth=None):
        jobs = _Jobs()
        handler = _handler_factory(service, jobs, "local-token", auth, 3600)
        self.server = DashboardServer(("127.0.0.1", 0), handler, jobs)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self.server.server_port

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _running_server(service, *, auth=None):
    return _RunningServer(service, auth=auth)


def _session(username, csrf, principal_id):
    now = time.time()
    return Session(username, now, now + 3600, csrf, "a" * 64, principal_id)


def _request_json(port, method, path, payload=None, *, token=None, cookie=None):
    headers = {"Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token is not None:
        headers["X-AI-Trade-Token"] = token
    if cookie is not None:
        headers["Cookie"] = f"ai_trade_session={cookie}"
    if method != "GET":
        headers["Origin"] = f"http://127.0.0.1:{port}"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    return response.status, json.loads(raw) if raw else {}


if __name__ == "__main__":
    unittest.main()
