import http.client
import json
from pathlib import Path
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from ai_trade.strategy_lab import (
    StrategyLabCapacityError,
    StrategyLabConflictError,
)
from ai_trade.web.auth import Session
from ai_trade.web.server import (
    DashboardServer,
    _handler_factory,
    _parse_strategy_lab_confirmation_payload,
    _parse_strategy_lab_lifecycle_payload,
    _parse_strategy_lab_manual_payload,
    _parse_strategy_lab_proposal_payload,
    _parse_strategy_lab_rollback_payload,
)
from ai_trade.web.service import DashboardService


CANDIDATE_ID = "cand_" + "a" * 32
ACTIVE_FINGERPRINT = "b" * 64


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
        self.calls = []

    def _record(self, name, **payload):
        self.calls.append((name, payload))
        result = {"operation": name}
        if "candidate_id" in payload:
            result["candidate_id"] = payload["candidate_id"]
        return result

    def strategy_lab(self, **payload):
        return self._record("summary", **payload)

    def strategy_lab_candidate(self, **payload):
        return self._record("candidate", **payload)

    def strategy_lab_create_manual(self, **payload):
        return self._record("manual", **payload)

    def strategy_lab_propose(self, **payload):
        return self._record("propose", **payload)

    def strategy_lab_validate(self, **payload):
        return self._record("validate", **payload)

    def strategy_lab_approve(self, **payload):
        return self._record("approve", **payload)

    def strategy_lab_export(self, **payload):
        return self._record("export", **payload)

    def strategy_lab_activate(self, **payload):
        return self._record("activate", **payload)

    def strategy_lab_rollback(self, **payload):
        return self._record("rollback", **payload)

    def strategy_lab_monitor(self, **payload):
        return self._record("monitor", **payload)

    def strategy_lab_lifecycle(self, **payload):
        return self._record(payload.get("action", "lifecycle"), **payload)


