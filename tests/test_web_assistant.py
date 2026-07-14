import http.client
import json
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ai_trade.web.auth import Session
from ai_trade.web.server import (
    DashboardServer,
    _handler_factory,
    _parse_assistant_analyze_payload,
)
from ai_trade.web.service import DashboardService


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

    def logout(self, _token):
        return True


class _Service:
    config = SimpleNamespace(reports_dir=None)

    def __init__(self):
        self.read_users = []
        self.analyze_calls = []
        self.analysis_error = None

    def assistant(self, *, user_id):
        self.read_users.append(user_id)
        return {
            "status": {"local_available": True},
            "instruments": [{"symbol": "510300", "name": "沪深300ETF"}],
            "defaults": {"symbol": "510300", "lookback": 180, "mode": "local"},
            "history": [{"owner": user_id}],
        }

    def assistant_analyze(self, **payload):
        if self.analysis_error is not None:
            raise self.analysis_error
        self.analyze_calls.append(payload)
        return {"analysis_id": "a" * 32, "owner": payload["user_id"]}


class AssistantServiceTests(unittest.TestCase):
    def test_service_exposes_available_instruments_defaults_and_user_history(self):
        config = SimpleNamespace(
            instruments=(
                SimpleNamespace(symbol="510300", name="沪深300ETF"),
                SimpleNamespace(symbol="missing", name="缺少缓存"),
            ),
            strategy=SimpleNamespace(benchmark="510300"),
        )
        market = SimpleNamespace(symbols={"510300": object()})
        engine = MagicMock()
        engine.status.return_value = {"local_available": True}
        engine.history.return_value = [{"analysis_id": "b" * 32}]
        engine.analyze.return_value = {"analysis_id": "c" * 32}

        with patch("ai_trade.web.service.AssistantEngine", return_value=engine) as cls:
            service = DashboardService(config)
            service.market = MagicMock(return_value=market)
            status = service.assistant(user_id="alice")
            result = service.assistant_analyze(
                symbol="510300",
                lookback=180,
                mode="local",
                user_id="alice",
            )

        self.assertEqual(
            status["instruments"], [{"symbol": "510300", "name": "沪深300ETF"}]
        )
        self.assertEqual(
            status["defaults"],
            {"symbol": "510300", "lookback": 180, "mode": "local"},
        )
        self.assertEqual(status["history"], [{"analysis_id": "b" * 32}])
        self.assertEqual(result, {"analysis_id": "c" * 32})
        cls.assert_called_once_with(config)
        engine.history.assert_called_once_with("alice", limit=20)
        engine.analyze.assert_called_once_with(
            market,
            "510300",
            lookback=180,
            mode="local",
            user_id="alice",
        )


