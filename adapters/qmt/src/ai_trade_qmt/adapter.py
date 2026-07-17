from __future__ import annotations

import hashlib
import importlib
import math
import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from types import ModuleType
from typing import Any

from ai_trade.broker.base import (
    Broker,
    BrokerAccount,
    BrokerEnvironment,
    BrokerFill,
    BrokerHealth,
    BrokerOrderRequest,
    BrokerOrderSnapshot,
    BrokerPosition,
    OrderSide,
    OrderStatus,
)


ADAPTER_NAME = "qmt-readonly"
_OPEN_ORDER_STATUSES = {
    OrderStatus.PENDING_SUBMIT,
    OrderStatus.SUBMITTED,
    OrderStatus.PARTIALLY_FILLED,
    OrderStatus.CANCEL_PENDING,
}
_ACCOUNT_ID_MAX_LENGTH = 128


@dataclass(frozen=True)
class QMTSettings:
    userdata_path: Path
    account_id: str
    session_id: int
    python_path: Path | None = None
    account_type: str = "STOCK"

    @classmethod
    def from_config(cls, config: Any) -> "QMTSettings":
        broker = config.raw.get("broker", {})
        if broker.get("mode") != BrokerEnvironment.SANDBOX.value:
            raise RuntimeError("The QMT read-only adapter requires broker.mode='sandbox'")
        if broker.get("adapter") != ADAPTER_NAME:
            raise RuntimeError(f"The configured broker adapter must be {ADAPTER_NAME!r}")

        account_id = str(broker.get("account_id") or "")
        _validate_account_id(account_id)

        raw_userdata = os.environ.get("AI_TRADE_QMT_USERDATA_PATH", "").strip()
        if not raw_userdata:
            raise RuntimeError("AI_TRADE_QMT_USERDATA_PATH is required")
        userdata_path = Path(raw_userdata).expanduser().resolve()
        if not userdata_path.is_dir():
            raise RuntimeError("AI_TRADE_QMT_USERDATA_PATH must be an existing directory")

        raw_python_path = os.environ.get("AI_TRADE_QMT_PYTHON_PATH", "").strip()
        python_path = Path(raw_python_path).expanduser().resolve() if raw_python_path else None
        if python_path is not None and not python_path.is_dir():
            raise RuntimeError("AI_TRADE_QMT_PYTHON_PATH must be an existing directory")
        if python_path is not None and not (python_path / "xtquant").is_dir():
            raise RuntimeError(
                "AI_TRADE_QMT_PYTHON_PATH must directly contain the xtquant package"
            )

        account_type = os.environ.get("AI_TRADE_QMT_ACCOUNT_TYPE", "STOCK").strip().upper()
        if account_type != "STOCK":
            raise RuntimeError("The first QMT adapter version supports STOCK accounts only")

        raw_session = os.environ.get("AI_TRADE_QMT_SESSION_ID", "").strip()
        if raw_session:
            try:
                session_id = int(raw_session)
            except ValueError as exc:
                raise RuntimeError("AI_TRADE_QMT_SESSION_ID must be an integer") from exc
            if not 1 <= session_id <= 2_147_483_647:
                raise RuntimeError("AI_TRADE_QMT_SESSION_ID must be between 1 and 2147483647")
        else:
            seed = f"{userdata_path}|{account_id}|{os.getpid()}".encode("utf-8")
            session_id = int.from_bytes(hashlib.sha256(seed).digest()[:4], "big")
            session_id = session_id % 2_147_483_647 or 1

        return cls(
            userdata_path=userdata_path,
            account_id=account_id,
            session_id=session_id,
            python_path=python_path,
            account_type=account_type,
        )


@dataclass(frozen=True)
class _QMTBindings:
    trader_class: type
    account_class: type
    constants: ModuleType | Any


