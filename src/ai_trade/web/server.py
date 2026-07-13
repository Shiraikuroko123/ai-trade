from __future__ import annotations

import json
import logging
import mimetypes
import secrets
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from ipaddress import ip_address
from urllib.parse import parse_qs, unquote, urlsplit

from ..config import AppConfig
from .jobs import JobManager
from .service import DashboardService


LOGGER = logging.getLogger(__name__)


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address, handler, jobs: JobManager):
        super().__init__(address, handler)
        self.jobs = jobs

    def server_close(self) -> None:
        self.jobs.close()
        super().server_close()


def create_dashboard_server(
    config: AppConfig,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> tuple[DashboardServer, str]:
    _require_loopback(host)
    service = DashboardService(config)
    jobs = JobManager(config)
    token = secrets.token_urlsafe(32)
    handler = _handler_factory(service, jobs, token)
    try:
        server = DashboardServer((host, port), handler, jobs)
    except Exception:
        jobs.close()
        raise
    return server, token


def serve_dashboard(
    config: AppConfig,
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
) -> None:
    server, _ = create_dashboard_server(config, host, port)
    actual_host, actual_port = server.server_address[:2]
    display_host = f"[{actual_host}]" if ":" in actual_host else actual_host
    url = f"http://{display_host}:{actual_port}/"
    LOGGER.info("AI Trade workstation listening at %s", url)
    print(f"AI Trade workstation: {url}")
    browser_timer: threading.Timer | None = None
    if open_browser:
        browser_timer = threading.Timer(0.4, lambda: webbrowser.open(url))
        browser_timer.daemon = True
        browser_timer.start()
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        print("Stopping AI Trade workstation")
    finally:
        if browser_timer is not None:
            browser_timer.cancel()
        server.server_close()


def _handler_factory(
    service: DashboardService,
    jobs: JobManager,
    token: str,
) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        server_version = "AITradeDashboard"
        sys_version = ""

        def do_HEAD(self) -> None:
            if not self._valid_host():
                self._json_error(HTTPStatus.FORBIDDEN, "Invalid host header")
                return
            try:
                path = urlsplit(self.path).path
                if path.startswith("/reports/"):
                    self._report(path, include_body=False)
                else:
                    self._static(path, include_body=False)
            except Exception:
                LOGGER.exception("Dashboard request failed: %s", self.path)
                self._json_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "The workstation could not complete this request. Check logs/ai_trade.log.",
                )

        def do_GET(self) -> None:
            if not self._valid_host():
                self._json_error(HTTPStatus.FORBIDDEN, "Invalid host header")
                return
            parsed = urlsplit(self.path)
            try:
                if parsed.path == "/api/bootstrap":
                    self._json(
                        {
                            "token": token,
                            "actions": list(jobs_action_names()),
                        }
                    )
                elif parsed.path == "/api/overview":
                    self._json(service.overview())
                elif parsed.path == "/api/research":
                    self._json(service.research())
                elif parsed.path == "/api/portfolio":
                    self._json(service.portfolio())
                elif parsed.path == "/api/trading":
                    self._json(service.trading())
                elif parsed.path == "/api/universe":
                    query = parse_qs(parsed.query)
                    raw_date = query.get("date", [None])[0]
                    selected = None
                    if raw_date:
                        from datetime import date

                        selected = date.fromisoformat(raw_date)
                    self._json(service.universe(selected))
                elif parsed.path == "/api/system":
                    self._json(service.system())
                elif parsed.path == "/api/jobs":
                    self._json({"jobs": jobs.list()})
                elif parsed.path.startswith("/api/jobs/"):
                    job = jobs.get(parsed.path.rsplit("/", 1)[-1])
                    if job is None:
                        self._json_error(HTTPStatus.NOT_FOUND, "Job not found")
                    else:
                        self._json(job.payload())
                elif parsed.path.startswith("/reports/"):
                    self._report(parsed.path)
                elif parsed.path.startswith("/api/"):
                    self._json_error(HTTPStatus.NOT_FOUND, "API endpoint not found")
                else:
                    self._static(parsed.path)
            except ValueError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, str(exc))
            except RuntimeError as exc:
                self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
            except Exception:
                LOGGER.exception("Dashboard request failed: %s", self.path)
                self._json_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "The workstation could not complete this request. Check logs/ai_trade.log.",
                )

        def do_POST(self) -> None:
            if not self._authorize_write(token):
                return
            parsed = urlsplit(self.path)
            if parsed.path != "/api/jobs":
                self._json_error(HTTPStatus.NOT_FOUND, "API endpoint not found")
                return
            try:
                payload = self._read_json()
                action = str(payload.get("action", ""))
                job = jobs.submit(action)
                self._json(job.payload(), HTTPStatus.ACCEPTED)
            except ValueError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, str(exc))
            except RuntimeError as exc:
                self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
            except Exception:
                LOGGER.exception("Dashboard request failed: %s", self.path)
                self._json_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "The workstation could not complete this request. Check logs/ai_trade.log.",
                )

        def do_DELETE(self) -> None:
            if not self._authorize_write(token):
                return
            parsed = urlsplit(self.path)
            if not parsed.path.startswith("/api/jobs/"):
                self._json_error(HTTPStatus.NOT_FOUND, "API endpoint not found")
                return
            try:
                job = jobs.cancel(parsed.path.rsplit("/", 1)[-1])
            except KeyError:
                self._json_error(HTTPStatus.NOT_FOUND, "Job not found")
                return
            except RuntimeError as exc:
                self._json_error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
                return
            self._json(job.payload())

        def log_message(self, format: str, *args) -> None:
            LOGGER.info("dashboard %s - %s", self.address_string(), format % args)

        def _authorize_write(self, expected: str) -> bool:
            if not self._valid_host():
                self._json_error(HTTPStatus.FORBIDDEN, "Invalid host header")
                return False
            provided = self.headers.get("X-AI-Trade-Token")
            if provided is None or not secrets.compare_digest(provided, expected):
                self._json_error(HTTPStatus.FORBIDDEN, "Write token is missing or invalid")
                return False
            origin = self.headers.get("Origin")
            if origin:
                expected_origins = {
                    f"http://{self.headers.get('Host')}",
                    f"http://localhost:{self.server.server_port}",
                    f"http://127.0.0.1:{self.server.server_port}",
                }
                if origin not in expected_origins:
                    self._json_error(HTTPStatus.FORBIDDEN, "Cross-origin write denied")
                    return False
            return True

        def _valid_host(self) -> bool:
            values = self.headers.get_all("Host", [])
            if len(values) != 1:
                return False
            try:
                hostname, port = _parse_host_header(values[0])
            except ValueError:
                return False
            bound_host = str(self.server.server_address[0]).lower()
            if hostname.lower() not in {"localhost", bound_host}:
                return False
            return port is None or port == self.server.server_port

        def _read_json(self) -> dict[str, object]:
            if self.headers.get_content_type() != "application/json":
                raise ValueError("Content-Type must be application/json")
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError as exc:
                raise ValueError("Content-Length is invalid") from exc
            if length <= 0 or length > 8192:
                raise ValueError("JSON request body must be between 1 and 8192 bytes")
            try:
                value = json.loads(self.rfile.read(length).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError("Request body is not valid UTF-8 JSON") from exc
            if not isinstance(value, dict):
                raise ValueError("JSON request body must be an object")
            return value

        def _static(self, path: str, include_body: bool = True) -> None:
            names = {
                "/": "index.html",
                "/index.html": "index.html",
                "/app.css": "app.css",
                "/app.js": "app.js",
            }
            name = names.get(path)
            if name is None:
                self._json_error(HTTPStatus.NOT_FOUND, "Resource not found")
                return
            resource = resources.files("ai_trade.web.assets").joinpath(name)
            content = resource.read_bytes()
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", f"{mime}; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            if include_body:
                self.wfile.write(content)

        def _report(self, path: str, include_body: bool = True) -> None:
            name = unquote(path.removeprefix("/reports/"))
            allowed_suffixes = {".csv", ".html", ".json", ".md"}
            if (
                not name
                or not name.isascii()
                or "/" in name
                or "\\" in name
                or name.startswith(".")
                or not any(name.endswith(suffix) for suffix in allowed_suffixes)
            ):
                self._json_error(HTTPStatus.NOT_FOUND, "Report not found")
                return
            report = service.config.reports_dir / name
            if not report.is_file():
                self._json_error(HTTPStatus.NOT_FOUND, "Report not found")
                return
            content = report.read_bytes()
            mime = mimetypes.guess_type(name)[0] or "application/octet-stream"
            if mime.startswith("text/") or mime in {
                "application/json",
                "application/javascript",
            }:
                mime = f"{mime}; charset=utf-8"
            self.send_response(HTTPStatus.OK)
            self._security_headers()
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Content-Disposition", f'attachment; filename="{name}"')
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if include_body:
                self.wfile.write(content)

        def _json(
            self, value: object, status: HTTPStatus = HTTPStatus.OK
        ) -> None:
            content = json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), default=str
            ).encode("utf-8")
            self.send_response(status)
            self._security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(content)

        def _json_error(self, status: HTTPStatus, message: str) -> None:
            self._json({"error": message}, status)

        def _security_headers(self) -> None:
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
                "base-uri 'self'; frame-ancestors 'none'; form-action 'self'",
            )
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Cross-Origin-Opener-Policy", "same-origin")

    return Handler


