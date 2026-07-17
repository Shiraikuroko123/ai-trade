from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from ..json_utils import load_unique_json
from .base import BrokerEnvironment


BROKER_LEDGER_SCOPE_SCHEMA_VERSION = 1
MAX_SCOPE_MANIFEST_BYTES = 64 * 1024
_SCOPE_FIELDS = {
    "schema_version",
    "scope_id",
    "adapter",
    "account_fingerprint",
    "environment",
    "config_fingerprint",
    "orders_ledger_fingerprint",
    "fills_ledger_fingerprint",
}


@dataclass(frozen=True)
class BrokerLedgerScope:
    adapter: str
    account_fingerprint: str
    environment: BrokerEnvironment
    config_fingerprint: str
    orders_ledger_fingerprint: str
    fills_ledger_fingerprint: str

    @property
    def scope_id(self) -> str:
        return _scope_id(self._identity_payload())

    def manifest(self) -> dict[str, object]:
        return {
            "schema_version": BROKER_LEDGER_SCOPE_SCHEMA_VERSION,
            "scope_id": self.scope_id,
            **self._identity_payload(),
        }

    def public_dict(self, status: str, message: str) -> dict[str, object]:
        return {
            "schema_version": BROKER_LEDGER_SCOPE_SCHEMA_VERSION,
            "status": status,
            "scope_id": self.scope_id,
            "adapter": self.adapter,
            "environment": self.environment.value,
            "account_reference": self.account_fingerprint[:12],
            "configuration_reference": self.config_fingerprint[:12],
            "message": message,
            "qualifying_evidence": False,
            "execution_enabled": False,
        }

    def require_ledger_path(self, role: str, path: Path) -> None:
        if role == "orders":
            expected = self.orders_ledger_fingerprint
        elif role == "fills":
            expected = self.fills_ledger_fingerprint
        else:
            raise ValueError("Broker ledger role must be orders or fills")
        if expected != _ledger_path_fingerprint(role, path):
            raise RuntimeError(
                f"Broker {role} ledger path does not match its bound scope"
            )

    def _identity_payload(self) -> dict[str, str]:
        return {
            "adapter": self.adapter,
            "account_fingerprint": self.account_fingerprint,
            "environment": self.environment.value,
            "config_fingerprint": self.config_fingerprint,
            "orders_ledger_fingerprint": self.orders_ledger_fingerprint,
            "fills_ledger_fingerprint": self.fills_ledger_fingerprint,
        }


