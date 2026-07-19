import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from ai_trade.cli import _maybe_automatic_cloud_backup, build_parser, main
from ai_trade.config import _validate_auth, load_config
from ai_trade.research_digest import ResearchDigestCapacityError
from ai_trade.web.auth import UserStore


class CliTests(unittest.TestCase):
    def test_automatic_cloud_backup_failure_emits_safe_web_job_event(self):
        endpoint = "https://secret-account.r2.cloudflarestorage.com"
        bucket = "private-bucket-name"
        object_key = "private/object/key.zip"
        secret = "secret-access-value"
        unsafe_error = RuntimeError(
            f"endpoint={endpoint} bucket={bucket} key={object_key} secret={secret}"
        )
        environment = {
            "AI_TRADE_WEB_JOB_PROTOCOL": "1",
            "AI_TRADE_R2_ENDPOINT": endpoint,
            "AI_TRADE_R2_BUCKET": bucket,
            "AI_TRADE_R2_SECRET_ACCESS_KEY": secret,
        }
        stderr = io.StringIO()

        with (
            patch.dict(os.environ, environment, clear=False),
            patch(
                "ai_trade.cloud.automatic_cloud_backup_enabled", return_value=True
            ),
            patch("ai_trade.cloud.load_cloud_settings", return_value=object()),
            patch("ai_trade.cloud.tracked_r2_store", return_value=object()),
            patch("ai_trade.cloud.backup_market_cache", side_effect=unsafe_error),
            patch("ai_trade.cli.logging.getLogger"),
            redirect_stderr(stderr),
        ):
            self.assertIsNone(_maybe_automatic_cloud_backup(object()))

        output = stderr.getvalue()
        self.assertEqual(
            output,
            '@@AI_TRADE_CLOUD_BACKUP@@{"schema_version":1,"status":"failed"}\n',
        )
        for sensitive_value in (endpoint, bucket, object_key, secret):
            self.assertNotIn(sensitive_value, output)

    def test_serve_parser_defaults_to_loopback(self):
        args = build_parser().parse_args(["serve"])
        self.assertEqual(args.host, "127.0.0.1")
        self.assertEqual(args.port, 8765)
        self.assertFalse(args.owner_local)
        owner = build_parser().parse_args(["serve", "--owner-local"])
        self.assertTrue(owner.owner_local)

    def test_assistant_analyze_parser_and_command(self):
        args = build_parser().parse_args(
            [
                "assistant-analyze",
                "--symbol",
                "510300",
                "--lookback",
                "240",
                "--mode",
                "model",
            ]
        )
        self.assertEqual(args.symbol, "510300")
        self.assertEqual(args.lookback, 240)
        self.assertEqual(args.mode, "model")

        config = object()
        market = object()
        engine = MagicMock()
        engine.analyze.return_value = {"analysis_id": "a" * 32}
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli._ensure_cache"),
            patch("ai_trade.cli.MarketData", return_value=market),
            patch("ai_trade.cli.AssistantEngine", return_value=engine),
            redirect_stdout(output),
        ):
            status = main(
                [
                    "assistant-analyze",
                    "--symbol",
                    "510300",
                    "--lookback",
                    "240",
                    "--mode",
                    "model",
                ]
            )

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue()), {"analysis_id": "a" * 32})
        engine.analyze.assert_called_once_with(
            market,
            "510300",
            lookback=240,
            mode="model",
            user_id="local-owner",
        )

    def test_market_intelligence_refresh_uses_explicit_iso_date(self):
        parsed = build_parser().parse_args(
            ["market-intelligence-refresh", "--date", "2024-01-05"]
        )
        self.assertEqual(parsed.command, "market-intelligence-refresh")
        self.assertEqual(parsed.date, "2024-01-05")

        config = object()
        result = {
            "schema_version": 1,
            "date": "2024-01-05",
            "status": "current",
            "available": True,
            "errors": [],
        }
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.MarketData") as market_data,
            patch("ai_trade.cli.download_universe") as download,
            patch(
                "ai_trade.data.market_intelligence.refresh_dragon_tiger",
                return_value=result,
            ) as refresh,
            redirect_stdout(output),
        ):
            status = main(
                ["market-intelligence-refresh", "--date", "2024-01-05"]
            )

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue()), result)
        refresh.assert_called_once_with(config, date(2024, 1, 5))
        market_data.assert_not_called()
        download.assert_not_called()

    def test_market_intelligence_refresh_defaults_to_verified_cache_date(self):
        parsed = build_parser().parse_args(["market-intelligence-refresh"])
        self.assertIsNone(parsed.date)

        config = object()
        market = MagicMock()
        market.latest_date.return_value = date(2024, 1, 8)
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.MarketData", return_value=market) as market_data,
            patch("ai_trade.cli._ensure_cache") as ensure_cache,
            patch("ai_trade.cli.download_universe") as download,
            patch(
                "ai_trade.data.market_intelligence.refresh_dragon_tiger",
                return_value={
                    "date": "2024-01-08",
                    "status": "empty",
                    "available": True,
                    "errors": [],
                },
            ) as refresh,
            redirect_stdout(output),
        ):
            status = main(["market-intelligence-refresh"])

        self.assertEqual(status, 0)
        market_data.assert_called_once_with(config)
        market.latest_date.assert_called_once_with()
        refresh.assert_called_once_with(config, date(2024, 1, 8))
        ensure_cache.assert_not_called()
        download.assert_not_called()

    def test_market_breadth_refresh_uses_explicit_iso_date(self):
        parsed = build_parser().parse_args(
            ["market-breadth-refresh", "--date", "2024-01-05"]
        )
        self.assertEqual(parsed.command, "market-breadth-refresh")
        self.assertEqual(parsed.date, "2024-01-05")

        config = object()
        result = {
            "schema_version": 1,
            "trade_date": "2024-01-05",
            "status": "current",
            "available": True,
            "errors": [],
        }
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.MarketData") as market_data,
            patch(
                "ai_trade.data.market_breadth.refresh_market_breadth",
                return_value=result,
            ) as refresh,
            redirect_stdout(output),
        ):
            status = main(
                ["market-breadth-refresh", "--date", "2024-01-05"]
            )

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue()), result)
        refresh.assert_called_once_with(config, date(2024, 1, 5))
        market_data.assert_not_called()

    def test_market_breadth_refresh_defaults_to_verified_cache_date(self):
        config = object()
        market = MagicMock()
        market.latest_date.return_value = date(2024, 1, 8)
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.MarketData", return_value=market) as market_data,
            patch(
                "ai_trade.data.market_breadth.refresh_market_breadth",
                return_value={
                    "trade_date": "2024-01-08",
                    "status": "current",
                    "available": True,
                    "errors": [],
                },
            ) as refresh,
            redirect_stdout(output),
        ):
            status = main(["market-breadth-refresh"])

        self.assertEqual(status, 0)
        market_data.assert_called_once_with(config)
        market.latest_date.assert_called_once_with()
        refresh.assert_called_once_with(config, date(2024, 1, 8))

    def test_capital_flow_refresh_uses_explicit_iso_date(self):
        parsed = build_parser().parse_args(
            ["capital-flow-refresh", "--date", "2024-01-05"]
        )
        self.assertEqual(parsed.command, "capital-flow-refresh")
        self.assertEqual(parsed.date, "2024-01-05")

        config = object()
        result = {
            "schema_version": 1,
            "trade_date": "2024-01-05",
            "status": "current",
            "available": True,
            "errors": [],
        }
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.MarketData") as market_data,
            patch(
                "ai_trade.data.capital_flow.refresh_capital_flow",
                return_value=result,
            ) as refresh,
            redirect_stdout(output),
        ):
            status = main(
                ["capital-flow-refresh", "--date", "2024-01-05"]
            )

        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output.getvalue()), result)
        refresh.assert_called_once_with(config, date(2024, 1, 5))
        market_data.assert_not_called()

    def test_capital_flow_refresh_defaults_to_verified_cache_date(self):
        config = object()
        market = MagicMock()
        market.latest_date.return_value = date(2024, 1, 8)
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.MarketData", return_value=market),
            patch(
                "ai_trade.data.capital_flow.refresh_capital_flow",
                return_value={
                    "trade_date": "2024-01-08",
                    "status": "current",
                    "available": True,
                    "errors": [],
                },
            ) as refresh,
            redirect_stdout(output),
        ):
            status = main(["capital-flow-refresh"])

        self.assertEqual(status, 0)
        market.latest_date.assert_called_once_with()
        refresh.assert_called_once_with(config, date(2024, 1, 8))

    def test_market_intelligence_refresh_rejects_bad_date_and_reports_failure(self):
        config = object()
        refresh = MagicMock()
        stderr = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.logging.getLogger"),
            patch(
                "ai_trade.data.market_intelligence.refresh_dragon_tiger",
                refresh,
            ),
            redirect_stderr(stderr),
        ):
            status = main(
                ["market-intelligence-refresh", "--date", "20240105"]
            )

        self.assertEqual(status, 1)
        self.assertIn("YYYY-MM-DD", stderr.getvalue())
        refresh.assert_not_called()

        unavailable = {
            "date": "2024-01-05",
            "status": "unavailable",
            "available": False,
            "errors": [
                {"code": "provider_unavailable", "message": "provider unavailable"}
            ],
        }
        refresh.return_value = unavailable
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch(
                "ai_trade.data.market_intelligence.refresh_dragon_tiger",
                refresh,
            ),
            redirect_stdout(output),
        ):
            status = main(
                ["market-intelligence-refresh", "--date", "2024-01-05"]
            )

        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue()), unavailable)
        refresh.assert_called_once_with(config, date(2024, 1, 5))

    def test_archive_generate_parser_has_bounded_audit_trigger(self):
        args = build_parser().parse_args(["archive-generate"])
        self.assertEqual(args.trigger, "manual")
        self.assertFalse(args.all_profiles)

        scheduled = build_parser().parse_args(
            ["archive-generate", "--all-profiles", "--trigger", "scheduled"]
        )
        self.assertTrue(scheduled.all_profiles)
        self.assertEqual(scheduled.trigger, "scheduled")

        for invalid in ("web", "backfill", "rebuild"):
            with (
                self.subTest(trigger=invalid),
                self.assertRaises(SystemExit),
                redirect_stderr(io.StringIO()),
            ):
                build_parser().parse_args(
                    ["archive-generate", "--trigger", invalid]
                )

    def test_archive_generate_passes_scheduled_trigger_to_every_profile(self):
        config = MagicMock()
        service = MagicMock()
        service.generate_research_digests.return_value = {
            "available": True,
            "status": "current",
            "summary": {"written": 1, "reused": 0},
            "errors": [],
        }
        users = MagicMock()
        users.enabled_account_ids.return_value = (
            "acct_" + "a" * 32,
            "acct_" + "b" * 32,
        )
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch(
                "ai_trade.web.service.DashboardService", return_value=service
            ),
            patch("ai_trade.web.auth.UserStore", return_value=users),
            redirect_stdout(output),
        ):
            status = main(
                ["archive-generate", "--all-profiles", "--trigger", "scheduled"]
            )

        self.assertEqual(status, 0)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["scope"], "all_profiles")
        self.assertEqual(payload["trigger"], "scheduled")
        self.assertEqual(payload["profiles_processed"], 3)
        self.assertEqual(service.generate_research_digests.call_count, 3)
        for call in service.generate_research_digests.call_args_list:
            self.assertEqual(call.kwargs["trigger"], "scheduled")
            self.assertEqual(call.kwargs["actor"], "scheduled-archive")

    def test_archive_all_profiles_reports_enumeration_failure(self):
        config = MagicMock()
        service = MagicMock()
        service.generate_research_digests.return_value = {
            "available": True,
            "status": "current",
            "summary": {"written": 1, "reused": 0},
            "errors": [],
        }
        users = MagicMock()
        users.enabled_account_ids.side_effect = ValueError("invalid beta store")
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch("ai_trade.cli.logging.getLogger"),
            patch(
                "ai_trade.web.service.DashboardService", return_value=service
            ),
            patch("ai_trade.web.auth.UserStore", return_value=users),
            redirect_stdout(output),
        ):
            status = main(["archive-generate", "--all-profiles"])

        self.assertEqual(status, 1)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["profiles_processed"], 1)
        self.assertEqual(payload["profile_warning"], "invalid beta store")
        service.generate_research_digests.assert_called_once()

    def test_archive_all_profiles_isolates_expected_profile_failures(self):
        account_ids = ("acct_" + "a" * 32, "acct_" + "b" * 32)
        success = {
            "available": True,
            "status": "current",
            "summary": {"written": 1, "reused": 0},
            "errors": [],
        }
        expected = (
            (OSError("disk unavailable"), "research_digest_generation_failed"),
            (RuntimeError("ledger unavailable"), "research_digest_generation_failed"),
            (ValueError("invalid evidence"), "research_digest_generation_failed"),
            (
                ResearchDigestCapacityError("digest capacity reached"),
                "research_digest_capacity",
            ),
        )
        for failure, error_code in expected:
            with self.subTest(error=type(failure).__name__):
                config = MagicMock()
                service = MagicMock()
                service.generate_research_digests.side_effect = [
                    success,
                    failure,
                    success,
                ]
                users = MagicMock()
                users.enabled_account_ids.return_value = account_ids
                output = io.StringIO()
                with (
                    patch("ai_trade.cli.load_config", return_value=config),
                    patch("ai_trade.cli._configure_logging"),
                    patch("ai_trade.cli.logging.getLogger"),
                    patch(
                        "ai_trade.web.service.DashboardService",
                        return_value=service,
                    ),
                    patch("ai_trade.web.auth.UserStore", return_value=users),
                    redirect_stdout(output),
                ):
                    status = main(["archive-generate", "--all-profiles"])

                self.assertEqual(status, 1)
                self.assertEqual(service.generate_research_digests.call_count, 3)
                payload = json.loads(output.getvalue())
                self.assertEqual(payload["profiles_processed"], 3)
                failed = payload["profiles"][1]
                self.assertFalse(failed["available"])
                self.assertEqual(failed["status"], "unavailable")
                self.assertEqual(failed["errors"][0]["code"], error_code)
                self.assertTrue(payload["profiles"][2]["available"])

    def test_archive_all_profiles_does_not_swallow_unknown_base_exception(self):
        class FatalArchiveError(BaseException):
            pass

        config = MagicMock()
        service = MagicMock()
        service.generate_research_digests.side_effect = FatalArchiveError(
            "stop immediately"
        )
        users = MagicMock()
        users.enabled_account_ids.return_value = ("acct_" + "a" * 32,)
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch(
                "ai_trade.web.service.DashboardService", return_value=service
            ),
            patch("ai_trade.web.auth.UserStore", return_value=users),
            self.assertRaises(FatalArchiveError),
        ):
            main(["archive-generate", "--all-profiles"])

        service.generate_research_digests.assert_called_once()

    def test_archive_partial_profile_is_nonzero_even_after_prefix_commit(self):
        config = MagicMock()
        service = MagicMock()
        service.generate_research_digests.return_value = {
            "available": True,
            "status": "partial",
            "summary": {"written": 1, "reused": 0},
            "errors": [{"code": "partial", "message": "second period failed"}],
        }
        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch(
                "ai_trade.web.service.DashboardService", return_value=service
            ),
            redirect_stdout(output),
        ):
            status = main(["archive-generate"])

        self.assertEqual(status, 1)
        payload = json.loads(output.getvalue())
        self.assertEqual(payload["profiles"][0]["status"], "partial")

    def test_archive_partial_evidence_without_write_error_is_successful(self):
        config = MagicMock()
        service = MagicMock()
        service.generate_research_digests.return_value = {
            "available": True,
            "status": "partial",
            "summary": {"written": 2, "reused": 0},
            "errors": [],
        }
        with (
            patch("ai_trade.cli.load_config", return_value=config),
            patch("ai_trade.cli._configure_logging"),
            patch(
                "ai_trade.web.service.DashboardService", return_value=service
            ),
            redirect_stdout(io.StringIO()),
        ):
            status = main(["archive-generate"])

        self.assertEqual(status, 0)

    def test_archive_scheduler_scripts_declare_scheduled_scope(self):
        scripts = Path(__file__).resolve().parents[1] / "scripts"
        paper = (scripts / "run_daily_paper.ps1").read_text(encoding="utf-8")
        archive = (scripts / "run_daily_archive.ps1").read_text(encoding="utf-8")
        installer = (scripts / "install_archive_task.ps1").read_text(
            encoding="utf-8"
        )

        self.assertIn("archive-generate --trigger scheduled", paper)
        self.assertIn(
            "archive-generate --all-profiles --trigger scheduled", archive
        )
        self.assertIn("-WindowStyle Hidden", installer)
        self.assertIn("-MultipleInstances IgnoreNew", installer)
        self.assertIn("[string]$RunAt = '18:30'", installer)

    def test_broker_probe_commands_are_read_only_cli_surfaces(self):
        self.assertEqual(build_parser().parse_args(["broker-list"]).command, "broker-list")
        self.assertEqual(
            build_parser().parse_args(["broker-probe"]).command, "broker-probe"
        )
        self.assertEqual(
            build_parser().parse_args(["broker-compare"]).command,
            "broker-compare",
        )

        output = io.StringIO()
        with (
            patch("ai_trade.cli.load_config", return_value=object()),
            patch("ai_trade.cli._configure_logging"),
            patch(
                "ai_trade.cli.probe_configured_broker",
                return_value={"evidence": {"qualifying_reconciliation_recorded": False}},
            ),
            redirect_stdout(output),
        ):
            status = main(["broker-probe"])
        self.assertEqual(status, 0)
        self.assertFalse(
            json.loads(output.getvalue())["evidence"][
                "qualifying_reconciliation_recorded"
            ]
        )

    def test_packaged_default_matches_repository_config(self):
        root = Path(__file__).resolve().parents[1]
        repository = json.loads((root / "config/default.json").read_text(encoding="utf-8"))
        packaged = json.loads(
            (root / "src/ai_trade/default_config.json").read_text(encoding="utf-8")
        )
        self.assertEqual(packaged, repository)
        repository_master = json.loads(
            (root / "config/security_master.json").read_text(encoding="utf-8")
        )
        packaged_master = json.loads(
            (root / "src/ai_trade/default_security_master.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(packaged_master, repository_master)

    def test_auth_configuration_validation(self):
        _validate_auth({})
        _validate_auth({"enabled": True, "session_hours": 8})
        for value, message in (
            ({"enabled": "yes"}, "enabled"),
            ({"users_file": ""}, "users_file"),
            ({"session_hours": 0}, "session_hours"),
            ({"max_failed_attempts": True}, "max_failed_attempts"),
        ):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, message):
                _validate_auth(value)

    def test_init_creates_standalone_workspace(self):
        with tempfile.TemporaryDirectory() as temporary:
            target = Path(temporary) / "workspace"
            self.assertEqual(main(["init", "--directory", str(target)]), 0)
            self.assertTrue((target / "config/default.json").exists())
            self.assertTrue((target / "config/security_master.json").exists())
            self.assertTrue((target / "data/cache/.gitkeep").exists())
            self.assertTrue((target / "state/.gitkeep").exists())

    def test_beta_user_cli_and_portable_whitelist(self):
        with tempfile.TemporaryDirectory() as temporary, patch(
            "ai_trade.cli._configure_logging"
        ):
            root = Path(temporary)
            first = root / "first"
            second = root / "second"
            self.assertEqual(main(["init", "--directory", str(first)]), 0)
            self.assertEqual(main(["init", "--directory", str(second)]), 0)
            first_config = first / "config/default.json"
            second_config = second / "config/default.json"
            password = "local-test-password"
            with patch("ai_trade.cli.getpass", side_effect=[password, password]):
                self.assertEqual(
                    main(
                        [
                            "--config",
                            str(first_config),
                            "beta-user-add",
                            "tester",
                        ]
                    ),
                    0,
                )
            first_store = UserStore(load_config(first_config).auth_users_file)
            self.assertTrue(first_store.verify("tester", password))
            self.assertNotIn(
                password,
                first_store.path.read_text(encoding="utf-8"),
            )

            bundle = root / "beta-users.json"
            self.assertEqual(
                main(
                    [
                        "--config",
                        str(first_config),
                        "beta-users-export",
                        str(bundle),
                    ]
                ),
                0,
            )
            self.assertNotIn(password, bundle.read_text(encoding="utf-8"))
            self.assertEqual(
                main(
                    [
                        "--config",
                        str(second_config),
                        "beta-users-import",
                        str(bundle),
                    ]
                ),
                0,
            )
            second_store = UserStore(load_config(second_config).auth_users_file)
            self.assertTrue(second_store.verify("tester", password))


if __name__ == "__main__":
    unittest.main()
