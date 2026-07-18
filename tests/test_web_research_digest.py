import http.client
import json
import threading
import time
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from ai_trade.research_digest import ResearchDigestCapacityError, ResearchDigestQuery
from ai_trade.web.auth import Session
from ai_trade.web.server import (
    DashboardServer,
    _handler_factory,
    _parse_research_digest_generate_payload,
    _parse_research_digest_query,
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

    def __init__(self, *, fail=False):
        self.digest_queries = []
        self.generations = []
        self.fail = fail

    def research_digests(self, **payload):
        self.digest_queries.append(payload)
        return {
            "schema_version": 1,
            "available": True,
            "status": "current",
            "digests": [],
            "summary": {},
            "errors": [],
        }

    def generate_research_digests(self, **payload):
        self.generations.append(payload)
        if self.fail:
            raise ResearchDigestCapacityError("digest capacity reached")
        return {
            "schema_version": 1,
            "available": True,
            "status": "current",
            "summary": {"written": 1, "reused": 0},
            "writes": [],
            "authority": {"research_only": True, "execution_authorized": False},
        }


class ResearchDigestHttpTests(unittest.TestCase):
    def test_query_parser_is_bounded_and_strict(self):
        self.assertEqual(_parse_research_digest_query(""), ResearchDigestQuery())
        self.assertEqual(
            _parse_research_digest_query("kind=all"), ResearchDigestQuery()
        )
        self.assertEqual(
            _parse_research_digest_query("date=2026-07-17"),
            ResearchDigestQuery(kind="daily", period_start=date(2026, 7, 17)),
        )
        self.assertEqual(
            _parse_research_digest_query("week=2026-07-13"),
            ResearchDigestQuery(kind="weekly", period_start=date(2026, 7, 13)),
        )
        self.assertEqual(
            _parse_research_digest_query(
                "kind=weekly&week=2026-07-13&limit=5&revisions=true"
            ),
            ResearchDigestQuery(
                kind="weekly",
                period_start=date(2026, 7, 13),
                limit=5,
                include_revisions=True,
            ),
        )
        for query in (
            "kind=unknown",
            "kind=daily&kind=weekly",
            "date=2026-07-17&week=2026-07-13",
            "kind=weekly&date=2026-07-17",
            "kind=daily&week=2026-07-13",
            "kind=all&date=2026-07-17",
            "kind=all&week=2026-07-13",
            "week=2026-07-14",
            "limit=0",
            "limit=201",
            "revisions=maybe",
            "owner=alice",
        ):
            with self.subTest(query=query), self.assertRaises(ValueError):
                _parse_research_digest_query(query)

    def test_get_rejects_conflicting_period_kind_before_service(self):
        service = _Service()
        cookie = "b" * 64
        principal = "acct_" + "a" * 32
        auth = _Auth({cookie: _session("alice", "csrf-token", principal)})
        with _running_server(service, auth=auth) as port:
            status, payload = _request_json(
                port,
                "GET",
                "/api/research/digests?kind=weekly&date=2026-07-17",
                cookie=cookie,
            )

        self.assertEqual(status, 400)
        self.assertIn("kind must be daily", payload["error"])
        self.assertEqual(service.digest_queries, [])

    def test_generation_payload_parser_rejects_ambiguous_scope(self):
        self.assertEqual(
            _parse_research_digest_generate_payload(
                {"kind": "weekly", "week": "2026-07-13"}
            ),
            ("weekly", None, date(2026, 7, 13), "manual"),
        )
        with self.assertRaisesRegex(ValueError, "Unsupported.*trigger"):
            _parse_research_digest_generate_payload(
                {"kind": "daily", "trigger": "scheduled"}
            )
        with self.assertRaisesRegex(ValueError, "Monday"):
            _parse_research_digest_generate_payload(
                {"kind": "weekly", "week": "2026-07-14"}
            )
        with self.assertRaises(ValueError):
            _parse_research_digest_generate_payload(
                {"date": "2026-07-17", "week": "2026-07-13"}
            )
        self.assertEqual(
            _parse_research_digest_generate_payload({"date": "2026-07-17"}),
            ("daily", date(2026, 7, 17), None, "manual"),
        )
        with self.assertRaisesRegex(ValueError, "kind must be daily"):
            _parse_research_digest_generate_payload(
                {"kind": "all", "date": "2026-07-17"}
            )

    def test_get_is_owner_bound_and_post_requires_csrf(self):
        service = _Service()
        cookie = "b" * 64
        principal = "acct_" + "a" * 32
        auth = _Auth({cookie: _session("alice", "csrf-token", principal)})
        with _running_server(service, auth=auth) as port:
            status, _payload = _request_json(
                port,
                "GET",
                "/api/research/digests?kind=daily&date=2026-07-17",
                cookie=cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(service.digest_queries[-1]["owner_id"], principal)
            self.assertEqual(
                service.digest_queries[-1]["query"],
                ResearchDigestQuery(kind="daily", period_start=date(2026, 7, 17)),
            )

            status, payload = _request_json(
                port,
                "POST",
                "/api/research/digests/generate",
                {"kind": "daily", "date": "2026-07-17"},
                cookie=cookie,
            )
            self.assertEqual(status, 403)
            self.assertIn("token", payload["error"].lower())
            self.assertEqual(service.generations, [])

            status, _payload = _request_json(
                port,
                "POST",
                "/api/research/digests/generate",
                {"kind": "daily", "date": "2026-07-17"},
                cookie=cookie,
                token="csrf-token",
            )
            self.assertEqual(status, 201)
            self.assertEqual(service.generations[-1]["owner_id"], principal)
            self.assertEqual(service.generations[-1]["trigger"], "manual")

    def test_capacity_is_reported_as_conflict(self):
        service = _Service(fail=True)
        cookie = "b" * 64
        auth = _Auth({cookie: _session("alice", "csrf-token", "acct_" + "a" * 32)})
        with _running_server(service, auth=auth) as port:
            status, payload = _request_json(
                port,
                "POST",
                "/api/research/digests/generate",
                {},
                cookie=cookie,
                token="csrf-token",
            )
        self.assertEqual(status, 409)
        self.assertIn("capacity", payload["error"])

    def test_frontend_mounts_digest_states_and_read_only_boundary(self):
        root = Path(__file__).resolve().parents[1]
        javascript = (root / "src/ai_trade/web/assets/app.js").read_text(
            encoding="utf-8"
        )
        stylesheet = (root / "src/ai_trade/web/assets/app.css").read_text(
            encoding="utf-8"
        )

        self.assertIn("${researchDigests(data.digests)}", javascript)
        self.assertIn("data-research-digest-generate", javascript)
        self.assertIn("generateResearchDigests(researchDigestGenerate)", javascript)
        self.assertIn("function restoreFocusAfterRender", javascript)
        self.assertIn(
            'restoreFocusAfterRender("[data-research-digest-generate]", "research")',
            javascript,
        )
        self.assertIn("正在从本地日报、账本和研究日志建立不可变版本", javascript)
        self.assertIn("暂时无法读取持久化归档", javascript)
        self.assertIn("尚无持久化日报或周报", javascript)
        self.assertIn('provisional: ["本周未收完", "warning"]', javascript)
        self.assertIn("归档仅部分完成", javascript)
        self.assertIn("归档写入完成", javascript)
        self.assertIn('scheduled: "标记为定时"', javascript)
        self.assertIn('state.researchDigestStatusKind = partial', javascript)
        self.assertIn("含周内暂存", javascript)
        self.assertIn("列表刷新失败，请重新读取页面确认", javascript)
        self.assertIn("不会创建订单或开放实盘权限", javascript)
        self.assertNotIn('JSON.stringify({ kind: "all", trigger:', javascript)
        self.assertIn(".digest-timeline", stylesheet)
        self.assertIn(".digest-evidence code", stylesheet)


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