def jobs_action_names() -> tuple[str, ...]:
    from .jobs import COMMANDS

    return tuple(COMMANDS)


def _require_loopback(host: str) -> None:
    if host.lower() == "localhost":
        return
    try:
        if ip_address(host).is_loopback:
            return
    except ValueError:
        pass
    raise ValueError("The workstation may bind only to localhost or a loopback address")


def _parse_host_header(value: str) -> tuple[str, int | None]:
    raw = value.strip()
    if not raw or any(character.isspace() for character in raw):
        raise ValueError("Host header is invalid")
    port_text: str | None = None
    if raw.startswith("["):
        closing = raw.find("]")
        if closing < 0:
            raise ValueError("Host header is invalid")
        hostname = raw[1:closing]
        suffix = raw[closing + 1 :]
        if suffix:
            if not suffix.startswith(":") or not suffix[1:]:
                raise ValueError("Host header is invalid")
            port_text = suffix[1:]
        try:
            if ip_address(hostname).version != 6:
                raise ValueError("Host header is invalid")
        except ValueError as exc:
            raise ValueError("Host header is invalid") from exc
    else:
        if raw.count(":") > 1:
            raise ValueError("Host header is invalid")
        hostname, separator, port_text = raw.partition(":")
        if not separator:
            port_text = None
    if not hostname:
        raise ValueError("Host header is invalid")
    if port_text is None:
        return hostname, None
    if not port_text.isascii() or not port_text.isdigit():
        raise ValueError("Host header is invalid")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ValueError("Host header is invalid")
    return hostname, port
