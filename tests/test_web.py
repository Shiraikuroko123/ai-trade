import http.client
import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from ai_trade import __version__
from ai_trade.cli import main
from ai_trade.config import load_config
from ai_trade.models import Bar, Instrument
from ai_trade.web.jobs import COMMANDS, JobManager
from ai_trade.web.server import _parse_host_header, create_dashboard_server
from ai_trade.web.service import DashboardService


class ServiceMarket:
    def latest_date(self):
        return date(2024, 1, 3)

    def latest_bar_on_or_before(self, symbol, on_date):
        return Bar(on_date, 10, 10, 10, 10, 100, 1000)

    def instrument(self, symbol):
        return Instrument(
            symbol,
            "沪深300ETF",
            "SH",
            "equity",
            asset_class="equity",
            sector="broad_market",
        )


class WebTests(unittest.TestCase):
    def test_first_use_system_and_overview_report_missing_market_data(self):
        source = load_config(
            Path(__file__).resolve().parents[1] / "config/default.json"
        )
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(source, project_root=Path(temporary))
            service = DashboardService(config)

            system = service.system()
            self.assertEqual(system["diagnosis"]["status"], "ERROR")
            self.assertEqual(
                system["diagnosis"]["missing_cache_symbols"],
                [instrument.symbol for instrument in config.instruments],
            )
            self.assertEqual(system["errors"][0]["recovery_action"], "refresh-data")
            self.assertIn("download --force", system["errors"][0]["recovery_command"])

            overview = service.overview()
            self.assertFalse(overview["market"]["available"])
            self.assertIsNone(overview["market"]["date"])
            self.assertIsNone(overview["signal"])
            self.assertFalse(overview["paper"]["initialized"])
            self.assertEqual(
                overview["market"]["error"]["code"], "market_data_unavailable"
            )
            self.assertIn("refresh-data", overview["market"]["error"]["message"])

    def test_first_use_system_and_overview_endpoints_return_200(self):
        source = load_config(
            Path(__file__).resolve().parents[1] / "config/default.json"
        )
        with tempfile.TemporaryDirectory() as temporary:
            config = replace(source, project_root=Path(temporary))
            server, _ = create_dashboard_server(config, port=0, auth_enabled=False)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                status, _, body = _request(server.server_port, "GET", "/api/system")
                self.assertEqual(status, 200, body)
                payload = json.loads(body)
                self.assertEqual(payload["diagnosis"]["status"], "ERROR")
                self.assertEqual(
                    payload["errors"][0]["code"], "market_data_unavailable"
                )

                status, _, body = _request(server.server_port, "GET", "/api/overview")
                self.assertEqual(status, 200)
                payload = json.loads(body)
                self.assertFalse(payload["market"]["available"])
                self.assertEqual(
                    payload["market"]["error"]["recovery_action"], "refresh-data"
                )
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_service_handles_missing_reports_and_uninitialized_account(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = SimpleNamespace(reports_dir=Path(temporary))
            service = DashboardService(config)
            research = service.research()
            self.assertIsNone(research["backtest"]["metrics"])
            self.assertEqual(research["walk_forward"]["segments"], [])
            service.market = lambda: ServiceMarket()
            service._paper_state = lambda: None
            portfolio = service.portfolio()
            self.assertFalse(portfolio["initialized"])
            self.assertEqual(portfolio["positions"], [])

    def test_service_builds_initialized_portfolio(self):
        service = DashboardService(SimpleNamespace())
        service.market = lambda: ServiceMarket()
        service._paper_state = lambda: {
            "account_id": "paper",
            "last_run_date": "2024-01-03",
            "last_equity": 2000,
            "cash": 1000,
            "high_water_mark": 2100,
            "positions": {"510300": 100},
            "pending_targets": {"510300": 0.6},
            "cooldown_remaining": 0,
            "pending_signal_date": "2024-01-03",
        }
        service._paper_equity_curve = lambda maximum: []
        portfolio = service.portfolio()
        self.assertTrue(portfolio["initialized"])
        self.assertEqual(portfolio["positions"][0]["market_value"], 1000)
        self.assertAlmostEqual(portfolio["pending_targets"][0]["difference"], 0.1)

    def test_job_manager_whitelist_duplicate_and_cancel(self):
        config = SimpleNamespace(path=Path("config.json"), project_root=Path.cwd())
        with patch.object(JobManager, "_work", return_value=None):
            manager = JobManager(config)
            first = manager.submit("backtest")
            self.assertIs(manager.submit("backtest"), first)
            paper_init = manager.submit("paper-init")
            self.assertEqual(COMMANDS[paper_init.action], ("paper-init",))
            cloud_backup = manager.submit("cloud-backup")
            self.assertEqual(COMMANDS[cloud_backup.action], ("cloud-backup",))
            intelligence = manager.submit("refresh-market-intelligence")
            self.assertEqual(
                COMMANDS[intelligence.action], ("market-intelligence-refresh",)
            )
            breadth = manager.submit("refresh-market-breadth")
            self.assertEqual(
                COMMANDS[breadth.action], ("market-breadth-refresh",)
            )
            capital_flow = manager.submit("refresh-capital-flow")
            self.assertEqual(
                COMMANDS[capital_flow.action], ("capital-flow-refresh",)
            )
            with self.assertRaisesRegex(ValueError, "Unsupported"):
                manager.submit("delete-everything")
            cancelled = manager.cancel(first.id)
            self.assertEqual(cancelled.status, "cancelled")
            cancelled_cloud = manager.cancel(cloud_backup.id)
            self.assertEqual(
                cancelled_cloud.payload()["cloud_backup"],
                {"status": "cancelled", "automatic": False},
            )
            manager.close()

    def test_job_manager_close_cancels_queue_and_rejects_new_work(self):
        config = SimpleNamespace(path=Path("config.json"), project_root=Path.cwd())
        with patch.object(JobManager, "_work", return_value=None):
            manager = JobManager(config)
            queued = manager.submit("backtest")
            manager.close()
            self.assertEqual(queued.status, "cancelled")
            self.assertIsNotNone(queued.finished_at)
            with self.assertRaisesRegex(RuntimeError, "closed"):
                manager.submit("validate")

    def test_cancel_during_process_startup_terminates_process(self):
        config = SimpleNamespace(path=Path("config.json"), project_root=Path.cwd())
        popen_entered = threading.Event()
        release_popen = threading.Event()
        process = _FakeProcess()

        def start_process(*args, **kwargs):
            popen_entered.set()
            release_popen.wait(timeout=2)
            return process

        with patch("ai_trade.web.jobs.subprocess.Popen", side_effect=start_process):
            manager = JobManager(config)
            job = manager.submit("cloud-backup")
            self.assertTrue(popen_entered.wait(timeout=2))
            manager.cancel(job.id)
            release_popen.set()
            deadline = time.monotonic() + 2
            while job.finished_at is None and time.monotonic() < deadline:
                time.sleep(0.01)
            self.assertTrue(process.terminated)
            self.assertEqual(job.status, "cancelled")
            self.assertEqual(
                job.payload()["cloud_backup"],
                {"status": "cancelled", "automatic": False},
            )
            manager.close()

    def test_job_manager_handles_cloud_backup_protocol_and_explicit_failure(self):
        config = SimpleNamespace(path=Path("config.json"), project_root=Path.cwd())
        marker = (
            '@@AI_TRADE_CLOUD_BACKUP@@{"schema_version":1,"status":"failed"}\n'
        )
        processes = [
            _FakeProcess(output=f"local task completed\n{marker}", return_code=0),
            _FakeProcess(output="cloud command failed\n", return_code=7),
        ]
        environments = []

        def start_process(*args, **kwargs):
            environments.append(kwargs["env"])
            return processes.pop(0)

        with patch("ai_trade.web.jobs.subprocess.Popen", side_effect=start_process):
            manager = JobManager(config)
            try:
                parent = manager.submit("refresh-data")
                _wait_for_job(parent)
                self.assertEqual(parent.status, "succeeded", parent.output)
                self.assertEqual(
                    parent.payload()["cloud_backup"],
                    {"status": "failed", "automatic": True},
                )
                self.assertEqual(parent.output, "local task completed\n")
                self.assertNotIn("@@AI_TRADE_CLOUD_BACKUP@@", parent.output)

                explicit = manager.submit("cloud-backup")
                _wait_for_job(explicit)
                self.assertEqual(explicit.status, "failed")
                self.assertEqual(
                    explicit.payload()["cloud_backup"],
                    {"status": "failed", "automatic": False},
                )
                self.assertEqual(
                    [value["AI_TRADE_WEB_JOB_PROTOCOL"] for value in environments],
                    ["1", "1"],
                )
            finally:
                manager.close()

    def test_market_intelligence_job_uses_fixed_command_without_ai_credentials(self):
        config = SimpleNamespace(path=Path("config.json"), project_root=Path.cwd())
        captured_commands = []
        captured_environments = []

        def start_process(*args, **kwargs):
            captured_commands.append(args[0])
            captured_environments.append(kwargs["env"])
            return _FakeProcess(output="done\n", return_code=0)

        sensitive = {
            "AI_TRADE_AI_API_KEY": "model-secret-value",
            "AI_TRADE_AI_BASE_URL": "https://models.example.test/v1",
            "AI_TRADE_AI_MODEL": "example-model",
            "AI_TRADE_AI_TIMEOUT_SECONDS": "30",
        }
        with (
            patch.dict(os.environ, sensitive, clear=False),
            patch("ai_trade.web.jobs.subprocess.Popen", side_effect=start_process),
        ):
            manager = JobManager(config)
            try:
                job = manager.submit("refresh-market-intelligence")
                _wait_for_job(job)
                self.assertEqual(job.status, "succeeded", job.output)
            finally:
                manager.close()

        self.assertEqual(len(captured_commands), 1)
        self.assertEqual(
            captured_commands[0][-3:],
            ["--config", str(config.path), "market-intelligence-refresh"],
        )
        self.assertNotIn("--date", captured_commands[0])
        self.assertFalse(
            any(
                name.startswith("AI_TRADE_AI_")
                for name in captured_environments[0]
            )
        )

    def test_close_stops_active_startup_and_never_runs_queued_job(self):
        config = SimpleNamespace(path=Path("config.json"), project_root=Path.cwd())
        popen_entered = threading.Event()
        release_popen = threading.Event()
        process = _FakeProcess()

        def start_process(*args, **kwargs):
            popen_entered.set()
            release_popen.wait(timeout=2)
            return process

        with patch(
            "ai_trade.web.jobs.subprocess.Popen", side_effect=start_process
        ) as popen:
            manager = JobManager(config)
            active = manager.submit("backtest")
            self.assertTrue(popen_entered.wait(timeout=2))
            queued = manager.submit("validate")
            closer = threading.Thread(target=manager.close, kwargs={"timeout": 2})
            closer.start()
            release_popen.set()
            closer.join(timeout=3)

            self.assertFalse(closer.is_alive())
            self.assertTrue(process.terminated)
            self.assertEqual(active.status, "cancelled")
            self.assertEqual(queued.status, "cancelled")
            self.assertEqual(popen.call_count, 1)
            self.assertFalse(manager._worker.is_alive())

    def test_paper_init_job_succeeds_once_without_overwriting_existing_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "workspace"
            self.assertEqual(main(["init", "--directory", str(workspace)]), 0)
            config = load_config(workspace / "config/default.json")
            manager = JobManager(config)
            try:
                first = manager.submit("paper-init")
                _wait_for_job(first)
                self.assertEqual(first.status, "succeeded", first.output)
                original_state = config.paper_state_file.read_bytes()

                second = manager.submit("paper-init")
                _wait_for_job(second)
                self.assertEqual(second.status, "failed")
                self.assertIn("already exists", second.output.lower())
                self.assertEqual(config.paper_state_file.read_bytes(), original_state)
            finally:
                manager.close()

    def test_server_rejects_non_loopback_binding(self):
        config = load_config(
            Path(__file__).resolve().parents[1] / "config/default.json"
        )
        with self.assertRaisesRegex(ValueError, "loopback"):
            create_dashboard_server(config, "0.0.0.0", 0)

    def test_static_api_host_and_write_token_security(self):
        config = load_config(
            Path(__file__).resolve().parents[1] / "config/default.json"
        )
        server, token = create_dashboard_server(config, port=0, auth_enabled=False)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        port = server.server_port
        try:
            status, headers, body = _request(port, "GET", "/")
            self.assertEqual(status, 200)
            self.assertIn("text/html", headers["content-type"])
            self.assertIn(b"AI Trade", body)

            status, headers, body = _request(port, "HEAD", "/?cache-bust=1")
            self.assertEqual(status, 200)
            self.assertGreater(int(headers["content-length"]), 0)
            self.assertEqual(body, b"")

            for path, content_type in (
                ("/app.css", "text/css"),
                ("/app.js", "javascript"),
            ):
                status, headers, body = _request(port, "GET", path)
                self.assertEqual(status, 200)
                self.assertIn(content_type, headers["content-type"])
                self.assertTrue(body)

            status, _, body = _request(port, "GET", "/api/bootstrap")
            self.assertEqual(status, 200)
            payload = json.loads(body)
            self.assertEqual(payload["token"], token)
            self.assertEqual(payload["version"], __version__)
            self.assertIn("backtest", payload["actions"])

            status, _, _ = _request(
                port, "GET", "/api/bootstrap", headers={"Host": "example.com"}
            )
            self.assertEqual(status, 403)

            status, _, _ = _request(
                port, "GET", "/api/bootstrap", headers={"Host": "[::1]evil"}
            )
            self.assertEqual(status, 403)

            status, _, _ = _request(
                port,
                "POST",
                "/api/jobs",
                body=json.dumps({"action": "backtest"}).encode(),
                headers={"Content-Type": "application/json"},
            )
            self.assertEqual(status, 403)

            status, _, body = _request(
                port,
                "POST",
                "/api/jobs",
                body=json.dumps({"action": "unknown"}).encode(),
                headers={
                    "Content-Type": "application/json",
                    "X-AI-Trade-Token": token,
                    "Origin": f"http://127.0.0.1:{port}",
                },
            )
            self.assertEqual(status, 400)
            self.assertIn(b"Unsupported job action", body)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_storage_api_is_safe_for_unconfigured_users_and_protects_writes(self):
        source = load_config(
            Path(__file__).resolve().parents[1] / "config/default.json"
        )
        cloud_names = {
            "AI_TRADE_CLOUD_ENABLED": "",
            "AI_TRADE_CLOUD_PREFIX": "",
            "AI_TRADE_CLOUD_INSTALLATION_ID": "",
            "AI_TRADE_R2_ENDPOINT": "",
            "AI_TRADE_R2_REGION": "",
            "AI_TRADE_R2_BUCKET": "",
            "AI_TRADE_R2_ACCESS_KEY_ID": "",
            "AI_TRADE_R2_SECRET_ACCESS_KEY": "",
        }
        with tempfile.TemporaryDirectory() as temporary, patch.dict(
            os.environ, cloud_names, clear=False
        ):
            config = replace(source, project_root=Path(temporary))
            server, token = create_dashboard_server(config, port=0, auth_enabled=False)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            port = server.server_port
            origin = f"http://127.0.0.1:{port}"
            try:
                status, _, body = _request(port, "GET", "/api/storage")
                self.assertEqual(status, 200)
                payload = json.loads(body)
                self.assertFalse(payload["configured"])
                self.assertEqual(payload["effective_storage_mode"], "local")
                self.assertFalse(payload["official_account_usage"])
                rendered = body.decode("utf-8")
                self.assertNotIn("endpoint_url", rendered)
                self.assertNotIn("secret_access_key", rendered)

                status, _, _ = _request(
                    port,
                    "POST",
                    "/api/storage/preferences",
                    body=json.dumps({"storage_mode": "local"}).encode(),
                    headers={"Content-Type": "application/json", "Origin": origin},
                )
                self.assertEqual(status, 403)

                status, _, body = _request(
                    port,
                    "POST",
                    "/api/storage/preferences",
                    body=json.dumps(
                        {
                            "storage_mode": "local",
                            "storage_limit_gb": 20,
                            "class_a_limit": 100,
                            "class_b_limit": 200,
                            "billing_cycle_day": 3,
                        }
                    ).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "X-AI-Trade-Token": token,
                        "Origin": origin,
                    },
                )
                self.assertEqual(status, 200, body)
                self.assertEqual(
                    json.loads(body)["preferences"]["storage_limit_gb"], 20
                )

                status, _, body = _request(
                    port,
                    "POST",
                    "/api/storage/preferences",
                    body=json.dumps({"storage_mode": "hybrid"}).encode(),
                    headers={
                        "Content-Type": "application/json",
                        "X-AI-Trade-Token": token,
                        "Origin": origin,
                    },
                )
                self.assertEqual(status, 400)
                self.assertIn(b"configured", body)

                status, _, body = _request(
                    port,
                    "POST",
                    "/api/storage/refresh",
                    headers={"X-AI-Trade-Token": token, "Origin": origin},
                )
                self.assertEqual(status, 200)
                self.assertTrue(json.loads(body)["inventory_error"])
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_host_header_parser_is_strict(self):
        self.assertEqual(_parse_host_header("127.0.0.1:8765"), ("127.0.0.1", 8765))
        self.assertEqual(_parse_host_header("[::1]:8765"), ("::1", 8765))
        for value in ("[::1]evil", "127.0.0.1:bad", "localhost:0", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                _parse_host_header(value)


class _FakeProcess:
    def __init__(self, output="", return_code=0):
        self.returncode = None
        self.terminated = False
        self.output = output
        self.completion_return_code = return_code

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def communicate(self):
        if self.returncode is None:
            self.returncode = self.completion_return_code
        return self.output, None


def _request(port, method, path, body=None, headers=None):
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    request_headers = dict(headers or {})
    if "Host" in request_headers:
        connection.putrequest(method, path, skip_host=True)
        for name, value in request_headers.items():
            connection.putheader(name, value)
        if body is not None:
            connection.putheader("Content-Length", str(len(body)))
        connection.endheaders(body)
    else:
        connection.request(method, path, body=body, headers=request_headers)
    response = connection.getresponse()
    value = (
        response.status,
        {name.lower(): value for name, value in response.getheaders()},
        response.read(),
    )
    connection.close()
    return value


def _wait_for_job(job, timeout=5):
    deadline = time.monotonic() + timeout
    while job.finished_at is None and time.monotonic() < deadline:
        time.sleep(0.01)
    if job.finished_at is None:
        raise AssertionError(f"Job {job.id} did not finish within {timeout} seconds")


if __name__ == "__main__":
    unittest.main()
