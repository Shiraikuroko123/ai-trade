from __future__ import annotations

import json
import logging
import math
import mimetypes
import re
import secrets
import threading
import webbrowser
from datetime import datetime, timezone
from http import HTTPStatus
from http.cookies import CookieError, SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from ipaddress import ip_address
from urllib.parse import parse_qs, unquote, urlsplit

from .. import __version__
from ..config import AppConfig
from .auth import (
    AuthManager,
    AuthenticationError,
    LoginRateLimiter,
    Session,
    SessionStore,
    UserStore,
)
from .jobs import JobManager
from .service import DashboardService


LOGGER = logging.getLogger(__name__)
SESSION_COOKIE_NAME = "ai_trade_session"
REPORT_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


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
    auth_enabled: bool | None = None,
) -> tuple[DashboardServer, str]:
    _require_loopback(host)
    service = DashboardService(config)
    jobs = JobManager(config)
    token = secrets.token_urlsafe(32)
    use_auth = config.auth_enabled if auth_enabled is None else auth_enabled
    auth: AuthManager | None = None
    session_max_age = 0
    if use_auth:
        settings = config.raw.get("auth", {})
        session_max_age = round(float(settings.get("session_hours", 8)) * 3600)
        auth = AuthManager(
            UserStore(config.auth_users_file),
            SessionStore(session_max_age),
            LoginRateLimiter(
                max_failures=int(settings.get("max_failed_attempts", 5)),
                window_seconds=int(settings.get("failure_window_minutes", 15)) * 60,
                lockout_seconds=int(settings.get("lockout_minutes", 15)) * 60,
            ),
        )
    handler = _handler_factory(service, jobs, token, auth, session_max_age)
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
    auth_enabled: bool | None = None,
) -> None:
    server, _ = create_dashboard_server(config, host, port, auth_enabled)
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
    auth: AuthManager | None,
    session_max_age: int,
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
                if path in {"/login", "/login.html"}:
                    if auth is None or self._session_context() is not None:
                        self._redirect("/")
                    else:
                        self._static(path, include_body=False)
                    return
                if path in {"/auth.css", "/auth.js"}:
                    self._static(path, include_body=False)
                    return
                if path == "/api/auth/session":
                    self._json(self._session_payload())
                    return
                if path.startswith("/api/"):
                    if auth is not None and self._require_session() is None:
                        return
                    self._json_error(
                        HTTPStatus.METHOD_NOT_ALLOWED, "HEAD is not supported for this API"
                    )
                    return
                if auth is not None and self._require_session(
                    page=path in {"/", "/index.html"}
                ) is None:
                    return
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
                if parsed.path in {"/login", "/login.html"}:
                    if auth is None or self._session_context() is not None:
                        self._redirect("/")
                    else:
                        self._static(parsed.path)
                    return
                if parsed.path in {"/auth.css", "/auth.js"}:
                    self._static(parsed.path)
                    return
                if parsed.path == "/api/auth/session":
                    self._json(self._session_payload())
                    return
                context = None
                if auth is not None:
                    context = self._require_session(
                        page=parsed.path in {"/", "/index.html"}
                    )
                    if context is None:
                        return
                if parsed.path == "/api/bootstrap":
                    session = context[1] if context is not None else None
                    self._json(
                        {
                            "token": session.csrf_token if session is not None else token,
                            "version": __version__,
                            "actions": list(jobs_action_names()),
                            "auth_enabled": auth is not None,
                            "user": {
                                "username": session.username
                                if session is not None
                                else "本地所有者"
                            },
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
                elif parsed.path == "/api/storage":
                    self._json(service.storage())
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
            if not self._valid_host():
                self._json_error(HTTPStatus.FORBIDDEN, "Invalid host header")
                return
            parsed = urlsplit(self.path)
            if parsed.path == "/api/auth/login":
                self._login()
                return
            if parsed.path == "/api/auth/logout":
                context = self._authorize_write()
                if context is None:
                    return
                if auth is not None:
                    auth.logout(context[0])
                self._json(
                    {"authenticated": False},
                    headers={"Set-Cookie": self._clear_session_cookie()},
                )
                return
            if self._authorize_write() is None:
                return
            try:
                if parsed.path == "/api/jobs":
                    payload = self._read_json()
                    action = str(payload.get("action", ""))
                    job = jobs.submit(action)
                    self._json(job.payload(), HTTPStatus.ACCEPTED)
                elif parsed.path == "/api/storage/preferences":
                    self._json(service.save_storage_preferences(self._read_json()))
                elif parsed.path == "/api/storage/refresh":
                    self._json(service.storage(refresh=True))
                else:
                    self._json_error(HTTPStatus.NOT_FOUND, "API endpoint not found")
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
            if self._authorize_write() is None:
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

        def _authorize_write(self) -> tuple[str, Session | None] | None:
            if not self._valid_host():
                self._json_error(HTTPStatus.FORBIDDEN, "Invalid host header")
                return None
            context = self._require_session() if auth is not None else ("", None)
            if context is None:
                return None
            provided = self.headers.get("X-AI-Trade-Token")
            expected = context[1].csrf_token if context[1] is not None else token
            if provided is None or not secrets.compare_digest(provided, expected):
                self._json_error(HTTPStatus.FORBIDDEN, "Write token is missing or invalid")
                return None
            if not self._same_origin():
                self._json_error(HTTPStatus.FORBIDDEN, "Cross-origin write denied")
                return None
            return context

        def _login(self) -> None:
            if auth is None:
                self._json_error(HTTPStatus.NOT_FOUND, "Authentication is not enabled")
                return
            if not self._same_origin():
                self._json_error(HTTPStatus.FORBIDDEN, "Cross-origin login denied")
                return
            try:
                payload = self._read_json()
                username = payload.get("username")
                password = payload.get("password")
                if not isinstance(username, str) or not isinstance(password, str):
                    username, password = "", ""
                grant = auth.login(username, password, source=self.client_address[0])
            except AuthenticationError as exc:
                retry_after = max(0, math.ceil(exc.retry_after))
                if retry_after:
                    self._json_error(
                        HTTPStatus.TOO_MANY_REQUESTS,
                        "登录尝试过多，请稍后再试",
                        payload={"retry_after": retry_after},
                        headers={"Retry-After": str(retry_after)},
                    )
                else:
                    self._json_error(
                        HTTPStatus.UNAUTHORIZED, "用户名或密码不正确"
                    )
                return
            except ValueError as exc:
                self._json_error(HTTPStatus.BAD_REQUEST, str(exc))
                return
            except Exception:
                LOGGER.exception("Dashboard authentication failed")
                self._json_error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    "登录服务暂时不可用，请检查本机日志",
                )
                return
            previous = self._cookie_token()
            if previous:
                auth.logout(previous)
            self._json(
                {
                    "authenticated": True,
                    "username": grant.username,
                    "expires_at": datetime.fromtimestamp(
                        grant.expires_at, timezone.utc
                    ).isoformat(),
                },
                headers={
                    "Set-Cookie": self._session_cookie(
                        grant.token, session_max_age
                    )
                },
            )

        def _session_context(self) -> tuple[str, Session] | None:
            if auth is None:
                return None
            token_value = self._cookie_token()
            if not token_value:
                return None
            session = auth.authenticate_session(token_value)
            return (token_value, session) if session is not None else None

        def _require_session(
            self, *, page: bool = False
        ) -> tuple[str, Session] | None:
            context = self._session_context()
            if context is not None:
                return context
            if page:
                self._redirect("/login")
            else:
                self._json_error(
                    HTTPStatus.UNAUTHORIZED,
                    "请先登录内测账号",
                    headers={"WWW-Authenticate": "Session"},
                )
            return None

        def _session_payload(self) -> dict[str, object]:
            if auth is None:
                return {
                    "auth_enabled": False,
                    "authenticated": True,
                    "configured": True,
                    "username": "本地所有者",
                }
            context = self._session_context()
            if context is None:
                return {
                    "auth_enabled": True,
                    "authenticated": False,
                    "configured": auth.users.has_users(),
                    "username": None,
                }
            return {
                "auth_enabled": True,
                "authenticated": True,
                "configured": True,
                "username": context[1].username,
                "expires_at": datetime.fromtimestamp(
                    context[1].expires_at, timezone.utc
                ).isoformat(),
            }

        def _cookie_token(self) -> str | None:
            values = self.headers.get_all("Cookie", [])
            if len(values) != 1 or len(values[0]) > 4096:
                return None
            cookies = SimpleCookie()
            try:
                cookies.load(values[0])
            except CookieError:
                return None
            morsel = cookies.get(SESSION_COOKIE_NAME)
            if morsel is None or not 20 <= len(morsel.value) <= 512:
                return None
            return morsel.value

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            host = self.headers.get("Host")
            return bool(origin and host and origin == f"http://{host}")

        @staticmethod
        def _session_cookie(value: str, max_age: int) -> str:
            return (
                f"{SESSION_COOKIE_NAME}={value}; Path=/; HttpOnly; "
                f"SameSite=Strict; Max-Age={max_age}"
            )

        @staticmethod
        def _clear_session_cookie() -> str:
            return (
                f"{SESSION_COOKIE_NAME}=; Path=/; HttpOnly; SameSite=Strict; "
                "Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT"
            )

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
                "/login": "login.html",
                "/login.html": "login.html",
                "/auth.css": "auth.css",
                "/auth.js": "auth.js",
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
                not REPORT_NAME_PATTERN.fullmatch(name)
                or not any(name.endswith(suffix) for suffix in allowed_suffixes)
            ):
                self._json_error(HTTPStatus.NOT_FOUND, "Report not found")
                return
            reports_root = service.config.reports_dir.resolve()
            candidate = reports_root / name
            if candidate.is_symlink():
                self._json_error(HTTPStatus.NOT_FOUND, "Report not found")
                return
            try:
                report = candidate.resolve(strict=True)
            except OSError:
                self._json_error(HTTPStatus.NOT_FOUND, "Report not found")
                return
            if report.parent != reports_root or not report.is_file():
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
            self,
            value: object,
            status: HTTPStatus = HTTPStatus.OK,
            headers: dict[str, str] | None = None,
        ) -> None:
            content = json.dumps(
                value, ensure_ascii=False, separators=(",", ":"), default=str
            ).encode("utf-8")
            self.send_response(status)
            self._security_headers()
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            for name, header_value in (headers or {}).items():
                self.send_header(name, header_value)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(content)

        def _json_error(
            self,
            status: HTTPStatus,
            message: str,
            *,
            payload: dict[str, object] | None = None,
            headers: dict[str, str] | None = None,
        ) -> None:
            self._json({"error": message, **(payload or {})}, status, headers)

        def _redirect(self, location: str) -> None:
            self.send_response(HTTPStatus.SEE_OTHER)
            self._security_headers()
            self.send_header("Location", location)
            self.send_header("Content-Length", "0")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()

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
            self.send_header("Cross-Origin-Resource-Policy", "same-origin")
            self.send_header(
                "Permissions-Policy", "camera=(), microphone=(), geolocation=()"
            )

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