class QMTReadOnlyBroker(Broker):
    adapter_name = ADAPTER_NAME
    environment = BrokerEnvironment.SANDBOX
    read_only = True
    runtime_environment_verified = False
    qualifying_reconciliation_supported = False
    fill_commission_complete = False
    fill_tax_complete = False

    def __init__(
        self,
        settings: QMTSettings,
        *,
        bindings: _QMTBindings | None = None,
    ) -> None:
        self.settings = settings
        self._lock = RLock()
        self._closed = False
        self._bindings = bindings or _load_bindings(settings.python_path)
        self._account = self._bindings.account_class(
            settings.account_id, settings.account_type
        )
        self._trader = self._bindings.trader_class(
            str(settings.userdata_path), settings.session_id
        )
        try:
            self._validate_read_surface()
            self._trader.start()
            if self._trader.connect() != 0:
                raise RuntimeError("QMT connection failed; confirm the local client is logged in")
            if self._trader.subscribe(self._account) != 0:
                raise RuntimeError("QMT account subscription failed")
        except Exception:
            self.close()
            raise

    def health(self) -> BrokerHealth:
        checked_at = datetime.now(timezone.utc)
        try:
            self._query_asset()
        except Exception:
            return BrokerHealth(
                connected=False,
                trading_session=False,
                message=(
                    "QMT account query failed; verify the local QMT session. "
                    "Order submission remains disabled."
                ),
                checked_at=checked_at,
            )
        return BrokerHealth(
            connected=True,
            trading_session=False,
            message="QMT connected in read-only observation mode; order submission is disabled",
            checked_at=checked_at,
        )

    def account(self) -> BrokerAccount:
        raw = self._query_asset()
        cash = _finite_float(raw.cash, "asset.cash", minimum=0.0)
        equity = _finite_float(raw.total_asset, "asset.total_asset", minimum=0.0)
        return BrokerAccount(
            account_id=self.settings.account_id,
            currency="CNY",
            cash=cash,
            available_cash=cash,
            equity=equity,
        )

    def positions(self) -> list[BrokerPosition]:
        rows = self._query_list("query_stock_positions")
        positions: list[BrokerPosition] = []
        for raw in rows:
            self._assert_row_account(raw)
            quantity = _integer_quantity(raw.volume, "position.volume")
            available = _integer_quantity(
                raw.can_use_volume, "position.can_use_volume"
            )
            if available > quantity:
                raise RuntimeError("QMT reported available position above total position")
            average_cost = _finite_float(
                getattr(raw, "avg_price", getattr(raw, "open_price", 0.0)),
                "position.average_cost",
                minimum=0.0,
            )
            positions.append(
                BrokerPosition(
                    symbol=_normalize_symbol(raw.stock_code),
                    quantity=quantity,
                    available_quantity=available,
                    average_cost=average_cost,
                    market_value=_finite_float(
                        raw.market_value, "position.market_value", minimum=0.0
                    ),
                )
            )
        return positions

    def open_orders(self) -> list[BrokerOrderSnapshot]:
        with self._lock:
            self._ensure_open()
            rows = self._trader.query_stock_orders(self._account, True)
        rows = _require_list(rows, "query_stock_orders")
        orders: list[BrokerOrderSnapshot] = []
        for raw in rows:
            self._assert_row_account(raw)
            status = self._order_status(raw.order_status)
            if status not in _OPEN_ORDER_STATUSES:
                continue
            quantity = _integer_quantity(raw.order_volume, "order.order_volume", positive=True)
            filled = _integer_quantity(raw.traded_volume, "order.traded_volume")
            if filled > quantity:
                raise RuntimeError("QMT reported filled quantity above order quantity")
            average_fill = _finite_float(
                raw.traded_price, "order.traded_price", minimum=0.0
            )
            broker_order_id = str(raw.order_id)
            orders.append(
                BrokerOrderSnapshot(
                    client_order_id=_external_client_order_id(
                        self.settings.account_id,
                        broker_order_id,
                        getattr(raw, "order_remark", ""),
                    ),
                    broker_order_id=broker_order_id,
                    symbol=_normalize_symbol(raw.stock_code),
                    side=self._order_side(raw.order_type),
                    quantity=quantity,
                    filled_quantity=filled,
                    limit_price=_finite_float(
                        raw.price, "order.price", minimum=0.0
                    ),
                    average_fill_price=average_fill if average_fill > 0 else None,
                    status=status,
                    updated_at=_qmt_timestamp(raw.order_time, "order.order_time"),
                    message=_order_message(raw, status),
                )
            )
        return orders

    def fills(self, since: datetime | None = None) -> list[BrokerFill]:
        if since is not None and since.tzinfo is None:
            raise ValueError("since must include a timezone")
        rows = self._query_list("query_stock_trades")
        fills: list[BrokerFill] = []
        for raw in rows:
            self._assert_row_account(raw)
            filled_at = _qmt_timestamp(raw.traded_time, "trade.traded_time")
            if since is not None and filled_at < since.astimezone(timezone.utc):
                continue
            broker_order_id = str(raw.order_id)
            quantity = _integer_quantity(
                raw.traded_volume, "trade.traded_volume", positive=True
            )
            price = _finite_float(raw.traded_price, "trade.traded_price", minimum=0.0)
            if price <= 0:
                raise RuntimeError("QMT reported a non-positive trade price")
            commission = _finite_float(
                getattr(raw, "commission", 0.0),
                "trade.commission",
                minimum=0.0,
            )
            fill_id = _stable_fill_id(self.settings.account_id, raw)
            fills.append(
                BrokerFill(
                    fill_id=fill_id,
                    broker_order_id=broker_order_id,
                    client_order_id=_external_client_order_id(
                        self.settings.account_id,
                        broker_order_id,
                        getattr(raw, "order_remark", ""),
                    ),
                    symbol=_normalize_symbol(raw.stock_code),
                    side=self._order_side(raw.order_type),
                    quantity=quantity,
                    price=price,
                    commission=commission,
                    tax=0.0,
                    filled_at=filled_at,
                )
            )
        return fills

    def submit_orders(
        self, orders: list[BrokerOrderRequest]
    ) -> list[BrokerOrderSnapshot]:
        del orders
        raise PermissionError(
            "The QMT adapter is read-only; order submission is not implemented"
        )

    def cancel_order(self, broker_order_id: str) -> BrokerOrderSnapshot:
        del broker_order_id
        raise PermissionError(
            "The QMT adapter is read-only; order cancellation is not implemented"
        )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            stop = getattr(self._trader, "stop", None)
            if callable(stop):
                try:
                    stop()
                except Exception:
                    pass

    def _query_asset(self) -> Any:
        with self._lock:
            self._ensure_open()
            raw = self._trader.query_stock_asset(self._account)
        if raw is None:
            raise RuntimeError("QMT returned no account asset data")
        self._assert_row_account(raw)
        return raw

    def _query_list(self, method_name: str) -> list[Any]:
        with self._lock:
            self._ensure_open()
            rows = getattr(self._trader, method_name)(self._account)
        return _require_list(rows, method_name)

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("QMT adapter is closed")

    def _assert_row_account(self, raw: Any) -> None:
        row_account = str(getattr(raw, "account_id", ""))
        if row_account != self.settings.account_id:
            raise RuntimeError("QMT returned data for a different account")

    def _validate_read_surface(self) -> None:
        required = (
            "start",
            "connect",
            "subscribe",
            "query_stock_asset",
            "query_stock_positions",
            "query_stock_orders",
            "query_stock_trades",
        )
        missing = [name for name in required if not callable(getattr(self._trader, name, None))]
        if missing:
            raise RuntimeError(
                "The installed xtquant build lacks required read methods: "
                + ", ".join(missing)
            )

    def _order_side(self, value: Any) -> OrderSide:
        if value == self._bindings.constants.STOCK_BUY:
            return OrderSide.BUY
        if value == self._bindings.constants.STOCK_SELL:
            return OrderSide.SELL
        raise RuntimeError("QMT returned an unsupported order type")

    def _order_status(self, value: Any) -> OrderStatus:
        constants = self._bindings.constants
        mapping = {
            constants.ORDER_UNREPORTED: OrderStatus.PENDING_SUBMIT,
            constants.ORDER_WAIT_REPORTING: OrderStatus.PENDING_SUBMIT,
            constants.ORDER_REPORTED: OrderStatus.SUBMITTED,
            constants.ORDER_REPORTED_CANCEL: OrderStatus.CANCEL_PENDING,
            constants.ORDER_PARTSUCC_CANCEL: OrderStatus.CANCEL_PENDING,
            constants.ORDER_PART_CANCEL: OrderStatus.CANCELLED,
            constants.ORDER_CANCELED: OrderStatus.CANCELLED,
            constants.ORDER_PART_SUCC: OrderStatus.PARTIALLY_FILLED,
            constants.ORDER_SUCCEEDED: OrderStatus.FILLED,
            constants.ORDER_JUNK: OrderStatus.REJECTED,
            constants.ORDER_UNKNOWN: OrderStatus.PENDING_SUBMIT,
        }
        return mapping.get(value, OrderStatus.PENDING_SUBMIT)


