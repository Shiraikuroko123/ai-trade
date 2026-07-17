from __future__ import annotations

import hashlib
import unittest
from pathlib import Path

from scripts.verify_distribution import (
    SDIST_REQUIRED,
    VENDORED_FILES_SHA256,
    WHEEL_REQUIRED,
    _is_text_member,
    _verify_safe_unique_names,
    _verify_text_content,
    _verify_release_versions,
)


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


class DistributionVerificationTests(unittest.TestCase):
    def test_market_refresh_modules_are_required_in_both_artifacts(self):
        for module in ("cache_snapshot.py", "tencent.py"):
            with self.subTest(module=module):
                self.assertIn(f"ai_trade/data/{module}", WHEEL_REQUIRED)
                self.assertIn(f"src/ai_trade/data/{module}", SDIST_REQUIRED)

    def test_cloud_usage_module_is_required_in_both_artifacts(self):
        self.assertIn("ai_trade/cloud_usage.py", WHEEL_REQUIRED)
        self.assertIn("src/ai_trade/cloud_usage.py", SDIST_REQUIRED)

    def test_assistant_release_surface_is_required_in_both_artifacts(self):
        for module in (
            "__init__.py",
            "engine.py",
            "features.py",
            "provider.py",
            "store.py",
        ):
            with self.subTest(module=module):
                self.assertIn(f"ai_trade/assistant/{module}", WHEEL_REQUIRED)
                self.assertIn(f"src/ai_trade/assistant/{module}", SDIST_REQUIRED)
        self.assertIn("docs/AI_ASSISTANT.md", SDIST_REQUIRED)
        self.assertIn("scripts/configure_ai.ps1", SDIST_REQUIRED)

    def test_strategy_lab_package_is_required_in_both_artifacts(self):
        for module in (
            "__init__.py",
            "engine.py",
            "lifecycle.py",
            "schema.py",
            "store.py",
        ):
            with self.subTest(module=module):
                self.assertIn(f"ai_trade/strategy_lab/{module}", WHEEL_REQUIRED)
                self.assertIn(f"src/ai_trade/strategy_lab/{module}", SDIST_REQUIRED)

    def test_broker_safety_surface_is_required_in_both_artifacts(self):
        for module in (
            "base.py",
            "ledger.py",
            "live.py",
            "live_guard.py",
            "mandate.py",
            "probe.py",
            "reconciliation.py",
            "shadow.py",
        ):
            with self.subTest(module=module):
                self.assertIn(f"ai_trade/broker/{module}", WHEEL_REQUIRED)
                self.assertIn(f"src/ai_trade/broker/{module}", SDIST_REQUIRED)

    def test_vendored_klinecharts_is_required_and_matches_upstream(self):
        assets = REPOSITORY_ROOT / "src" / "ai_trade" / "web" / "assets"
        for name, expected in VENDORED_FILES_SHA256.items():
            with self.subTest(name=name):
                self.assertIn(f"ai_trade/web/assets/{name}", WHEEL_REQUIRED)
                self.assertIn(f"src/ai_trade/web/assets/{name}", SDIST_REQUIRED)
                actual = hashlib.sha256((assets / name).read_bytes()).hexdigest()
                self.assertEqual(actual, expected)
        self.assertIn(
            "ai_trade/web/assets/vendor/klinecharts.SOURCE.txt", WHEEL_REQUIRED
        )
        self.assertIn(
            "src/ai_trade/web/assets/vendor/klinecharts.SOURCE.txt", SDIST_REQUIRED
        )

    def test_disabling_cloud_preserves_the_installation_identity(self):
        source = (REPOSITORY_ROOT / "scripts/configure_cloud.ps1").read_text(
            encoding="utf-8"
        )
        disable_block = source.split("if ($Disable) {", 1)[1].split(
            "function Read-EnvFile", 1
        )[0]

        self.assertNotIn("AI_TRADE_CLOUD_INSTALLATION_ID", disable_block)
        self.assertNotIn("AI_TRADE_CLOUD_PREFIX", disable_block)

    def test_sensitive_release_content_is_rejected(self):
        cases = (
            b"endpoint=https://0123456789abcdef0123456789abcdef.r2.cloudflarestorage.com",
            b"AI_TRADE_R2_SECRET_ACCESS_KEY=literal-secret-value-1234",
            b"AI_TRADE_AI_API_KEY=literal-ai-provider-key-1234",
            b"OPENAI_API_KEY=literal-openai-provider-key-1234",
            b"GITHUB_TOKEN=literal-github-token-value-1234",
            b"Authorization: Bearer literal-bearer-token-1234",
            b"github_pat_0123456789abcdefghijklmnop",
            b"sk-0123456789abcdefghijklmnop",
            b"log=C:\\Users\\developer\\project\\trace.log",
            b"proxy=https://account:password@proxy.example",
            b"-----BEGIN PRIVATE KEY-----",
        )
        for content in cases:
            with self.subTest(content=content), self.assertRaises(SystemExit):
                _verify_text_content("sample.txt", content, "sample.zip")

    def test_documented_variable_names_and_placeholders_are_allowed(self):
        content = (
            b"AI_TRADE_R2_SECRET_ACCESS_KEY\n"
            b"AI_TRADE_R2_ENDPOINT=https://<account-id>.r2.cloudflarestorage.com\n"
            b"AI_TRADE_R2_SECRET_ACCESS_KEY=$secretKey\n"
            b"AI_TRADE_AI_API_KEY=<api-key>\n"
            b"Authorization: Bearer ${ACCESS_TOKEN}\n"
        )
        _verify_text_content("guide.md", content, "sample.zip")

    def test_runtime_cache_and_sensitive_files_are_rejected(self):
        cases = (
            "ai_trade-0.11.0/data/cache/prices.parquet",
            "ai_trade-0.11.0/state/ledger.json",
            "ai_trade-0.11.0/credentials.json",
            "ai_trade-0.11.0/token.json",
            "ai_trade-0.11.0/client.pem",
            "ai_trade-0.11.0/audit.sqlite3",
        )
        for name in cases:
            with self.subTest(name=name), self.assertRaises(SystemExit):
                _verify_safe_unique_names([name], "sample.tar.gz")

    def test_structured_text_formats_are_scanned(self):
        for name in ("data.csv", "config.yaml", "settings.ini", "script.sh"):
            with self.subTest(name=name):
                self.assertTrue(_is_text_member(name))

    def test_verifier_source_passes_its_own_content_scan(self):
        path = REPOSITORY_ROOT / "scripts" / "verify_distribution.py"
        _verify_text_content(path.name, path.read_bytes(), "sample.zip")

    def test_release_versions_must_match_source_and_artifact_names(self):
        _verify_release_versions(
            "0.8.0",
            "0.8.0",
            "0.8.0",
            "ai_trade-0.8.0-py3-none-any.whl",
            "ai_trade-0.8.0.tar.gz",
        )
        cases = (
            (
                "0.7.0",
                "0.8.0",
                "ai_trade-0.8.0-py3-none-any.whl",
                "ai_trade-0.8.0.tar.gz",
            ),
            (
                "0.8.0",
                "0.7.0",
                "ai_trade-0.8.0-py3-none-any.whl",
                "ai_trade-0.8.0.tar.gz",
            ),
            (
                "0.8.0",
                "0.8.0",
                "ai_trade-0.7.0-py3-none-any.whl",
                "ai_trade-0.8.0.tar.gz",
            ),
            (
                "0.8.0",
                "0.8.0",
                "ai_trade-0.8.0-py3-none-any.whl",
                "ai_trade-0.7.0.tar.gz",
            ),
        )
        for wheel_version, sdist_version, wheel_name, sdist_name in cases:
            with (
                self.subTest(
                    wheel_version=wheel_version,
                    sdist_version=sdist_version,
                    wheel_name=wheel_name,
                    sdist_name=sdist_name,
                ),
                self.assertRaises(SystemExit),
            ):
                _verify_release_versions(
                    "0.8.0",
                    wheel_version,
                    sdist_version,
                    wheel_name,
                    sdist_name,
                )


if __name__ == "__main__":
    unittest.main()
