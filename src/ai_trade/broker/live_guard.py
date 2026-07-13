from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import (
    AppConfig,
    DEFAULT_BROKER_MAX_DAILY_NOTIONAL,
    DEFAULT_BROKER_MAX_ORDER_NOTIONAL,
)
from ..data.market import MarketData
from .base import BrokerRegistry
from .paper import _config_fingerprint
from .paper_audit import audit_paper
from .reconciliation import audit_reconciliations


LIVE_CONFIRMATION = "I_ACCEPT_LIVE_TRADING_RISK"


def require_live_confirmation() -> None:
    if os.environ.get("AI_TRADE_LIVE_CONFIRMATION") != LIVE_CONFIRMATION:
        raise RuntimeError(
            "Live trading is disabled. Set AI_TRADE_LIVE_CONFIRMATION only after paper "
            "validation, broker sandbox reconciliation, and an explicit order review."
        )


def evaluate_live_readiness(
    config: AppConfig,
    paper_audit: dict[str, Any] | None = None,
) -> dict[str, Any]:
    broker = config.raw.get("broker", {})
    adapter = broker.get("adapter")
    account_id = broker.get("account_id")
    fingerprint = _config_fingerprint(config)
    live_fingerprint = _live_configuration_fingerprint(config, fingerprint)
    paper_fingerprint = str((paper_audit or {}).get("config_fingerprint", ""))
    paper_configuration_current = paper_fingerprint == fingerprint
    reconciliation = audit_reconciliations(
        config.broker_reconciliation_file,
        str(adapter or ""),
        str(account_id or ""),
        int(broker.get("sandbox_minimum_reconciliations", 20)),
        fingerprint,
    )
    authorization = _load_authorization(config.live_authorization_file)
    authorization_valid, authorization_reason = _authorization_status(
        authorization,
        adapter=str(adapter or ""),
        account_id=str(account_id or ""),
        config_fingerprint=live_fingerprint,
    )
    installed = set(BrokerRegistry.available())
    checks = {
        "broker_mode_live": broker.get("mode") == "live",
        "adapter_configured": bool(adapter),
        "adapter_installed": bool(adapter) and adapter in installed,
        "account_configured": bool(account_id),
        "paper_configuration_current": paper_configuration_current,
        "paper_gate_passed": paper_configuration_current
        and bool((paper_audit or {}).get("eligible_for_broker_sandbox", False)),
        "sandbox_reconciled": reconciliation["eligible"],
        "kill_switch_clear": not config.live_kill_switch_file.exists(),
        "authorization_valid": authorization_valid,
        "environment_confirmed": (
            os.environ.get("AI_TRADE_LIVE_CONFIRMATION") == LIVE_CONFIRMATION
        ),
    }
    return {
        "stage": _stage(checks),
        "live_ready": all(checks.values()),
        "checks": checks,
        "adapter": adapter,
        "account_id": account_id,
        "paper_config_fingerprint": fingerprint,
        "config_fingerprint": live_fingerprint,
        "installed_adapters": sorted(installed),
        "reconciliation": reconciliation,
        "authorization": {
            "valid": authorization_valid,
            "reason": authorization_reason,
            "expires_at": authorization.get("expires_at") if authorization else None,
        },
        "kill_switch_file": str(config.live_kill_switch_file),
        "limits": {
            "max_order_notional": float(
                broker.get(
                    "max_order_notional", DEFAULT_BROKER_MAX_ORDER_NOTIONAL
                )
            ),
            "max_daily_notional": float(
                broker.get(
                    "max_daily_notional", DEFAULT_BROKER_MAX_DAILY_NOTIONAL
                )
            ),
        },
    }


def assert_live_submission_allowed(
    config: AppConfig,
    paper_audit: dict[str, Any],
    market: MarketData,
) -> dict[str, Any]:
    require_live_confirmation()
    authoritative_audit = audit_paper(config, market)
    if (
        paper_audit.get("account_id") != authoritative_audit.get("account_id")
        or paper_audit.get("config_fingerprint")
        != authoritative_audit.get("config_fingerprint")
    ):
        raise RuntimeError("Supplied paper audit does not match the active paper account")
    readiness = evaluate_live_readiness(config, authoritative_audit)
    if not readiness["live_ready"]:
        missing = [name for name, passed in readiness["checks"].items() if not passed]
        raise RuntimeError(f"Live submission gates are incomplete: {', '.join(missing)}")
    return readiness


def _load_authorization(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _authorization_status(
    authorization: dict[str, Any] | None,
    *,
    adapter: str,
    account_id: str,
    config_fingerprint: str,
) -> tuple[bool, str]:
    if authorization is None:
        return False, "authorization file is missing or invalid"
    if authorization.get("approved") is not True:
        return False, "authorization is not approved"
    if authorization.get("adapter") != adapter:
        return False, "authorization adapter does not match configuration"
    if authorization.get("account_id") != account_id:
        return False, "authorization account does not match configuration"
    if authorization.get("config_fingerprint") != config_fingerprint:
        return False, "authorization configuration fingerprint is stale"
    try:
        raw_expiry = str(authorization["expires_at"])
        if raw_expiry.endswith("Z"):
            raw_expiry = raw_expiry[:-1] + "+00:00"
        expires = datetime.fromisoformat(raw_expiry)
        if expires.tzinfo is None:
            return False, "authorization expiry must include a timezone"
        if expires <= datetime.now(timezone.utc):
            return False, "authorization has expired"
    except (KeyError, TypeError, ValueError):
        return False, "authorization expiry is invalid"
    return True, "authorization matches the active account and configuration"


def _live_configuration_fingerprint(
    config: AppConfig, paper_fingerprint: str
) -> str:
    broker = config.raw.get("broker", {})
    payload = {
        "paper_config_fingerprint": paper_fingerprint,
        "broker": {
            "mode": broker.get("mode", "disabled"),
            "adapter": broker.get("adapter"),
            "account_id": broker.get("account_id"),
            "sandbox_minimum_reconciliations": int(
                broker.get("sandbox_minimum_reconciliations", 20)
            ),
            "max_order_notional": float(
                broker.get(
                    "max_order_notional", DEFAULT_BROKER_MAX_ORDER_NOTIONAL
                )
            ),
            "max_daily_notional": float(
                broker.get(
                    "max_daily_notional", DEFAULT_BROKER_MAX_DAILY_NOTIONAL
                )
            ),
            "reconciliation_file": broker.get(
                "reconciliation_file", "state/broker_reconciliation.csv"
            ),
            "orders_file": broker.get("orders_file", "state/broker_orders.csv"),
            "fills_file": broker.get("fills_file", "state/broker_fills.csv"),
            "authorization_file": broker.get(
                "authorization_file", "state/live_authorization.json"
            ),
            "kill_switch_file": broker.get(
                "kill_switch_file", "state/LIVE_KILL_SWITCH"
            ),
        },
    }
    encoded = json.dumps(
        payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def _stage(checks: dict[str, bool]) -> str:
    if checks["environment_confirmed"] and all(checks.values()):
        return "live_authorized"
    if checks["sandbox_reconciled"]:
        return "sandbox_reconciled"
    if checks["paper_gate_passed"]:
        return "sandbox_review"
    return "paper_evidence"


class BrokerNotConfigured(RuntimeError):
    pass
