from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from ..config import AppConfig
from .base import (
    Broker,
    BrokerAccount,
    BrokerEnvironment,
    BrokerFill,
    BrokerHealth,
    BrokerOrderSnapshot,
    BrokerPosition,
    BrokerRegistry,
)
from .paper import paper_status
from .reconciliation import ReconciliationIssue, reconcile_account


@dataclass(frozen=True)
class _BrokerObservation:
    broker: Broker
    health: BrokerHealth
    account: BrokerAccount
    positions: list[BrokerPosition]
    open_orders: list[BrokerOrderSnapshot]
    fills: list[BrokerFill]


def available_broker_adapters() -> dict[str, object]:
    return {
        "schema_version": 1,
        "adapters": list(BrokerRegistry.available()),
        "discovery_group": BrokerRegistry.ENTRY_POINT_GROUP,
    }


def probe_configured_broker(config: AppConfig) -> dict[str, object]:
    observation = _collect_observation(config)
    try:
        broker = observation.broker
        return {
            "schema_version": 1,
            "observed_at": datetime.now(timezone.utc).isoformat(),
            "adapter": broker.adapter_name,
            "environment": broker.environment.value,
            "authority": _authority(broker),
            "health": _serialize(observation.health),
            "account": _public_account(observation.account),
            "positions": [_serialize(value) for value in observation.positions],
            "open_orders": [
                _serialize(value) for value in observation.open_orders
            ],
            "fills": [_serialize(value) for value in observation.fills],
            "fee_observation": {
                "commission_complete": bool(
                    getattr(broker, "fill_commission_complete", False)
                ),
                "tax_complete": bool(getattr(broker, "fill_tax_complete", False)),
                "warning": (
                    "Missing broker fee fields are observational gaps; this probe "
                    "does not treat zero as proof that no fee was charged."
                ),
            },
            "evidence": {
                "qualifying_reconciliation_recorded": False,
                "reason": (
                    "A read-only probe cannot demonstrate sandbox order execution "
                    "or write promotion evidence."
                ),
            },
        }
    finally:
        _close(observation.broker)


def compare_configured_broker(config: AppConfig) -> dict[str, object]:
    observation = _collect_observation(config)
    try:
        state = paper_status(config)
        expected_cash = float(state["cash"])
        expected_positions = {
            str(symbol): int(quantity)
            for symbol, quantity in dict(state["positions"]).items()
        }
        issues = reconcile_account(
            expected_cash,
            expected_positions,
            observation.account,
            observation.positions,
        )
        return {
            "schema_version": 1,
            "compared_at": datetime.now(timezone.utc).isoformat(),
            "adapter": observation.broker.adapter_name,
            "environment": observation.broker.environment.value,
            "diagnostic_only": True,
            "matches_account_and_positions": not issues,
            "paper_account_hint": _account_hint(str(state["account_id"])),
            "broker_account_hint": _account_hint(observation.account.account_id),
            "expected_cash": expected_cash,
            "observed_cash": observation.account.cash,
            "issues": [_serialize_issue(issue) for issue in issues],
            "observed_open_orders": len(observation.open_orders),
            "observed_fills_today": len(observation.fills),
            "qualifying_reconciliation_recorded": False,
            "reason": (
                "This comparison is read-only and does not prove broker sandbox "
                "execution, paper/live isolation, fee completeness, or order lifecycle "
                "reconciliation."
            ),
        }
    finally:
        _close(observation.broker)


def _collect_observation(config: AppConfig) -> _BrokerObservation:
    broker_config = config.raw.get("broker", {})
    if broker_config.get("mode") != BrokerEnvironment.SANDBOX.value:
        raise RuntimeError("Broker probes require broker.mode='sandbox'")
    adapter = str(broker_config.get("adapter") or "")
    configured_account = str(broker_config.get("account_id") or "")
    if not adapter or not configured_account:
        raise RuntimeError("Broker probes require a configured adapter and account_id")

    broker = BrokerRegistry.create(adapter, config, BrokerEnvironment.SANDBOX)
    try:
        if broker.environment != BrokerEnvironment.SANDBOX:
            raise RuntimeError("Broker probe returned the wrong environment")
        health = broker.health()
        if not health.connected:
            raise RuntimeError(health.message or "Broker connection is unavailable")
        account = broker.account()
        if account.account_id != configured_account:
            raise RuntimeError("Broker account does not match the configured account")
        return _BrokerObservation(
            broker=broker,
            health=health,
            account=account,
            positions=broker.positions(),
            open_orders=broker.open_orders(),
            fills=broker.fills(),
        )
    except Exception:
        _close(broker)
        raise


def _authority(broker: Broker) -> dict[str, bool]:
    read_only = bool(getattr(broker, "read_only", False))
    return {
        "read_only": read_only,
        "order_submission_available": not read_only,
        "order_cancellation_available": not read_only,
        "runtime_environment_verified": bool(
            getattr(broker, "runtime_environment_verified", False)
        ),
        "qualifying_reconciliation_supported": bool(
            getattr(broker, "qualifying_reconciliation_supported", False)
        ),
    }


def _public_account(account: BrokerAccount) -> dict[str, object]:
    return {
        "account_hint": _account_hint(account.account_id),
        "currency": account.currency,
        "cash": account.cash,
        "available_cash": account.available_cash,
        "equity": account.equity,
    }


def _account_hint(value: str) -> str:
    if len(value) <= 4:
        return "*" * len(value)
    return f"{'*' * min(8, len(value) - 4)}{value[-4:]}"


def _serialize(value: Any) -> dict[str, object]:
    payload = asdict(value)
    for key, item in tuple(payload.items()):
        if isinstance(item, datetime):
            payload[key] = item.isoformat()
        elif isinstance(item, Enum):
            payload[key] = item.value
    return payload


def _serialize_issue(issue: ReconciliationIssue) -> dict[str, object]:
    return {
        "kind": issue.kind,
        "key": issue.key,
        "expected": issue.expected,
        "actual": issue.actual,
    }


def _close(broker: Broker) -> None:
    close = getattr(broker, "close", None)
    if callable(close):
        close()