class AssistantHttpTests(unittest.TestCase):
    def test_local_owner_read_and_write_use_fixed_identity(self):
        service = _Service()
        token = "local-csrf-token"
        with _running_server(service, token=token) as port:
            status, body = _request(port, "GET", "/api/assistant")
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["history"], [{"owner": "local-owner"}])

            status, _ = _request(port, "GET", "/api/assistant?user_id=someone-else")
            self.assertEqual(status, 400)

            status, body = _request_json(
                port,
                "/api/assistant/analyze",
                {"symbol": "510300", "lookback": 180, "mode": "local"},
                headers={
                    "Origin": _origin(port),
                    "X-AI-Trade-Token": token,
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["owner"], "local-owner")
        self.assertEqual(service.read_users, ["local-owner"])
        self.assertEqual(service.analyze_calls[0]["user_id"], "local-owner")

    def test_authenticated_requests_require_login_csrf_and_isolate_users(self):
        service = _Service()
        alice_token = "a" * 32
        bob_token = "b" * 32
        alice_account_id = "acct_" + "1" * 32
        bob_account_id = "acct_" + "2" * 32
        sessions = {
            alice_token: _session("alice", "alice-csrf", alice_account_id),
            bob_token: _session("bob", "bob-csrf", bob_account_id),
        }
        with _running_server(service, auth=_Auth(sessions)) as port:
            status, _ = _request(port, "GET", "/api/assistant")
            self.assertEqual(status, 401)
            status, _ = _request_json(
                port,
                "/api/assistant/analyze",
                {"symbol": "510300"},
                headers={
                    "Origin": _origin(port),
                    "X-AI-Trade-Token": "alice-csrf",
                },
            )
            self.assertEqual(status, 401)

            for token, owner_id in (
                (alice_token, alice_account_id),
                (bob_token, bob_account_id),
            ):
                status, body = _request(
                    port,
                    "GET",
                    "/api/assistant",
                    headers={"Cookie": f"ai_trade_session={token}"},
                )
                self.assertEqual(status, 200)
                self.assertEqual(json.loads(body)["history"], [{"owner": owner_id}])

            status, _ = _request_json(
                port,
                "/api/assistant/analyze",
                {"symbol": "510300"},
                headers={
                    "Cookie": f"ai_trade_session={alice_token}",
                    "Origin": _origin(port),
                    "X-AI-Trade-Token": "wrong",
                },
            )
            self.assertEqual(status, 403)

            status, body = _request_json(
                port,
                "/api/assistant/analyze",
                {"symbol": "510300", "user_id": "bob"},
                headers={
                    "Cookie": f"ai_trade_session={alice_token}",
                    "Origin": _origin(port),
                    "X-AI-Trade-Token": "alice-csrf",
                },
            )
            self.assertEqual(status, 400)
            self.assertIn("user_id", json.loads(body)["error"])

            status, body = _request_json(
                port,
                "/api/assistant/analyze",
                {"symbol": "510300", "mode": "model"},
                headers={
                    "Cookie": f"ai_trade_session={alice_token}",
                    "Origin": _origin(port),
                    "X-AI-Trade-Token": "alice-csrf",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(json.loads(body)["owner"], alice_account_id)

        self.assertEqual(service.read_users, [alice_account_id, bob_account_id])
        self.assertEqual(len(service.analyze_calls), 1)
        self.assertEqual(service.analyze_calls[0]["user_id"], alice_account_id)

    def test_same_origin_validation_and_error_mapping(self):
        service = _Service()
        token = "local-csrf-token"
        with _running_server(service, token=token) as port:
            status, _ = _request_json(
                port,
                "/api/assistant/analyze",
                {"symbol": "510300"},
                headers={
                    "Origin": "http://example.com",
                    "X-AI-Trade-Token": token,
                },
            )
            self.assertEqual(status, 403)

            service.analysis_error = ValueError("bad analysis")
            status, _ = _request_json(
                port,
                "/api/assistant/analyze",
                {"symbol": "510300"},
                headers={
                    "Origin": _origin(port),
                    "X-AI-Trade-Token": token,
                },
            )
            self.assertEqual(status, 400)

            service.analysis_error = RuntimeError("model unavailable")
            status, _ = _request_json(
                port,
                "/api/assistant/analyze",
                {"symbol": "510300"},
                headers={
                    "Origin": _origin(port),
                    "X-AI-Trade-Token": token,
                },
            )
            self.assertEqual(status, 503)

    def test_analysis_payload_is_strict(self):
        self.assertEqual(
            _parse_assistant_analyze_payload({"symbol": "510300"}),
            ("510300", 180, "local"),
        )
        for payload in (
            {},
            {"symbol": " 510300"},
            {"symbol": []},
            {"symbol": "510300", "lookback": True},
            {"symbol": "510300", "lookback": "180"},
            {"symbol": "510300", "lookback": 59},
            {"symbol": "510300", "lookback": 501},
            {"symbol": "510300", "mode": "ai"},
            {"symbol": "510300", "extra": "value"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                _parse_assistant_analyze_payload(payload)


def _session(username, csrf, account_id):
    now = time.time()
    return Session(username, now, now + 3600, csrf, "a" * 64, account_id)


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


def _origin(port):
    return f"http://127.0.0.1:{port}"


def _request_json(port, path, payload, headers=None):
    return _request(
        port,
        "POST",
        path,
        body=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", **(headers or {})},
    )


def _request(port, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    result = response.status, response.read()
    connection.close()
    return result


if __name__ == "__main__":
    unittest.main()