def create_broker(config: Any, environment: BrokerEnvironment) -> Broker:
    if environment != BrokerEnvironment.SANDBOX:
        raise PermissionError("The QMT read-only adapter cannot be created in live mode")
    return QMTReadOnlyBroker(QMTSettings.from_config(config))


def _load_bindings(python_path: Path | None) -> _QMTBindings:
    if python_path is not None:
        value = str(python_path)
        if value not in sys.path:
            sys.path.insert(0, value)
    try:
        trader_module = importlib.import_module("xtquant.xttrader")
        type_module = importlib.import_module("xtquant.xttype")
        constants = importlib.import_module("xtquant.xtconstant")
        trader_class = trader_module.XtQuantTrader
        account_class = type_module.StockAccount
    except (AttributeError, ImportError) as exc:
        raise RuntimeError(
            "xtquant could not be imported. Use the package supplied by your QMT "
            "broker installation and set AI_TRADE_QMT_PYTHON_PATH when needed."
        ) from exc
    if python_path is not None:
        loaded_from = Path(str(getattr(trader_module, "__file__", ""))).resolve()
        if not _is_relative_to(loaded_from, python_path):
            raise RuntimeError(
                "xtquant was imported from outside AI_TRADE_QMT_PYTHON_PATH; "
                "restart Python and verify the configured package directory"
            )
    return _QMTBindings(trader_class, account_class, constants)


