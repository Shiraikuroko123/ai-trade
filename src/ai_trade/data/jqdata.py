"""Credential-safe, read-only JQData account probing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from getpass import getpass
import importlib
import json
import math
import os
from pathlib import Path
import re
from threading import RLock
from typing import Any, Callable, Literal, Mapping, Protocol, Sequence, cast

from .evidence_io import atomic_create_json, evidence_store_lock
from ..json_utils import load_unique_json


USERNAME_ENV = "AI_TRADE_JQDATA_USERNAME"
PASSWORD_ENV = "AI_TRADE_JQDATA_PASSWORD"
JQDATA_USAGE_SCOPE = "personal_research_only"
JQDATA_PROBE_SCHEMA_VERSION = 1
JQDATA_SAMPLE_SCHEMA_VERSION = 1
MAX_JQDATA_PROBE_BYTES = 256 * 1024
MAX_JQDATA_PROBES_PER_DAY = 100
MAX_JQDATA_SAMPLE_BYTES = 2 * 1024 * 1024
MAX_JQDATA_SAMPLES_PER_DAY = 20
JQDATA_PROBE_ID = re.compile(r"jqp_[0-9a-f]{32}\Z")
JQDATA_SAMPLE_ID = re.compile(r"jqs_[0-9a-f]{32}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_MOBILE_NUMBER = re.compile(r"(?<!\d)(1\d{2})\d{6}(\d{2})(?!\d)")
_PROBE_FIELDS = frozenset(
    {
        "schema_version",
        "probe_id",
        "created_at",
        "provider",
        "sdk_version",
        "account",
        "query_count",
        "usage_scope",
        "data_requested",
        "credentials_persisted",
        "export_allowed",
        "record_sha256",
    }
)
_ACCOUNT_FIELDS = frozenset(
    {
        "license",
        "date_range_start",
        "date_range_end",
        "query_count_limit",
        "expire_time",
        "mob",
    }
)
JQDATA_SAMPLE_SECURITIES = (
    ("510300", "510300.XSHG"),
    ("510500", "510500.XSHG"),
    ("159915", "159915.XSHE"),
)
JQDATA_SAMPLE_FIELDS = (
    "open",
    "close",
    "high",
    "low",
    "volume",
    "money",
    "factor",
    "high_limit",
    "low_limit",
    "avg",
    "pre_close",
    "paused",
)
JQDATA_SAMPLE_COUNT = 20
JQDATA_SAMPLE_TOLERANCES = {
    "adjusted_price_relative": 0.005,
    "adjusted_return_absolute": 0.002,
    "volume_relative": 0.01,
    "money_relative": 0.005,
}
_SAMPLE_FIELDS = frozenset(
    {
        "schema_version",
        "sample_id",
        "created_at",
        "provider",
        "sdk_version",
        "account",
        "query_count_before",
        "query_count_after",
        "query_count_consumed",
        "request",
        "series",
        "comparison",
        "usage_scope",
        "data_requested",
        "credentials_persisted",
        "export_allowed",
        "record_sha256",
    }
)
_SESSION_LOCK = RLock()


class JQDataError(RuntimeError):
    """A JQData failure whose message is safe to log."""


class JQDataDependencyError(JQDataError):
    """The optional JQData SDK is unavailable."""


class JQDataCredentialError(JQDataError):
    """JQData credentials are missing or invalid."""


class JQDataSDK(Protocol):
    def auth(self, username: str, password: str) -> object: ...

    def logout(self) -> object: ...

    def get_account_info(self) -> object: ...

    def get_query_count(self) -> object: ...


class JQDataPriceSDK(JQDataSDK, Protocol):
    def get_price(
        self,
        security: str,
        *,
        start_date: object = None,
        end_date: object = None,
        frequency: str = "daily",
        fields: Sequence[str] | None = None,
        skip_paused: bool = False,
        fq: str = "pre",
        count: int | None = None,
        panel: bool = True,
        fill_paused: bool = True,
        round: bool = True,
    ) -> object: ...


@dataclass(frozen=True, repr=False)
class JQDataCredentials:
    username: str
    password: str

    def __post_init__(self) -> None:
        _credential_text(self.username, "username", 320)
        _credential_text(self.password, "password", 1_024)

    def __repr__(self) -> str:
        return "JQDataCredentials(username=<redacted>, password=<redacted>)"


def credentials_from_environment(
    environment: Mapping[str, str] | None = None,
) -> JQDataCredentials | None:
    values = os.environ if environment is None else environment
    username = values.get(USERNAME_ENV, "")
    password = values.get(PASSWORD_ENV, "")
    if not username and not password:
        return None
    if not username or not password:
        raise JQDataCredentialError(
            f"{USERNAME_ENV} and {PASSWORD_ENV} must be supplied together"
        )
    return JQDataCredentials(username.strip(), password)


def prompt_credentials(
    *,
    account_reader: Callable[[str], str] = input,
    password_reader: Callable[[str], str] = getpass,
) -> JQDataCredentials:
    username = account_reader("JQData account: ").strip()
    password = password_reader("JQData password: ")
    return JQDataCredentials(username, password)


def load_jqdata_sdk() -> JQDataSDK:
    try:
        module = importlib.import_module("jqdatasdk")
    except (ImportError, ModuleNotFoundError):
        raise JQDataDependencyError(
            "JQData SDK is not installed; install AI Trade with the jqdata extra"
        ) from None
    for name in ("auth", "logout", "get_account_info", "get_query_count"):
        if not callable(getattr(module, name, None)):
            raise JQDataDependencyError(
                f"Installed JQData SDK does not provide required method {name}"
            )
    return cast(JQDataSDK, module)


def probe_account(
    credentials: JQDataCredentials,
    *,
    sdk: JQDataSDK | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Authenticate, inspect entitlement and quota, then always log out."""

    selected_sdk = sdk or load_jqdata_sdk()
    with _AuthenticatedSession(selected_sdk, credentials) as authenticated:
        account_raw = _safe_sdk_call(
            authenticated.get_account_info,
            "account information query",
            credentials,
        )
        query_raw = _safe_sdk_call(
            authenticated.get_query_count,
            "query-count inspection",
            credentials,
        )

    account = _normalize_account_info(account_raw)
    query_count = _public_value(query_raw, depth=0, budget=[0])
    sdk_version = _sdk_version(selected_sdk)
    timestamp = _timestamp_text(created_at or datetime.now(timezone.utc))
    record: dict[str, Any] = {
        "schema_version": JQDATA_PROBE_SCHEMA_VERSION,
        "probe_id": None,
        "created_at": timestamp,
        "provider": "jqdata",
        "sdk_version": sdk_version,
        "account": account,
        "query_count": query_count,
        "usage_scope": JQDATA_USAGE_SCOPE,
        "data_requested": False,
        "credentials_persisted": False,
        "export_allowed": False,
        "record_sha256": None,
    }
    record["probe_id"] = "jqp_" + _probe_identity(record)[:32]
    record["record_sha256"] = _record_fingerprint(record)
    validate_probe(record)
    return record


