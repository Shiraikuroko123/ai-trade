from __future__ import annotations

from .config import AppConfig
from .data.market import MarketData


def diagnose(config: AppConfig, market: MarketData) -> dict[str, object]:
    coverage = {}
    latest_dates = []
    active_symbols = set(market.active_symbols(market.latest_date()))
    active_symbols.add(config.strategy.benchmark)
    for symbol, item in market.symbols.items():
        if symbol in active_symbols:
            latest_dates.append(item.bars[-1].date)
        coverage[symbol] = {
            "name": item.instrument.name,
            "active": symbol in active_symbols,
            "rows": len(item.bars),
            "first": item.bars[0].date.isoformat(),
            "last": item.bars[-1].date.isoformat(),
            "sha256": market.file_hashes[symbol],
            "excluded_incomplete_dates": [
                value.isoformat() for value in market.excluded_dates[symbol]
            ],
        }
    aligned = len(set(latest_dates)) == 1
    latest_market_date = market.latest_date()
    latest_common_date = min(latest_dates) if latest_dates else latest_market_date
    market_data_current = latest_common_date >= market.completed_through
    market_data_lag_days = max(0, (market.completed_through - latest_common_date).days)
    research_warnings = []
    if config.security_master.metadata.get("selection_method") == "curated_static":
        research_warnings.append(
            "Default universe is curated_static and does not remove survivorship bias"
        )
    if config.raw["data"].get("adjustment") != "none":
        research_warnings.append(
            "Adjusted bars are still used for simulated execution; raw prices and corporate "
            "actions are not yet separated"
        )
    if not aligned:
        research_warnings.append(
            "Active instruments do not share the same latest market date; refresh the "
            "complete data snapshot before generating signals or reports"
        )
    if not market_data_current:
        research_warnings.append(
            f"Market data ends on {latest_common_date.isoformat()}, before the expected "
            f"completed-session cutoff {market.completed_through.isoformat()}; run the "
            "refresh-data action or verify that the gap is an exchange holiday"
        )

    manifest = market.manifest if isinstance(market.manifest, dict) else None
    source_counts: dict[str, int] = {}
    if manifest:
        files = manifest.get("files", {})
        if isinstance(files, dict):
            for value in files.values():
                source = (
                    str(value.get("source", "unknown"))
                    if isinstance(value, dict)
                    else "unknown"
                )
                source_counts[source] = source_counts.get(source, 0) + 1
        fallback_count = source_counts.get("validated_local_fallback", 0)
        if fallback_count:
            research_warnings.append(
                f"The latest refresh used validated local fallback data for "
                f"{fallback_count} instrument(s); verify provider availability"
            )
    else:
        research_warnings.append(
            "Cache manifest is missing; data snapshot provenance cannot be verified"
        )

    status = (
        "OK" if aligned and market_data_current and manifest is not None else "WARNING"
    )
    return {
        "status": status,
        "config": str(config.path),
        "completed_session_cutoff": market.completed_through.isoformat(),
        "latest_market_date": latest_market_date.isoformat(),
        "latest_common_market_date": latest_common_date.isoformat(),
        "market_data_current": market_data_current,
        "market_data_lag_days": market_data_lag_days,
        "universe_latest_dates_aligned": aligned,
        "point_in_time_universe": {
            "name": config.universe_name,
            "active_count": len(market.active_symbols(market.latest_date())),
            "loaded_instrument_count": len(config.instruments),
            "selection_method": config.security_master.metadata.get("selection_method"),
            "security_master_sha256": config.security_master.fingerprint(),
        },
        "research_warnings": research_warnings,
        "coverage": coverage,
        "cache_manifest": {
            "available": manifest is not None,
            "downloaded_at": manifest.get("downloaded_at") if manifest else None,
            "completed_through": (
                manifest.get("completed_through") if manifest else None
            ),
            "source_counts": source_counts,
        },
        "live_trading": "DISABLED",
    }
