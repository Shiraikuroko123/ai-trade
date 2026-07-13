from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import secrets
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


USER_FILE_SCHEMA_VERSION = 1
USER_EXPORT_FORMAT = "ai-trade-users"
USER_EXPORT_VERSION = 1
PASSWORD_HASH_VERSION = 1
PASSWORD_ALGORITHM = "pbkdf2_hmac_sha256"
DEFAULT_PBKDF2_ITERATIONS = 600_000
MIN_PBKDF2_ITERATIONS = 100_000
MAX_PBKDF2_ITERATIONS = 10_000_000
SALT_BYTES = 16
PASSWORD_DIGEST_BYTES = 32
SESSION_TOKEN_BYTES = 32
MAX_USER_FILE_BYTES = 10 * 1024 * 1024
MIN_PASSWORD_BYTES = 8
MAX_PASSWORD_BYTES = 1024

_USERNAME_PATTERN = re.compile(r"[a-z0-9](?:[a-z0-9._-]{1,62}[a-z0-9])?\Z")
_CREDENTIAL_REVISION_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_GENERIC_LOGIN_ERROR = "Authentication failed"


class AuthenticationError(RuntimeError):
    """A deliberately generic login error that does not reveal account state."""

    def __init__(self, retry_after: float = 0.0):
        retry = float(retry_after)
        self.retry_after = retry if math.isfinite(retry) and retry > 0 else 0.0
        super().__init__(_GENERIC_LOGIN_ERROR)


class UserStoreError(RuntimeError):
    pass


class CorruptUserStoreError(UserStoreError):
    pass


class UserAlreadyExistsError(UserStoreError):
    pass


class InvalidUsernameError(ValueError):
    pass


class PasswordPolicyError(ValueError):
    pass


@dataclass(frozen=True)
class UserInfo:
    username: str
    enabled: bool
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Session:
    username: str
    created_at: float
    expires_at: float
    csrf_token: str
    credential_revision: str


@dataclass(frozen=True)
class SessionGrant:
    token: str
    session: Session

    @property
    def username(self) -> str:
        return self.session.username

    @property
    def expires_at(self) -> float:
        return self.session.expires_at


@dataclass(frozen=True)
class _PasswordRecord:
    version: int
    algorithm: str
    iterations: int
    salt: str
    digest: str


@dataclass(frozen=True)
class _StoredUser:
    username: str
    enabled: bool
    created_at: str
    updated_at: str
    password: _PasswordRecord


@dataclass
class _FailureState:
    attempts: list[float]
    locked_until: float = 0.0


def normalize_username(username: str) -> str:
    if not isinstance(username, str):
        raise InvalidUsernameError("Username must be a string")
    normalized = unicodedata.normalize("NFKC", username).strip().casefold()
    if not _USERNAME_PATTERN.fullmatch(normalized):
        raise InvalidUsernameError(
            "Username must be 3-64 ASCII letters, digits, dots, underscores, or hyphens; "
            "it must start and end with a letter or digit"
        )
    return normalized


