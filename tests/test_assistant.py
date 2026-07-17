from __future__ import annotations

import json
import os
import tempfile
import unittest
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_trade.assistant import AssistantEngine
from ai_trade.assistant.features import ALLOWED_CONCLUSIONS
from ai_trade.assistant.store import MAX_ANALYSIS_RECORD_BYTES, AssistantRecordStore
from ai_trade.assistant.provider import (
    AssistantProviderError,
    DEFAULT_MAX_RESPONSE_BYTES,
    ProviderSettings,
    _completion_endpoint,
    _validate_enhancement,
)
from ai_trade.models import Bar, Instrument


class _Market:
    def __init__(self, bars: list[Bar], symbol: str = "510300"):
        self._bars = bars
        self._symbol = symbol
        self.symbols = {symbol: SimpleNamespace()}
        self.file_hashes = {symbol: "a" * 64}
        self.completed_through = bars[-1].date
        self.latest_common_session = bars[-1].date

    def latest_date(self):
        return self._bars[-1].date

    def history(self, symbol, on_date, count):
        assert symbol == self._symbol
        return [bar for bar in self._bars if bar.date <= on_date][-count:]

    def instrument(self, symbol):
        assert symbol == self._symbol
        return Instrument(symbol, "沪深300ETF", "SH", "equity")