def _validate_account_id(value: str) -> None:
    if (
        not value
        or value != value.strip()
        or len(value) > _ACCOUNT_ID_MAX_LENGTH
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise RuntimeError("broker.account_id is invalid for the QMT adapter")


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _require_list(value: Any, operation: str) -> list[Any]:
    if not isinstance(value, (list, tuple)):
        raise RuntimeError(f"QMT {operation} returned an invalid collection")
    return list(value)


def _integer_quantity(value: Any, name: str, *, positive: bool = False) -> int:
    if isinstance(value, bool):
        raise RuntimeError(f"QMT {name} is not an integer quantity")
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"QMT {name} is not numeric") from exc
    if not math.isfinite(numeric) or numeric != math.trunc(numeric):
        raise RuntimeError(f"QMT {name} is not an integer quantity")
    result = int(numeric)
    if result < 0 or (positive and result == 0):
        raise RuntimeError(f"QMT {name} has an invalid quantity")
    return result


def _finite_float(value: Any, name: str, *, minimum: float) -> float:
    if isinstance(value, bool):
        raise RuntimeError(f"QMT {name} is not numeric")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"QMT {name} is not numeric") from exc
    if not math.isfinite(result) or result < minimum:
        raise RuntimeError(f"QMT {name} is outside the supported range")
    return result


def _normalize_symbol(value: Any) -> str:
    symbol = str(value or "").strip().upper()
    match = re.fullmatch(r"(\d{6})\.(SH|SZ)", symbol)
    if match:
        return match.group(1)
    match = re.fullmatch(r"(SH|SZ)\.?([0-9]{6})", symbol)
    if match:
        return match.group(2)
    if not symbol or any(character.isspace() for character in symbol):
        raise RuntimeError("QMT returned an invalid security code")
    return symbol


def _qmt_timestamp(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise RuntimeError(f"QMT {name} lacks timezone information")
        return value.astimezone(timezone.utc)
    try:
        timestamp = float(value)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"QMT {name} is not a Unix timestamp") from exc
    if not math.isfinite(timestamp) or timestamp <= 0:
        raise RuntimeError(f"QMT {name} is not a positive Unix timestamp")
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise RuntimeError(f"QMT {name} is outside the supported timestamp range") from exc


def _external_client_order_id(account_id: str, order_id: str, remark: Any) -> str:
    remark_value = str(remark or "").strip()
    match = re.fullmatch(r"ai-trade:([A-Za-z0-9._-]{1,64})", remark_value)
    if match:
        return match.group(1)
    digest = hashlib.sha256(f"{account_id}|{order_id}".encode("utf-8")).hexdigest()
    return f"qmt-external-{digest[:20]}"


def _stable_fill_id(account_id: str, raw: Any) -> str:
    parts = (
        account_id,
        str(getattr(raw, "traded_id", "")),
        str(getattr(raw, "order_id", "")),
        str(getattr(raw, "stock_code", "")),
        str(getattr(raw, "traded_time", "")),
        str(getattr(raw, "traded_volume", "")),
        str(getattr(raw, "traded_price", "")),
    )
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"qmt-{digest[:24]}"


def _order_message(raw: Any, status: OrderStatus) -> str:
    message = str(getattr(raw, "status_msg", "") or "").strip()
    raw_status = getattr(raw, "order_status", None)
    if status == OrderStatus.PENDING_SUBMIT and raw_status not in {
        48,
        49,
    }:
        suffix = f"Unrecognized or unknown QMT status {raw_status}; kept open"
        return f"{message}; {suffix}" if message else suffix
    return message
