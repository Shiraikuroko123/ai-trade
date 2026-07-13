import copy
import http.client
import json
import tempfile
import threading
import unittest
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from ai_trade.config import load_config
from ai_trade.web.auth import MIN_PBKDF2_ITERATIONS, UserStore
from ai_trade.web.server import create_dashboard_server


USERNAME = "friend"
PASSWORD = "123456789"


class WebLoginTests(unittest.TestCase):
    def test_legacy_config_without_auth_defaults_to_login(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = load_config(
                Path(__file__).resolve().parents[1] / "config/default.json"
            )
            raw = copy.deepcopy(source.raw)
            raw.pop("auth")
            config = replace(source, project_root=Path(temporary), raw=raw)

            self.assertTrue(config.auth_enabled)
            with _running_server(config, auth_enabled=None) as port:
                status, headers, _ = _request(port, "GET", "/")
                self.assertEqual(status, 303)
                self.assertEqual(headers["location"], "/login")

                status, _, body = _request(port, "GET", "/api/auth/session")
                self.assertEqual(status, 200)
                session = json.loads(body)
                self.assertTrue(session["auth_enabled"])
                self.assertFalse(session["authenticated"])
                self.assertFalse(session["configured"])

    def test_unauthenticated_routes_and_host_precedence(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = _auth_config(Path(temporary))
            _create_user(config)
            _create_report(config)
            with _running_server(config) as port:
                status, headers, _ = _request(port, "GET", "/")
                self.assertEqual(status, 303)
                self.assertEqual(headers["location"], "/login")

                for path in ("/login", "/login.html", "/auth.css", "/auth.js"):
                    with self.subTest(path=path):
                        status, _, body = _request(port, "GET", path)
                        self.assertEqual(status, 200)
                        self.assertTrue(body)

                status, _, body = _request(port, "GET", "/api/auth/session")
                self.assertEqual(status, 200)
                self.assertFalse(json.loads(body)["authenticated"])

                for path in ("/api/overview", "/reports/sample.json"):
                    with self.subTest(path=path):
                        status, headers, _ = _request(port, "GET", path)
                        self.assertEqual(status, 401)
                        self.assertEqual(headers["www-authenticate"], "Session")

                status, _, body = _request(
                    port,
                    "GET",
                    "/api/overview",
                    headers={"Host": "example.com"},
                )
                self.assertEqual(status, 403)
                self.assertIn(b"Invalid host header", body)

    def test_login_requires_exact_origin_and_rate_limits_without_enumeration(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = _auth_config(Path(temporary), max_failed_attempts=3)
            _create_user(config)
            with _running_server(config) as port:
                payload = {"username": USERNAME, "password": "wrongpass"}
                status, _, _ = _request_json(
                    port, "POST", "/api/auth/login", payload
                )
                self.assertEqual(status, 403)

                for origin in (
                    "http://evil.example",
                    f"http://localhost:{port}",
                    f"http://127.0.0.1:{port}/",
                ):
                    with self.subTest(origin=origin):
                        status, _, _ = _request_json(
                            port,
                            "POST",
                            "/api/auth/login",
                            payload,
                            headers={"Origin": origin},
                        )
                        self.assertEqual(status, 403)

                origin = _origin(port)
                for attempt in range(1, 4):
                    status, headers, body = _request_json(
                        port,
                        "POST",
                        "/api/auth/login",
                        payload,
                        headers={"Origin": origin},
                    )
                    if attempt < 3:
                        self.assertEqual(status, 401)
                        self.assertNotIn("retry-after", headers)
                    else:
                        self.assertEqual(status, 429)
                        self.assertGreater(int(headers["retry-after"]), 0)
                        self.assertGreater(json.loads(body)["retry_after"], 0)

                status, headers, _ = _request_json(
                    port,
                    "POST",
                    "/api/auth/login",
                    {"username": USERNAME, "password": PASSWORD},
                    headers={"Origin": origin},
                )
                self.assertEqual(status, 429)
                self.assertGreater(int(headers["retry-after"]), 0)

    def test_authenticated_session_csrf_jobs_and_logout(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = _auth_config(Path(temporary))
            _create_user(config)
            with _running_server(config) as port:
                cookie, login = _login(port)
                attributes = {
                    value.strip().lower()
                    for value in login["set-cookie"].split(";")[1:]
                }
                self.assertIn("httponly", attributes)
                self.assertIn("samesite=strict", attributes)
                self.assertIn("path=/", attributes)
                self.assertIn("max-age=28800", attributes)
                self.assertNotIn("secure", attributes)
                self.assertFalse(any(value.startswith("domain=") for value in attributes))

                status, _, body = _request(
                    port,
                    "GET",
                    "/api/auth/session",
                    headers={"Cookie": cookie},
                )
                self.assertEqual(status, 200)
                session_status = json.loads(body)
                self.assertTrue(session_status["authenticated"])
                self.assertEqual(session_status["username"], USERNAME)

                status, _, body = _request(
                    port,
                    "GET",
                    "/api/bootstrap",
                    headers={"Cookie": cookie},
                )
                self.assertEqual(status, 200)
                bootstrap = json.loads(body)
                csrf = bootstrap["token"]
                self.assertTrue(csrf)
                self.assertNotEqual(csrf, cookie.split("=", 1)[1])
                self.assertEqual(bootstrap["user"]["username"], USERNAME)

                job_payload = {"action": "not-a-real-job"}
                cases = (
                    ({"Cookie": cookie, "Origin": _origin(port)}, 403),
                    (
                        {
                            "Cookie": cookie,
                            "Origin": _origin(port),
                            "X-AI-Trade-Token": "wrong-token",
                        },
                        403,
                    ),
                    ({"Cookie": cookie, "X-AI-Trade-Token": csrf}, 403),
                    (
                        {
                            "Cookie": cookie,
                            "Origin": "http://evil.example",
                            "X-AI-Trade-Token": csrf,
                        },
                        403,
                    ),
                )
                for headers, expected in cases:
                    with self.subTest(headers=headers):
                        status, _, _ = _request_json(
                            port,
                            "POST",
                            "/api/jobs",
                            job_payload,
                            headers=headers,
                        )
                        self.assertEqual(status, expected)

                status, _, body = _request_json(
                    port,
                    "POST",
                    "/api/jobs",
                    job_payload,
                    headers={
                        "Cookie": cookie,
                        "Origin": _origin(port),
                        "X-AI-Trade-Token": csrf,
                    },
                )
                self.assertEqual(status, 400)
                self.assertIn(b"Unsupported job action", body)

                status, _, _ = _request_json(
                    port,
                    "POST",
                    "/api/auth/logout",
                    {},
                    headers={"Cookie": cookie, "Origin": _origin(port)},
                )
                self.assertEqual(status, 403)

                status, headers, body = _request_json(
                    port,
                    "POST",
                    "/api/auth/logout",
                    {},
                    headers={
                        "Cookie": cookie,
                        "Origin": _origin(port),
                        "X-AI-Trade-Token": csrf,
                    },
                )
                self.assertEqual(status, 200)
                self.assertFalse(json.loads(body)["authenticated"])
                cleared = headers["set-cookie"].lower()
                self.assertIn("max-age=0", cleared)
                self.assertIn("httponly", cleared)
                self.assertIn("samesite=strict", cleared)

                status, _, _ = _request(
                    port,
                    "GET",
                    "/api/overview",
                    headers={"Cookie": cookie},
                )
                self.assertEqual(status, 401)

    def test_sessions_do_not_survive_server_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = _auth_config(Path(temporary))
            _create_user(config)
            with _running_server(config) as first_port:
                cookie, _ = _login(first_port)
                status, _, _ = _request(
                    first_port,
                    "GET",
                    "/api/overview",
                    headers={"Cookie": cookie},
                )
                self.assertEqual(status, 200)

            with _running_server(config) as second_port:
                status, _, _ = _request(
                    second_port,
                    "GET",
                    "/api/overview",
                    headers={"Cookie": cookie},
                )
                self.assertEqual(status, 401)

    def test_authenticated_report_get_head_and_path_restrictions(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = _auth_config(root)
            _create_user(config)
            report = _create_report(config)
            (config.reports_dir / "secret.exe").write_bytes(b"not a report")
            outside = root / "outside.json"
            outside.write_text('{"outside":true}', encoding="utf-8")
            link = config.reports_dir / "linked.json"
            real_symlink = True
            try:
                link.symlink_to(outside)
            except OSError:
                real_symlink = False

            with _running_server(config) as port:
                cookie, _ = _login(port)
                status, headers, body = _request(
                    port,
                    "GET",
                    "/reports/sample.json",
                    headers={"Cookie": cookie},
                )
                self.assertEqual(status, 200)
                self.assertEqual(body, report.read_bytes())
                self.assertEqual(
                    headers["content-disposition"],
                    'attachment; filename="sample.json"',
                )

                status, headers, body = _request(
                    port,
                    "HEAD",
                    "/reports/sample.json",
                    headers={"Cookie": cookie},
                )
                self.assertEqual(status, 200)
                self.assertEqual(body, b"")
                self.assertEqual(int(headers["content-length"]), report.stat().st_size)

                for path in (
                    "/reports/%2e%2e%2foutside.json",
                    "/reports/secret.exe",
                    "/reports/missing.json",
                    "/reports/bad%22name.json",
                    "/reports/sample.json%2fextra",
                ):
                    with self.subTest(path=path):
                        status, _, _ = _request(
                            port, "GET", path, headers={"Cookie": cookie}
                        )
                        self.assertEqual(status, 404)

                if real_symlink:
                    status, _, _ = _request(
                        port,
                        "GET",
                        "/reports/linked.json",
                        headers={"Cookie": cookie},
                    )
                else:
                    original = Path.is_symlink

                    def fake_is_symlink(value):
                        return value.name == "linked.json" or original(value)

                    with patch.object(Path, "is_symlink", fake_is_symlink):
                        status, _, _ = _request(
                            port,
                            "GET",
                            "/reports/linked.json",
                            headers={"Cookie": cookie},
                        )
                self.assertEqual(status, 404)


def _auth_config(root: Path, *, max_failed_attempts: int = 3):
    source = load_config(Path(__file__).resolve().parents[1] / "config/default.json")
    raw = copy.deepcopy(source.raw)
    raw["auth"].update(
        {
            "enabled": True,
            "users_file": "state/test-users.json",
            "session_hours": 8,
            "max_failed_attempts": max_failed_attempts,
            "failure_window_minutes": 1,
            "lockout_minutes": 1,
        }
    )
    return replace(source, project_root=root, raw=raw)


def _create_user(config) -> None:
    UserStore(
        config.auth_users_file,
        iterations=MIN_PBKDF2_ITERATIONS,
    ).add_user(USERNAME, PASSWORD)


def _create_report(config) -> Path:
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    report = config.reports_dir / "sample.json"
    report.write_text('{"status":"ok"}', encoding="utf-8")
    return report


@contextmanager
def _running_server(config, *, auth_enabled=True):
    server, _ = create_dashboard_server(
        config, port=0, auth_enabled=auth_enabled
    )
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def _origin(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _login(port: int) -> tuple[str, dict[str, str]]:
    status, headers, body = _request_json(
        port,
        "POST",
        "/api/auth/login",
        {"username": USERNAME, "password": PASSWORD},
        headers={"Origin": _origin(port)},
    )
    if status != 200:
        raise AssertionError(f"Login failed with {status}: {body!r}")
    cookie = headers["set-cookie"].split(";", 1)[0]
    return cookie, headers


def _request_json(port, method, path, payload, headers=None):
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    return _request(
        port,
        method,
        path,
        body=json.dumps(payload).encode("utf-8"),
        headers=request_headers,
    )


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
    result = (
        response.status,
        {name.lower(): value for name, value in response.getheaders()},
        response.read(),
    )
    connection.close()
    return result


if __name__ == "__main__":
    unittest.main()
