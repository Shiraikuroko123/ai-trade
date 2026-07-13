from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class BrokerOrder:
    symbol: str
    side: str
    quantity: int
    limit_price: float | None = None


class Broker(ABC):
    @abstractmethod
    def positions(self) -> dict[str, int]:
        raise NotImplementedError

    @abstractmethod
    def cash(self) -> float:
        raise NotImplementedError

    @abstractmethod
    def submit(self, orders: list[BrokerOrder]) -> list[str]:
        raise NotImplementedError