class UserStore:
    """Atomic JSON user storage containing only salted password verifiers."""

    def __init__(
        self,
        path: str | Path,
        *,
        iterations: int = DEFAULT_PBKDF2_ITERATIONS,
    ):
        if (
            isinstance(iterations, bool)
            or not isinstance(iterations, int)
            or not MIN_PBKDF2_ITERATIONS <= iterations <= MAX_PBKDF2_ITERATIONS
        ):
            raise ValueError(
                f"iterations must be between {MIN_PBKDF2_ITERATIONS} "
                f"and {MAX_PBKDF2_ITERATIONS}"
            )
        self.path = Path(path)
        self.iterations = iterations
        self._lock = threading.RLock()
        self._dummy_password = _hash_password(
            secrets.token_urlsafe(24), self.iterations
        )

    def has_users(self) -> bool:
        with self._lock:
            return bool(self._load_users())

    def add_user(
        self, username: str, password: str, *, replace: bool = False
    ) -> UserInfo:
        normalized = normalize_username(username)
        password_bytes = _validated_password(password)
        with self._lock:
            users = self._load_users()
            existing = users.get(normalized)
            if existing is not None and not replace:
                raise UserAlreadyExistsError("User already exists")
            now = _utc_now()
            user = _StoredUser(
                username=normalized,
                enabled=existing.enabled if existing is not None else True,
                created_at=existing.created_at if existing is not None else now,
                updated_at=now,
                password=_hash_password_bytes(password_bytes, self.iterations),
            )
            users[normalized] = user
            self._write_users(users)
            return _public_user(user)

    def list_users(self) -> tuple[UserInfo, ...]:
        with self._lock:
            users = self._load_users()
            return tuple(_public_user(users[name]) for name in sorted(users))

    def set_enabled(self, username: str, enabled: bool) -> UserInfo:
        normalized = normalize_username(username)
        if not isinstance(enabled, bool):
            raise TypeError("enabled must be a boolean")
        with self._lock:
            users = self._load_users()
            existing = users.get(normalized)
            if existing is None:
                raise UserStoreError("User does not exist")
            if existing.enabled == enabled:
                return _public_user(existing)
            updated = _StoredUser(
                username=existing.username,
                enabled=enabled,
                created_at=existing.created_at,
                updated_at=_utc_now(),
                password=existing.password,
            )
            users[normalized] = updated
            self._write_users(users)
            return _public_user(updated)

    def remove_user(self, username: str) -> bool:
        normalized = normalize_username(username)
        with self._lock:
            users = self._load_users()
            if users.pop(normalized, None) is None:
                return False
            self._write_users(users)
            return True

    def export_users(self, destination: str | Path) -> int:
        output = Path(destination)
        if _same_path(output, self.path):
            raise UserStoreError("Export destination must differ from the active user file")
        with self._lock:
            users = self._load_users()
            payload = {
                "format": USER_EXPORT_FORMAT,
                "version": USER_EXPORT_VERSION,
                "users": [
                    _stored_user_payload(users[name]) for name in sorted(users)
                ],
            }
            _atomic_write_json(output, payload)
            return len(users)

    def import_users(
        self,
        source: str | Path,
        *,
        mode: str = "reject",
    ) -> tuple[UserInfo, ...]:
        if mode not in {"reject", "replace", "merge"}:
            raise ValueError("Import mode must be reject, replace, or merge")
        input_path = Path(source)
        if _same_path(input_path, self.path):
            raise UserStoreError("Import source must differ from the active user file")
        imported = _load_user_export(input_path)
        with self._lock:
            existing = self._load_users()
            if mode == "reject" and existing:
                raise UserStoreError("Active user file is not empty; choose replace or merge")
            if mode == "merge":
                duplicates = sorted(set(existing) & set(imported))
                if duplicates:
                    raise UserAlreadyExistsError(
                        "Import contains existing usernames: " + ", ".join(duplicates)
                    )
                result = {**existing, **imported}
            else:
                result = dict(imported)
            self._write_users(result)
            return tuple(_public_user(result[name]) for name in sorted(result))

    def verify(self, username: str, password: str) -> bool:
        """Low-level constant-time verification; interactive callers should use AuthManager."""
        return self.authenticate_credentials(username, password) is not None

    def authenticate_credentials(self, username: str, password: str) -> str | None:
        """Verify credentials and return the revision that a session must remain bound to."""
        try:
            normalized = normalize_username(username)
        except InvalidUsernameError:
            normalized = ""
        password_bytes = _candidate_password(password)
        with self._lock:
            users = self._load_users()
            user = users.get(normalized)
            verifier = user.password if user is not None else self._dummy_password
            matches = _verify_password(verifier, password_bytes)
            if user is None or not user.enabled or not matches:
                return None
            return _credential_revision(user)

    def is_session_current(self, username: str, credential_revision: str) -> bool:
        if (
            not isinstance(credential_revision, str)
            or not _CREDENTIAL_REVISION_PATTERN.fullmatch(credential_revision)
        ):
            return False
        try:
            normalized = normalize_username(username)
        except InvalidUsernameError:
            return False
        with self._lock:
            user = self._load_users().get(normalized)
            return bool(
                user is not None
                and user.enabled
                and hmac.compare_digest(
                    _credential_revision(user), credential_revision
                )
            )

    def is_enabled(self, username: str) -> bool:
        try:
            normalized = normalize_username(username)
        except InvalidUsernameError:
            return False
        with self._lock:
            user = self._load_users().get(normalized)
            return bool(user is not None and user.enabled)

    def _load_users(self) -> dict[str, _StoredUser]:
        if not self.path.exists():
            return {}
        try:
            if self.path.stat().st_size > MAX_USER_FILE_BYTES:
                raise CorruptUserStoreError("User file exceeds the maximum supported size")
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise CorruptUserStoreError("User file is unreadable or invalid JSON") from exc
        if (
            not isinstance(payload, dict)
            or isinstance(payload.get("schema_version"), bool)
            or payload.get("schema_version") != USER_FILE_SCHEMA_VERSION
        ):
            raise CorruptUserStoreError("Unsupported user file schema")
        raw_users = payload.get("users")
        if not isinstance(raw_users, list):
            raise CorruptUserStoreError("User file users field must be a list")
        users: dict[str, _StoredUser] = {}
        for raw_user in raw_users:
            user = _parse_stored_user(raw_user)
            if user.username in users:
                raise CorruptUserStoreError("User file contains duplicate usernames")
            users[user.username] = user
        return users

    def _write_users(self, users: dict[str, _StoredUser]) -> None:
        payload = {
            "schema_version": USER_FILE_SCHEMA_VERSION,
            "users": [_stored_user_payload(users[name]) for name in sorted(users)],
        }
        _atomic_write_json(self.path, payload)