class StrategyLabHttpTests(unittest.TestCase):
    def test_monitoring_and_human_lifecycle_routes_are_state_bound(self):
        service = _Service()
        token = "local-csrf"
        monitor_id = "monitor_" + "c" * 32
        common = {
            "confirmed": True,
            "note": "Human-reviewed lifecycle decision",
            "expected_active_candidate_id": CANDIDATE_ID,
            "expected_active_fingerprint": ACTIVE_FINGERPRINT,
            "monitor_id": monitor_id,
        }
        with _running_server(service, token=token) as port:
            status, payload = _request_json(
                port,
                "POST",
                "/api/strategy-lab/monitor",
                {},
                token=token,
            )
            self.assertEqual(status, 201)
            self.assertEqual(payload["operation"], "monitor")
            for action in ("suspend", "resume", "retire"):
                status, payload = _request_json(
                    port,
                    "POST",
                    f"/api/strategy-lab/lifecycle/{action}",
                    common,
                    token=token,
                )
                self.assertEqual(status, 200, (action, payload))
                self.assertEqual(payload["operation"], action)

        monitor_call = next(
            call for operation, call in service.calls if operation == "monitor"
        )
        self.assertEqual(monitor_call["owner_id"], "local-owner")
        self.assertEqual(monitor_call["actor"], "local-owner")
        for action in ("suspend", "resume", "retire"):
            call = next(call for operation, call in service.calls if operation == action)
            self.assertEqual(call["owner_id"], "local-owner")
            self.assertEqual(call["actor"], "local-owner")
            self.assertEqual(call["monitor_id"], monitor_id)
            self.assertEqual(call["expected_active_fingerprint"], ACTIVE_FINGERPRINT)

    def test_local_owner_can_use_complete_candidate_workflow(self):
        service = _Service()
        token = "local-csrf"
        with _running_server(service, token=token) as port:
            status, payload = _request_json(port, "GET", "/api/strategy-lab")
            self.assertEqual(status, 200)
            self.assertEqual(
                service.calls[-1], ("summary", {"owner_id": "local-owner"})
            )

            status, payload = _request_json(
                port,
                "POST",
                "/api/strategy-lab/candidates",
                {
                    "changes": {"strategy": {"top_n": 3}},
                    "title": "Lower concentration",
                    "hypothesis": "A broader basket may reduce concentration.",
                    "reason": "Manual review",
                },
                token=token,
            )
            self.assertEqual(status, 201)
            self.assertEqual(payload["operation"], "manual")

            status, payload = _request_json(
                port,
                "POST",
                "/api/strategy-lab/propose",
                {
                    "title": "Drawdown review",
                    "hypothesis": "Lower exposure may reduce drawdown.",
                    "objective": "drawdown",
                },
                token=token,
            )
            self.assertEqual(status, 201)
            self.assertEqual(payload["operation"], "propose")

            operations = {
                "validate": {},
                "approve": {"confirmed": True, "note": "Reviewed"},
                "export": {"confirmed": True},
                "activate": {"confirmed": True, "note": "Use in paper only"},
            }
            for action, body in operations.items():
                status, payload = _request_json(
                    port,
                    "POST",
                    f"/api/strategy-lab/candidates/{CANDIDATE_ID}/{action}",
                    body,
                    token=token,
                )
                self.assertEqual(status, 200, (action, payload))
                self.assertEqual(payload["operation"], action)

            status, payload = _request_json(
                port,
                "GET",
                f"/api/strategy-lab/candidates/{CANDIDATE_ID}",
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["candidate_id"], CANDIDATE_ID)

            status, payload = _request_json(
                port,
                "POST",
                "/api/strategy-lab/rollback",
                {
                    "confirmed": True,
                    "note": "Restore prior paper version",
                    "expected_active_candidate_id": CANDIDATE_ID,
                    "expected_active_fingerprint": ACTIVE_FINGERPRINT,
                },
                token=token,
            )
            self.assertEqual(status, 200)
            self.assertEqual(payload["operation"], "rollback")

        for operation, call in service.calls:
            self.assertEqual(call["owner_id"], "local-owner")
            if operation not in {"summary", "candidate"}:
                self.assertEqual(call["actor"], "local-owner")
        rollback_call = next(
            call for operation, call in service.calls if operation == "rollback"
        )
        self.assertEqual(
            rollback_call["expected_active_candidate_id"], CANDIDATE_ID
        )
        self.assertEqual(
            rollback_call["expected_active_fingerprint"], ACTIVE_FINGERPRINT
        )

    def test_session_identity_is_not_accepted_from_payload(self):
        service = _Service()
        alice_account_id = "acct_" + "3" * 32
        alice = _session("alice", "alice-csrf", alice_account_id)
        alice_token = "b" * 64
        auth = _Auth({alice_token: alice})
        with _running_server(service, auth=auth) as port:
            status, payload = _request_json(
                port,
                "GET",
                "/api/strategy-lab",
                cookie=alice_token,
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                service.calls[-1], ("summary", {"owner_id": alice_account_id})
            )
            serialized = json.dumps(payload, sort_keys=True)
            self.assertNotIn(alice_account_id, serialized)
            self.assertNotIn("account_id", serialized)
            self.assertNotIn("principal_id", serialized)

            status, payload = _request_json(
                port,
                "POST",
                "/api/strategy-lab/candidates",
                {
                    "changes": {"risk": {"cooldown_days": 8}},
                    "title": "Risk review",
                    "hypothesis": "A longer cooldown may limit repeated losses.",
                    "reason": "Manual review",
                },
                token="alice-csrf",
                cookie=alice_token,
            )
            self.assertEqual(status, 201)
            self.assertEqual(payload, {"operation": "manual"})
            self.assertEqual(service.calls[-1][1]["owner_id"], alice_account_id)
            self.assertEqual(service.calls[-1][1]["actor"], "alice")
            self.assertNotIn(alice_account_id, json.dumps(payload, sort_keys=True))

            status, payload = _request_json(
                port,
                "POST",
                "/api/strategy-lab/candidates",
                {
                    "changes": {"risk": {"cooldown_days": 8}},
                    "title": "Risk review",
                    "hypothesis": "A longer cooldown may limit repeated losses.",
                    "reason": "Manual review",
                    "owner": "bob",
                },
                token="alice-csrf",
                cookie=alice_token,
            )
            self.assertEqual(status, 400)
            self.assertIn("owner", payload["error"])

    def test_mutations_require_same_origin_csrf_and_confirmation(self):
        service = _Service()
        with _running_server(service, token="csrf") as port:
            path = f"/api/strategy-lab/candidates/{CANDIDATE_ID}/approve"
            status, _ = _request_json(
                port,
                "POST",
                path,
                {"confirmed": True},
                token="wrong",
            )
            self.assertEqual(status, 403)

            status, _ = _request_json(
                port,
                "POST",
                path,
                {"confirmed": True},
                token="csrf",
                origin="http://example.com",
            )
            self.assertEqual(status, 403)

            status, payload = _request_json(
                port,
                "POST",
                path,
                {"confirmed": False},
                token="csrf",
            )
            self.assertEqual(status, 400)
            self.assertIn("confirmed=true", payload["error"])

    def test_invalid_candidate_paths_do_not_reach_service(self):
        service = _Service()
        with _running_server(service, token="csrf") as port:
            status, _ = _request_json(
                port,
                "GET",
                "/api/strategy-lab/candidates/not-an-id",
            )
            self.assertEqual(status, 404)
            status, _ = _request_json(
                port,
                "POST",
                f"/api/strategy-lab/candidates/{CANDIDATE_ID}/unknown",
                {},
                token="csrf",
            )
            self.assertEqual(status, 404)
        self.assertEqual(service.calls, [])

    def test_strategy_lab_resource_conflicts_return_http_409(self):
        validating = _Service()
        validating.strategy_lab_validate = MagicMock(
            side_effect=StrategyLabConflictError("已有策略验证正在运行，请稍后重试")
        )
        with _running_server(validating, token="csrf") as port:
            status, payload = _request_json(
                port,
                "POST",
                f"/api/strategy-lab/candidates/{CANDIDATE_ID}/validate",
                {},
                token="csrf",
            )
        self.assertEqual(status, 409)
        self.assertIn("验证正在运行", payload["error"])

        at_capacity = _Service()
        at_capacity.strategy_lab_create_manual = MagicMock(
            side_effect=StrategyLabCapacityError(
                "策略候选已达到每个账号 100 个的上限，无法继续创建"
            )
        )
        with _running_server(at_capacity, token="csrf") as port:
            status, payload = _request_json(
                port,
                "POST",
                "/api/strategy-lab/candidates",
                {
                    "changes": {"strategy": {"top_n": 3}},
                    "title": "Capacity boundary",
                    "hypothesis": "Creation must stop at the owner limit.",
                    "reason": "Focused HTTP conflict test.",
                },
                token="csrf",
            )
        self.assertEqual(status, 409)
        self.assertIn("每个账号 100 个的上限", payload["error"])

        stale_rollback = _Service()
        stale_rollback.strategy_lab_rollback = MagicMock(
            side_effect=StrategyLabConflictError(
                "活动策略版本已变化；请重新核对后再决定是否回滚"
            )
        )
        with _running_server(stale_rollback, token="csrf") as port:
            status, payload = _request_json(
                port,
                "POST",
                "/api/strategy-lab/rollback",
                {
                    "confirmed": True,
                    "expected_active_candidate_id": CANDIDATE_ID,
                    "expected_active_fingerprint": ACTIVE_FINGERPRINT,
                },
                token="csrf",
            )
        self.assertEqual(status, 409)
        self.assertIn("重新核对后再决定是否回滚", payload["error"])


