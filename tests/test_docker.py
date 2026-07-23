from __future__ import annotations

import unittest
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class DockerDeploymentContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dockerfile = (REPOSITORY_ROOT / "Dockerfile").read_text(encoding="utf-8")
        cls.compose = (REPOSITORY_ROOT / "compose.yaml").read_text(encoding="utf-8")
        cls.bind_compose = (REPOSITORY_ROOT / "compose.bind.yaml").read_text(
            encoding="utf-8"
        )
        cls.ignore = (REPOSITORY_ROOT / ".dockerignore").read_text(encoding="utf-8")
        cls.example = (REPOSITORY_ROOT / "docker.env.example").read_text(
            encoding="utf-8"
        )

    def test_runtime_image_is_non_root_and_has_a_healthcheck(self):
        self.assertGreaterEqual(self.dockerfile.count("FROM ${PYTHON_IMAGE}"), 2)
        self.assertIn("USER 10001:10001", self.dockerfile)
        self.assertIn("HEALTHCHECK", self.dockerfile)
        self.assertIn("PYTHONDONTWRITEBYTECODE=1", self.dockerfile)
        self.assertIn("ENTRYPOINT", self.dockerfile)
        self.assertIn("/wheels/boto3-*.whl", self.dockerfile)
        self.assertIn("ai-trade-entrypoint", self.dockerfile)

    def test_compose_requires_authenticated_container_mode_on_host_loopback(self):
        self.assertIn(
            '"127.0.0.1:${AI_TRADE_DOCKER_PORT:-8877}:8765"', self.compose
        )
        self.assertIn("--container-bind", self.compose)
        self.assertNotIn("--owner-local", self.compose)
        self.assertIn("read_only: true", self.compose)
        self.assertIn("no-new-privileges:true", self.compose)
        self.assertIn("cap_drop:", self.compose)
        self.assertIn("- ALL", self.compose)

    def test_persistent_writes_are_explicit_mounts(self):
        for source, target in (
            ("config", "/workspace/config"),
            ("data", "/workspace/data"),
            ("reports", "/workspace/reports"),
            ("state", "/workspace/state"),
            ("logs", "/workspace/logs"),
            ("local", "/workspace/local"),
        ):
            with self.subTest(source=source):
                self.assertIn(f"source: {source}", self.compose)
                self.assertIn(f"target: {target}", self.compose)
                self.assertIn(f"source: ./{source}", self.bind_compose)
                self.assertIn(f"target: {target}", self.bind_compose)

    def test_build_context_excludes_local_state_and_credentials(self):
        for value in (
            ".git",
            ".venv",
            "data/cache/*",
            "reports/*",
            "state/*",
            "logs/*",
            "local/*",
            ".env.*",
        ):
            with self.subTest(value=value):
                self.assertIn(value, self.ignore.splitlines())
        self.assertNotIn("qwerty123", self.example)
        self.assertNotIn("sk-", self.example)

    def test_model_governance_limits_are_explicit(self):
        for name in (
            "AI_TRADE_AI_MAX_RETRIES",
            "AI_TRADE_AI_MAX_CONCURRENT_CALLS",
            "AI_TRADE_AI_MAX_TOKENS_PER_CALL",
            "AI_TRADE_AI_DAILY_TOKEN_BUDGET",
            "AI_TRADE_AI_INPUT_COST_PER_MILLION_USD",
            "AI_TRADE_AI_OUTPUT_COST_PER_MILLION_USD",
            "AI_TRADE_AI_DAILY_COST_BUDGET_USD",
        ):
            with self.subTest(name=name):
                self.assertIn(name, self.compose)
                self.assertIn(name, self.example)


if __name__ == "__main__":
    unittest.main()
