from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from importlib import metadata
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:
    from ..config import AppConfig


class BrokerEnvironment(str, Enum):
    SANDBOX = "sandbox"
    LIVE = "live"


class OrderSide(str, Enum):
    BUY = "BUY"
    SELL = "SELL"


class OrderStatus(str, Enum):
    PENDING_SUBMIT = "PENDING_SUBMIT"
    SUBMITTED = "SUBMITTED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCEL_PENDING = "CANCEL_PENDING"
    CANCELLED = "CANCELLED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class BrokerHealth:
    connected: bool
    trading_session: bool
    message: str
    checked_at: datetime


@dataclass(frozen=True)
class BrokerAccount:
    account_id: str
    currency: str
    cash: float
    available_cash: float
    equity: float


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    quantity: int
    available_quantity: int
    average_cost: float
    market_value: float


@dataclass(frozen=True)
class BrokerOrderRequest:
    client_order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    limit_price: float
    time_in_force: str = "DAY"
    metadata: dict[str, Any] = field(default_factory=dict)


BrokerOrder = BrokerOrderRequest


@dataclass(frozen=True)
class BrokerOrderSnapshot:
    client_order_id: str
    broker_order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    filled_quantity: int
    limit_price: float
    average_fill_price: float | None
    status: OrderStatus
    updated_at: datetime
    message: str = ""


@dataclass(frozen=True)
class BrokerFill:
    fill_id: str
    broker_order_id: str
    client_order_id: str
    symbol: str
    side: OrderSide
    quantity: int
    price: float
    commission: float
    tax: float
    filled_at: datetime


class Broker(ABC):
    adapter_name: str
    environment: BrokerEnvironment

    @abstractmethod
    def health(self) -> BrokerHealth:
        raise NotImplementedError

    @abstractmethod
    def account(self) -> BrokerAccount:
        raise NotImplementedError

    @abstractmethod
    def positions(self) -> list[BrokerPosition]:
        raise NotImplementedError

    @abstractmethod
    def open_orders(self) -> list[BrokerOrderSnapshot]:
        raise NotImplementedError

    @abstractmethod
    def submit_orders(
        self, orders: list[BrokerOrderRequest]
    ) -> list[BrokerOrderSnapshot]:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> BrokerOrderSnapshot:
        raise NotImplementedError

    @abstractmethod
    def fills(self, since: datetime | None = None) -> list[BrokerFill]:
        raise NotImplementedError


BrokerFactory = Callable[["AppConfig", BrokerEnvironment], Broker]


class BrokerRegistry:
    ENTRY_POINT_GROUP = "ai_trade.brokers"

    @classmethod
    def discover(cls) -> dict[str, metadata.EntryPoint]:
        points = metadata.entry_points()
        if hasattr(points, "select"):
            selected = points.select(group=cls.ENTRY_POINT_GROUP)
        else:  # pragma: no cover - compatibility for older Python 3.10 builds
            selected = points.get(cls.ENTRY_POINT_GROUP, ())
        return {point.name: point for point in selected}

    @classmethod
    def available(cls) -> tuple[str, ...]:
        return tuple(sorted(cls.discover()))

    @classmethod
    def create(
        cls,
        name: str,
        config: "AppConfig",
        environment: BrokerEnvironment,
    ) -> Broker:
        point = cls.discover().get(name)
        if point is None:
            available = ", ".join(cls.available()) or "none"
            raise RuntimeError(
                f"Broker adapter {name!r} is not installed; available adapters: {available}"
            )
        factory: BrokerFactory = point.load()
        broker = factory(config, environment)
        if not isinstance(broker, Broker):
            raise TypeError(f"Broker entry point {name!r} did not return a Broker")
        return broker