class StrategyLabServiceTests(unittest.TestCase):
    def test_service_uses_one_engine_and_current_market(self):
        config = SimpleNamespace(project_root=SimpleNamespace())
        owner_id = "acct_" + "4" * 32
        actor = "alice"
        engine = MagicMock()
        engine.summary.return_value = {"candidates": []}
        engine.parameter_schema.return_value = {"schema_version": 1, "parameters": []}
        engine.get_candidate.return_value = {"candidate_id": CANDIDATE_ID}
        engine.create_manual_candidate.return_value = {"candidate_id": CANDIDATE_ID}
        engine.propose_local_ai_candidate.return_value = {"candidate_id": CANDIDATE_ID}
        engine.validate_candidate.return_value = {"candidate_id": CANDIDATE_ID}
        engine.approve_candidate.return_value = {"candidate_id": CANDIDATE_ID}
        engine.export_paper_config.return_value = {
            "candidate_id": CANDIDATE_ID,
            "path": "private-owner-path",
        }
        engine.activate_candidate.return_value = {"candidate_id": CANDIDATE_ID}
        engine.monitor_active_candidate.return_value = {"monitor_id": "monitor-test"}
        engine.suspend_active_candidate.return_value = {
            "candidate_id": CANDIDATE_ID,
            "lifecycle_state": "SUSPENDED",
        }
        engine.rollback.return_value = {"candidate_id": None}
        market = object()

        with patch(
            "ai_trade.web.service.StrategyLabEngine", return_value=engine
        ) as cls:
            service = DashboardService(config)
            service.market = MagicMock(return_value=market)
            summary = service.strategy_lab(owner_id=owner_id)
            candidate = service.strategy_lab_candidate(
                candidate_id=CANDIDATE_ID, owner_id=owner_id
            )
            manual = service.strategy_lab_create_manual(
                changes={"strategy": {"top_n": 3}},
                title="Manual",
                hypothesis="Manual hypothesis",
                reason="Human review",
                owner_id=owner_id,
                actor=actor,
            )
            proposed = service.strategy_lab_propose(
                title="Proposal",
                hypothesis="Proposal hypothesis",
                objective="balanced",
                owner_id=owner_id,
                actor=actor,
            )
            validated = service.strategy_lab_validate(
                candidate_id=CANDIDATE_ID,
                owner_id=owner_id,
                actor=actor,
            )
            approved = service.strategy_lab_approve(
                candidate_id=CANDIDATE_ID,
                note="Reviewed",
                owner_id=owner_id,
                actor=actor,
            )
            exported = service.strategy_lab_export(
                candidate_id=CANDIDATE_ID,
                owner_id=owner_id,
                actor=actor,
            )
            activated = service.strategy_lab_activate(
                candidate_id=CANDIDATE_ID,
                note="Paper baseline",
                owner_id=owner_id,
                actor=actor,
            )
            monitored = service.strategy_lab_monitor(owner_id=owner_id, actor=actor)
            suspended = service.strategy_lab_lifecycle(
                action="suspend",
                note="Review",
                expected_active_candidate_id=CANDIDATE_ID,
                expected_active_fingerprint=ACTIVE_FINGERPRINT,
                monitor_id=None,
                owner_id=owner_id,
                actor=actor,
            )
            rolled_back = service.strategy_lab_rollback(
                note="Restore",
                expected_active_candidate_id=CANDIDATE_ID,
                expected_active_fingerprint=ACTIVE_FINGERPRINT,
                owner_id=owner_id,
                actor=actor,
            )

        self.assertEqual(summary["parameter_schema"]["schema_version"], 1)
        for result in (candidate, manual, proposed, validated, approved, activated):
            self.assertEqual(result["candidate_id"], CANDIDATE_ID)
        self.assertEqual(
            exported,
            {"candidate_id": CANDIDATE_ID, "path": "private-owner-path"},
        )
        self.assertEqual(rolled_back["candidate_id"], None)
        self.assertEqual(monitored["monitor_id"], "monitor-test")
        self.assertEqual(suspended["lifecycle_state"], "SUSPENDED")
        cls.assert_called_once_with(config)
        engine.summary.assert_called_once_with(owner_id)
        engine.get_candidate.assert_called_once_with(owner_id, CANDIDATE_ID)
        engine.create_manual_candidate.assert_called_once_with(
            owner_id,
            {"strategy": {"top_n": 3}},
            "Manual",
            "Manual hypothesis",
            "Human review",
            actor=actor,
        )
        engine.propose_local_ai_candidate.assert_called_once_with(
            owner_id,
            "Proposal",
            "Proposal hypothesis",
            "balanced",
            actor=actor,
        )
        engine.validate_candidate.assert_called_once_with(
            owner_id,
            CANDIDATE_ID,
            market,
            actor=actor,
        )
        engine.approve_candidate.assert_called_once_with(
            owner_id,
            CANDIDATE_ID,
            approved_by=actor,
            note="Reviewed",
        )
        engine.export_paper_config.assert_called_once_with(
            owner_id,
            CANDIDATE_ID,
            actor=actor,
        )
        engine.activate_candidate.assert_called_once_with(
            owner_id,
            CANDIDATE_ID,
            activated_by=actor,
            note="Paper baseline",
        )
        engine.monitor_active_candidate.assert_called_once_with(
            owner_id,
            market,
            actor=actor,
        )
        engine.suspend_active_candidate.assert_called_once_with(
            owner_id,
            actor=actor,
            expected_active_candidate_id=CANDIDATE_ID,
            expected_active_fingerprint=ACTIVE_FINGERPRINT,
            note="Review",
            monitor_id=None,
        )
        engine.rollback.assert_called_once_with(
            owner_id,
            rolled_back_by=actor,
            expected_active_candidate_id=CANDIDATE_ID,
            expected_active_fingerprint=ACTIVE_FINGERPRINT,
            note="Restore",
        )

    def test_validation_limit_is_process_wide_and_releases_after_completion(self):
        entered = threading.Event()
        release = threading.Event()
        failures = []
        first_engine = MagicMock()
        second_engine = MagicMock()
        market = object()

        def blocking_validation(*_args, **_kwargs):
            entered.set()
            if not release.wait(timeout=2):
                raise AssertionError("validation test release timed out")
            return {"candidate_id": CANDIDATE_ID}

        first_engine.validate_candidate.side_effect = blocking_validation
        second_engine.validate_candidate.return_value = {"candidate_id": CANDIDATE_ID}
        first_service = DashboardService(SimpleNamespace())
        second_service = DashboardService(SimpleNamespace())
        first_service._strategy_lab = first_engine
        second_service._strategy_lab = second_engine
        first_service.market = MagicMock(return_value=market)
        second_service.market = MagicMock(return_value=market)

        def run_first_validation():
            try:
                first_service.strategy_lab_validate(
                    candidate_id=CANDIDATE_ID,
                    owner_id="acct_" + "1" * 32,
                    actor="alice",
                )
            except BaseException as exc:  # pragma: no cover - asserted below
                failures.append(exc)

        thread = threading.Thread(target=run_first_validation)
        thread.start()
        self.assertTrue(entered.wait(timeout=2))
        try:
            with self.assertRaisesRegex(
                StrategyLabConflictError,
                "一次只能验证一个候选",
            ):
                second_service.strategy_lab_validate(
                    candidate_id=CANDIDATE_ID,
                    owner_id="acct_" + "2" * 32,
                    actor="bob",
                )
            second_engine.validate_candidate.assert_not_called()
        finally:
            release.set()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(failures, [])
        result = second_service.strategy_lab_validate(
            candidate_id=CANDIDATE_ID,
            owner_id="acct_" + "2" * 32,
            actor="bob",
        )
        self.assertEqual(result["candidate_id"], CANDIDATE_ID)


