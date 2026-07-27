from __future__ import annotations

from typing import Any, Mapping


def actual_snapshot_provider(metadata: Mapping[str, Any]) -> str:
    """Return the provider(s) that supplied the files bound to a snapshot."""

    configured = str(metadata.get("provider") or "local-cache").strip().lower()
    manifest = metadata.get("manifest")
    if not isinstance(manifest, Mapping):
        return configured
    files = manifest.get("files")
    if not isinstance(files, Mapping) or not files:
        return configured

    providers: set[str] = set()
    for symbol, entry in files.items():
        provider = _file_provider(entry, configured)
        if provider is None:
            raise RuntimeError(
                f"Feature evidence cannot identify the actual provider for {symbol}"
            )
        providers.add(provider)
    if len(providers) == 1:
        return next(iter(providers))
    return "mixed:" + ",".join(sorted(providers))


def _file_provider(entry: object, configured: str) -> str | None:
    if not isinstance(entry, Mapping):
        return None
    for candidate in (
        entry.get("source_provider"),
        entry.get("source"),
        entry.get("cached_seed_source"),
    ):
        value = str(candidate or "").strip().lower()
        for provider in ("eastmoney", "tencent", "yahoo", "tushare", "baostock"):
            if provider in value:
                return provider
        if value == "network":
            return configured
    return None


__all__ = ["actual_snapshot_provider"]
