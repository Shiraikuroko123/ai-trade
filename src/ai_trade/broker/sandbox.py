"""Local sandbox broker drill.

The sandbox exercises the full broker order-lifecycle machinery — scope
binding, mandate enforcement, intent reservation, and observation
reconciliation — against deterministic fills derived from the local bar
cache. It is a rehearsal environment, not a qualification path:

* Its ledgers live under ``state/sandbox/`` and are bound to their own
  scope manifest; they can never be confused with the live broker ledgers.
* It never writes the promotion-countable reconciliation evidence
  (``state/broker_reconciliation.csv``) or any live broker file, and every
  drill record attests with before/after digests that those files were
  untouched.
* Drill outcomes carry ``qualifying_evidence: false`` and
  ``execution_enabled: false``; a sandbox fill grants no authority.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from ..config import (
    AppConfig,
    DEFAULT_BROKER_MAX_DAILY_NOTIONAL,
    DEFAULT_BROKER_MAX_ORDER_NOTIONAL,
)
from ..data.eastmoney import load_cached_bars
from ..data.evidence_io import atomic_create_json, evidence_store_lock
from ..json_utils import load_unique_json
from ..models import Bar, Instrument
from .base import (
    Broker,
    BrokerAccessLevel,
    BrokerAccount,
    BrokerCapabilities,
    BrokerEnvironment,
    BrokerFill,
    BrokerHealth,
    BrokerOperation,
    BrokerOrderRequest,
    BrokerOrderSnapshot,
    BrokerPosition,
    OrderSide,
    OrderStatus,
)
from .ledger import (
    append_broker_observation,
    initialize_broker_ledger_scope,
    recover_order_lifecycle,
    reserve_order_intents,
    submitted_order_count,
    submitted_order_notional,
)
from .lifecycle import TERMINAL_ORDER_STATUSES
from .mandate import BrokerMandate, order_batch_fingerprint
from .scope import BrokerLedgerScope, create_broker_ledger_scope


SANDBOX_SCHEMA_VERSION = 1
SANDBOX_ENGINE_VERSION = "sandbox-1.0.0"
SANDBOX_ADAPTER_NAME = "local-sandbox"
SANDBOX_ACCOUNT_ID = "sandbox-account"
SANDBOX_VIRTUAL_CASH = 1_000_000.0
MAX_DRILLS = 500
MAX_DRILL_RECORD_BYTES = 128 * 1024
CHINA_STANDARD_TIME = timezone(timedelta(hours=8))
_SUBMIT_TIME = time(9, 30)
_SETTLE_TIME = time(15, 0)

SANDBOX_SAFETY = {
    "research_only": True,
    "environment": "sandbox",
    "qualifying_evidence": False,
    "promotion_countable": False,
    "execution_enabled": False,
    "reconciliation_evidence_written": False,
    "live_ledgers_touched": False,
}

_BATCH_APPROVAL_DISCLOSURE = (
    "一次性批次审批文件是实盘专属的人工授权闸门；沙箱演练只计算并记录批次指纹，"
    "不创建、不消费任何审批文件，也不会因此获得实盘提交权限。"
)

_DRILL_FIELDS = {
    "schema_version",
    "engine_version",
    "drill_id",
    "created_at",
    "session",
    "scope",
    "order",
    "batch_fingerprint",
    "mandate",
    "outcome",
    "lifecycle",
    "protected_evidence",
    "disclosure",
    "safety",
    "record_fingerprint",
}

SANDBOX_CAPABILITIES = BrokerCapabilities(
    adapter_name=SANDBOX_ADAPTER_NAME,
    access_level=BrokerAccessLevel.SANDBOX,
    operations=frozenset(
        {
            BrokerOperation.READ_ACCOUNT,
            BrokerOperation.READ_POSITIONS,
            BrokerOperation.READ_ORDERS,
            BrokerOperation.READ_FILLS,
            BrokerOperation.SUBMIT_ORDERS,
        }
    ),
    # The adapter constructs its own sandbox in-process, so the runtime
    # environment is verified by construction rather than by probing.
    environments=frozenset({BrokerEnvironment.SANDBOX}),
    runtime_environment_verified=True,
    qualifying_reconciliation_supported=False,
    requires_local_client=False,
)


@dataclass(frozen=True)
class _SandboxOutcome:
    status: OrderStatus
    fill_price: float | None
    commission: float
    tax: float


class SandboxBroker(Broker):
    """Deterministic in-process broker bound to one cached trading session."""

    adapter_name = SANDBOX_ADAPTER_NAME
    environment = BrokerEnvironment.SANDBOX
    capabilities = SANDBOX_CAPABILITIES

    def __init__(
        self,
        config: AppConfig,
        instrument: Instrument,
        bar: Bar,
        *,
        holdings: dict[str, int] | None = None,
    ):
        self._config = config
        self._instrument = instrument
        self._bar = bar
        self._holdings = dict(holdings or {})
        self._orders: dict[str, BrokerOrderSnapshot] = {}
        self._fills: list[BrokerFill] = []

    def health(self) -> BrokerHealth:
        return BrokerHealth(
            connected=True,
            trading_session=True,
            message=f"Local sandbox replaying cached session {self._bar.date}",
            checked_at=datetime.now(timezone.utc),
        )

    def account(self) -> BrokerAccount:
        return BrokerAccount(
            account_id=SANDBOX_ACCOUNT_ID,
            currency=self._instrument.currency,
            cash=SANDBOX_VIRTUAL_CASH,
            available_cash=SANDBOX_VIRTUAL_CASH,
            equity=SANDBOX_VIRTUAL_CASH,
        )

    def positions(self) -> list[BrokerPosition]:
        return [
            BrokerPosition(
                symbol=symbol,
                quantity=quantity,
                available_quantity=quantity,
                average_cost=self._bar.open,
                market_value=quantity * self._bar.close,
            )
            for symbol, quantity in sorted(self._holdings.items())
        ]

    def open_orders(self) -> list[BrokerOrderSnapshot]:
        return [
            order
            for order in self._orders.values()
            if order.status not in TERMINAL_ORDER_STATUSES
        ]

    def submit_orders(
        self, orders: list[BrokerOrderRequest]
    ) -> list[BrokerOrderSnapshot]:
        submitted = []
        acknowledged_at = datetime.combine(
            self._bar.date, _SUBMIT_TIME, tzinfo=CHINA_STANDARD_TIME
        )
        for order in orders:
            if order.symbol != self._instrument.symbol:
                raise ValueError(
                    "Sandbox session is bound to "
                    f"{self._instrument.symbol}; got {order.symbol}"
                )
            snapshot = BrokerOrderSnapshot(
                client_order_id=order.client_order_id,
                broker_order_id=_broker_order_id(order.client_order_id),
                symbol=order.symbol,
                side=order.side,
                quantity=order.quantity,
                filled_quantity=0,
                limit_price=order.limit_price,
                average_fill_price=None,
                status=OrderStatus.SUBMITTED,
                updated_at=acknowledged_at,
                message="Sandbox acknowledged the order deterministically",
            )
            self._orders[order.client_order_id] = snapshot
            submitted.append(snapshot)
        return submitted

    def cancel_order(self, broker_order_id: str) -> BrokerOrderSnapshot:
        raise PermissionError(
            "The local sandbox drill does not declare cancel_orders; "
            "deterministic sessions settle every order at the close"
        )

    def fills(self, since: datetime | None = None) -> list[BrokerFill]:
        if since is None:
            return list(self._fills)
        return [fill for fill in self._fills if fill.filled_at >= since]

    def settle(self) -> tuple[list[BrokerOrderSnapshot], list[BrokerFill]]:
        """Resolve every acknowledged order against the cached session bar.

        Crossing rule (deterministic, disclosed): a resting BUY fills iff
        its limit is at or above the session low; a resting SELL fills iff
        its limit is at or below the session high. Fills execute at the
        limit price (conservative); everything else expires at the close.
        """
        settled_at = datetime.combine(
            self._bar.date, _SETTLE_TIME, tzinfo=CHINA_STANDARD_TIME
        )
        terminal: list[BrokerOrderSnapshot] = []
        fills: list[BrokerFill] = []
        for order in self._orders.values():
            if order.status in TERMINAL_ORDER_STATUSES:
                continue
            outcome = self._outcome(order)
            if outcome.status is OrderStatus.FILLED:
                snapshot = BrokerOrderSnapshot(
                    client_order_id=order.client_order_id,
                    broker_order_id=order.broker_order_id,
                    symbol=order.symbol,
                    side=order.side,
                    quantity=order.quantity,
                    filled_quantity=order.quantity,
                    limit_price=order.limit_price,
                    average_fill_price=outcome.fill_price,
                    status=OrderStatus.FILLED,
                    updated_at=settled_at,
                    message="Sandbox limit crossed the cached session range",
                )
                fills.append(
                    BrokerFill(
                        fill_id=f"sbxf_{order.broker_order_id[5:]}",
                        broker_order_id=order.broker_order_id,
                        client_order_id=order.client_order_id,
                        symbol=order.symbol,
                        side=order.side,
                        quantity=order.quantity,
                        price=float(outcome.fill_price or 0.0),
                        commission=outcome.commission,
                        tax=outcome.tax,
                        filled_at=settled_at,
                    )
                )
            else:
                snapshot = BrokerOrderSnapshot(
                    client_order_id=order.client_order_id,
                    broker_order_id=order.broker_order_id,
                    symbol=order.symbol,
                    side=order.side,
                    quantity=order.quantity,
                    filled_quantity=0,
                    limit_price=order.limit_price,
                    average_fill_price=None,
                    status=OrderStatus.EXPIRED,
                    updated_at=settled_at,
                    message="Sandbox limit never crossed the cached session range",
                )
            self._orders[order.client_order_id] = snapshot
            terminal.append(snapshot)
        self._fills.extend(fills)
        return terminal, fills

    def _outcome(self, order: BrokerOrderSnapshot) -> _SandboxOutcome:
        crossed = (
            order.limit_price >= self._bar.low
            if order.side is OrderSide.BUY
            else order.limit_price <= self._bar.high
        )
        if not crossed:
            return _SandboxOutcome(OrderStatus.EXPIRED, None, 0.0, 0.0)
        notional = order.quantity * order.limit_price
        schedule = self._config.costs.for_instrument(
            self._instrument, self._bar.date
        )
        commission = max(
            schedule.minimum_commission,
            notional * schedule.commission_bps / 10_000.0,
        ) + notional * schedule.transfer_fee_bps / 10_000.0
        tax = (
            notional * schedule.sell_stamp_duty_bps / 10_000.0
            if order.side is OrderSide.SELL
            else 0.0
        )
        return _SandboxOutcome(
            OrderStatus.FILLED, order.limit_price, commission, tax
        )


class SandboxCycleEngine:
    """Run one auditable order-lifecycle drill in the isolated sandbox scope."""

    def __init__(self, config: AppConfig):
        self.config = config
        self.root = (config.project_root / "state" / "sandbox").resolve()
        self.orders_path = self.root / "orders.csv"
        self.fills_path = self.root / "fills.csv"
        self.scope_path = self.root / "ledger_scope.json"
        self.drills_root = self.root / "drills"
        self._assert_isolation()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def cycle(
        self,
        symbol: str,
        *,
        side: str = "BUY",
        quantity: int | None = None,
        session: date | None = None,
        limit_price: float | None = None,
    ) -> dict[str, Any]:
        instrument = self._instrument(symbol)
        order_side = self._side(side)
        bar = self._session_bar(instrument, session)
        request, holdings = self._order_request(
            instrument, order_side, quantity, bar, limit_price
        )
        broker = SandboxBroker(
            self.config, instrument, bar, holdings=holdings
        )
        broker.capabilities.require(
            frozenset(
                {
                    BrokerOperation.READ_ACCOUNT,
                    BrokerOperation.READ_POSITIONS,
                    BrokerOperation.SUBMIT_ORDERS,
                }
            ),
            BrokerEnvironment.SANDBOX,
        )
        self._assert_resources(broker, request)

        protected_before = self._protected_digests()
        scope = self._scope()
        initialize_broker_ledger_scope(
            self.scope_path, self.orders_path, self.fills_path, scope
        )
        mandate = self._mandate(instrument, order_side)
        submitted_notional = submitted_order_notional(
            self.orders_path,
            bar.date,
            scope_path=self.scope_path,
            scope=scope,
        )
        submitted_count = submitted_order_count(
            self.orders_path,
            bar.date,
            scope_path=self.scope_path,
            scope=scope,
        )
        mandate.enforce(
            [request],
            submitted_orders=submitted_count,
            submitted_notional=submitted_notional,
        )
        batch_fingerprint = order_batch_fingerprint(
            [request],
            on_date=bar.date,
            adapter=SANDBOX_ADAPTER_NAME,
            account_id=SANDBOX_ACCOUNT_ID,
            config_fingerprint=scope.config_fingerprint,
        )
        reserve_order_intents(
            self.orders_path,
            [request],
            bar.date,
            mandate.max_daily_notional,
            mandate.max_orders_per_day,
            scope_path=self.scope_path,
            scope=scope,
        )
        acknowledged = broker.submit_orders([request])
        append_broker_observation(
            self.orders_path,
            self.fills_path,
            acknowledged,
            [],
            scope_path=self.scope_path,
            scope=scope,
        )
        terminal, fills = broker.settle()
        report = append_broker_observation(
            self.orders_path,
            self.fills_path,
            terminal,
            fills,
            scope_path=self.scope_path,
            scope=scope,
        )

        protected_after = self._protected_digests()
        protected = self._protected_attestation(
            protected_before, protected_after
        )
        outcome_order = terminal[0]
        fill = fills[0] if fills else None
        record = {
            "schema_version": SANDBOX_SCHEMA_VERSION,
            "engine_version": SANDBOX_ENGINE_VERSION,
            "drill_id": f"drill_{uuid4().hex}",
            "created_at": _utc_now(),
            "session": bar.date.isoformat(),
            "scope": {
                "scope_id": scope.scope_id,
                "adapter": scope.adapter,
                "environment": scope.environment.value,
                "account_reference": scope.account_fingerprint[:12],
                "config_fingerprint": scope.config_fingerprint,
            },
            "order": {
                "client_order_id": request.client_order_id,
                "symbol": request.symbol,
                "side": request.side.value,
                "quantity": request.quantity,
                "limit_price": request.limit_price,
                "notional": request.quantity * request.limit_price,
            },
            "batch_fingerprint": batch_fingerprint,
            "mandate": mandate.public_dict(),
            "outcome": {
                "status": outcome_order.status.value,
                "fill_price": fill.price if fill else None,
                "fill_quantity": fill.quantity if fill else 0,
                "commission": fill.commission if fill else 0.0,
                "tax": fill.tax if fill else 0.0,
                "session_low": bar.low,
                "session_high": bar.high,
            },
            "lifecycle": {
                "status": report["status"],
                "order_count": report["order_count"],
                "open_order_count": report["open_order_count"],
                "fill_count": report["fill_count"],
                "submission_unconfirmed_count": report[
                    "submission_unconfirmed_count"
                ],
            },
            "protected_evidence": protected,
            "disclosure": _BATCH_APPROVAL_DISCLOSURE,
            "safety": dict(SANDBOX_SAFETY),
        }
        record["record_fingerprint"] = _record_fingerprint(record)
        self._publish(record)
        return record

    def status(self) -> dict[str, Any]:
        scope = self._scope()
        report = recover_order_lifecycle(
            self.orders_path,
            self.fills_path,
            scope_path=self.scope_path,
            expected_scope=scope,
        )
        return {
            "schema_version": SANDBOX_SCHEMA_VERSION,
            "root": str(self.root),
            "lifecycle": report,
            "drills": self.list_drills(limit=5),
            "safety": dict(SANDBOX_SAFETY),
        }

    def list_drills(self, *, limit: int = 50) -> dict[str, Any]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= MAX_DRILLS
        ):
            raise ValueError(
                f"Sandbox drill list limit must be between 1 and {MAX_DRILLS}"
            )
        records = self._records()
        ordered = sorted(
            records,
            key=lambda item: (str(item["created_at"]), str(item["drill_id"])),
            reverse=True,
        )
        visible = [
            {
                "drill_id": item["drill_id"],
                "created_at": item["created_at"],
                "session": item["session"],
                "symbol": item["order"]["symbol"],
                "side": item["order"]["side"],
                "status": item["outcome"]["status"],
                "lifecycle_status": item["lifecycle"]["status"],
                "protected_evidence_unchanged": all(
                    entry["unchanged"] for entry in item["protected_evidence"]
                ),
            }
            for item in ordered[:limit]
        ]
        return {
            "schema_version": SANDBOX_SCHEMA_VERSION,
            "drills": visible,
            "summary": {
                "total": len(ordered),
                "returned": len(visible),
                "limit": limit,
                "maximum": MAX_DRILLS,
            },
            "safety": dict(SANDBOX_SAFETY),
        }

    def get_drill(self, drill_id: str) -> dict[str, Any]:
        if (
            not isinstance(drill_id, str)
            or not drill_id.startswith("drill_")
            or not 6 < len(drill_id) <= 44
        ):
            raise ValueError("Invalid sandbox drill id")
        path = self.drills_root / f"{drill_id}.json"
        if path.is_symlink() or not path.is_file():
            raise KeyError(drill_id)
        return _read_drill(path)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _assert_isolation(self) -> None:
        protected = {}
        for name in (
            "broker_orders_file",
            "broker_fills_file",
            "broker_ledger_scope_file",
            "broker_reconciliation_file",
        ):
            value = getattr(self.config, name, None)
            if value is not None:
                protected[name] = Path(value).resolve()
        sandbox_paths = {
            self.orders_path.resolve(),
            self.fills_path.resolve(),
            self.scope_path.resolve(),
        }
        for name, value in protected.items():
            if value in sandbox_paths:
                raise RuntimeError(
                    "Sandbox ledgers must not alias the live broker path "
                    f"{name}: {value}"
                )

    def _protected_digests(self) -> dict[str, str | None]:
        digests: dict[str, str | None] = {}
        for name in (
            "broker_orders_file",
            "broker_fills_file",
            "broker_ledger_scope_file",
            "broker_reconciliation_file",
        ):
            value = getattr(self.config, name, None)
            if value is None:
                continue
            path = Path(value)
            digests[name] = (
                hashlib.sha256(path.read_bytes()).hexdigest()
                if path.is_file()
                else None
            )
        return digests

    def _protected_attestation(
        self,
        before: dict[str, str | None],
        after: dict[str, str | None],
    ) -> list[dict[str, Any]]:
        attestation = []
        for name in sorted(before):
            unchanged = before[name] == after.get(name)
            if not unchanged:
                raise RuntimeError(
                    "Sandbox drill must never modify promotion-countable "
                    f"broker evidence, but {name} changed during the cycle"
                )
            attestation.append(
                {
                    "name": name,
                    "digest": before[name],
                    "unchanged": True,
                }
            )
        return attestation

    def _scope(self) -> BrokerLedgerScope:
        return create_broker_ledger_scope(
            adapter=SANDBOX_ADAPTER_NAME,
            account_id=SANDBOX_ACCOUNT_ID,
            environment=BrokerEnvironment.SANDBOX,
            config_fingerprint=self._config_fingerprint(),
            orders_path=self.orders_path,
            fills_path=self.fills_path,
        )

    def _config_fingerprint(self) -> str:
        costs = self.config.costs
        return _fingerprint(
            {
                "schema_version": SANDBOX_SCHEMA_VERSION,
                "engine_version": SANDBOX_ENGINE_VERSION,
                "adapter": SANDBOX_ADAPTER_NAME,
                "universe": sorted(
                    item.symbol for item in self.config.instruments
                ),
                "costs": {
                    "commission_bps": costs.commission_bps,
                    "minimum_commission": costs.minimum_commission,
                    "sell_stamp_duty_bps": costs.sell_stamp_duty_bps,
                    "transfer_fee_bps": costs.transfer_fee_bps,
                },
                "broker_limits": {
                    "max_order_notional": self._max_order_notional(),
                    "max_daily_notional": self._max_daily_notional(),
                },
            }
        )

    def _max_order_notional(self) -> float:
        return float(
            self.config.raw.get("broker", {}).get(
                "max_order_notional", DEFAULT_BROKER_MAX_ORDER_NOTIONAL
            )
        )

    def _max_daily_notional(self) -> float:
        return float(
            self.config.raw.get("broker", {}).get(
                "max_daily_notional", DEFAULT_BROKER_MAX_DAILY_NOTIONAL
            )
        )

    def _mandate(
        self, instrument: Instrument, side: OrderSide
    ) -> BrokerMandate:
        # require_batch_approval=False is deliberate and disclosed: the
        # one-time approval file is a live-only human authority gate, and a
        # sandbox drill must not create or consume live approval artifacts.
        return BrokerMandate(
            allowed_symbols=frozenset({instrument.symbol}),
            allowed_sides=frozenset({side}),
            max_order_notional=self._max_order_notional(),
            max_daily_notional=self._max_daily_notional(),
            max_orders_per_day=20,
            require_batch_approval=False,
        )

    def _instrument(self, symbol: str) -> Instrument:
        for item in self.config.instruments:
            if item.symbol == symbol:
                return item
        raise ValueError(f"Symbol is outside the configured universe: {symbol}")

    def _side(self, side: str) -> OrderSide:
        try:
            return OrderSide(str(side).upper())
        except ValueError as exc:
            raise ValueError("Sandbox side must be BUY or SELL") from exc

    def _session_bar(
        self, instrument: Instrument, session: date | None
    ) -> Bar:
        path = self.config.cache_dir / f"{instrument.symbol}.csv"
        if not path.is_file():
            raise RuntimeError(
                f"No cached bars for {instrument.symbol}; run refresh-data first"
            )
        bars = load_cached_bars(path)
        if not bars:
            raise RuntimeError(f"Cached bars for {instrument.symbol} are empty")
        if session is None:
            return bars[-1]
        for bar in reversed(bars):
            if bar.date == session:
                return bar
        raise ValueError(
            f"Session {session} is not present in the {instrument.symbol} cache"
        )

    def _order_request(
        self,
        instrument: Instrument,
        side: OrderSide,
        quantity: int | None,
        bar: Bar,
        limit_price: float | None,
    ) -> tuple[BrokerOrderRequest, dict[str, int]]:
        lot = instrument.lot_size
        resolved_quantity = lot if quantity is None else quantity
        if (
            isinstance(resolved_quantity, bool)
            or not isinstance(resolved_quantity, int)
            or resolved_quantity <= 0
            or resolved_quantity % lot != 0
        ):
            raise ValueError(
                f"Sandbox quantity must be a positive multiple of lot size {lot}"
            )
        resolved_price = bar.open if limit_price is None else float(limit_price)
        if not resolved_price > 0:
            raise ValueError("Sandbox limit price must be positive")
        resolved_price = _tick_aligned(resolved_price, instrument.tick_size)
        request = BrokerOrderRequest(
            client_order_id=f"sbx_{bar.date.strftime('%Y%m%d')}_{uuid4().hex[:12]}",
            symbol=instrument.symbol,
            side=side,
            quantity=resolved_quantity,
            limit_price=resolved_price,
        )
        holdings = (
            {instrument.symbol: resolved_quantity}
            if side is OrderSide.SELL
            else {}
        )
        return request, holdings

    def _assert_resources(
        self, broker: SandboxBroker, request: BrokerOrderRequest
    ) -> None:
        account = broker.account()
        notional = request.quantity * request.limit_price
        if request.side is OrderSide.BUY:
            if notional > account.available_cash:
                raise ValueError(
                    "Sandbox buy notional exceeds the virtual account cash"
                )
            return
        available = sum(
            position.available_quantity
            for position in broker.positions()
            if position.symbol == request.symbol
        )
        if request.quantity > available:
            raise ValueError(
                "Sandbox sell quantity exceeds the virtual position"
            )

    def _records(self) -> list[dict[str, Any]]:
        if not self.drills_root.is_dir():
            return []
        records = []
        for path in sorted(self.drills_root.glob("drill_*.json")):
            if path.is_symlink():
                raise RuntimeError(
                    f"Sandbox drill record must not be symbolic: {path.name}"
                )
            records.append(_read_drill(path))
        return records

    def _publish(self, record: dict[str, Any]) -> None:
        target = self.drills_root / f"{record['drill_id']}.json"
        with evidence_store_lock(self.drills_root, "Sandbox drill"):
            if len(self._records()) >= MAX_DRILLS:
                raise RuntimeError(
                    f"Sandbox drill capacity reached ({MAX_DRILLS}); "
                    "archive old drills before running more"
                )
            atomic_create_json(
                self.drills_root,
                target,
                record,
                label="sandbox drill",
                maximum_bytes=MAX_DRILL_RECORD_BYTES,
            )


def _read_drill(path: Path) -> dict[str, Any]:
    value = load_unique_json(path, max_bytes=MAX_DRILL_RECORD_BYTES)
    if not isinstance(value, dict) or set(value) != _DRILL_FIELDS:
        raise RuntimeError(
            f"Invalid sandbox drill record schema: {path.name}"
        )
    expected = _record_fingerprint(
        {key: value[key] for key in value if key != "record_fingerprint"}
    )
    if value["record_fingerprint"] != expected:
        raise RuntimeError(
            f"Invalid sandbox drill record fingerprint: {path.name}"
        )
    if value["drill_id"] != path.stem:
        raise RuntimeError(
            f"Sandbox drill id does not match its file name: {path.name}"
        )
    if value.get("safety") != SANDBOX_SAFETY:
        raise RuntimeError(
            f"Sandbox drill safety contract is invalid: {path.name}"
        )
    return value


def _broker_order_id(client_order_id: str) -> str:
    digest = hashlib.sha256(client_order_id.encode("utf-8")).hexdigest()
    return f"sbxo_{digest[:16]}"


def _tick_aligned(value: float, tick_size: float) -> float:
    ticks = round(value / tick_size)
    aligned = round(ticks * tick_size, 6)
    if aligned <= 0:
        raise ValueError("Sandbox limit price must stay positive after tick alignment")
    return aligned


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _record_fingerprint(record: dict[str, Any]) -> str:
    return _fingerprint(
        {key: record[key] for key in record if key != "record_fingerprint"}
    )


def _utc_now() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )
