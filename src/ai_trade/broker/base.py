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


class BrokerAccessLevel(str, Enum):
    UNDECLARED = "undeclared"
    READ_ONLY = "read_only"
    SANDBOX = "sandbox"
    LIVE = "live"


class BrokerOperation(str, Enum):
    READ_ACCOUNT = "read_account"
    READ_POSITIONS = "read_positions"
    READ_ORDERS = "read_orders"
    READ_FILLS = "read_fills"
    SUBMIT_ORDERS = "submit_orders"
    CANCEL_ORDERS = "cancel_orders"


@dataclass(frozen=True)
class BrokerCapabilities:
    adapter_name: str
    access_level: BrokerAccessLevel = BrokerAccessLevel.UNDECLARED
    operations: frozenset[BrokerOperation] = frozenset()
    environments: frozenset[BrokerEnvironment] = frozenset()
    runtime_environment_verified: bool = False
    qualifying_reconciliation_supported: bool = False
    requires_local_client: bool = False

    def require(
        self,
        operations: set[BrokerOperation] | frozenset[BrokerOperation],
        environment: BrokerEnvironment,
    ) -> None:
        if environment not in self.environments:
            raise PermissionError(
                f"Broker adapter {self.adapter_name!r} does not declare support for "
                f"the {environment.value} environment"
            )
        missing = sorted(
            (operation.value for operation in operations - self.operations)
        )
        if missing:
            raise PermissionError(
                f"Broker adapter {self.adapter_name!r} does not allow operations: "
                + ", ".join(missing)
            )

    def public_dict(self) -> dict[str, Any]:
        return {
            "adapter": self.adapter_name,
            "access_level": self.access_level.value,
            "operations": sorted(value.value for value in self.operations),
            "environments": sorted(value.value for value in self.environments),
            "runtime_environment_verified": self.runtime_environment_verified,
            "qualifying_reconciliation_supported": (
                self.qualifying_reconciliation_supported
            ),
            "requires_local_client": self.requires_local_client,
        }


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
    capabilities = BrokerCapabilities(adapter_name="")

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
    CAPABILITY_ENTRY_POINT_GROUP = "ai_trade.broker_capabilities"

    @classmethod
    def discover(cls) -> dict[str, metadata.EntryPoint]:
        points = metadata.entry_points()
        if hasattr(points, "select"):
            selected = points.select(group=cls.ENTRY_POINT_GROUP)
        else:  # pragma: no cover - compatibility for older Python 3.10 builds
            selected = points.get(cls.ENTRY_POINT_GROUP, ())
        return _index_entry_points(selected, cls.ENTRY_POINT_GROUP)

    @classmethod
    def available(cls) -> tuple[str, ...]:
        return tuple(sorted(cls.discover()))

    @classmethod
    def discover_capabilities(cls) -> dict[str, metadata.EntryPoint]:
        points = metadata.entry_points()
        if hasattr(points, "select"):
            selected = points.select(group=cls.CAPABILITY_ENTRY_POINT_GROUP)
        else:  # pragma: no cover - compatibility for older Python 3.10 builds
            selected = points.get(cls.CAPABILITY_ENTRY_POINT_GROUP, ())
        return _index_entry_points(selected, cls.CAPABILITY_ENTRY_POINT_GROUP)

    @classmethod
    def capabilities(cls, name: str) -> BrokerCapabilities:
        point = cls.discover_capabilities().get(name)
        if point is None:
            return BrokerCapabilities(adapter_name=name)
        loaded = point.load()
        value = loaded() if callable(loaded) else loaded
        if not isinstance(value, BrokerCapabilities):
            raise TypeError(
                f"Broker capability entry point {name!r} did not return "
                "BrokerCapabilities"
            )
        _validate_capability_declaration(value)
        if value.adapter_name != name:
            raise RuntimeError(
                f"Broker capability entry point {name!r} declared a different adapter"
            )
        return value

    @classmethod
    def descriptions(cls) -> tuple[BrokerCapabilities, ...]:
        return tuple(cls.capabilities(name) for name in cls.available())

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
        declared_capabilities = cls.capabilities(name)
        declared_capabilities.require(frozenset(), environment)
        factory: BrokerFactory = point.load()
        broker = factory(config, environment)
        if not isinstance(broker, Broker):
            raise TypeError(f"Broker entry point {name!r} did not return a Broker")
        if broker.adapter_name != name:
            raise RuntimeError(
                f"Broker entry point {name!r} returned a different adapter identity"
            )
        if (
            not isinstance(broker.environment, BrokerEnvironment)
            or broker.environment is not environment
        ):
            raise RuntimeError(
                f"Broker adapter {name!r} returned a different runtime environment"
            )
        runtime_capabilities = getattr(broker, "capabilities", None)
        if not isinstance(runtime_capabilities, BrokerCapabilities):
            raise TypeError(
                f"Broker adapter {name!r} does not expose BrokerCapabilities"
            )
        if runtime_capabilities != declared_capabilities:
            raise RuntimeError(
                f"Broker adapter {name!r} runtime capabilities do not match its "
                "installed declaration"
            )
        return broker


def _index_entry_points(
    points: Any,
    group: str,
) -> dict[str, metadata.EntryPoint]:
    indexed: dict[str, metadata.EntryPoint] = {}
    for point in points:
        name = getattr(point, "name", None)
        if not isinstance(name, str) or not name:
            raise RuntimeError(f"Broker entry point in {group!r} has an invalid name")
        if name in indexed:
            raise RuntimeError(
                f"Duplicate broker entry point {name!r} in group {group!r}"
            )
        indexed[name] = point
    return indexed


def _validate_capability_declaration(value: BrokerCapabilities) -> None:
    if (
        not isinstance(value.adapter_name, str)
        or not value.adapter_name
        or not isinstance(value.access_level, BrokerAccessLevel)
        or not isinstance(value.operations, frozenset)
        or any(not isinstance(item, BrokerOperation) for item in value.operations)
        or not isinstance(value.environments, frozenset)
        or any(
            not isinstance(item, BrokerEnvironment) for item in value.environments
        )
        or type(value.runtime_environment_verified) is not bool
        or type(value.qualifying_reconciliation_supported) is not bool
        or type(value.requires_local_client) is not bool
    ):
        raise RuntimeError("Broker capability declaration has invalid runtime types")