class SessionStore:
    """In-memory sessions indexed only by SHA-256 token digests."""

    def __init__(
        self,
        ttl_seconds: float = 8 * 60 * 60,
        *,
        clock: Callable[[], float] = time.time,
    ):
        if not math.isfinite(ttl_seconds) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be finite and positive")
        self.ttl_seconds = float(ttl_seconds)
        self._clock = clock
        self._sessions: dict[bytes, Session] = {}
        self._lock = threading.RLock()

    def create(self, username: str, credential_revision: str) -> SessionGrant:
        normalized = normalize_username(username)
        if (
            not isinstance(credential_revision, str)
            or not _CREDENTIAL_REVISION_PATTERN.fullmatch(credential_revision)
        ):
            raise ValueError("credential_revision must be a lowercase SHA-256 digest")
        now = self._now()
        session = Session(
            normalized,
            now,
            now + self.ttl_seconds,
            secrets.token_urlsafe(SESSION_TOKEN_BYTES),
            credential_revision,
        )
        with self._lock:
            self._purge_expired(now)
            while True:
                token = secrets.token_urlsafe(SESSION_TOKEN_BYTES)
                token_hash = _token_digest(token)
                if token_hash not in self._sessions:
                    break
            self._sessions[token_hash] = session
        return SessionGrant(token, session)

    def authenticate(self, token: str) -> Session | None:
        token_hash = _safe_token_digest(token)
        if token_hash is None:
            return None
        now = self._now()
        with self._lock:
            session = self._sessions.get(token_hash)
            if session is None:
                return None
            if session.expires_at <= now:
                self._sessions.pop(token_hash, None)
                return None
            return session

    def revoke(self, token: str) -> bool:
        token_hash = _safe_token_digest(token)
        if token_hash is None:
            return False
        with self._lock:
            return self._sessions.pop(token_hash, None) is not None

    def verify_csrf(self, token: str, csrf_token: str) -> bool:
        session = self.authenticate(token)
        if (
            session is None
            or not isinstance(csrf_token, str)
            or not csrf_token
            or len(csrf_token) > 512
        ):
            return False
        return hmac.compare_digest(session.csrf_token, csrf_token)

    def revoke_user(self, username: str) -> int:
        normalized = normalize_username(username)
        with self._lock:
            matching = [
                token_hash
                for token_hash, session in self._sessions.items()
                if session.username == normalized
            ]
            for token_hash in matching:
                self._sessions.pop(token_hash, None)
            return len(matching)

    def purge_expired(self) -> int:
        with self._lock:
            return self._purge_expired(self._now())

    def _purge_expired(self, now: float) -> int:
        expired = [
            token_hash
            for token_hash, session in self._sessions.items()
            if session.expires_at <= now
        ]
        for token_hash in expired:
            self._sessions.pop(token_hash, None)
        return len(expired)

    def _now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value):
            raise RuntimeError("Session clock returned a non-finite value")
        return value


