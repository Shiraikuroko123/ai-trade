"""Read-only QMT/xtquant adapter for AI Trade."""

from .adapter import (
    QMTReadOnlyBroker,
    QMTSettings,
    broker_capabilities,
    create_broker,
)

__all__ = [
    "QMTReadOnlyBroker",
    "QMTSettings",
    "broker_capabilities",
    "create_broker",
]
__version__ = "0.2.0"