class StrategyLabPayloadTests(unittest.TestCase):
    def test_manual_payload_is_strict(self):
        payload = {
            "changes": {"strategy": {"top_n": 4}},
            "title": "Candidate",
            "hypothesis": "Test a wider basket.",
            "reason": "Human change",
        }
        changes, title, hypothesis, reason = _parse_strategy_lab_manual_payload(payload)
        self.assertEqual(changes, {"strategy": {"top_n": 4}})
        self.assertEqual(
            (title, hypothesis, reason),
            tuple(payload[key] for key in ("title", "hypothesis", "reason")),
        )

        invalid = [
            {},
            {**payload, "owner": "alice"},
            {**payload, "changes": {}},
            {**payload, "changes": {"broker": {"mode": "live"}}},
            {**payload, "changes": {"strategy": {}}},
            {**payload, "title": " Candidate"},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                _parse_strategy_lab_manual_payload(value)

    def test_proposal_and_confirmation_payloads_are_strict(self):
        self.assertEqual(
            _parse_strategy_lab_proposal_payload(
                {"title": "Candidate", "hypothesis": "Review drawdown."}
            ),
            ("Candidate", "Review drawdown.", "balanced"),
        )
        with self.assertRaises(ValueError):
            _parse_strategy_lab_proposal_payload(
                {
                    "title": "Candidate",
                    "hypothesis": "Review drawdown.",
                    "objective": "profit",
                }
            )
        with self.assertRaises(ValueError):
            _parse_strategy_lab_confirmation_payload(
                {"confirmed": 1}, action="approval"
            )
        self.assertEqual(
            _parse_strategy_lab_confirmation_payload(
                {"confirmed": True, "note": "Reviewed"}, action="approval"
            ),
            "Reviewed",
        )

    def test_rollback_payload_is_strict_and_state_bound(self):
        payload = {
            "confirmed": True,
            "note": "Restore",
            "expected_active_candidate_id": CANDIDATE_ID,
            "expected_active_fingerprint": ACTIVE_FINGERPRINT,
        }
        self.assertEqual(
            _parse_strategy_lab_rollback_payload(payload),
            (CANDIDATE_ID, ACTIVE_FINGERPRINT, "Restore"),
        )
        invalid = [
            {},
            {**payload, "confirmed": 1},
            {**payload, "expected_active_candidate_id": None},
            {**payload, "expected_active_candidate_id": "cand_invalid"},
            {**payload, "expected_active_fingerprint": "A" * 64},
            {**payload, "expected_active_fingerprint": "b" * 63},
            {**payload, "owner": "alice"},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                _parse_strategy_lab_rollback_payload(value)

    def test_lifecycle_payload_requires_confirmation_reason_and_exact_evidence(self):
        monitor_id = "monitor_" + "c" * 32
        payload = {
            "confirmed": True,
            "note": "Reviewed the latest decay evidence",
            "expected_active_candidate_id": CANDIDATE_ID,
            "expected_active_fingerprint": ACTIVE_FINGERPRINT,
            "monitor_id": monitor_id,
        }
        self.assertEqual(
            _parse_strategy_lab_lifecycle_payload(payload),
            (
                CANDIDATE_ID,
                ACTIVE_FINGERPRINT,
                "Reviewed the latest decay evidence",
                monitor_id,
            ),
        )
        invalid = [
            {**payload, "confirmed": False},
            {**payload, "note": ""},
            {**payload, "expected_active_candidate_id": "cand_invalid"},
            {**payload, "expected_active_fingerprint": "0" * 63},
            {**payload, "monitor_id": "monitor_invalid"},
            {**payload, "owner_id": "attacker"},
        ]
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                _parse_strategy_lab_lifecycle_payload(value)

    def test_candidate_window_has_a_visible_truncation_label(self):
        asset = (
            Path(__file__).resolve().parents[1]
            / "src"
            / "ai_trade"
            / "web"
            / "assets"
            / "app.js"
        ).read_text(encoding="utf-8")
        self.assertIn("strategyCandidateCountLabel(data, candidates)", asset)
        self.assertIn("最近 ${count} / 共 ${total} 个不可变记录", asset)
        self.assertIn(
            "expected_active_candidate_id: active.candidate_id",
            asset,
        )
        self.assertIn(
            "expected_active_fingerprint: active.fingerprint",
            asset,
        )
        self.assertIn("if (error.status === 409)", asset)
        self.assertIn("await reloadStrategyLab()", asset)
        self.assertEqual(asset.count("state.strategyActionBusy = false;"), 1)
        self.assertIn(
            'if (strategyLabReloaded && state.route === "strategy-lab")',
            asset,
        )
        self.assertIn('data-strategy-monitor', asset)
        self.assertIn('data-strategy-lifecycle-form', asset)
        self.assertIn('/api/strategy-lab/monitor', asset)
        self.assertIn('/api/strategy-lab/lifecycle/${encodeURIComponent(action)}', asset)
        self.assertIn('strategyMonitorVerdictChip', asset)


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