class LoginRateLimiter:
    """In-memory fixed-window failure lockout with fail-closed capacity limits."""

    def __init__(
        self,
        max_failures: int = 5,
        window_seconds: float = 5 * 60,
        lockout_seconds: float = 15 * 60,
        *,
        max_keys: int = 10_000,
        clock: Callable[[], float] = time.time,
    ):
        if isinstance(max_failures, bool) or not isinstance(max_failures, int) or max_failures < 1:
            raise ValueError("max_failures must be a positive integer")
        if isinstance(max_keys, bool) or not isinstance(max_keys, int) or max_keys < 1:
            raise ValueError("max_keys must be a positive integer")
        if not math.isfinite(window_seconds) or window_seconds <= 0:
            raise ValueError("window_seconds must be finite and positive")
        if not math.isfinite(lockout_seconds) or lockout_seconds <= 0:
            raise ValueError("lockout_seconds must be finite and positive")
        self.max_failures = max_failures
        self.window_seconds = float(window_seconds)
        self.lockout_seconds = float(lockout_seconds)
        self.max_keys = max_keys
        self._clock = clock
        self._states: dict[bytes, _FailureState] = {}
        self._capacity_locked_until = 0.0
        self._lock = threading.RLock()

    def retry_after(self, key: str) -> float:
        key_hash = _rate_key(key)
        now = self._now()
        with self._lock:
            self._prune(now)
            global_retry = max(0.0, self._capacity_locked_until - now)
            state = self._states.get(key_hash)
            key_retry = max(0.0, state.locked_until - now) if state else 0.0
            return max(global_retry, key_retry)

    def record_failure(self, key: str) -> float:
        key_hash = _rate_key(key)
        now = self._now()
        with self._lock:
            self._prune(now)
            state = self._states.get(key_hash)
            if state is None:
                if len(self._states) >= self.max_keys:
                    self._capacity_locked_until = max(
                        self._capacity_locked_until, now + self.lockout_seconds
                    )
                    return self.lockout_seconds
                state = _FailureState([])
                self._states[key_hash] = state
            if state.locked_until > now:
                return state.locked_until - now
            state.attempts.append(now)
            if len(state.attempts) >= self.max_failures:
                state.attempts.clear()
                state.locked_until = now + self.lockout_seconds
                return self.lockout_seconds
            return 0.0

    def record_success(self, key: str) -> None:
        key_hash = _rate_key(key)
        with self._lock:
            self._states.pop(key_hash, None)

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        removable = []
        for key_hash, state in self._states.items():
            state.attempts[:] = [value for value in state.attempts if value > cutoff]
            if state.locked_until <= now and not state.attempts:
                removable.append(key_hash)
        for key_hash in removable:
            self._states.pop(key_hash, None)
        if self._capacity_locked_until <= now:
            self._capacity_locked_until = 0.0

    def _now(self) -> float:
        value = float(self._clock())
        if not math.isfinite(value):
            raise RuntimeError("Rate limiter clock returned a non-finite value")
        return value


class AuthManager:
    """Safe composition of user verification, failure throttling, and sessions."""

    def __init__(
        self,
        users: UserStore,
        sessions: SessionStore | None = None,
        limiter: LoginRateLimiter | None = None,
        *,
        source_max_failures: int = 20,
    ):
        self.users = users
        self.sessions = sessions or SessionStore()
        self.limiter = limiter or LoginRateLimiter()
        self.source_limiter = LoginRateLimiter(max_failures=source_max_failures)

    def login(self, username: str, password: str, *, source: str = "local") -> SessionGrant:
        principal_key, source_key = _login_rate_keys(username, source)
        retry_after = max(
            self.limiter.retry_after(principal_key),
            self.source_limiter.retry_after(source_key),
        )
        if retry_after > 0:
            raise AuthenticationError(retry_after)
        credential_revision = self.users.authenticate_credentials(username, password)
        if credential_revision is None:
            retry_after = max(
                self.limiter.record_failure(principal_key),
                self.source_limiter.record_failure(source_key),
            )
            raise AuthenticationError(retry_after)
        self.limiter.record_success(principal_key)
        return self.sessions.create(
            normalize_username(username), credential_revision
        )

    def authenticate_session(self, token: str) -> Session | None:
        session = self.sessions.authenticate(token)
        if session is None:
            return None
        if not self.users.is_session_current(
            session.username, session.credential_revision
        ):
            self.sessions.revoke(token)
            return None
        return session

    def logout(self, token: str) -> bool:
        return self.sessions.revoke(token)

    def disable_user(self, username: str) -> UserInfo:
        user = self.users.set_enabled(username, False)
        self.sessions.revoke_user(user.username)
        return user

    def remove_user(self, username: str) -> bool:
        normalized = normalize_username(username)
        removed = self.users.remove_user(normalized)
        if removed:
            self.sessions.revoke_user(normalized)
        return removed


