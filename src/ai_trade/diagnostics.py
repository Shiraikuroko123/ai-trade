from __future__ import annotations

import re

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
    latest_common_date = getattr(
        market,
        "latest_common_session",
        min(latest_dates) if latest_dates else latest_market_date,
    )
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
    refresh_failures: list[dict[str, object]] = []
    provider_degraded = False
    if manifest:
        files = manifest.get("files", {})
        if isinstance(files, dict):
            for symbol, value in files.items():
                source = (
                    str(value.get("source", "unknown"))
                    if isinstance(value, dict)
                    else "unknown"
                )
                source_counts[source] = source_counts.get(source, 0) + 1
                errors = value.get("network_errors", []) if isinstance(value, dict) else []
                if isinstance(errors, list) and errors:
                    recorded_attempts = (
                        value.get("eastmoney_attempts")
                        if isinstance(value, dict)
                        else None
                    )
                    if (
                        isinstance(recorded_attempts, bool)
                        or not isinstance(recorded_attempts, int)
                        or recorded_attempts < 0
                    ):
                        recorded_attempts = sum(
                            re.match(r"^attempt \d+/\d+:", str(item)) is not None
                            for item in errors
                        )
                    error_types = sorted(
                        {
                            parts[1].strip()
                            for item in errors
                            if len(parts := str(item).split(":", 2)) >= 2
                        }
                    )
                    refresh_failures.append(
                        {
                            "symbol": str(symbol),
                            "source": source,
                            "attempts": recorded_attempts,
                            "error_types": error_types,
                            **(
                                {"skipped_reason": value["eastmoney_skip_reason"]}
                                if isinstance(value, dict)
                                and isinstance(
                                    value.get("eastmoney_skip_reason"), str
                                )
                                and value["eastmoney_skip_reason"]
                                else {}
                            ),
                        }
                    )
        fallback_count = source_counts.get("validated_local_fallback", 0)
        tencent_fallback_count = source_counts.get("tencent_network_fallback", 0)
        recovered_count = sum(
            value["source"] == "network" for value in refresh_failures
        )
        provider_degraded = bool(
            fallback_count or tencent_fallback_count or refresh_failures
        )
        if fallback_count:
            research_warnings.append(
                f"The latest refresh used validated local fallback data for "
                f"{fallback_count} instrument(s); verify provider availability"
            )
        if tencent_fallback_count:
            research_warnings.append(
                f"The latest refresh used Tencent network fallback data for "
                f"{tencent_fallback_count} instrument(s) after Eastmoney was unavailable; "
                "market data was refreshed, but primary-provider connectivity remains "
                "degraded"
            )
        if recovered_count:
            research_warnings.append(
                f"The latest refresh recovered from network errors for "
                f"{recovered_count} instrument(s); provider connectivity was unstable"
            )
    else:
        research_warnings.append(
            "Cache manifest is missing; data snapshot provenance cannot be verified"
        )

    status = "OK"
    if not aligned or not market_data_current or manifest is None or provider_degraded:
        status = "WARNING"
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
            "latest_common_session": (
                manifest.get("latest_common_session") if manifest else None
            ),
            "request_policy": manifest.get("request_policy") if manifest else None,
            "source_counts": source_counts,
            "refresh_failures": refresh_failures,
        },
        "live_trading": "DISABLED",
    }
