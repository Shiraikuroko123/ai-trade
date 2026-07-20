"""Uniform market-data provider boundary.

The download orchestration records one manifest regardless of which network
provider supplied a file.  Provider implementations intentionally stay thin:
the existing Eastmoney and Tencent parsers remain the source of truth while
this module gives configuration and future adapters one stable contract.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any, Protocol

from ..models import Instrument

if TYPE_CHECKING:
    from ..config import AppConfig


class ProviderConfigurationError(ValueError):
    """Raised when a configured provider is not registered."""


@dataclass(frozen=True)
class ProviderDescriptor:
    """Public capabilities used by diagnostics and future UI surfaces."""

    key: str
    display_name: str
    implementation: str
    daily_bars: bool = True
    intraday_bars: bool = False
    quotes: bool = False
    status: str = "implemented"
    snapshot_eligible: bool = True
    cross_check_fields: tuple[str, ...] = (
        "open",
        "high",
        "low",
        "close",
        "volume",
        "amount",
    )
    supported_adjustments: tuple[str, ...] = ("none", "forward", "backward")


class MarketDataProvider(Protocol):
    """Normalized per-instrument download contract."""

    descriptor: ProviderDescriptor
    primary_source_label: str
    fallback_source_label: str

    def download(
        self,
        config: AppConfig,
        instrument: Instrument,
        output_path: Path,
        *,
        cache_path: Path | None,
        cutoff: date,
        proxy_mode: str,
        network_errors: list[str],
        provider_metadata: dict[str, object],
    ) -> Path:
        ...

    def is_transport_failure(self, error: Exception) -> bool:
        ...


class _EastmoneyProvider:
    descriptor = ProviderDescriptor(
        key="eastmoney",
        display_name="Eastmoney",
        implementation="eastmoney.daily_kline",
        daily_bars=True,
        intraday_bars=True,
        quotes=True,
    )
    primary_source_label = "network"
    fallback_source_label = "eastmoney_network_fallback"

    def download(
        self,
        config: AppConfig,
        instrument: Instrument,
        output_path: Path,
        *,
        cache_path: Path | None,
        cutoff: date,
        proxy_mode: str,
        network_errors: list[str],
        provider_metadata: dict[str, object],
    ) -> Path:
        # Import lazily so eastmoney.py can keep its compatibility exports.
        from .eastmoney import download_instrument

        return download_instrument(
            config,
            instrument,
            True,
            output_path,
            network_errors=network_errors,
            cutoff=cutoff,
            proxy_mode=proxy_mode,
        )

    def is_transport_failure(self, error: Exception) -> bool:
        from .eastmoney import _is_transport_failure

        return _is_transport_failure(error)


class _TencentProvider:
    descriptor = ProviderDescriptor(
        key="tencent",
        display_name="Tencent Finance",
        implementation="tencent.newfqkline.daily_kline",
        daily_bars=True,
        intraday_bars=False,
        quotes=True,
    )
    primary_source_label = "network"
    fallback_source_label = "tencent_network_fallback"

    def download(
        self,
        config: AppConfig,
        instrument: Instrument,
        output_path: Path,
        *,
        cache_path: Path | None,
        cutoff: date,
        proxy_mode: str,
        network_errors: list[str],
        provider_metadata: dict[str, object],
    ) -> Path:
        from .tencent import download_instrument

        try:
            return download_instrument(
                config,
                instrument,
                output_path,
                cache_path=cache_path,
                cutoff=cutoff,
                proxy_mode=proxy_mode,
                provider_metadata=provider_metadata,
            )
        except Exception as exc:
            # Tencent's parser intentionally owns its retry loop and does not
            # mutate the shared error list.  Keep the manifest contract uniform.
            network_errors.append(f"{type(exc).__name__}: {exc}")
            raise

    def is_transport_failure(self, error: Exception) -> bool:
        # Tencent currently wraps transport failures in RuntimeError after its
        # own retries.  Do not open a global circuit for it until the provider
        # exposes a structured attempt error; a malformed payload must not be
        # confused with a network outage.
        return False


class _YahooProvider:
    descriptor = ProviderDescriptor(
        key="yahoo",
        display_name="Yahoo Finance",
        implementation="yahoo.chart.daily_reference",
        daily_bars=True,
        intraday_bars=False,
        quotes=False,
        status="implemented_reference_only",
        snapshot_eligible=False,
        cross_check_fields=("open", "high", "low", "close", "volume"),
        supported_adjustments=("none", "forward"),
    )
    # These labels satisfy the common protocol, but configuration validation
    # prevents a reference-only provider from entering the snapshot chain.
    primary_source_label = "network"
    fallback_source_label = "yahoo_network_fallback"

    def download(
        self,
        config: AppConfig,
        instrument: Instrument,
        output_path: Path,
        *,
        cache_path: Path | None,
        cutoff: date,
        proxy_mode: str,
        network_errors: list[str],
        provider_metadata: dict[str, object],
    ) -> Path:
        from .yahoo import download_instrument

        try:
            return download_instrument(
                config,
                instrument,
                output_path,
                cutoff=cutoff,
                proxy_mode=proxy_mode,
                provider_metadata=provider_metadata,
            )
        except Exception as exc:
            network_errors.append(f"{type(exc).__name__}: {exc}")
            raise

    def is_transport_failure(self, error: Exception) -> bool:
        from .yahoo import is_transport_failure

        return is_transport_failure(error)


_PROVIDERS: dict[str, MarketDataProvider] = {
    "eastmoney": _EastmoneyProvider(),
    "tencent": _TencentProvider(),
    "yahoo": _YahooProvider(),
}


def provider_for(name: object) -> MarketDataProvider:
    """Return the registered provider for a normalized configuration value."""

    if not isinstance(name, str):
        raise ProviderConfigurationError("data provider name must be text")
    key = name.strip().lower()
    try:
        return _PROVIDERS[key]
    except KeyError as exc:
        supported = ", ".join(sorted(_PROVIDERS))
        raise ProviderConfigurationError(
            f"Unsupported data provider {name!r}; registered providers: {supported}"
        ) from exc


def registered_provider_names() -> tuple[str, ...]:
    """Return stable provider keys for diagnostics and configuration tooling."""

    return tuple(sorted(_PROVIDERS))


def snapshot_provider_names() -> tuple[str, ...]:
    """Return providers allowed to supply strategy-visible snapshot files."""

    return tuple(
        key for key in sorted(_PROVIDERS) if _PROVIDERS[key].descriptor.snapshot_eligible
    )


def provider_catalog() -> list[dict[str, Any]]:
    """Return non-secret capability metadata suitable for a status response."""

    return [
        {
            "key": descriptor.key,
            "display_name": descriptor.display_name,
            "implementation": descriptor.implementation,
            "daily_bars": descriptor.daily_bars,
            "intraday_bars": descriptor.intraday_bars,
            "quotes": descriptor.quotes,
            "status": descriptor.status,
            "snapshot_eligible": descriptor.snapshot_eligible,
            "cross_check_fields": list(descriptor.cross_check_fields),
            "supported_adjustments": list(descriptor.supported_adjustments),
        }
        for descriptor in (
            _PROVIDERS[key].descriptor for key in sorted(_PROVIDERS)
        )
    ]


__all__ = [
    "MarketDataProvider",
    "ProviderConfigurationError",
    "ProviderDescriptor",
    "provider_catalog",
    "provider_for",
    "registered_provider_names",
    "snapshot_provider_names",
]