def _validated_password(password: str) -> bytes:
    if not isinstance(password, str):
        raise PasswordPolicyError("Password must be a string")
    encoded = password.encode("utf-8")
    if len(encoded) < MIN_PASSWORD_BYTES:
        raise PasswordPolicyError(
            f"Password must contain at least {MIN_PASSWORD_BYTES} UTF-8 bytes"
        )
    if len(encoded) > MAX_PASSWORD_BYTES:
        raise PasswordPolicyError(
            f"Password must not exceed {MAX_PASSWORD_BYTES} UTF-8 bytes"
        )
    if "\x00" in password:
        raise PasswordPolicyError("Password must not contain NUL characters")
    return encoded


def _candidate_password(password: str) -> bytes:
    if not isinstance(password, str):
        return b""
    encoded = password.encode("utf-8")
    return encoded if len(encoded) <= MAX_PASSWORD_BYTES else b""


def _hash_password(password: str, iterations: int) -> _PasswordRecord:
    return _hash_password_bytes(password.encode("utf-8"), iterations)


def _hash_password_bytes(password: bytes, iterations: int) -> _PasswordRecord:
    salt = secrets.token_bytes(SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password, salt, iterations, dklen=PASSWORD_DIGEST_BYTES
    )
    return _PasswordRecord(
        version=PASSWORD_HASH_VERSION,
        algorithm=PASSWORD_ALGORITHM,
        iterations=iterations,
        salt=base64.b64encode(salt).decode("ascii"),
        digest=base64.b64encode(digest).decode("ascii"),
    )


def _verify_password(record: _PasswordRecord, password: bytes) -> bool:
    salt = _decode_base64(record.salt, SALT_BYTES, "salt")
    expected = _decode_base64(record.digest, PASSWORD_DIGEST_BYTES, "digest")
    actual = hashlib.pbkdf2_hmac(
        "sha256", password, salt, record.iterations, dklen=PASSWORD_DIGEST_BYTES
    )
    return hmac.compare_digest(actual, expected)


def _parse_stored_user(value: object) -> _StoredUser:
    if not isinstance(value, dict):
        raise CorruptUserStoreError("User record must be an object")
    try:
        username = normalize_username(value["username"])
        if username != value["username"]:
            raise CorruptUserStoreError("Stored username is not normalized")
        enabled = value["enabled"]
        if not isinstance(enabled, bool):
            raise CorruptUserStoreError("Stored enabled flag must be boolean")
        created_at = value["created_at"]
        updated_at = value["updated_at"]
        if not isinstance(created_at, str) or not isinstance(updated_at, str):
            raise CorruptUserStoreError("Stored timestamps are invalid")
        for timestamp in (created_at, updated_at):
            parsed = datetime.fromisoformat(timestamp)
            if parsed.tzinfo is None:
                raise CorruptUserStoreError("Stored timestamps must include a timezone")
        raw_password = value["password"]
        if not isinstance(raw_password, dict):
            raise CorruptUserStoreError("Stored password record must be an object")
        version = raw_password["version"]
        algorithm = raw_password["algorithm"]
        iterations = raw_password["iterations"]
        salt = raw_password["salt"]
        digest = raw_password["digest"]
    except (KeyError, TypeError, ValueError, InvalidUsernameError) as exc:
        raise CorruptUserStoreError("User record is missing required fields") from exc
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != PASSWORD_HASH_VERSION
        or algorithm != PASSWORD_ALGORITHM
    ):
        raise CorruptUserStoreError("Unsupported password verifier version")
    if (
        isinstance(iterations, bool)
        or not isinstance(iterations, int)
        or not MIN_PBKDF2_ITERATIONS <= iterations <= MAX_PBKDF2_ITERATIONS
    ):
        raise CorruptUserStoreError("Stored PBKDF2 iteration count is invalid")
    if not isinstance(salt, str) or not isinstance(digest, str):
        raise CorruptUserStoreError("Stored password verifier encoding is invalid")
    _decode_base64(salt, SALT_BYTES, "salt")
    _decode_base64(digest, PASSWORD_DIGEST_BYTES, "digest")
    return _StoredUser(
        username,
        enabled,
        created_at,
        updated_at,
        _PasswordRecord(version, algorithm, iterations, salt, digest),
    )


