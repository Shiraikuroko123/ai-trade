from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from ai_trade.assistant.governance import GovernanceSettings, ModelCallGovernance
from ai_trade.assistant.provider import (
    AssistantProviderError,
    MAX_COMPLETION_TOKENS,
    OpenAICompatibleProvider,
    PROMPT_TEMPLATE_VERSION,
    ProviderSettings,
)


class GovernanceTests(unittest.TestCase):
    def test_success_cache_and_daily_accounting_are_user_isolated(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = _AuditedProvider()
            governance = _governance(
                Path(temporary),
                GovernanceSettings(
                    max_retries=0,
                    max_concurrent_calls=1,
                    max_tokens_per_call=50_000,
                    daily_token_budget=100_000,
                    input_cost_per_million_usd=1.0,
                    output_cost_per_million_usd=2.0,
                    daily_cost_budget_usd=10.0,
                ),
            )

            first = governance.enhance(
                user_id="alice", provider=provider, **_request()
            )
            cached = governance.enhance(
                user_id="alice", provider=provider, **_request()
            )
            other = governance.enhance(
                user_id="bob", provider=provider, **_request()
            )

            self.assertEqual(provider.calls, 2)
            self.assertEqual(first[2]["status"], "success")
            self.assertEqual(first[2]["attempt_count"], 1)
            self.assertEqual(first[2]["usage"]["total_tokens"], 30)
            self.assertFalse(first[2]["cache_hit"])
            self.assertEqual(cached[2]["status"], "cache_hit")
            self.assertTrue(cached[2]["cache_hit"])
            self.assertEqual(cached[1]["total_tokens"], 0)
            self.assertEqual(cached[2]["budget"]["tokens_used_before"], 30)
            self.assertEqual(other[2]["budget"]["tokens_used_before"], 0)

            records = list(governance.audit_root.rglob("call_*.json"))
            self.assertEqual(len(records), 3)
            self.assertEqual(len({path.parent.parent.name for path in records}), 2)
            for path in records:
                raw = path.read_text(encoding="utf-8")
                value = json.loads(raw)
                self.assertEqual(value["role"], "research_assistant_wording")
                self.assertEqual(len(value["evidence_sha256"]), 64)
                self.assertEqual(len(value["record_sha256"]), 64)
                self.assertNotIn("private prompt", raw)
                self.assertNotIn("api-key", raw)
                self.assertNotIn('"messages"', raw)
            cache_records = list(governance.cache_root.rglob("*.json"))
            self.assertEqual(len(cache_records), 2)
            self.assertTrue(
                all(
                    len(json.loads(path.read_text(encoding="utf-8"))["record_sha256"])
                    == 64
                    for path in cache_records
                )
            )
            for path in cache_records:
                value = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(len(value["source_call_id"]), 32)
                self.assertEqual(len(value["source_audit_record_sha256"]), 64)

    def test_per_call_and_daily_budgets_deny_before_provider_io(self):
        cases = (
            (
                GovernanceSettings(
                    max_retries=0,
                    max_tokens_per_call=2_000,
                    daily_token_budget=100_000,
                ),
                "model_call_token_budget_exceeded",
            ),
            (
                GovernanceSettings(
                    max_retries=0,
                    max_tokens_per_call=50_000,
                    daily_token_budget=2_000,
                ),
                "model_daily_token_budget_exceeded",
            ),
            (
                GovernanceSettings(
                    max_retries=0,
                    max_tokens_per_call=50_000,
                    daily_token_budget=100_000,
                    input_cost_per_million_usd=1000.0,
                    output_cost_per_million_usd=1000.0,
                    daily_cost_budget_usd=0.01,
                ),
                "model_daily_cost_budget_exceeded",
            ),
        )
        for settings, code in cases:
            with self.subTest(code=code), tempfile.TemporaryDirectory() as temporary:
                provider = _AuditedProvider()
                governance = _governance(Path(temporary), settings)
                with self.assertRaises(AssistantProviderError) as raised:
                    governance.enhance(
                        user_id="alice", provider=provider, **_request()
                    )
                self.assertEqual(raised.exception.code, code)
                self.assertEqual(provider.calls, 0)
                self.assertEqual(raised.exception.audit["status"], "denied")
                self.assertEqual(
                    raised.exception.audit["budget"]["decision"], code
                )

    def test_zero_usage_retry_is_conservatively_accounted_on_success(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = _RetryThenSuccessProvider()
            governance = _governance(
                Path(temporary),
                GovernanceSettings(
                    max_retries=1,
                    max_tokens_per_call=50_000,
                    daily_token_budget=100_000,
                    input_cost_per_million_usd=1.0,
                    output_cost_per_million_usd=2.0,
                ),
            )

            _, usage, audit = governance.enhance(
                user_id="alice", provider=provider, **_request()
            )

            self.assertEqual(usage["total_tokens"], 30)
            self.assertEqual(audit["attempt_count"], 2)
            self.assertEqual(audit["retry_count"], 1)
            self.assertGreater(audit["budget"]["accounted_tokens"], 30)
            self.assertGreater(audit["estimated_cost_usd"], 0.00005)

    def test_tampered_cache_fails_before_another_provider_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = _AuditedProvider()
            governance = _governance(
                Path(temporary),
                GovernanceSettings(max_retries=0, daily_token_budget=100_000),
            )
            governance.enhance(user_id="alice", provider=provider, **_request())
            cache = next(governance.cache_root.rglob("*.json"))
            value = json.loads(cache.read_text(encoding="utf-8"))
            value["enhancement"]["diagnosis"]["summary"] = "tampered"
            cache.write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaises(AssistantProviderError) as raised:
                governance.enhance(
                    user_id="alice", provider=provider, **_request()
                )

            self.assertEqual(raised.exception.code, "model_cache_integrity_error")
            self.assertEqual(provider.calls, 1)
            self.assertEqual(raised.exception.audit["status"], "failed")

    def test_concurrency_limit_denies_another_user_without_provider_io(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = _BlockingProvider()
            governance = _governance(
                Path(temporary),
                GovernanceSettings(
                    max_retries=0,
                    max_concurrent_calls=1,
                    max_tokens_per_call=50_000,
                    daily_token_budget=100_000,
                ),
            )
            errors = []

            def first_call():
                try:
                    governance.enhance(
                        user_id="alice", provider=provider, **_request()
                    )
                except Exception as exc:  # pragma: no cover - asserted below
                    errors.append(exc)

            thread = threading.Thread(target=first_call)
            thread.start()
            self.assertTrue(provider.entered.wait(timeout=2))
            try:
                with self.assertRaises(AssistantProviderError) as raised:
                    governance.enhance(
                        user_id="bob", provider=provider, **_request()
                    )
                self.assertEqual(raised.exception.code, "model_concurrency_limited")
                self.assertEqual(provider.calls, 1)
            finally:
                provider.release.set()
                thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(errors, [])

    def test_audit_failure_disables_later_model_calls(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "state").mkdir()
            (root / "state" / "assistant_calls").write_text(
                "not a directory", encoding="utf-8"
            )
            provider = _AuditedProvider()
            governance = _governance(
                root,
                GovernanceSettings(max_retries=0, daily_token_budget=100_000),
            )
            with self.assertRaises(AssistantProviderError) as first:
                governance.enhance(
                    user_id="alice", provider=provider, **_request()
                )
            self.assertEqual(first.exception.code, "model_audit_unavailable")
            self.assertEqual(provider.calls, 0)

            with self.assertRaises(AssistantProviderError) as second:
                governance.enhance(
                    user_id="alice", provider=provider, **_request()
                )
            self.assertEqual(second.exception.code, "model_audit_unavailable")
            self.assertEqual(provider.calls, 0)

    def test_tampered_audit_stops_before_another_provider_call(self):
        with tempfile.TemporaryDirectory() as temporary:
            provider = _AuditedProvider()
            governance = _governance(
                Path(temporary),
                GovernanceSettings(max_retries=0, daily_token_budget=100_000),
            )
            governance.enhance(user_id="alice", provider=provider, **_request())
            record = next(governance.audit_root.rglob("call_*.json"))
            value = json.loads(record.read_text(encoding="utf-8"))
            value["accounted_tokens"] = 0
            record.write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaises(AssistantProviderError) as raised:
                governance.enhance(
                    user_id="alice", provider=provider, **_request()
                )
            self.assertEqual(raised.exception.code, "model_audit_integrity_error")
            self.assertEqual(provider.calls, 1)


class ProviderRetryTests(unittest.TestCase):
    def test_retryable_attempts_emit_attempt_level_audit(self):
        provider = OpenAICompatibleProvider(
            ProviderSettings(
                api_key="api-key",
                model="test-model",
                endpoint="https://models.example.test/v1/chat/completions",
                timeout_seconds=5,
                max_response_bytes=1024 * 1024,
            )
        )
        enhancement = _enhancement()
        attempts = []
        with (
            patch.object(
                provider,
                "_complete",
                side_effect=[
                    AssistantProviderError("model_server_error"),
                    (
                        enhancement,
                        {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
                        json.dumps(enhancement),
                    ),
                ],
            ) as complete,
            patch("ai_trade.assistant.provider.time.sleep"),
        ):
            result, usage = provider.enhance(
                **_request(), max_retries=1, audit_hook=attempts.append
            )

        self.assertEqual(result, enhancement)
        self.assertEqual(usage["total_tokens"], 30)
        self.assertEqual(complete.call_count, 2)
        self.assertEqual([item["outcome"] for item in attempts], ["error", "success"])
        self.assertEqual([item["retry"] for item in attempts], [0, 1])
        self.assertEqual(attempts[0]["error_code"], "model_server_error")
        self.assertNotEqual(
            attempts[0]["request_sha256"], "private prompt"
        )


class _AuditedProvider:
    def __init__(self):
        self.calls = 0

    def enhance(self, **kwargs):
        self.calls += 1
        kwargs["audit_hook"](
            {
                "attempt": 1,
                "validation_round": 1,
                "retry": 0,
                "request_sha256": "a" * 64,
                "response_sha256": "b" * 64,
                "outcome": "success",
                "error_code": None,
                "elapsed_ms": 12,
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            }
        )
        return _enhancement(), {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
        }


class _RetryThenSuccessProvider(_AuditedProvider):
    def enhance(self, **kwargs):
        self.calls += 1
        kwargs["audit_hook"](
            {
                "attempt": 1,
                "validation_round": 1,
                "retry": 0,
                "request_sha256": "a" * 64,
                "response_sha256": None,
                "outcome": "error",
                "error_code": "model_transport_error",
                "elapsed_ms": 4,
                "usage": {
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "total_tokens": 0,
                },
            }
        )
        kwargs["audit_hook"](
            {
                "attempt": 2,
                "validation_round": 1,
                "retry": 1,
                "request_sha256": "b" * 64,
                "response_sha256": "c" * 64,
                "outcome": "success",
                "error_code": None,
                "elapsed_ms": 8,
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            }
        )
        return _enhancement(), {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
        }


class _BlockingProvider(_AuditedProvider):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def enhance(self, **kwargs):
        self.calls += 1
        self.entered.set()
        if not self.release.wait(timeout=2):
            raise AssistantProviderError("model_transport_error")
        kwargs["audit_hook"](
            {
                "attempt": 1,
                "validation_round": 1,
                "retry": 0,
                "request_sha256": "a" * 64,
                "response_sha256": "b" * 64,
                "outcome": "success",
                "error_code": None,
                "elapsed_ms": 10,
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 20,
                    "total_tokens": 30,
                },
            }
        )
        return _enhancement(), {
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
        }


def _governance(root: Path, settings: GovernanceSettings) -> ModelCallGovernance:
    return ModelCallGovernance(
        root,
        settings,
        model="test-model",
        endpoint="https://models.example.test/v1/chat/completions",
        template_version=PROMPT_TEMPLATE_VERSION,
        maximum_completion_tokens=MAX_COMPLETION_TOKENS,
    )


def _request() -> dict:
    return {
        "symbol": "600000",
        "data_date": "2026-07-23",
        "diagnosis": {
            "trend": "UP",
            "regime": "TREND",
            "volatility": "NORMAL",
            "score": 70,
            "gate": "PROCEED",
            "evidence": [
                {
                    "evidence_id": "price.close",
                    "value": 10.0,
                    "interpretation": "private prompt marker is hashed",
                }
            ],
        },
        "assessment": {
            "conclusion": "WATCH",
            "summary": "local",
            "risk_level": "LOW",
            "risk_budget_pct": 25,
            "evidence_ids": ["price.close"],
            "invalidation": ["new data"],
            "scenarios": [
                {"name": "base", "trigger": "close", "implication": "review"}
            ],
        },
    }


def _enhancement() -> dict:
    return {
        "diagnosis": {"summary": "model summary", "evidence_ids": ["price.close"]},
        "assessment": {
            "conclusion": "WATCH",
            "summary": "model assessment",
            "risk_level": "LOW",
            "risk_budget_pct": 25,
            "evidence_ids": ["price.close"],
            "invalidation": ["new data"],
            "scenarios": [
                {"name": "base", "trigger": "close", "implication": "review"}
            ],
        },
    }


if __name__ == "__main__":
    unittest.main()
