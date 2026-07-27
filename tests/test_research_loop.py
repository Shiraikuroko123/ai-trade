from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from ai_trade.assistant.governance import GovernanceSettings, ModelCallGovernance
from ai_trade.assistant.provider import (
    MAX_COMPLETION_TOKENS,
    RESEARCH_LOOP_PROMPT_TEMPLATE_VERSION,
    OpenAICompatibleProvider,
    ProviderSettings,
    valid_research_action_shape,
)
from ai_trade.cli import build_parser
from ai_trade.research_loop import (
    LOOP_SAFETY,
    ResearchLoopEngine,
    ResearchLoopStore,
    StaticResearchPlanner,
)
from ai_trade.research_loop.schema import validate_proposal


class _Market:
    symbols = {"510300": object(), "510500": object()}

    def snapshot_metadata(self):
        return {
            "provider": "fixture",
            "latest_common_session": "2026-07-24",
            "manifest_sha256": "a" * 64,
        }


class ResearchLoopTests(unittest.TestCase):
    def test_loop_preserves_failure_then_stops_without_execution_authority(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = SimpleNamespace(project_root=root)
            calls = []

            def execute(tool, arguments):
                calls.append((tool, arguments))
                if tool == "factor-define":
                    return {
                        "name": arguments["name"],
                        "fingerprint": "b" * 64,
                        "reused": False,
                    }
                raise RuntimeError("negative evidence is retained")

            planner = StaticResearchPlanner(
                [
                    {
                        "tool": "factor-define",
                        "arguments": {
                            "name": "model_gap_20",
                            "expression": "close / sma(close, 20) - 1",
                            "direction": 1,
                            "label": "Model gap",
                        },
                        "rationale": "Register one bounded expression factor.",
                    },
                    {
                        "tool": "factor-evaluate",
                        "arguments": {
                            "factor_id": "model_gap_20",
                            "horizons": [5, 20],
                            "step": 5,
                        },
                        "rationale": "Evaluate the registered factor point in time.",
                    },
                    {
                        "tool": "stop",
                        "arguments": {},
                        "rationale": "Stop after recording the negative result.",
                    },
                ]
            )
            result = ResearchLoopEngine(config, executor=execute).run(
                "alice", _Market(), planner, max_rounds=3, max_tool_units=5
            )

            self.assertEqual(result["status"], "stopped")
            self.assertEqual(result["safety"], LOOP_SAFETY)
            event_types = [item["event_type"] for item in result["events"]]
            self.assertIn("tool_succeeded", event_types)
            self.assertIn("tool_failed", event_types)
            self.assertEqual(calls[0][0], "factor-define")
            self.assertEqual(calls[1][0], "factor-evaluate")
            failed = next(
                item for item in result["events"] if item["event_type"] == "tool_failed"
            )
            self.assertEqual(failed["payload"]["error_code"], "RuntimeError")
            self.assertFalse(result["safety"]["may_approve"])
            self.assertFalse(result["safety"]["may_activate"])
            self.assertFalse(result["safety"]["may_trade"])

            listed = ResearchLoopStore(root / "state" / "research_loop").list(
                "alice"
            )
            self.assertEqual(listed["loops"][0]["loop_id"], result["loop_id"])

    def test_tool_budget_denies_before_executor_call(self):
        with TemporaryDirectory() as temporary:
            calls = []
            config = SimpleNamespace(project_root=Path(temporary))
            planner = StaticResearchPlanner(
                [
                    {
                        "tool": "factor-evaluate",
                        "arguments": {
                            "factor_id": "momentum_120_5",
                            "horizons": [20],
                            "step": 5,
                        },
                        "rationale": "This action costs more than the loop budget.",
                    }
                ]
            )
            result = ResearchLoopEngine(
                config,
                executor=lambda tool, arguments: calls.append((tool, arguments)),
            ).run("alice", _Market(), planner, max_rounds=1, max_tool_units=1)

            self.assertEqual(result["status"], "budget_exhausted")
            self.assertEqual(calls, [])
            rejected = next(
                item
                for item in result["events"]
                if item["event_type"] == "tool_rejected"
            )
            self.assertEqual(rejected["payload"]["reason"], "tool_budget_exhausted")

    def test_hypothesis_can_only_use_ids_produced_inside_the_loop(self):
        with TemporaryDirectory() as temporary:
            config = SimpleNamespace(project_root=Path(temporary))
            planner = StaticResearchPlanner(
                [
                    {
                        "tool": "hypothesis-from-model",
                        "arguments": {"evaluation_id": "mdl_" + "c" * 32},
                        "rationale": "Attempt to use unrelated stored evidence.",
                    }
                ]
            )
            result = ResearchLoopEngine(
                config, executor=lambda tool, arguments: {}
            ).run("alice", _Market(), planner, max_rounds=1)

            self.assertEqual(result["status"], "planner_failed")
            self.assertTrue(
                any(
                    item["event_type"] == "planner_failed"
                    for item in result["events"]
                )
            )

    def test_model_result_can_feed_only_the_next_gated_hypothesis_step(self):
        with TemporaryDirectory() as temporary:
            config = SimpleNamespace(project_root=Path(temporary))
            evaluation_id = "mdl_" + "d" * 32
            hypothesis_id = "hyp_" + "e" * 32

            def execute(tool, arguments):
                if tool == "model-evaluate":
                    return {
                        "evaluation_id": evaluation_id,
                        "model": {"model_id": arguments["model_id"]},
                        "results": {
                            "model": {"mean_ic": 0.04},
                            "model_minus_best_factor_ic": 0.01,
                            "statistical_validation": {
                                "model_ic": {
                                    "adjusted_p_value": 0.04,
                                    "reject_null": True,
                                    "positive_subperiods": 3,
                                }
                            },
                        },
                    }
                return {
                    "hypothesis_id": hypothesis_id,
                    "source": {"kind": "model_evidence_deterministic"},
                }

            planner = StaticResearchPlanner(
                [
                    {
                        "tool": "model-evaluate",
                        "arguments": {
                            "model_id": "ridge_v1",
                            "factor_ids": ["momentum_120_5"],
                            "horizon": 20,
                            "step": 5,
                        },
                        "rationale": "Evaluate one registered model.",
                    },
                    {
                        "tool": "hypothesis-from-model",
                        "arguments": {"evaluation_id": evaluation_id},
                        "rationale": "Apply the existing statistical evidence gate.",
                    },
                    {
                        "tool": "stop",
                        "arguments": {},
                        "rationale": "Stop before candidate materialization.",
                    },
                ]
            )
            result = ResearchLoopEngine(config, executor=execute).run(
                "alice", _Market(), planner, max_rounds=3, max_tool_units=8
            )
            succeeded = [
                item
                for item in result["events"]
                if item["event_type"] == "tool_succeeded"
            ]
            self.assertEqual(len(succeeded), 2)
            self.assertEqual(
                succeeded[1]["payload"]["result"]["hypothesis_id"], hypothesis_id
            )

    def test_tampered_event_breaks_hash_chain(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = SimpleNamespace(project_root=root)
            planner = StaticResearchPlanner(
                [
                    {
                        "tool": "stop",
                        "arguments": {},
                        "rationale": "No experiment is required.",
                    }
                ]
            )
            result = ResearchLoopEngine(config, executor=lambda tool, args: {}).run(
                "alice", _Market(), planner, max_rounds=1
            )
            event_path = next(
                (
                    root
                    / "state"
                    / "research_loop"
                    / "users"
                    / result["owner"]
                    / "loops"
                    / result["loop_id"]
                    / "events"
                ).glob("*.json")
            )
            value = json.loads(event_path.read_text(encoding="utf-8"))
            value["payload"]["available_tools"] = []
            event_path.write_text(json.dumps(value), encoding="utf-8")

            with self.assertRaisesRegex(RuntimeError, "fingerprint"):
                ResearchLoopStore(root / "state" / "research_loop").get(
                    "alice", result["loop_id"]
                )

    def test_static_plan_rejects_duplicate_json_keys(self):
        with TemporaryDirectory() as temporary:
            path = Path(temporary) / "plan.json"
            path.write_text(
                '{"schema_version":1,"schema_version":1,"actions":[]}',
                encoding="utf-8",
            )
            with self.assertRaisesRegex(ValueError, "duplicate"):
                StaticResearchPlanner.from_file(path)

    def test_forbidden_tool_and_provider_shape_fail_closed(self):
        proposal = {
            "tool": "approve",
            "arguments": {"candidate_id": "cand_" + "a" * 32},
            "rationale": "Bypass the research boundary.",
        }
        self.assertFalse(valid_research_action_shape(proposal))
        with self.assertRaisesRegex(ValueError, "allowlisted"):
            validate_proposal(
                proposal,
                allowed_factors={"momentum_120_5"},
                allowed_models={"ridge_v1"},
                model_evaluation_ids=set(),
                hypothesis_ids=set(),
            )

    def test_model_planner_call_uses_structured_validation_and_governance(self):
        proposal = {
            "tool": "stop",
            "arguments": {},
            "rationale": "No statistically defensible action remains.",
        }
        usage = {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18}
        provider = OpenAICompatibleProvider(
            ProviderSettings(
                api_key="test-key",
                model="test-model",
                endpoint="https://models.example.test/v1/chat/completions",
                timeout_seconds=5,
                max_response_bytes=64 * 1024,
            )
        )
        with patch.object(
            provider,
            "_complete",
            return_value=(proposal, usage, json.dumps(proposal)),
        ):
            result, observed_usage = provider.research_action(
                context={
                    "authority": "research_only",
                    "execution_authorized": False,
                    "tool_contracts": {"stop": {"arguments": []}},
                }
            )
        self.assertEqual(result, proposal)
        self.assertEqual(observed_usage, usage)

        with TemporaryDirectory() as temporary:
            governance = ModelCallGovernance(
                Path(temporary),
                GovernanceSettings(max_retries=0, daily_token_budget=100_000),
                model="test-model",
                endpoint="https://models.example.test/v1/chat/completions",
                template_version=RESEARCH_LOOP_PROMPT_TEMPLATE_VERSION,
                maximum_completion_tokens=MAX_COMPLETION_TOKENS,
            )
            governed, governed_usage, audit = governance.run_structured(
                user_id="alice",
                role="research_loop_planner",
                template_version=RESEARCH_LOOP_PROMPT_TEMPLATE_VERSION,
                request_payload={"round": 1},
                evidence={"snapshot": "fixture"},
                provider_call=lambda retries, hook: (proposal, usage),
                result_validator=valid_research_action_shape,
            )
            self.assertEqual(governed, proposal)
            self.assertEqual(governed_usage, usage)
            self.assertEqual(audit["role"], "research_loop_planner")

    def test_cli_has_no_approval_activation_or_trade_flags(self):
        args = build_parser().parse_args(
            [
                "research-loop-run",
                "--mode",
                "local",
                "--plan-file",
                "plan.json",
                "--max-rounds",
                "3",
                "--max-tool-units",
                "8",
            ]
        )
        self.assertEqual(args.max_rounds, 3)
        self.assertFalse(hasattr(args, "approve"))
        self.assertFalse(hasattr(args, "activate"))
        self.assertFalse(hasattr(args, "trade"))


if __name__ == "__main__":
    unittest.main()