class AssistantEngineTests(unittest.TestCase):
    def test_local_analysis_schema_evidence_chart_and_safe_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = _config(Path(temporary))
            market = _Market(_bars(220, daily_return=0.002))
            engine = AssistantEngine(config)

            result = engine.analyze(market, "510300")

            self.assertEqual(result["schema_version"], 1)
            self.assertEqual(result["authority"], "research_only")
            self.assertIsNone(result["order_intent"])
            self.assertEqual(result["mode"], "local")
            self.assertEqual(result["model"], "local-deterministic-v1")
            self.assertEqual(result["diagnosis"]["stage"], "market_diagnosis")
            self.assertEqual(result["assessment"]["stage"], "risk_assessment")
            self.assertIn(result["assessment"]["conclusion"], ALLOWED_CONCLUSIONS)
            self.assertTrue(result["validation"]["valid"])
            self.assertFalse(result["validation"]["model_enhanced"])
            self.assertEqual(result["snapshot"]["bar_count"], 180)
            self.assertEqual(len(result["chart"]["points"]), 180)
            self.assertEqual(
                set(result["chart"]["points"][0]), {"date", "close", "ema20", "ema50"}
            )

            evidence_ids = {
                item["evidence_id"] for item in result["diagnosis"]["evidence"]
            }
            self.assertEqual(len(evidence_ids), len(result["diagnosis"]["evidence"]))
            self.assertTrue(set(result["assessment"]["evidence_ids"]) <= evidence_ids)
            for step in result["decision_path"]:
                self.assertTrue(set(step["evidence_ids"]) <= evidence_ids)

            records = list((Path(temporary) / "state" / "assistant").rglob("*.json"))
            self.assertEqual(len(records), 1)
            disk = records[0].read_text(encoding="utf-8")
            self.assertNotIn('"prompt":', disk.lower())
            self.assertNotIn('"messages":', disk.lower())
            self.assertNotIn('"raw_response":', disk.lower())
            self.assertNotIn("api_key", disk.lower())
            self.assertNotIn("reasoning_content", disk.lower())
            self.assertEqual(json.loads(disk)["analysis_id"], result["analysis_id"])

    def test_history_skips_ambiguous_or_oversized_records(self):
        with tempfile.TemporaryDirectory() as temporary:
            store = AssistantRecordStore(Path(temporary))
            directory = store._user_directory("alice")
            directory.mkdir(parents=True)
            path = directory / ("a" * 32 + ".json")

            path.write_text('{"schema_version":1,"schema_version":1}', encoding="utf-8")
            self.assertEqual(store.history("alice"), [])

            path.write_bytes(b" " * (MAX_ANALYSIS_RECORD_BYTES + 1))
            self.assertEqual(store.history("alice"), [])

    def test_history_is_user_isolated_and_comparison_uses_previous_record(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine = AssistantEngine(_config(Path(temporary)))
            market = _Market(_bars(220, daily_return=0.001))
            first = engine.analyze(market, "510300", user_id="alice")
            second = engine.analyze(market, "510300", user_id="alice")
            other = engine.analyze(market, "510300", user_id="bob")

            alice = engine.history("alice")
            bob = engine.history("bob")
            self.assertEqual({row["analysis_id"] for row in alice}, {
                first["analysis_id"], second["analysis_id"]
            })
            self.assertEqual([row["analysis_id"] for row in bob], [other["analysis_id"]])
            self.assertTrue(second["comparison"]["available"])
            self.assertEqual(
                second["comparison"]["previous_analysis_id"], first["analysis_id"]
            )
            self.assertFalse(second["comparison"]["data_advanced"])
            user_directories = [path for path in engine._store.root.iterdir() if path.is_dir()]
            self.assertEqual(len(user_directories), 2)
            self.assertNotIn("alice", {path.name for path in user_directories})

    def test_input_validation_and_model_configuration_gate(self):
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {
                "AI_TRADE_AI_API_KEY": "",
                "AI_TRADE_AI_MODEL": "",
                "AI_TRADE_AI_BASE_URL": "",
            },
            clear=False,
        ):
            engine = AssistantEngine(_config(Path(temporary)))
            market = _Market(_bars(80))
            with self.assertRaisesRegex(ValueError, "Unknown"):
                engine.analyze(market, "000001")
            for lookback in (59, 501, True):
                with self.subTest(lookback=lookback), self.assertRaises(ValueError):
                    engine.analyze(market, "510300", lookback=lookback)
            with self.assertRaisesRegex(ValueError, "local or model"):
                engine.analyze(market, "510300", mode="agent")
            with self.assertRaisesRegex(RuntimeError, "not configured"):
                engine.analyze(market, "510300", mode="model")
            with self.assertRaises(ValueError):
                engine.history("alice", 0)

    def test_status_never_exposes_key_or_base_url(self):
        secret = "assistant-secret-key-123"
        base = "https://models.example.test/v1"
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ,
            {
                "AI_TRADE_AI_API_KEY": secret,
                "AI_TRADE_AI_MODEL": "example-model",
                "AI_TRADE_AI_BASE_URL": base,
            },
            clear=False,
        ):
            status = AssistantEngine(_config(Path(temporary))).status()
            rendered = json.dumps(status)
            self.assertTrue(status["model_configured"])
            self.assertEqual(status["supported_modes"], ["local", "model"])
            self.assertNotIn(secret, rendered)
            self.assertNotIn(base, rendered)
            self.assertNotIn("base_url", rendered)
            self.assertNotIn("api_key", rendered)

    def test_model_layer_cannot_relax_deterministic_conclusion(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine = AssistantEngine(_config(Path(temporary)))
            engine._settings = SimpleNamespace(model="test-model")
            engine._provider = _PermissiveProvider()
            market = _Market(_bars(220, daily_return=-0.004))

            result = engine.analyze(market, "510300", mode="model")

            self.assertEqual(result["assessment"]["conclusion"], "NO_ACTION")
            self.assertEqual(result["assessment"]["risk_budget_pct"], 0)
            self.assertTrue(result["validation"]["model_enhanced"])
            self.assertEqual(result["validation"]["usage"]["total_tokens"], 30)

    def test_model_failure_falls_back_without_leaking_transport_details(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine = AssistantEngine(_config(Path(temporary)))
            engine._settings = SimpleNamespace(model="test-model")
            engine._provider = _FailingProvider()

            result = engine.analyze(_Market(_bars(100)), "510300", mode="model")

            self.assertTrue(result["validation"]["valid"])
            self.assertFalse(result["validation"]["model_enhanced"])
            warning = result["validation"]["warnings"][0]
            self.assertIn("model_transport_error", warning)
            self.assertNotIn("http", warning.lower())
            self.assertNotIn("secret", warning.lower())

    def test_snapshot_and_evidence_ids_are_stable_for_same_market_window(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine = AssistantEngine(_config(Path(temporary)))
            market = _Market(_bars(200))
            first = engine.analyze(market, "510300")
            second = engine.analyze(market, "510300")

            self.assertNotEqual(first["analysis_id"], second["analysis_id"])
            self.assertEqual(
                first["snapshot"]["snapshot_id"], second["snapshot"]["snapshot_id"]
            )
            self.assertEqual(
                [row["evidence_id"] for row in first["diagnosis"]["evidence"]],
                [row["evidence_id"] for row in second["diagnosis"]["evidence"]],
            )


class ProviderPolicyTests(unittest.TestCase):
    def test_response_limit_is_fixed_and_not_environment_configurable(self):
        with patch.dict(
            os.environ,
            {
                "AI_TRADE_AI_API_KEY": "test-key",
                "AI_TRADE_AI_MODEL": "test-model",
                "AI_TRADE_AI_BASE_URL": "https://api.example.test/v1",
                "AI_TRADE_AI_TIMEOUT_SECONDS": "30",
                "AI_TRADE_AI_MAX_RESPONSE_BYTES": "not-a-number",
            },
            clear=False,
        ):
            settings, error = ProviderSettings.from_environment()

        self.assertIsNone(error)
        self.assertIsNotNone(settings)
        self.assertEqual(settings.max_response_bytes, DEFAULT_MAX_RESPONSE_BYTES)

    def test_completion_endpoint_allows_https_and_loopback_http_only(self):
        self.assertEqual(
            _completion_endpoint("https://api.example.test/v1"),
            "https://api.example.test/v1/chat/completions",
        )
        self.assertEqual(
            _completion_endpoint("http://127.0.0.1:11434/v1"),
            "http://127.0.0.1:11434/v1/chat/completions",
        )
        for value in (
            "http://api.example.test/v1",
            "https://user:password@api.example.test/v1",
            "https://api.example.test/v1?token=secret",
            "file:///tmp/model",
        ):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _completion_endpoint(value)

    def test_enhancement_validation_rejects_unknown_evidence_and_order_conclusion(self):
        value = {
            "diagnosis": {"summary": "summary", "evidence_ids": ["unknown"]},
            "assessment": {
                "conclusion": "BUY",
                "summary": "summary",
                "risk_level": "LOW",
                "risk_budget_pct": 100,
                "evidence_ids": ["unknown"],
                "invalidation": ["condition"],
                "scenarios": [
                    {"name": "base", "trigger": "trigger", "implication": "wait"}
                ],
            },
        }

        _, errors = _validate_enhancement(value, {"price.close"})

        self.assertTrue(errors)
        self.assertTrue(any("conclusion" in error for error in errors))
        self.assertTrue(any("unknown" in error for error in errors))

    def test_assistant_package_has_no_broker_dependency(self):
        root = Path(__file__).resolve().parents[1] / "src" / "ai_trade" / "assistant"
        sources = "\n".join(
            path.read_text(encoding="utf-8") for path in root.glob("*.py")
        )
        self.assertNotIn("ai_trade.broker", sources)
        self.assertNotIn("from ..broker", sources)


class _PermissiveProvider:
    def enhance(self, **kwargs):
        evidence = kwargs["diagnosis"]["evidence"]
        refs = [row["evidence_id"] for row in evidence[:3]]
        return (
            {
                "diagnosis": {"summary": "模型总结。", "evidence_ids": refs},
                "assessment": {
                    "conclusion": "REVIEW_CANDIDATE",
                    "summary": "模型建议进入候选复核。",
                    "risk_level": "LOW",
                    "risk_budget_pct": 100,
                    "evidence_ids": refs,
                    "invalidation": ["趋势失效时重新评估。"],
                    "scenarios": [
                        {
                            "name": "观察",
                            "trigger": "新数据完成",
                            "implication": "重新运行研究",
                        }
                    ],
                },
            },
            {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
        )


class _FailingProvider:
    def enhance(self, **kwargs):
        raise AssistantProviderError("model_transport_error")


def _config(root: Path):
    return SimpleNamespace(
        project_root=root,
        raw={"data": {"provider": "eastmoney", "adjustment": "forward"}},
    )


def _bars(count: int, daily_return: float = 0.001) -> list[Bar]:
    start = date(2025, 1, 1)
    close = 10.0
    result = []
    for index in range(count):
        close *= 1.0 + daily_return
        open_price = close / (1.0 + daily_return / 2.0)
        result.append(
            Bar(
                start + timedelta(days=index),
                open_price,
                close,
                max(open_price, close) * 1.01,
                min(open_price, close) * 0.99,
                1_000_000.0 + index,
                close * (1_000_000.0 + index),
            )
        )
    return result


if __name__ == "__main__":
    unittest.main()