def capture_price_sample(
    credentials: JQDataCredentials,
    *,
    end_date: date,
    local_cache_dir: str | Path,
    sdk: JQDataSDK | None = None,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Capture one bounded unadjusted price sample and compare local evidence."""

    requested_end = _sample_end_date(end_date)
    local_context = _load_local_sample_context(Path(local_cache_dir))
    selected_sdk = sdk or load_jqdata_sdk()
    if not callable(getattr(selected_sdk, "get_price", None)):
        raise JQDataDependencyError(
            "Installed JQData SDK does not provide required method get_price"
        )
    price_sdk = cast(JQDataPriceSDK, selected_sdk)
    request_end = f"{requested_end.isoformat()} 23:59:59"
    with _AuthenticatedSession(selected_sdk, credentials) as authenticated:
        account_raw = _safe_sdk_call(
            authenticated.get_account_info,
            "account information query",
            credentials,
        )
        account = _normalize_account_info(account_raw)
        _require_entitled_date(account, requested_end)
        query_before_raw = _safe_sdk_call(
            authenticated.get_query_count,
            "pre-query count inspection",
            credentials,
        )
        series: list[dict[str, Any]] = []
        for symbol, security in JQDATA_SAMPLE_SECURITIES:
            frame = _safe_sdk_call(
                lambda: price_sdk.get_price(
                    security,
                    start_date=None,
                    end_date=request_end,
                    frequency="daily",
                    fields=list(JQDATA_SAMPLE_FIELDS),
                    skip_paused=False,
                    fq="none",
                    count=JQDATA_SAMPLE_COUNT,
                    panel=False,
                    fill_paused=False,
                    round=False,
                ),
                f"price sample query for {symbol}",
                credentials,
            )
            series.append(_normalize_price_frame(frame, symbol, security))
        query_after_raw = _safe_sdk_call(
            authenticated.get_query_count,
            "post-query count inspection",
            credentials,
        )

    query_before = _public_value(query_before_raw, depth=0, budget=[0])
    query_after = _public_value(query_after_raw, depth=0, budget=[0])
    comparison = _compare_price_series(
        series,
        local_context,
        requested_end=requested_end,
    )
    timestamp = _timestamp_text(created_at or datetime.now(timezone.utc))
    record: dict[str, Any] = {
        "schema_version": JQDATA_SAMPLE_SCHEMA_VERSION,
        "sample_id": None,
        "created_at": timestamp,
        "provider": "jqdata",
        "sdk_version": _sdk_version(selected_sdk),
        "account": account,
        "query_count_before": query_before,
        "query_count_after": query_after,
        "query_count_consumed": _query_count_consumed(
            query_before,
            query_after,
        ),
        "request": {
            "securities": [
                {"symbol": symbol, "security": security}
                for symbol, security in JQDATA_SAMPLE_SECURITIES
            ],
            "start_date": None,
            "end_date": request_end,
            "frequency": "daily",
            "fields": list(JQDATA_SAMPLE_FIELDS),
            "skip_paused": False,
            "fq": "none",
            "count": JQDATA_SAMPLE_COUNT,
            "panel": False,
            "fill_paused": False,
            "round": False,
        },
        "series": series,
        "comparison": comparison,
        "usage_scope": JQDATA_USAGE_SCOPE,
        "data_requested": True,
        "credentials_persisted": False,
        "export_allowed": False,
        "record_sha256": None,
    }
    record["sample_id"] = "jqs_" + _sample_identity(record)[:32]
    record["record_sha256"] = _record_fingerprint(record)
    validate_price_sample(record)
    return record


class JQDataSampleStore:
    """Create-once local storage for licensed price samples."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    @property
    def samples_root(self) -> Path:
        return self.root / "samples"

    def publish(self, record: Mapping[str, Any]) -> dict[str, Any]:
        validate_price_sample(record)
        sample_id = str(record["sample_id"])
        day = _parse_timestamp(record["created_at"]).date().isoformat()
        target = self.samples_root / day / f"{sample_id}.json"
        with evidence_store_lock(self.root, "JQData sample"):
            if target.exists() or target.is_symlink():
                existing = self._read(target)
                if existing != record:
                    raise RuntimeError("JQData sample id collision")
                result = _json_clone(existing)
                result["reused"] = True
                return result
            directory = self.samples_root / day
            if directory.exists():
                if directory.is_symlink() or not directory.is_dir():
                    raise RuntimeError("JQData sample day directory is invalid")
                if len(list(directory.iterdir())) >= MAX_JQDATA_SAMPLES_PER_DAY:
                    raise RuntimeError("JQData daily sample capacity reached")
            atomic_create_json(
                self.root,
                target,
                dict(record),
                label="JQData sample",
                maximum_bytes=MAX_JQDATA_SAMPLE_BYTES,
            )
        result = self._read(target)
        result["reused"] = False
        return result

    def get(self, created_at: datetime, sample_id: str) -> dict[str, Any]:
        _sample_id(sample_id)
        day = _parse_aware_datetime(created_at, "created_at").date().isoformat()
        path = self.samples_root / day / f"{sample_id}.json"
        if path.is_symlink() or not path.is_file():
            raise KeyError(sample_id)
        return self._read(path)

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            value = load_unique_json(path, max_bytes=MAX_JQDATA_SAMPLE_BYTES)
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeError(f"Invalid JQData sample {path.name}: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("JQData sample must be an object")
        try:
            validate_price_sample(value)
        except ValueError as exc:
            raise RuntimeError(f"Invalid JQData sample {path.name}: {exc}") from exc
        if value["sample_id"] != path.stem:
            raise RuntimeError("JQData sample id does not match its file name")
        expected_day = _parse_timestamp(value["created_at"]).date().isoformat()
        if path.parent.name != expected_day:
            raise RuntimeError("JQData sample date does not match its directory")
        return value


def summarize_price_sample(value: Mapping[str, Any]) -> dict[str, Any]:
    """Return a console-safe summary that never includes licensed bar rows."""

    reused = bool(value.get("reused", False))
    canonical = dict(value)
    canonical.pop("reused", None)
    validate_price_sample(canonical)
    value = canonical
    account = cast(Mapping[str, Any], value["account"])
    comparison = cast(Mapping[str, Any], value["comparison"])
    symbol_results = cast(Sequence[Mapping[str, Any]], comparison["symbols"])
    series = cast(Sequence[Mapping[str, Any]], value["series"])
    return {
        "schema_version": value["schema_version"],
        "sample_id": value["sample_id"],
        "created_at": value["created_at"],
        "provider": value["provider"],
        "sdk_version": value["sdk_version"],
        "entitlement": {
            "date_range_start": account["date_range_start"],
            "date_range_end": account["date_range_end"],
            "expire_time": account["expire_time"],
        },
        "request": value["request"],
        "query_count_before": value["query_count_before"],
        "query_count_after": value["query_count_after"],
        "query_count_consumed": value["query_count_consumed"],
        "series": [
            {
                "symbol": item["symbol"],
                "security": item["security"],
                "row_count": item["row_count"],
                "first_session": item["first_session"],
                "last_session": item["last_session"],
                "normalized_rows_sha256": item["normalized_rows_sha256"],
            }
            for item in series
        ],
        "comparison": {
            "summary": comparison["summary"],
            "symbols": [
                {
                    "symbol": item["symbol"],
                    "status": item["status"],
                    "overlap_sessions": item["overlap_sessions"],
                    "volume_unit": item["volume_unit"],
                    "checks": item["checks"],
                    "metrics": item["metrics"],
                    "missing_local_sessions": item["missing_local_sessions"],
                    "missing_jqdata_sessions": item["missing_jqdata_sessions"],
                }
                for item in symbol_results
            ],
        },
        "usage_scope": value["usage_scope"],
        "data_requested": value["data_requested"],
        "credentials_persisted": value["credentials_persisted"],
        "export_allowed": value["export_allowed"],
        "record_sha256": value["record_sha256"],
        "reused": reused,
    }


class JQDataProbeStore:
    """Create-once local storage for sanitized entitlement probes."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    @property
    def probes_root(self) -> Path:
        return self.root / "probes"

    def publish(self, record: Mapping[str, Any]) -> dict[str, Any]:
        validate_probe(record)
        probe_id = str(record["probe_id"])
        day = _parse_timestamp(record["created_at"]).date().isoformat()
        target = self.probes_root / day / f"{probe_id}.json"
        with evidence_store_lock(self.root, "JQData probe"):
            if target.exists() or target.is_symlink():
                existing = self._read(target)
                if existing != record:
                    raise RuntimeError("JQData probe id collision")
                result = _json_clone(existing)
                result["reused"] = True
                return result
            directory = self.probes_root / day
            if directory.exists():
                if directory.is_symlink() or not directory.is_dir():
                    raise RuntimeError("JQData probe day directory is invalid")
                members = list(directory.iterdir())
                if len(members) >= MAX_JQDATA_PROBES_PER_DAY:
                    raise RuntimeError("JQData daily probe capacity reached")
            atomic_create_json(
                self.root,
                target,
                dict(record),
                label="JQData probe",
                maximum_bytes=MAX_JQDATA_PROBE_BYTES,
            )
        result = self._read(target)
        result["reused"] = False
        return result

    def get(self, created_at: datetime, probe_id: str) -> dict[str, Any]:
        _probe_id(probe_id)
        day = _parse_aware_datetime(created_at, "created_at").date().isoformat()
        path = self.probes_root / day / f"{probe_id}.json"
        if path.is_symlink() or not path.is_file():
            raise KeyError(probe_id)
        return self._read(path)

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            value = load_unique_json(path, max_bytes=MAX_JQDATA_PROBE_BYTES)
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeError(f"Invalid JQData probe {path.name}: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("JQData probe must be an object")
        try:
            validate_probe(value)
        except ValueError as exc:
            raise RuntimeError(f"Invalid JQData probe {path.name}: {exc}") from exc
        if value["probe_id"] != path.stem:
            raise RuntimeError("JQData probe id does not match its file name")
        expected_day = _parse_timestamp(value["created_at"]).date().isoformat()
        if path.parent.name != expected_day:
            raise RuntimeError("JQData probe date does not match its directory")
        return value


class _AuthenticatedSession:
    def __init__(self, sdk: JQDataSDK, credentials: JQDataCredentials) -> None:
        self.sdk = sdk
        self.credentials = credentials
        self.authenticated = False
        self.locked = False

    def __enter__(self) -> JQDataSDK:
        _SESSION_LOCK.acquire()
        self.locked = True
        try:
            result = self.sdk.auth(self.credentials.username, self.credentials.password)
        except Exception as exc:
            self._release()
            detail = _safe_exception(exc, self.credentials)
            raise JQDataCredentialError(
                f"JQData authentication failed: {detail}"
            ) from None
        if not _auth_succeeded(result):
            self._release()
            raise JQDataCredentialError("JQData authentication was rejected")
        self.authenticated = True
        return self.sdk

    def __exit__(
        self,
        exception_type: type[BaseException] | None,
        exception: BaseException | None,
        traceback: object,
    ) -> Literal[False]:
        logout_error: Exception | None = None
        try:
            if self.authenticated:
                try:
                    self.sdk.logout()
                except Exception as exc:
                    logout_error = exc
        finally:
            self.authenticated = False
            self._release()
        if logout_error is not None and exception_type is None:
            detail = _safe_exception(logout_error, self.credentials)
            raise JQDataError(f"JQData logout failed: {detail}") from None
        return False

    def _release(self) -> None:
        if self.locked:
            self.locked = False
            _SESSION_LOCK.release()


def validate_probe(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != _PROBE_FIELDS:
        raise ValueError("JQData probe fields are invalid")
    if value.get("schema_version") != JQDATA_PROBE_SCHEMA_VERSION:
        raise ValueError("JQData probe schema version is invalid")
    probe_id = _probe_id(value.get("probe_id"))
    timestamp = _timestamp_text(_parse_timestamp(value.get("created_at")))
    if value.get("created_at") != timestamp:
        raise ValueError("JQData probe timestamp is not canonical UTC")
    if value.get("provider") != "jqdata":
        raise ValueError("JQData probe provider is invalid")
    _text(value.get("sdk_version"), "sdk_version", 80)

    account = value.get("account")
    if not isinstance(account, Mapping) or set(account) != _ACCOUNT_FIELDS:
        raise ValueError("JQData probe account fields are invalid")
    for key, item in account.items():
        normalized_item = _public_value(item, depth=0, budget=[0])
        if normalized_item != item:
            raise ValueError("JQData probe account value is not canonical")
        if key == "mob" and item is not None:
            if not isinstance(item, str) or "*" not in item:
                raise ValueError("JQData probe mobile number is not masked")
    query_count = _public_value(value.get("query_count"), depth=0, budget=[0])
    if query_count != value.get("query_count"):
        raise ValueError("JQData probe query count is not canonical")
    if value.get("usage_scope") != JQDATA_USAGE_SCOPE:
        raise ValueError("JQData probe usage scope is invalid")
    if (
        value.get("data_requested") is not False
        or value.get("credentials_persisted") is not False
        or value.get("export_allowed") is not False
    ):
        raise ValueError("JQData probe safety boundary is invalid")
    if probe_id != "jqp_" + _probe_identity(value)[:32]:
        raise ValueError("JQData probe id does not match content")
    fingerprint = _fingerprint(value.get("record_sha256"), "record_sha256")
    if fingerprint != _record_fingerprint(value):
        raise ValueError("JQData probe fingerprint does not match content")


def _normalize_account_info(value: object) -> dict[str, Any]:
    raw = _mapping(value, "JQData account information")
    result = {
        key: _public_value(raw.get(key), depth=0, budget=[0]) for key in _ACCOUNT_FIELDS
    }
    result["mob"] = _mask_mobile(raw.get("mob"))
    return result


def _mapping(value: object, label: str) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        if isinstance(converted, Mapping):
            return converted
    raise JQDataError(f"{label} returned an unsupported result")


def _public_value(value: object, *, depth: int, budget: list[int]) -> Any:
    if depth > 6:
        raise JQDataError("JQData public result is too deeply nested")
    budget[0] += 1
    if budget[0] > 2_000:
        raise JQDataError("JQData public result contains too many values")
    if value is None or type(value) is bool or type(value) is int:
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise JQDataError("JQData public result contains a non-finite number")
        return value
    if isinstance(value, str):
        text = _MOBILE_NUMBER.sub(r"\1******\2", value)
        if len(text) > 1_000 or any(ord(character) < 32 for character in text):
            raise JQDataError("JQData public result contains invalid text")
        return text
    if isinstance(value, (datetime,)):
        return _timestamp_text(value)
    isoformat = getattr(value, "isoformat", None)
    if callable(isoformat):
        text = str(isoformat())
        if len(text) <= 100:
            return text
    item = getattr(value, "item", None)
    if callable(item):
        converted = item()
        if converted is not value:
            return _public_value(converted, depth=depth + 1, budget=budget)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item_value in value.items():
            key_text = str(key)
            if not key_text or len(key_text) > 200:
                raise JQDataError("JQData public result contains an invalid key")
            result[key_text] = _public_value(item_value, depth=depth + 1, budget=budget)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [
            _public_value(item_value, depth=depth + 1, budget=budget)
            for item_value in value
        ]
    raise JQDataError("JQData public result contains an unsupported value")


def _mask_mobile(value: object) -> str | None:
    if value is None:
        return None
    text = str(value)
    match = re.fullmatch(r"(1\d{2})\d{6}(\d{2})", text)
    if match:
        return f"{match.group(1)}******{match.group(2)}"
    if len(text) <= 4:
        return "*" * len(text)
    return text[:2] + "*" * (len(text) - 4) + text[-2:]


def _auth_succeeded(value: object) -> bool:
    if value is False:
        return False
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return not value or value[0] is not False
    return True


def _safe_sdk_call(
    function: Callable[[], object],
    label: str,
    credentials: JQDataCredentials,
) -> object:
    try:
        return function()
    except Exception as exc:
        detail = _safe_exception(exc, credentials)
        raise JQDataError(f"JQData {label} failed: {detail}") from None


def _safe_exception(error: BaseException, credentials: JQDataCredentials) -> str:
    message = f"{type(error).__name__}: {error}"
    for secret in (credentials.password, credentials.username):
        if secret:
            message = message.replace(secret, "<redacted>")
    message = _MOBILE_NUMBER.sub(r"\1******\2", message)
    message = " ".join(message.split())
    return message[:300] or type(error).__name__


def _sdk_version(sdk: object) -> str:
    value = getattr(sdk, "__version__", "unknown")
    text = str(value).strip()
    return text if text and len(text) <= 80 else "unknown"


def _probe_identity(value: Mapping[str, Any]) -> str:
    body = _json_clone(value)
    body["probe_id"] = None
    body["record_sha256"] = None
    return _json_fingerprint(body)


def _record_fingerprint(value: Mapping[str, Any]) -> str:
    body = _json_clone(value)
    body["record_sha256"] = None
    return _json_fingerprint(body)


def _json_fingerprint(value: object) -> str:
    from hashlib import sha256

    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _parse_aware_datetime(value: datetime, label: str) -> datetime:
    if not isinstance(value, datetime):
        raise ValueError(f"{label} must be a datetime")
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{label} must include a timezone")
    return value.astimezone(timezone.utc)


def _parse_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("JQData probe timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("JQData probe timestamp is invalid") from exc
    return _parse_aware_datetime(parsed, "created_at")


def _timestamp_text(value: datetime) -> str:
    return (
        _parse_aware_datetime(value, "created_at")
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def _credential_text(value: object, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise JQDataCredentialError(f"JQData {label} is invalid")
    return value


def _probe_id(value: object) -> str:
    if not isinstance(value, str) or JQDATA_PROBE_ID.fullmatch(value) is None:
        raise ValueError("JQData probe id is invalid")
    return value


def _fingerprint(value: object, label: str) -> str:
    if not isinstance(value, str) or _FINGERPRINT.fullmatch(value) is None:
        raise ValueError(f"JQData probe {label} is invalid")
    return value


def _text(value: object, label: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value.strip() != value
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"JQData probe {label} is invalid")
    return value


def validate_price_sample(value: Mapping[str, Any]) -> None:
    if not isinstance(value, Mapping) or set(value) != _SAMPLE_FIELDS:
        raise ValueError("JQData sample fields are invalid")
    if value.get("schema_version") != JQDATA_SAMPLE_SCHEMA_VERSION:
        raise ValueError("JQData sample schema version is invalid")
    sample_id = _sample_id(value.get("sample_id"))
    timestamp = _timestamp_text(_parse_timestamp(value.get("created_at")))
    if value.get("created_at") != timestamp:
        raise ValueError("JQData sample timestamp is not canonical UTC")
    if value.get("provider") != "jqdata":
        raise ValueError("JQData sample provider is invalid")
    _text(value.get("sdk_version"), "sdk_version", 80)
    _validate_sample_account(value.get("account"))
    for field in ("query_count_before", "query_count_after"):
        normalized = _public_value(value.get(field), depth=0, budget=[0])
        if normalized != value.get(field):
            raise ValueError(f"JQData sample {field} is not canonical")
    consumed = value.get("query_count_consumed")
    if consumed is not None:
        _nonnegative_number(consumed, "query_count_consumed")
    requested_end = _validate_sample_request(value.get("request"))
    series = value.get("series")
    if not isinstance(series, list) or len(series) != len(JQDATA_SAMPLE_SECURITIES):
        raise ValueError("JQData sample series are invalid")
    for item, expected in zip(series, JQDATA_SAMPLE_SECURITIES):
        _validate_price_series(item, expected, requested_end)
    _validate_sample_comparison(value.get("comparison"))
    if value.get("usage_scope") != JQDATA_USAGE_SCOPE:
        raise ValueError("JQData sample usage scope is invalid")
    if (
        value.get("data_requested") is not True
        or value.get("credentials_persisted") is not False
        or value.get("export_allowed") is not False
    ):
        raise ValueError("JQData sample safety boundary is invalid")
    if sample_id != "jqs_" + _sample_identity(value)[:32]:
        raise ValueError("JQData sample id does not match content")
    fingerprint = _fingerprint(value.get("record_sha256"), "record_sha256")
    if fingerprint != _record_fingerprint(value):
        raise ValueError("JQData sample fingerprint does not match content")


def _validate_sample_account(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != _ACCOUNT_FIELDS:
        raise ValueError("JQData sample account fields are invalid")
    for key, item in value.items():
        normalized = _public_value(item, depth=0, budget=[0])
        if normalized != item:
            raise ValueError("JQData sample account value is not canonical")
        if key == "mob" and item is not None:
            if not isinstance(item, str) or "*" not in item:
                raise ValueError("JQData sample mobile number is not masked")


def _validate_sample_request(value: object) -> date:
    expected_fields = {
        "securities",
        "start_date",
        "end_date",
        "frequency",
        "fields",
        "skip_paused",
        "fq",
        "count",
        "panel",
        "fill_paused",
        "round",
    }
    if not isinstance(value, Mapping) or set(value) != expected_fields:
        raise ValueError("JQData sample request fields are invalid")
    expected_securities = [
        {"symbol": symbol, "security": security}
        for symbol, security in JQDATA_SAMPLE_SECURITIES
    ]
    if value.get("securities") != expected_securities:
        raise ValueError("JQData sample securities are invalid")
    if (
        value.get("start_date") is not None
        or value.get("frequency") != "daily"
        or value.get("fields") != list(JQDATA_SAMPLE_FIELDS)
        or value.get("skip_paused") is not False
        or value.get("fq") != "none"
        or value.get("count") != JQDATA_SAMPLE_COUNT
        or value.get("panel") is not False
        or value.get("fill_paused") is not False
        or value.get("round") is not False
    ):
        raise ValueError("JQData sample request policy is invalid")
    end_text = value.get("end_date")
    if (
        not isinstance(end_text, str)
        or not end_text.endswith(" 23:59:59")
        or len(end_text) != 19
    ):
        raise ValueError("JQData sample end date is invalid")
    try:
        return date.fromisoformat(end_text[:10])
    except ValueError as exc:
        raise ValueError("JQData sample end date is invalid") from exc


def _validate_price_series(
    value: object,
    expected: tuple[str, str],
    requested_end: date,
) -> None:
    fields = {
        "symbol",
        "security",
        "row_count",
        "first_session",
        "last_session",
        "normalized_rows_sha256",
        "rows",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("JQData price series fields are invalid")
    symbol, security = expected
    if value.get("symbol") != symbol or value.get("security") != security:
        raise ValueError("JQData price series identity is invalid")
    rows = value.get("rows")
    row_count = value.get("row_count")
    if (
        isinstance(row_count, bool)
        or not isinstance(row_count, int)
        or row_count < 1
        or row_count > JQDATA_SAMPLE_COUNT
        or not isinstance(rows, list)
        or len(rows) != row_count
    ):
        raise ValueError("JQData price series row count is invalid")
    previous: date | None = None
    for row in rows:
        session = _validate_remote_row(row)
        if previous is not None and session <= previous:
            raise ValueError("JQData price sessions must be strictly increasing")
        if session > requested_end:
            raise ValueError("JQData price session exceeds requested end date")
        previous = session
    if (
        value.get("first_session") != rows[0]["session"]
        or value.get("last_session") != rows[-1]["session"]
    ):
        raise ValueError("JQData price series bounds are invalid")
    fingerprint = _fingerprint(
        value.get("normalized_rows_sha256"),
        "normalized_rows_sha256",
    )
    if fingerprint != _json_fingerprint(rows):
        raise ValueError("JQData normalized rows fingerprint does not match")


def _validate_remote_row(value: object) -> date:
    expected = {"session", *JQDATA_SAMPLE_FIELDS}
    if not isinstance(value, Mapping) or set(value) != expected:
        raise ValueError("JQData price row fields are invalid")
    session = _canonical_date(value.get("session"), "JQData price session")
    paused = value.get("paused")
    if type(paused) is not bool:
        raise ValueError("JQData paused value is invalid")
    for field in JQDATA_SAMPLE_FIELDS:
        if field != "paused" and value.get(field) is not None:
            _finite_number(value.get(field), f"JQData {field}")
    if not paused:
        for field in ("open", "close", "high", "low", "volume", "money", "factor"):
            if value.get(field) is None:
                raise ValueError(f"JQData active row omits {field}")
    prices = [
        value.get(field)
        for field in ("open", "close", "high", "low")
        if value.get(field) is not None
    ]
    if prices:
        numeric_prices = [_finite_number(item, "JQData price") for item in prices]
        if min(numeric_prices) <= 0:
            raise ValueError("JQData price row contains a non-positive price")
        high = value.get("high")
        low = value.get("low")
        if high is not None and low is not None:
            if _finite_number(high, "JQData high") < max(numeric_prices) or (
                _finite_number(low, "JQData low") > min(numeric_prices)
            ):
                raise ValueError("JQData price row has an invalid OHLC relationship")
    for field in ("volume", "money"):
        item = value.get(field)
        if item is not None and float(item) < 0:
            raise ValueError(f"JQData {field} is negative")
    factor = value.get("factor")
    if factor is not None and float(factor) <= 0:
        raise ValueError("JQData factor must be positive")
    return session


def _validate_sample_comparison(value: object) -> None:
    if not isinstance(value, Mapping) or set(value) != {
        "local_snapshot",
        "methodology",
        "symbols",
        "summary",
    }:
        raise ValueError("JQData sample comparison fields are invalid")
    snapshot = value.get("local_snapshot")
    if not isinstance(snapshot, Mapping) or set(snapshot) != {
        "provider",
        "adjustment",
        "downloaded_at",
        "completed_through",
        "latest_common_session",
        "manifest_sha256",
    }:
        raise ValueError("JQData local snapshot fields are invalid")
    _text(snapshot.get("provider"), "local provider", 80)
    if snapshot.get("adjustment") != "forward":
        raise ValueError("JQData comparison requires a forward-adjusted local cache")
    for field in ("downloaded_at", "completed_through", "latest_common_session"):
        item = snapshot.get(field)
        if item is not None:
            _text(item, field, 100)
    _fingerprint(snapshot.get("manifest_sha256"), "manifest_sha256")
    methodology = value.get("methodology")
    if methodology != {
        "jqdata_price_adjustment": "none",
        "local_price_adjustment": "forward",
        "adjusted_reference_formula": "jqdata_price_x_factor",
        "volume_unit_candidates": [1, 100],
        "tolerances": JQDATA_SAMPLE_TOLERANCES,
    }:
        raise ValueError("JQData comparison methodology is invalid")
    symbols = value.get("symbols")
    if not isinstance(symbols, list) or len(symbols) != len(JQDATA_SAMPLE_SECURITIES):
        raise ValueError("JQData comparison symbols are invalid")
    matched = 0
    for item, expected in zip(symbols, JQDATA_SAMPLE_SECURITIES):
        _validate_symbol_comparison(item, expected[0])
        if item["status"] == "matched":
            matched += 1
    summary = value.get("summary")
    expected_summary = {
        "checked": len(JQDATA_SAMPLE_SECURITIES),
        "matched": matched,
        "mismatch": len(JQDATA_SAMPLE_SECURITIES) - matched,
        "gate_passed": matched == len(JQDATA_SAMPLE_SECURITIES),
    }
    if summary != expected_summary:
        raise ValueError("JQData comparison summary is invalid")


def _validate_symbol_comparison(value: object, expected_symbol: str) -> None:
    fields = {
        "symbol",
        "status",
        "local_source",
        "local_cache_sha256",
        "local_rows_sha256",
        "local_rows",
        "overlap_sessions",
        "missing_local_sessions",
        "missing_jqdata_sessions",
        "remote_paused_sessions",
        "volume_unit",
        "checks",
        "metrics",
    }
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("JQData symbol comparison fields are invalid")
    if value.get("symbol") != expected_symbol:
        raise ValueError("JQData comparison symbol is invalid")
    if value.get("status") not in {"matched", "mismatch"}:
        raise ValueError("JQData comparison status is invalid")
    _text(value.get("local_source"), "local source", 120)
    _fingerprint(value.get("local_cache_sha256"), "local_cache_sha256")
    local_rows = value.get("local_rows")
    if not isinstance(local_rows, list):
        raise ValueError("JQData local comparison rows are invalid")
    previous: date | None = None
    for row in local_rows:
        session = _validate_local_row(row)
        if previous is not None and session <= previous:
            raise ValueError("Local comparison sessions must be strictly increasing")
        previous = session
    local_hash = _fingerprint(value.get("local_rows_sha256"), "local_rows_sha256")
    if local_hash != _json_fingerprint(local_rows):
        raise ValueError("Local comparison rows fingerprint does not match")
    overlap = value.get("overlap_sessions")
    if isinstance(overlap, bool) or not isinstance(overlap, int) or overlap < 0:
        raise ValueError("JQData overlap session count is invalid")
    for field in (
        "missing_local_sessions",
        "missing_jqdata_sessions",
        "remote_paused_sessions",
    ):
        sessions = value.get(field)
        if not isinstance(sessions, list):
            raise ValueError(f"JQData {field} is invalid")
        for session in sessions:
            _canonical_date(session, field)
    if value.get("volume_unit") not in {
        "same_unit",
        "jqdata_shares_local_lots_100",
        "unrecognized",
    }:
        raise ValueError("JQData comparison volume unit is invalid")
    checks = value.get("checks")
    if (
        not isinstance(checks, Mapping)
        or not checks
        or not all(
            isinstance(key, str) and type(item) is bool for key, item in checks.items()
        )
    ):
        raise ValueError("JQData comparison checks are invalid")
    if (value.get("status") == "matched") != all(checks.values()):
        raise ValueError("JQData comparison status does not match checks")
    metrics = value.get("metrics")
    if not isinstance(metrics, Mapping):
        raise ValueError("JQData comparison metrics are invalid")
    for item in metrics.values():
        if item is not None:
            _finite_number(item, "JQData comparison metric")


def _validate_local_row(value: object) -> date:
    fields = {"session", "open", "close", "high", "low", "volume", "money"}
    if not isinstance(value, Mapping) or set(value) != fields:
        raise ValueError("Local comparison row fields are invalid")
    session = _canonical_date(value.get("session"), "local comparison session")
    for field in fields - {"session"}:
        _finite_number(value.get(field), f"local {field}")
    prices = [float(value[field]) for field in ("open", "close", "high", "low")]
    if (
        min(prices) <= 0
        or prices[2] < max(prices)
        or prices[3] > min(prices)
        or float(value["volume"]) < 0
        or float(value["money"]) < 0
    ):
        raise ValueError("Local comparison row values are invalid")
    return session


def _load_local_sample_context(cache_dir: Path) -> dict[str, Any]:
    supplied_root = Path(cache_dir)
    if supplied_root.is_symlink():
        raise RuntimeError("Local cache directory must not be symbolic")
    root = supplied_root.resolve()
    if not root.is_dir():
        raise RuntimeError(f"Local cache directory is missing: {root}")
    manifest_path = root / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        raise RuntimeError("Local cache manifest is missing or symbolic")
    try:
        manifest = load_unique_json(
            manifest_path,
            max_bytes=MAX_JQDATA_SAMPLE_BYTES,
        )
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Local cache manifest is invalid: {exc}") from exc
    if not isinstance(manifest, Mapping):
        raise RuntimeError("Local cache manifest must be an object")
    provider = manifest.get("provider")
    if not isinstance(provider, str) or not provider:
        raise RuntimeError("Local cache provider is invalid")
    if manifest.get("adjustment") != "forward":
        raise RuntimeError(
            "JQData sample comparison requires data/cache adjustment=forward"
        )
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        raise RuntimeError("Local cache manifest files are invalid")

    from .eastmoney import load_cached_bars

    symbols: dict[str, dict[str, Any]] = {}
    for symbol, _security in JQDATA_SAMPLE_SECURITIES:
        metadata = files.get(symbol)
        if not isinstance(metadata, Mapping):
            raise RuntimeError(f"Local cache manifest omits {symbol}")
        path = root / f"{symbol}.csv"
        if path.is_symlink() or not path.is_file() or path.resolve().parent != root:
            raise RuntimeError(f"Local cache file is missing or invalid for {symbol}")
        actual_hash = _file_sha256(path)
        expected_hash = metadata.get("sha256")
        if (
            not isinstance(expected_hash, str)
            or _FINGERPRINT.fullmatch(expected_hash.lower()) is None
            or actual_hash != expected_hash.lower()
        ):
            raise RuntimeError(f"Local cache hash does not match manifest for {symbol}")
        bars = load_cached_bars(path)
        expected_rows = metadata.get("rows")
        if (
            isinstance(expected_rows, bool)
            or not isinstance(expected_rows, int)
            or expected_rows != len(bars)
        ):
            raise RuntimeError(
                f"Local cache row count does not match manifest for {symbol}"
            )
        source = metadata.get("source")
        if not isinstance(source, str) or not source:
            raise RuntimeError(f"Local cache source is invalid for {symbol}")
        symbols[symbol] = {
            "bars": bars,
            "cache_sha256": actual_hash,
            "source": source,
        }
    return {
        "snapshot": {
            "provider": provider,
            "adjustment": "forward",
            "downloaded_at": _bounded_optional_text(
                manifest.get("downloaded_at"), "downloaded_at"
            ),
            "completed_through": _bounded_optional_text(
                manifest.get("completed_through"), "completed_through"
            ),
            "latest_common_session": _bounded_optional_text(
                manifest.get("latest_common_session"), "latest_common_session"
            ),
            "manifest_sha256": _file_sha256(manifest_path),
        },
        "symbols": symbols,
    }


def _normalize_price_frame(
    frame: object,
    symbol: str,
    security: str,
) -> dict[str, Any]:
    try:
        columns = [str(item) for item in getattr(frame, "columns")]
        index = list(getattr(frame, "index"))
    except (AttributeError, TypeError) as exc:
        raise JQDataError(
            f"JQData price query for {symbol} returned an unsupported table"
        ) from exc
    missing = [field for field in JQDATA_SAMPLE_FIELDS if field not in columns]
    if missing:
        raise JQDataError(
            f"JQData price query for {symbol} omitted fields: {', '.join(missing)}"
        )
    to_dict = getattr(frame, "to_dict", None)
    if not callable(to_dict):
        raise JQDataError(
            f"JQData price query for {symbol} returned an unsupported table"
        )
    try:
        raw_rows = to_dict(orient="records")
    except (TypeError, ValueError) as exc:
        raise JQDataError(
            f"JQData price query for {symbol} could not be normalized"
        ) from exc
    if (
        not isinstance(raw_rows, list)
        or len(raw_rows) != len(index)
        or not raw_rows
        or len(raw_rows) > JQDATA_SAMPLE_COUNT
    ):
        raise JQDataError(
            f"JQData price query for {symbol} returned an invalid row count"
        )

    rows: list[dict[str, Any]] = []
    previous: date | None = None
    for position, raw_row in enumerate(raw_rows):
        if not isinstance(raw_row, Mapping):
            raise JQDataError(f"JQData price row for {symbol} is not an object")
        session = _normalize_daily_session(index[position], symbol)
        if previous is not None and session <= previous:
            raise JQDataError(
                f"JQData price sessions for {symbol} are not strictly increasing"
            )
        previous = session
        normalized: dict[str, Any] = {"session": session.isoformat()}
        for field in JQDATA_SAMPLE_FIELDS:
            if field == "paused":
                normalized[field] = _normalize_paused(raw_row.get(field), symbol)
            else:
                normalized[field] = _normalize_optional_number(
                    raw_row.get(field), f"{symbol} {field}"
                )
        _validate_remote_row(normalized)
        rows.append(normalized)
    return {
        "symbol": symbol,
        "security": security,
        "row_count": len(rows),
        "first_session": rows[0]["session"],
        "last_session": rows[-1]["session"],
        "normalized_rows_sha256": _json_fingerprint(rows),
        "rows": rows,
    }


def _normalize_daily_session(value: object, symbol: str) -> date:
    candidate = value
    to_datetime = getattr(candidate, "to_pydatetime", None)
    if callable(to_datetime):
        candidate = to_datetime()
    if isinstance(candidate, datetime):
        return candidate.date()
    if type(candidate) is date:
        return cast(date, candidate)
    text = str(candidate)
    try:
        return date.fromisoformat(text[:10])
    except (TypeError, ValueError) as exc:
        raise JQDataError(
            f"JQData price query for {symbol} returned an invalid session"
        ) from exc


def _normalize_optional_number(value: object, label: str) -> float | None:
    if value is None:
        return None
    item = getattr(value, "item", None)
    if callable(item):
        converted = item()
        if converted is not value:
            value = converted
    try:
        number = float(cast(Any, value))
    except (TypeError, ValueError) as exc:
        raise JQDataError(f"JQData {label} is not numeric") from exc
    if math.isnan(number):
        return None
    if not math.isfinite(number):
        raise JQDataError(f"JQData {label} is not finite")
    return number


def _normalize_paused(value: object, symbol: str) -> bool:
    if type(value) is bool:
        return cast(bool, value)
    number = _normalize_optional_number(value, f"{symbol} paused")
    if number not in {0.0, 1.0}:
        raise JQDataError(f"JQData paused value for {symbol} is invalid")
    return number == 1.0


def _compare_price_series(
    series: Sequence[Mapping[str, Any]],
    local_context: Mapping[str, Any],
    *,
    requested_end: date,
) -> dict[str, Any]:
    local_symbols = cast(Mapping[str, Mapping[str, Any]], local_context["symbols"])
    results = [
        _compare_symbol_series(
            item,
            local_symbols[str(item["symbol"])],
            requested_end=requested_end,
        )
        for item in series
    ]
    matched = sum(item["status"] == "matched" for item in results)
    return {
        "local_snapshot": _json_clone(local_context["snapshot"]),
        "methodology": {
            "jqdata_price_adjustment": "none",
            "local_price_adjustment": "forward",
            "adjusted_reference_formula": "jqdata_price_x_factor",
            "volume_unit_candidates": [1, 100],
            "tolerances": dict(JQDATA_SAMPLE_TOLERANCES),
        },
        "symbols": results,
        "summary": {
            "checked": len(results),
            "matched": matched,
            "mismatch": len(results) - matched,
            "gate_passed": matched == len(results),
        },
    }


def _compare_symbol_series(
    remote_series: Mapping[str, Any],
    local_context: Mapping[str, Any],
    *,
    requested_end: date,
) -> dict[str, Any]:
    remote_rows = cast(Sequence[Mapping[str, Any]], remote_series["rows"])
    first = date.fromisoformat(str(remote_rows[0]["session"]))
    last = date.fromisoformat(str(remote_rows[-1]["session"]))
    bars = local_context["bars"]
    local_rows = [
        {
            "session": bar.date.isoformat(),
            "open": float(bar.open),
            "close": float(bar.close),
            "high": float(bar.high),
            "low": float(bar.low),
            "volume": float(bar.volume),
            "money": float(bar.amount),
        }
        for bar in bars
        if first <= bar.date <= last
    ]
    local_by_session = {str(row["session"]): row for row in local_rows}
    remote_by_session = {str(row["session"]): row for row in remote_rows}
    remote_sessions = list(remote_by_session)
    local_sessions = list(local_by_session)
    missing_local = [
        session for session in remote_sessions if session not in local_by_session
    ]
    missing_remote = [
        session for session in local_sessions if session not in remote_by_session
    ]
    overlap_sessions = [
        session for session in remote_sessions if session in local_by_session
    ]
    pairs = [
        (remote_by_session[session], local_by_session[session])
        for session in overlap_sessions
    ]

    close_scales: list[float] = []
    factors: list[float] = []
    for remote, local in pairs:
        factor = _positive_value(remote.get("factor"))
        remote_close = _positive_value(remote.get("close"))
        local_close = _positive_value(local.get("close"))
        if factor is not None:
            factors.append(factor)
        if factor is not None and remote_close is not None and local_close is not None:
            close_scales.append(local_close / (remote_close * factor))
    price_scale = _median(close_scales)
    price_errors: list[float] = []
    if price_scale is not None and price_scale > 0:
        for remote, local in pairs:
            factor = _positive_value(remote.get("factor"))
            if factor is None:
                continue
            for field in ("open", "close", "high", "low"):
                remote_value = _positive_value(remote.get(field))
                local_value = _positive_value(local.get(field))
                if remote_value is None or local_value is None:
                    continue
                expected = remote_value * factor * price_scale
                price_errors.append(abs(local_value / expected - 1.0))

    return_errors: list[float] = []
    for (remote_previous, local_previous), (remote_current, local_current) in zip(
        pairs, pairs[1:]
    ):
        previous_factor = _positive_value(remote_previous.get("factor"))
        current_factor = _positive_value(remote_current.get("factor"))
        previous_remote_close = _positive_value(remote_previous.get("close"))
        current_remote_close = _positive_value(remote_current.get("close"))
        previous_local_close = _positive_value(local_previous.get("close"))
        current_local_close = _positive_value(local_current.get("close"))
        if (
            previous_factor is None
            or current_factor is None
            or previous_remote_close is None
            or current_remote_close is None
            or previous_local_close is None
            or current_local_close is None
        ):
            continue
        reference_return = (
            current_remote_close
            * current_factor
            / (previous_remote_close * previous_factor)
            - 1.0
        )
        local_return = current_local_close / previous_local_close - 1.0
        return_errors.append(abs(local_return - reference_return))

    volume_ratios: list[float] = []
    money_errors: list[float] = []
    for remote, local in pairs:
        remote_volume = _nonnegative_value(remote.get("volume"))
        local_volume = _nonnegative_value(local.get("volume"))
        if (
            remote_volume is not None
            and local_volume is not None
            and remote_volume > 0
            and local_volume > 0
        ):
            volume_ratios.append(remote_volume / local_volume)
        remote_money = _nonnegative_value(remote.get("money"))
        local_money = _nonnegative_value(local.get("money"))
        if remote_money is not None and local_money is not None:
            money_errors.append(
                abs(remote_money - local_money) / max(abs(remote_money), 1.0)
            )
    volume_median = _median(volume_ratios)
    volume_target: float | None = None
    volume_errors: list[float] = []
    if volume_median is not None:
        volume_target = min(
            (1.0, 100.0),
            key=lambda item: abs(volume_median / item - 1),
        )
        volume_errors = [abs(item / volume_target - 1.0) for item in volume_ratios]
    volume_max_error = _maximum(volume_errors)
    volume_recognized = (
        volume_target is not None
        and volume_max_error is not None
        and volume_max_error <= JQDATA_SAMPLE_TOLERANCES["volume_relative"]
    )
    if not volume_recognized:
        volume_unit = "unrecognized"
    elif volume_target == 100.0:
        volume_unit = "jqdata_shares_local_lots_100"
    else:
        volume_unit = "same_unit"

    state_fields_available = all(
        row.get("factor") is not None
        and row.get("high_limit") is not None
        and row.get("low_limit") is not None
        and row.get("pre_close") is not None
        and row.get("paused") is not None
        for row in remote_rows
    )
    factor_available = len(factors) == len(remote_rows)
    price_max_error = _maximum(price_errors)
    return_max_error = _maximum(return_errors)
    money_max_error = _maximum(money_errors)
    checks = {
        "expected_row_count": len(remote_rows) == JQDATA_SAMPLE_COUNT,
        "end_date_inclusive": str(remote_series["last_session"])
        == requested_end.isoformat(),
        "session_calendar_match": not missing_local
        and not missing_remote
        and len(overlap_sessions) == JQDATA_SAMPLE_COUNT,
        "factor_available": factor_available,
        "price_state_fields_available": state_fields_available,
        "adjusted_ohlc_match": price_max_error is not None
        and price_max_error <= JQDATA_SAMPLE_TOLERANCES["adjusted_price_relative"],
        "adjusted_return_match": return_max_error is not None
        and return_max_error <= JQDATA_SAMPLE_TOLERANCES["adjusted_return_absolute"],
        "volume_unit_recognized": volume_recognized,
        "volume_match": volume_max_error is not None
        and volume_max_error <= JQDATA_SAMPLE_TOLERANCES["volume_relative"],
        "money_match": money_max_error is not None
        and money_max_error <= JQDATA_SAMPLE_TOLERANCES["money_relative"],
    }
    metrics = {
        "adjusted_price_scale": _metric(price_scale),
        "adjusted_price_max_relative_error": _metric(price_max_error),
        "adjusted_return_max_absolute_error": _metric(return_max_error),
        "volume_ratio_median": _metric(volume_median),
        "volume_ratio_target": _metric(volume_target),
        "volume_max_relative_error": _metric(volume_max_error),
        "money_max_relative_error": _metric(money_max_error),
        "factor_min": _metric(min(factors) if factors else None),
        "factor_max": _metric(max(factors) if factors else None),
    }
    return {
        "symbol": remote_series["symbol"],
        "status": "matched" if all(checks.values()) else "mismatch",
        "local_source": local_context["source"],
        "local_cache_sha256": local_context["cache_sha256"],
        "local_rows_sha256": _json_fingerprint(local_rows),
        "local_rows": local_rows,
        "overlap_sessions": len(overlap_sessions),
        "missing_local_sessions": missing_local,
        "missing_jqdata_sessions": missing_remote,
        "remote_paused_sessions": [
            str(row["session"]) for row in remote_rows if row.get("paused") is True
        ],
        "volume_unit": volume_unit,
        "checks": checks,
        "metrics": metrics,
    }


def _require_entitled_date(account: Mapping[str, Any], requested_end: date) -> None:
    try:
        entitlement_start = date.fromisoformat(str(account["date_range_start"])[:10])
        entitlement_end = date.fromisoformat(str(account["date_range_end"])[:10])
    except (KeyError, TypeError, ValueError) as exc:
        raise JQDataError("JQData entitlement date range is invalid") from exc
    if not entitlement_start <= requested_end <= entitlement_end:
        raise JQDataError(
            "Requested JQData sample end date is outside the licensed date range"
        )


def _query_count_consumed(before: object, after: object) -> float | int | None:
    if not isinstance(before, Mapping) or not isinstance(after, Mapping):
        return None
    before_spare = before.get("spare")
    after_spare = after.get("spare")
    if (
        isinstance(before_spare, bool)
        or isinstance(after_spare, bool)
        or not isinstance(before_spare, (int, float))
        or not isinstance(after_spare, (int, float))
    ):
        return None
    consumed = float(before_spare) - float(after_spare)
    if not math.isfinite(consumed) or consumed < 0:
        return None
    return int(consumed) if consumed.is_integer() else consumed


def _sample_end_date(value: date) -> date:
    if type(value) is not date:
        raise ValueError("JQData sample end_date must be a date")
    return value


def _sample_identity(value: Mapping[str, Any]) -> str:
    body = _json_clone(value)
    body["sample_id"] = None
    body["record_sha256"] = None
    return _json_fingerprint(body)


def _sample_id(value: object) -> str:
    if not isinstance(value, str) or JQDATA_SAMPLE_ID.fullmatch(value) is None:
        raise ValueError("JQData sample id is invalid")
    return value


def _canonical_date(value: object, label: str) -> date:
    if not isinstance(value, str) or len(value) != 10:
        raise ValueError(f"{label} is invalid")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{label} is not canonical")
    return parsed


def _bounded_optional_text(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > 100:
        raise RuntimeError(f"Local cache {label} is invalid")
    return value


def _file_sha256(path: Path) -> str:
    from hashlib import sha256

    digest = sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{label} is not numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ValueError(f"{label} is not finite")
    return number


def _nonnegative_number(value: object, label: str) -> float:
    number = _finite_number(value, label)
    if number < 0:
        raise ValueError(f"{label} is negative")
    return number


def _positive_value(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number > 0 else None


def _nonnegative_value(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _median(values: Sequence[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    midpoint = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) / 2.0


def _maximum(values: Sequence[float]) -> float | None:
    return max(values) if values else None


def _metric(value: float | None) -> float | None:
    if value is None:
        return None
    return round(float(value), 12)


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


__all__ = [
    "JQDATA_PROBE_ID",
    "JQDATA_PROBE_SCHEMA_VERSION",
    "JQDATA_USAGE_SCOPE",
    "PASSWORD_ENV",
    "USERNAME_ENV",
    "JQDataCredentialError",
    "JQDataCredentials",
    "JQDataDependencyError",
    "JQDataError",
    "JQDataProbeStore",
    "credentials_from_environment",
    "load_jqdata_sdk",
    "probe_account",
    "prompt_credentials",
    "validate_probe",
    "JQDATA_SAMPLE_COUNT",
    "JQDATA_SAMPLE_FIELDS",
    "JQDATA_SAMPLE_ID",
    "JQDATA_SAMPLE_SCHEMA_VERSION",
    "JQDATA_SAMPLE_SECURITIES",
    "JQDataSampleStore",
    "capture_price_sample",
    "summarize_price_sample",
    "validate_price_sample",
]