def _load_user_export(path: Path) -> dict[str, _StoredUser]:
    try:
        if path.stat().st_size > MAX_USER_FILE_BYTES:
            raise CorruptUserStoreError("User export exceeds the maximum supported size")
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CorruptUserStoreError("User export is unreadable or invalid JSON") from exc
    if not isinstance(payload, dict) or set(payload) != {"format", "version", "users"}:
        raise CorruptUserStoreError("User export schema is invalid")
    if payload["format"] != USER_EXPORT_FORMAT:
        raise CorruptUserStoreError("User export format is invalid")
    version = payload["version"]
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version != USER_EXPORT_VERSION
    ):
        raise CorruptUserStoreError("User export version is unsupported")
    raw_users = payload["users"]
    if not isinstance(raw_users, list):
        raise CorruptUserStoreError("User export users field must be a list")
    users: dict[str, _StoredUser] = {}
    expected_user_fields = {
        "username",
        "enabled",
        "created_at",
        "updated_at",
        "password",
    }
    expected_password_fields = {
        "version",
        "algorithm",
        "iterations",
        "salt",
        "digest",
    }
    for raw_user in raw_users:
        if not isinstance(raw_user, dict) or set(raw_user) != expected_user_fields:
            raise CorruptUserStoreError("User export contains an invalid user record")
        raw_password = raw_user.get("password")
        if not isinstance(raw_password, dict) or set(raw_password) != expected_password_fields:
            raise CorruptUserStoreError("User export contains an invalid password record")
        user = _parse_stored_user(raw_user)
        if user.username in users:
            raise CorruptUserStoreError("User export contains duplicate usernames")
        users[user.username] = user
    return users


def _decode_base64(value: str, length: int, name: str) -> bytes:
    try:
        decoded = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeError, ValueError) as exc:
        raise CorruptUserStoreError(f"Stored password {name} is invalid") from exc
    if len(decoded) != length:
        raise CorruptUserStoreError(f"Stored password {name} has an invalid length")
    return decoded


def _stored_user_payload(user: _StoredUser) -> dict[str, object]:
    return {
        "username": user.username,
        "enabled": user.enabled,
        "created_at": user.created_at,
        "updated_at": user.updated_at,
        "password": {
            "version": user.password.version,
            "algorithm": user.password.algorithm,
            "iterations": user.password.iterations,
            "salt": user.password.salt,
            "digest": user.password.digest,
        },
    }


def _credential_revision(user: _StoredUser) -> str:
    material = "\0".join(
        (
            user.username,
            user.updated_at,
            str(user.password.version),
            user.password.algorithm,
            str(user.password.iterations),
            user.password.salt,
            user.password.digest,
        )
    ).encode("utf-8")
    return hashlib.sha256(material).hexdigest()


def _public_user(user: _StoredUser) -> UserInfo:
    return UserInfo(user.username, user.enabled, user.created_at, user.updated_at)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _atomic_write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    temporary = Path(temporary_name)
    try:
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass
        raise


def _same_path(first: Path, second: Path) -> bool:
    try:
        first_value = first.resolve(strict=False)
        second_value = second.resolve(strict=False)
    except OSError:
        first_value = first.absolute()
        second_value = second.absolute()
    return os.path.normcase(str(first_value)) == os.path.normcase(str(second_value))


def _token_digest(token: str) -> bytes:
    return hashlib.sha256(token.encode("ascii")).digest()


def _safe_token_digest(token: str) -> bytes | None:
    if not isinstance(token, str) or not token or len(token) > 512:
        return None
    try:
        return _token_digest(token)
    except UnicodeEncodeError:
        return None


def _rate_key(key: str) -> bytes:
    if not isinstance(key, str) or not key:
        raise ValueError("Rate-limit key must be a non-empty string")
    return hashlib.sha256(key.encode("utf-8")).digest()


def _login_rate_keys(username: str, source: str) -> tuple[str, str]:
    raw_username = username if isinstance(username, str) else ""
    canonical = unicodedata.normalize("NFKC", raw_username).strip().casefold()
    source_value = source if isinstance(source, str) and source else "unknown"
    username_hash = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    source_hash = hashlib.sha256(source_value.encode("utf-8")).hexdigest()
    return f"principal:{username_hash}:{source_hash}", f"source:{source_hash}"
