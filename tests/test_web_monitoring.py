import http.client
import json
import threading
import time
from types import SimpleNamespace
import unittest

from ai_trade.monitoring import MonitoringConflictError
from ai_trade.web.auth import Session
from ai_trade.web.server import (
    DashboardServer,
    _handler_factory,
    _parse_monitoring_alert_action_payload,
    _parse_monitoring_rule_payload,
    _parse_monitoring_watchlist_payload,
)


WATCHLIST_ID = "watch_" + "a" * 32
RULE_ID = "rule_" + "b" * 32
ALERT_ID = "alert_" + "c" * 32


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

    def __init__(self, *, conflict=False, alert_conflict=False):
        self.calls = []
        self.conflict = conflict
        self.alert_conflict = alert_conflict

    def _record(self, operation, **payload):
        self.calls.append((operation, payload))
        return {"operation": operation, "authority": {"execution_authorized": False}}

    def monitoring(self, **payload):
        return self._record("status", **payload)

    def monitoring_create_watchlist(self, **payload):
        if self.conflict:
            raise MonitoringConflictError("monitoring configuration changed")
        return self._record("create_watchlist", **payload)

    def monitoring_watchlist_action(self, **payload):
        return self._record("watchlist_action", **payload)

    def monitoring_create_rule(self, **payload):
        return self._record("create_rule", **payload)

    def monitoring_rule_action(self, **payload):
        return self._record("rule_action", **payload)

    def monitoring_scan(self, **payload):
        return self._record("scan", **payload)

    def monitoring_alert_action(self, **payload):
        if self.alert_conflict:
            raise MonitoringConflictError("alert state changed; reload before writing")
        return self._record("alert_action", **payload)


