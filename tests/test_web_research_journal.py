import http.client
import json
from pathlib import Path
import threading
import time
import unittest
from types import SimpleNamespace

from ai_trade.research_journal import JournalDraft, JournalQuery
from ai_trade.web.auth import Session
from ai_trade.web.server import (
    DashboardServer,
    _handler_factory,
    _parse_research_journal_payload,
    _parse_research_journal_query,
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
        self._sessions = sessions

    def authenticate_session(self, token):
        return self._sessions.get(token)


class _Service:
    config = SimpleNamespace(reports_dir=None)

    def __init__(self):
        self.calls = []

    def research(self, **payload):
        self.calls.append(("list", payload))
        return {"journal": {"entries": []}}

    def append_research_journal(self, **payload):
        self.calls.append(("append", payload))
        return {
            "entry_id": "journal_" + "a" * 32,
            "authority": {"execution_authorized": False},
        }


class ResearchJournalHttpTests(unittest.TestCase):
    def test_local_get_filters_and_csrf_protected_append(self):
        service = _Service()
        token = "local-csrf"
        with _running_server(service, token=token) as port:
            status, payload = _request_json(
                port,
                "GET",
                "/api/research?category=risk&symbol=510300&q=liquidity&limit=20",
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["journal"]["entries"], [])
            query = service.calls[-1][1]["journal_query"]
            self.assertEqual(
                query,
                JournalQuery(
                    category="risk",
                    symbol="510300",
                    query="liquidity",
                    limit=20,
                ),
            )
            self.assertEqual(service.calls[-1][1]["owner_id"], "local-owner")

            body = {
                "research_date": "2026-07-18",
                "category": "decision",
                "symbol": "510300",
                "title": "Closing decision",
                "note": "Wait for another completed session before changing exposure.",
                "decision": "hold",
                "confidence": 70,
                "correction_of": None,
            }
            status, payload = _request_json(
                port,
                "POST",
                "/api/research/journal",
                body,
                token="wrong-token",
            )
            self.assertEqual(status, 403)
            self.assertEqual(len(service.calls), 1)

            status, payload = _request_json(
                port,
                "POST",
                "/api/research/journal",
                body,
                token=token,
            )
            self.assertEqual(status, 201)
            self.assertFalse(payload["authority"]["execution_authorized"])
            append = service.calls[-1][1]
            self.assertEqual(append["owner_id"], "local-owner")
            self.assertEqual(append["actor"], "local-owner")
            self.assertIsInstance(append["draft"], JournalDraft)

    def test_session_identity_is_server_bound_and_payload_cannot_grant_authority(self):
        service = _Service()
        account_id = "acct_" + "3" * 32
        session = _session("alice", "alice-csrf", account_id)
        cookie = "b" * 64
        auth = _Auth({cookie: session})
        body = {
            "research_date": "2026-07-18",
            "category": "observation",
            "symbol": None,
            "title": "Market breadth",
            "note": "Evidence is descriptive and does not authorize execution.",
            "decision": "not_recorded",
            "confidence": None,
        }
        with _running_server(service, auth=auth) as port:
            status, _ = _request_json(
                port,
                "GET",
                "/api/research",
                cookie=cookie,
            )
            self.assertEqual(status, 200)
            self.assertEqual(service.calls[-1][1]["owner_id"], account_id)

            status, _ = _request_json(
                port,
                "POST",
                "/api/research/journal",
                {**body, "execution_authorized": True},
                token="alice-csrf",
                cookie=cookie,
            )
            self.assertEqual(status, 400)

            status, _ = _request_json(
                port,
                "POST",
                "/api/research/journal",
                body,
                token="alice-csrf",
                cookie=cookie,
            )
            self.assertEqual(status, 201)
            call = service.calls[-1][1]
            self.assertEqual(call["owner_id"], account_id)
            self.assertEqual(call["actor"], "alice")

    def test_utf8_note_within_character_limit_is_not_rejected_by_byte_cap(self):
        service = _Service()
        token = "local-csrf"
        note = "汉" * 4_000
        body = {
            "research_date": "2026-07-18",
            "category": "observation",
            "symbol": "510300",
            "title": "收盘观察",
            "note": note,
            "decision": "not_recorded",
            "confidence": None,
        }
        self.assertGreater(len(json.dumps(body).encode("utf-8")), 8_192)

        with _running_server(service, token=token) as port:
            status, _ = _request_json(
                port,
                "POST",
                "/api/research/journal",
                body,
                token=token,
            )

        self.assertEqual(status, 201)
        self.assertEqual(service.calls[-1][1]["draft"].note, note)

    def test_query_and_payload_contracts_reject_ambiguous_values(self):
        self.assertEqual(_parse_research_journal_query(""), JournalQuery())
        for query in (
            "category=unknown",
            "category=risk&category=decision",
            "symbol=%20bad",
            "q=",
            "limit=0",
            "limit=201",
            "owner=alice",
        ):
            with self.subTest(query=query), self.assertRaises(ValueError):
                _parse_research_journal_query(query)

        valid = {
            "research_date": "2026-07-18",
            "category": "risk",
            "symbol": "510300",
            "title": "Risk review",
            "note": "No order is authorized by this research entry.",
            "decision": "consider_reduce",
            "confidence": 55,
            "correction_of": "journal_" + "c" * 32,
        }
        draft = _parse_research_journal_payload(valid)
        self.assertEqual(draft.category, "risk")
        self.assertEqual(draft.confidence, 55)
        invalid = [
            {**valid, "confidence": True},
            {**valid, "confidence": 101},
            {**valid, "category": "trade"},
            {**valid, "research_date": "2026-7-18"},
            {**valid, "correction_of": "journal_bad"},
            {**valid, "owner_id": "alice"},
        ]
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                _parse_research_journal_payload(payload)

    def test_frontend_contract_keeps_journal_accessible_and_non_executing(self):
        asset = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "ai_trade"
            / "web"
            / "assets"
            / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn('id="research-journal-form"', asset)
        self.assertIn('id="research-journal-status"', asset)
        self.assertIn('role="status"', asset)
        self.assertIn('/api/research/journal', asset)
        self.assertIn("不会改变策略、模拟账户、订单或券商权限", asset)
        self.assertNotIn("execution_authorized: true", asset)
        append_start = asset.index("async function appendResearchJournal")
        busy_index = asset.index("state.journalBusy = false;", append_start)
        refresh_index = asset.index("await reloadResearch();", busy_index)
        self.assertLess(busy_index, refresh_index)


class _RunningServer:
    def __init__(self, service, *, token="local-token", auth=None):
        self.jobs = _Jobs()
        handler = _handler_factory(service, self.jobs, token, auth, 3600)
        self.server = DashboardServer(("127.0.0.1", 0), handler, self.jobs)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self.server.server_port

    def __exit__(self, *_args):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)


def _running_server(service, *, token="local-token", auth=None):
    return _RunningServer(service, token=token, auth=auth)


def _session(username, csrf, account_id):
    now = time.time()
    return Session(username, now, now + 3600, csrf, "a" * 64, account_id)


def _request_json(
    port,
    method,
    path,
    payload=None,
    *,
    token=None,
    cookie=None,
):
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