def create_broker_ledger_scope(
    *,
    adapter: str,
    account_id: str,
    environment: BrokerEnvironment,
    config_fingerprint: str,
    orders_path: Path,
    fills_path: Path,
) -> BrokerLedgerScope:
    adapter = _identity_text(adapter, "adapter")
    account_id = _identity_text(account_id, "account_id")
    if not isinstance(environment, BrokerEnvironment):
        raise ValueError("Broker ledger environment must be declared")
    config_fingerprint = _sha256_value(
        config_fingerprint, "configuration fingerprint"
    )
    if orders_path.resolve() == fills_path.resolve():
        raise ValueError("Broker order and fill ledgers must use different paths")
    account_payload = json.dumps(
        {"adapter": adapter, "account_id": account_id},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return BrokerLedgerScope(
        adapter=adapter,
        account_fingerprint=hashlib.sha256(
            account_payload.encode("ascii")
        ).hexdigest(),
        environment=environment,
        config_fingerprint=config_fingerprint,
        orders_ledger_fingerprint=_ledger_path_fingerprint(
            "orders", orders_path
        ),
        fills_ledger_fingerprint=_ledger_path_fingerprint("fills", fills_path),
    )


def ensure_scope_manifest(
    path: Path,
    expected: BrokerLedgerScope,
    *,
    legacy_ledgers_exist: bool,
) -> None:
    preflight_scope_manifest(
        path,
        expected,
        legacy_ledgers_exist=legacy_ledgers_exist,
    )
    if path.exists():
        return
    _atomic_write_manifest(path, expected.manifest())


def preflight_scope_manifest(
    path: Path,
    expected: BrokerLedgerScope,
    *,
    legacy_ledgers_exist: bool,
) -> None:
    """Reject mismatched or unscoped evidence without creating new authority."""
    if path.exists():
        require_scope_manifest(path, expected)
        return
    if legacy_ledgers_exist:
        raise RuntimeError(
            "Existing broker ledgers have no scope manifest; archive them and start "
            "new scoped ledgers before broker I/O"
        )


def require_scope_manifest(path: Path, expected: BrokerLedgerScope) -> None:
    if not path.exists():
        raise RuntimeError("Broker ledger scope manifest is missing")
    actual = read_scope_manifest(path)
    if actual != expected:
        raise RuntimeError(
            "Broker ledger scope does not match the active adapter, account, "
            "environment, configuration, or ledger paths"
        )


def read_scope_manifest(path: Path) -> BrokerLedgerScope:
    try:
        value = load_unique_json(path, max_bytes=MAX_SCOPE_MANIFEST_BYTES)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError("Broker ledger scope manifest cannot be read") from exc
    if not isinstance(value, dict) or set(value) != _SCOPE_FIELDS:
        raise RuntimeError("Broker ledger scope manifest schema is invalid")
    if (
        isinstance(value["schema_version"], bool)
        or value["schema_version"] != BROKER_LEDGER_SCOPE_SCHEMA_VERSION
    ):
        raise RuntimeError("Broker ledger scope manifest version is invalid")
    try:
        scope = BrokerLedgerScope(
            adapter=_identity_text(value["adapter"], "adapter"),
            account_fingerprint=_sha256_value(
                value["account_fingerprint"], "account fingerprint"
            ),
            environment=BrokerEnvironment(
                _identity_text(value["environment"], "environment")
            ),
            config_fingerprint=_sha256_value(
                value["config_fingerprint"], "configuration fingerprint"
            ),
            orders_ledger_fingerprint=_sha256_value(
                value["orders_ledger_fingerprint"], "order ledger fingerprint"
            ),
            fills_ledger_fingerprint=_sha256_value(
                value["fills_ledger_fingerprint"], "fill ledger fingerprint"
            ),
        )
    except ValueError as exc:
        raise RuntimeError(f"Broker ledger scope manifest is invalid: {exc}") from exc
    if value["scope_id"] != scope.scope_id:
        raise RuntimeError("Broker ledger scope manifest failed content validation")
    return scope


def inspect_scope_manifest(
    path: Path,
    orders_path: Path,
    fills_path: Path,
    expected: BrokerLedgerScope | None = None,
) -> dict[str, object]:
    ledgers_exist = orders_path.exists() or fills_path.exists()
    if not path.exists():
        status = "UNSCOPED" if ledgers_exist else "EMPTY"
        message = (
            "Existing lifecycle ledgers predate scope binding"
            if ledgers_exist
            else "No scoped broker lifecycle has been created"
        )
        return _empty_scope_report(status, message)
    try:
        actual = read_scope_manifest(path)
    except RuntimeError as exc:
        return _empty_scope_report("INVALID", str(exc))
    current_paths_match = (
        actual.orders_ledger_fingerprint
        == _ledger_path_fingerprint("orders", orders_path)
        and actual.fills_ledger_fingerprint
        == _ledger_path_fingerprint("fills", fills_path)
    )
    if not current_paths_match:
        return actual.public_dict(
            "INVALID", "Scope manifest belongs to different ledger paths"
        )
    if expected is not None and actual != expected:
        report = actual.public_dict(
            "MISMATCH", "Scope manifest does not match the active broker context"
        )
        report["mismatch_dimensions"] = _mismatch_dimensions(actual, expected)
        return report
    return actual.public_dict(
        "BOUND", "Lifecycle ledgers are bound to the active broker context"
    )


def _mismatch_dimensions(
    actual: BrokerLedgerScope,
    expected: BrokerLedgerScope,
) -> list[str]:
    dimensions = []
    for name in (
        "adapter",
        "account_fingerprint",
        "environment",
        "config_fingerprint",
        "orders_ledger_fingerprint",
        "fills_ledger_fingerprint",
    ):
        if getattr(actual, name) != getattr(expected, name):
            dimensions.append(
                {
                    "account_fingerprint": "account",
                    "config_fingerprint": "configuration",
                    "orders_ledger_fingerprint": "order_ledger_path",
                    "fills_ledger_fingerprint": "fill_ledger_path",
                }.get(name, name)
            )
    return dimensions


def _empty_scope_report(status: str, message: str) -> dict[str, object]:
    return {
        "schema_version": BROKER_LEDGER_SCOPE_SCHEMA_VERSION,
        "status": status,
        "scope_id": None,
        "adapter": None,
        "environment": None,
        "account_reference": None,
        "configuration_reference": None,
        "message": message,
        "qualifying_evidence": False,
        "execution_enabled": False,
    }


def _scope_id(payload: dict[str, str]) -> str:
    raw = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )
    return "v1_" + hashlib.sha256(raw.encode("ascii")).hexdigest()[:24]


def _ledger_path_fingerprint(role: str, path: Path) -> str:
    normalized = os.path.normcase(str(path.resolve())).replace("\\", "/")
    return hashlib.sha256(f"{role}|{normalized}".encode("utf-8")).hexdigest()


def _identity_text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value.strip() != value:
        raise ValueError(f"Broker ledger {label} must be a non-empty string")
    if any(ord(character) < 32 for character in value):
        raise ValueError(f"Broker ledger {label} contains a control character")
    return value


def _sha256_value(value: object, label: str) -> str:
    value = _identity_text(value, label)
    if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
        raise ValueError(f"Broker ledger {label} must be a lowercase SHA-256 value")
    return value


def _atomic_write_manifest(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    encoded = (
        json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