class MonitoringHttpTests(unittest.TestCase):
    def test_local_status_and_mutations_use_local_owner(self):
        service = _Service()
        csrf = "local-csrf"
        with _running_server(service, token=csrf) as port:
            status, payload = _request_json(port, "GET", "/api/monitoring")
            self.assertEqual(status, 200)
            self.assertEqual(payload["operation"], "status")
            self.assertEqual(service.calls[-1], ("status", {"owner_id": "local-owner"}))

            status, _ = _request_json(
                port,
                "POST",
                "/api/monitoring/watchlist",
                {"action": "create", "name": "Core", "expected_revision": 0},
            )
            self.assertEqual(status, 403)
            self.assertEqual(len(service.calls), 1)

            status, payload = _request_json(
                port,
                "POST",
                "/api/monitoring/watchlist",
                {"action": "create", "name": "Core", "expected_revision": 0},
                token=csrf,
            )
            self.assertEqual(status, 201)
            self.assertEqual(payload["operation"], "create_watchlist")
            call = service.calls[-1][1]
            self.assertEqual(call["owner_id"], "local-owner")
            self.assertEqual(call["actor"], "local-owner")

            status, _ = _request_json(
                port,
                "POST",
                "/api/monitoring/scan",
                {"force": True},
                token=csrf,
            )
            self.assertEqual(status, 400)

    def test_authenticated_owner_and_actor_are_session_bound(self):
        service = _Service()
        alice_cookie = "a" * 64
        bob_cookie = "b" * 64
        alice_id = "acct_" + "1" * 32
        bob_id = "acct_" + "2" * 32
        auth = _Auth(
            {
                alice_cookie: _session("alice", "alice-csrf", alice_id),
                bob_cookie: _session("bob", "bob-csrf", bob_id),
            }
        )
        with _running_server(service, auth=auth) as port:
            for cookie, owner_id in ((alice_cookie, alice_id), (bob_cookie, bob_id)):
                status, _ = _request_json(port, "GET", "/api/monitoring", cookie=cookie)
                self.assertEqual(status, 200)
                self.assertEqual(service.calls[-1][1]["owner_id"], owner_id)

            body = {
                "action": "add_symbol",
                "watchlist_id": WATCHLIST_ID,
                "symbol": "510300",
                "expected_revision": 1,
            }
            status, _ = _request_json(
                port,
                "POST",
                "/api/monitoring/watchlist",
                {**body, "owner_id": bob_id, "actor": "admin"},
                token="alice-csrf",
                cookie=alice_cookie,
            )
            self.assertEqual(status, 400)

            status, payload = _request_json(
                port,
                "POST",
                "/api/monitoring/watchlist",
                body,
                token="alice-csrf",
                cookie=alice_cookie,
            )
            self.assertEqual(status, 200)
            self.assertFalse(payload["authority"]["execution_authorized"])
            call = service.calls[-1][1]
            self.assertEqual(call["owner_id"], alice_id)
            self.assertEqual(call["actor"], "alice")
            self.assertNotIn("principal_id", call)

    def test_authenticated_write_requires_session_bound_csrf_and_same_origin(self):
        service = _Service()
        cookie = "a" * 64
        account_id = "acct_" + "3" * 32
        auth = _Auth({cookie: _session("alice", "alice-csrf", account_id)})
        body = {"action": "create", "name": "Core", "expected_revision": 0}
        with _running_server(service, auth=auth) as port:
            status, _ = _request_json(
                port,
                "POST",
                "/api/monitoring/watchlist",
                body,
                token="alice-csrf",
            )
            self.assertEqual(status, 401)
            status, _ = _request_json(
                port,
                "POST",
                "/api/monitoring/watchlist",
                body,
                token="wrong-csrf",
                cookie=cookie,
            )
            self.assertEqual(status, 403)
            status, _ = _request_json(
                port,
                "POST",
                "/api/monitoring/watchlist",
                body,
                token="alice-csrf",
                cookie=cookie,
                origin="https://attacker.invalid",
            )
            self.assertEqual(status, 403)
            self.assertEqual(service.calls, [])

    def test_query_and_payloads_are_strict(self):
        service = _Service()
        with _running_server(service, token="csrf") as port:
            status, _ = _request_json(
                port,
                "GET",
                "/api/monitoring?owner_id=other",
            )
            self.assertEqual(status, 400)
            self.assertEqual(service.calls, [])

            status, _ = _request_json(
                port,
                "POST",
                "/api/monitoring/alerts/not-an-alert/actions",
                {"action": "acknowledge"},
                token="csrf",
            )
            self.assertEqual(status, 404)

        with self.assertRaises(ValueError):
            _parse_monitoring_watchlist_payload(
                {"action": "create", "name": "Core", "owner": "other"}
            )
        with self.assertRaises(ValueError):
            _parse_monitoring_watchlist_payload(
                {"action": "rename", "watchlist_id": WATCHLIST_ID, "name": " Core "}
            )
        with self.assertRaises(ValueError):
            _parse_monitoring_rule_payload(
                {
                    "action": "create",
                    "watchlist_id": WATCHLIST_ID,
                    "symbol": "510300",
                    "rule_type": "close_above",
                    "threshold": float("nan"),
                }
            )
        with self.assertRaises(ValueError):
            _parse_monitoring_rule_payload(
                {
                    "action": "delete",
                    "rule_id": RULE_ID,
                    "threshold": 10,
                }
            )
        with self.assertRaises(ValueError):
            _parse_monitoring_alert_action_payload(
                {"action": "snooze", "snooze_until": "2026-7-31"}
            )
        with self.assertRaises(ValueError):
            _parse_monitoring_alert_action_payload(
                {"action": "dismiss", "snooze_until": "2026-07-31"}
            )
        parsed = _parse_monitoring_alert_action_payload(
            {
                "action": "acknowledge",
                "expected_state_fingerprint": "d" * 64,
            }
        )
        self.assertEqual(parsed, ("acknowledge", "", None, "d" * 64))

    def test_revision_conflict_maps_to_http_409(self):
        service = _Service(conflict=True)
        with _running_server(service, token="csrf") as port:
            status, payload = _request_json(
                port,
                "POST",
                "/api/monitoring/watchlist",
                {"action": "create", "name": "Core", "expected_revision": 0},
                token="csrf",
            )
        self.assertEqual(status, 409)
        self.assertIn("configuration changed", payload["error"])

    def test_web_configuration_writes_require_revision_cas(self):
        service = _Service()
        with _running_server(service, token="csrf") as port:
            status, payload = _request_json(
                port,
                "POST",
                "/api/monitoring/watchlist",
                {"action": "create", "name": "Core"},
                token="csrf",
            )
            self.assertEqual(status, 400)
            self.assertIn("expected_revision is required", payload["error"])

            status, payload = _request_json(
                port,
                "POST",
                "/api/monitoring/rules",
                {
                    "action": "create",
                    "watchlist_id": WATCHLIST_ID,
                    "symbol": "510300",
                    "rule_type": "close_above",
                },
                token="csrf",
            )
            self.assertEqual(status, 400)
            self.assertIn("expected_revision is required", payload["error"])
        self.assertEqual(service.calls, [])

    def test_web_alert_writes_require_state_fingerprint(self):
        service = _Service()
        with _running_server(service, token="csrf") as port:
            status, payload = _request_json(
                port,
                "POST",
                f"/api/monitoring/alerts/{ALERT_ID}/actions",
                {"action": "acknowledge"},
                token="csrf",
            )
        self.assertEqual(status, 400)
        self.assertIn("expected_state_fingerprint is required", payload["error"])
        self.assertEqual(service.calls, [])

    def test_rule_scan_and_alert_routes_forward_only_validated_fields(self):
        service = _Service()
        with _running_server(service, token="csrf") as port:
            status, _ = _request_json(
                port,
                "POST",
                "/api/monitoring/rules",
                {
                    "action": "create",
                    "expected_revision": 2,
                    "watchlist_id": WATCHLIST_ID,
                    "symbol": "510300",
                    "rule_type": "close_above",
                    "threshold": 4.5,
                    "cooldown_sessions": 2,
                    "severity": "critical",
                    "enabled": True,
                },
                token="csrf",
            )
            self.assertEqual(status, 201)
            rule_call = service.calls[-1][1]
            self.assertEqual(rule_call["rule"]["threshold"], 4.5)
            self.assertEqual(rule_call["owner_id"], "local-owner")

            status, _ = _request_json(
                port,
                "POST",
                "/api/monitoring/scan",
                {},
                token="csrf",
            )
            self.assertEqual(status, 201)
            self.assertEqual(service.calls[-1][0], "scan")

            status, _ = _request_json(
                port,
                "POST",
                f"/api/monitoring/alerts/{ALERT_ID}/actions",
                {
                    "action": "snooze",
                    "note": "Review later",
                    "snooze_until": "2026-07-31",
                    "expected_state_fingerprint": "d" * 64,
                },
                token="csrf",
            )
            self.assertEqual(status, 200)
            alert_call = service.calls[-1][1]
            self.assertEqual(alert_call["alert_id"], ALERT_ID)
            self.assertEqual(alert_call["actor"], "local-owner")
            self.assertEqual(alert_call["snooze_until"], "2026-07-31")
            self.assertEqual(alert_call["expected_state_fingerprint"], "d" * 64)

    def test_alert_state_conflict_maps_to_http_409(self):
        service = _Service(alert_conflict=True)
        with _running_server(service, token="csrf") as port:
            status, payload = _request_json(
                port,
                "POST",
                f"/api/monitoring/alerts/{ALERT_ID}/actions",
                {
                    "action": "dismiss",
                    "expected_state_fingerprint": "e" * 64,
                },
                token="csrf",
            )
        self.assertEqual(status, 409)
        self.assertIn("alert state changed", payload["error"])


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
    origin=None,
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
        headers["Origin"] = origin or f"http://127.0.0.1:{port}"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    connection.request(method, path, body=body, headers=headers)
    response = connection.getresponse()
    raw = response.read()
    connection.close()
    return response.status, json.loads(raw) if raw else {}


if __name__ == "__main__":
    unittest.main()
