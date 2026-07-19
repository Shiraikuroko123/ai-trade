"""Independent daily-bar reconciliation for an installed market snapshot.

The primary provider remains the source used by the strategy.  This module is
deliberately read-only with respect to CSV data: it fetches a bounded recent
window from a different registered provider, compares the two observations,
and attaches the result to the active manifest.  A failed comparison never
replaces a bar and never grants trading authority.
"""

from __future__ import annotations

import copy
import json
import math
import tempfile
from contextlib import nullcontext
from dataclasses import replace
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any, Iterable, Mapping
from uuid import uuid4

from ..config import AppConfig
from ..json_utils import load_unique_json
from .cache_snapshot import (
    MAX_MANIFEST_BYTES,
    recover_pending_snapshot,
    replace_manifest,
    snapshot_refresh_lock,
)
from .eastmoney import completed_session_cutoff, load_cached_bars
from .providers import provider_for


SCHEMA_VERSION = 1
DEFAULT_LOOKBACK_SESSIONS = 5
DEFAULT_MINIMUM_OVERLAP_SESSIONS = 3
MAX_LOOKBACK_SESSIONS = 20
MAX_SYMBOLS = 500

# The tolerances are intentionally explicit and conservative.  They account
# for provider rounding/transport differences without turning a materially
# different OHLCV series into a pass.
PRICE_ABSOLUTE_TOLERANCE = 0.02
PRICE_RELATIVE_TOLERANCE = 0.005
VOLUME_RELATIVE_TOLERANCE = 0.10
AMOUNT_RELATIVE_TOLERANCE = 0.15
MAX_BREACHES_PER_SYMBOL = 12
MAX_ERROR_LENGTH = 320


def cross_check_market_snapshot(
    config: AppConfig,
    *,
    symbols: Iterable[str] | None = None,
    force: bool = False,
    lock_held: bool = False,
) -> dict[str, Any]:
    """Run and persist a bounded independent-provider audit.

    ``force`` is used by the explicit CLI/web job.  Normal refreshes only run
    when ``data.cross_check.enabled`` is true, which lets offline installations
    keep the existing primary/fallback behavior without unexpected network
    calls.
    """

    cross_config = config.raw.get("data", {}).get("cross_check", {})
    if not isinstance(cross_config, dict):
        cross_config = {}
    if not force and not bool(cross_config.get("enabled", False)):
        return {
            "schema_version": SCHEMA_VERSION,
            "status": "not_run",
            "confidence": "not_available",
            "reason": "cross_check_disabled",
            "generated_at": None,
            "persisted": False,
        }

    context = nullcontext() if lock_held else snapshot_refresh_lock(config.cache_dir)
    with context:
        recover_pending_snapshot(config.cache_dir)
        return _run_locked(config, symbols=symbols, cross_config=cross_config)


def cross_source_projection(
    manifest: Mapping[str, Any] | None,
    *,
    file_hashes: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Return a bounded, tamper-aware projection suitable for API responses."""

    if not isinstance(manifest, Mapping):
        return {
            "status": "not_run",
            "confidence": "not_available",
            "valid": False,
            "reason": "manifest_unavailable",
        }
    raw = manifest.get("cross_source_check")
    if not isinstance(raw, Mapping):
        return {
            "status": "not_run",
            "confidence": "not_available",
            "valid": False,
            "reason": "cross_check_not_run",
        }
    result = dict(raw)
    valid, reason = _verify_payload(manifest, raw, file_hashes=file_hashes)
    result["valid"] = valid
    if not valid:
        result["status"] = "invalid"
        result["confidence"] = "not_available"
        result["reason"] = reason
    return _bounded_projection(result)


def cross_check_dataset_digest(manifest: Mapping[str, Any]) -> str | None:
    """Return the digest binding an audit to the installed CSV file entries."""

    files = manifest.get("files")
    if not isinstance(files, Mapping):
        return None
    normalized: dict[str, dict[str, Any]] = {}
    for symbol in sorted(files):
        entry = files[symbol]
        if not isinstance(symbol, str) or not isinstance(entry, Mapping):
            return None
        digest = entry.get("sha256")
        rows = entry.get("rows")
        if not isinstance(digest, str) or not isinstance(rows, int) or isinstance(rows, bool):
            return None
        normalized[symbol] = {"rows": rows, "sha256": digest}
    body = {
        "adjustment": manifest.get("adjustment"),
        "files": normalized,
    }
    return _sha256_json(body)


def _run_locked(
    config: AppConfig,
    *,
    symbols: Iterable[str] | None,
    cross_config: Mapping[str, Any],
) -> dict[str, Any]:
    manifest_path = config.cache_dir / "manifest.json"
    try:
        manifest = load_unique_json(manifest_path, max_bytes=MAX_MANIFEST_BYTES)
    except (OSError, UnicodeError, ValueError) as exc:
        return _unpersisted_result("manifest_unavailable", exc)
    if not isinstance(manifest, dict):
        return _unpersisted_result("manifest_invalid")

    dataset_digest = cross_check_dataset_digest(manifest)
    if dataset_digest is None:
        return _unpersisted_result("manifest_files_invalid")
    requested_symbols = _normalize_symbols(config, symbols)
    lookback = _bounded_int(
        cross_config.get("lookback_sessions"),
        DEFAULT_LOOKBACK_SESSIONS,
        minimum=1,
        maximum=MAX_LOOKBACK_SESSIONS,
    )
    minimum_overlap = _bounded_int(
        cross_config.get("minimum_overlap_sessions"),
        min(DEFAULT_MINIMUM_OVERLAP_SESSIONS, lookback),
        minimum=1,
        maximum=lookback,
    )
    primary_name = str(
        manifest.get("provider") or config.raw["data"].get("provider", "eastmoney")
    ).strip().lower()
    reference_name = str(
        cross_config.get(
            "reference_provider",
            config.raw["data"].get("fallback_provider", "tencent"),
        )
    ).strip().lower()
    now = _now()
    base: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "warning",
        "confidence": "independent_incomplete",
        "generated_at": now,
        "primary_provider": primary_name,
        "reference_provider": reference_name,
        "lookback_sessions": lookback,
        "minimum_overlap_sessions": minimum_overlap,
        "symbols_requested": requested_symbols,
        "symbols": [],
        "dataset_sha256": dataset_digest,
        "methodology": {
            "price_absolute_tolerance": PRICE_ABSOLUTE_TOLERANCE,
            "price_relative_tolerance": PRICE_RELATIVE_TOLERANCE,
            "volume_relative_tolerance": VOLUME_RELATIVE_TOLERANCE,
            "amount_relative_tolerance": AMOUNT_RELATIVE_TOLERANCE,
            "comparison_fields": ["open", "high", "low", "close", "volume", "amount"],
        },
    }
    if reference_name in {"", "none"}:
        base.update(
            status="unavailable",
            confidence="not_available",
            reason="reference_provider_not_configured",
        )
        return _persist_result(config, manifest, base)
    if reference_name == primary_name:
        base.update(
            status="unavailable",
            confidence="not_available",
            reason="reference_provider_must_differ_from_primary",
        )
        return _persist_result(config, manifest, base)
    try:
        provider_for(reference_name)
    except Exception as exc:
        base.update(
            status="unavailable",
            confidence="not_available",
            reason="reference_provider_unregistered",
            error=_safe_error(exc),
        )
        return _persist_result(config, manifest, base)

    cutoff = completed_session_cutoff(
        market_close=config.raw["data"].get("market_close_time", "15:30")
    )
    try:
        base["symbols"] = _audit_symbols(
            config,
            manifest,
            requested_symbols,
            primary_name,
            reference_name,
            cutoff,
            lookback,
            minimum_overlap,
        )
    except Exception as exc:  # defensive: audit must not invalidate the snapshot
        base.update(status="warning", confidence="independent_incomplete", error=_safe_error(exc))

    statuses = [str(item.get("status")) for item in base["symbols"]]
    if statuses and all(value == "matched" for value in statuses):
        base.update(status="passed", confidence="independent_confirmed")
    elif any(value == "mismatch" for value in statuses):
        base.update(status="failed", confidence="independent_conflict")
    elif statuses:
        base.update(status="warning", confidence="independent_incomplete")
        if any(value == "reference_unavailable" for value in statuses):
            base["reason"] = "reference_provider_unavailable"
        elif any(value == "insufficient_overlap" for value in statuses):
            base["reason"] = "insufficient_reference_overlap"
        elif any(value == "not_independent" for value in statuses):
            base["reason"] = "independent_provider_not_available"
    else:
        base.update(status="unavailable", confidence="not_available", reason="no_symbols_checked")
    base["summary"] = {
        "checked": len(statuses),
        "matched": statuses.count("matched"),
        "mismatch": statuses.count("mismatch"),
        "insufficient_overlap": statuses.count("insufficient_overlap"),
        "reference_unavailable": statuses.count("reference_unavailable"),
        "not_independent": statuses.count("not_independent"),
    }
    return _persist_result(config, manifest, base)


def _audit_symbols(
    config: AppConfig,
    manifest: Mapping[str, Any],
    symbols: list[str],
    primary_name: str,
    reference_name: str,
    cutoff: date,
    lookback: int,
    minimum_overlap: int,
) -> list[dict[str, Any]]:
    files = manifest.get("files")
    if not isinstance(files, Mapping):
        return []
    results: list[dict[str, Any]] = []
    instruments = {item.symbol: item for item in config.instruments}
    providers: dict[str, Any] = {}
    provider_failures: dict[str, str] = {}
    with tempfile.TemporaryDirectory(prefix="ai-trade-cross-check-") as temporary:
        temp_root = Path(temporary)
        for symbol in symbols:
            instrument = instruments[symbol]
            cache_file = config.cache_dir / f"{symbol}.csv"
            primary_bars = [bar for bar in load_cached_bars(cache_file) if bar.date <= cutoff]
            selected = primary_bars[-lookback:]
            file_entry = files.get(symbol)
            actual_provider = _actual_provider(file_entry, primary_name)
            audit_reference_name = reference_name
            if actual_provider == audit_reference_name and primary_name != actual_provider:
                # The configured fallback may have supplied this file.  Try
                # the configured primary as the independent source instead of
                # accidentally comparing a provider with its own output.
                audit_reference_name = primary_name
            item: dict[str, Any] = {
                "symbol": symbol,
                "status": "reference_unavailable",
                "actual_provider": actual_provider,
                "reference_provider": audit_reference_name,
                "requested_sessions": len(selected),
                "overlap_sessions": 0,
                "primary_latest": selected[-1].date.isoformat() if selected else None,
                "reference_latest": None,
                "missing_reference_dates": [],
                "max_deviation": {},
                "breaches": [],
            }
            if not selected:
                item.update(status="insufficient_overlap", reason="primary_history_empty")
                results.append(item)
                continue
            if actual_provider is None:
                item.update(
                    status="not_independent",
                    reason="actual_source_provider_unknown",
                )
                results.append(item)
                continue
            if audit_reference_name == actual_provider:
                item.update(
                    status="not_independent",
                    reason="reference_matches_actual_source",
                )
                results.append(item)
                continue
            try:
                reference = providers.get(audit_reference_name)
                if reference is None:
                    reference = provider_for(audit_reference_name)
                    providers[audit_reference_name] = reference
            except Exception as exc:
                item.update(
                    status="reference_unavailable",
                    reason="reference_provider_unregistered",
                    error=_safe_error(exc),
                )
                results.append(item)
                continue
            if audit_reference_name in provider_failures:
                item.update(
                    status="reference_unavailable",
                    reason="reference_provider_circuit_open",
                    error=provider_failures[audit_reference_name],
                )
                results.append(item)
                continue
            raw = copy.deepcopy(config.raw)
            raw_data = raw.setdefault("data", {})
            raw_data["start"] = selected[0].date.isoformat()
            raw_data["end"] = selected[-1].date.isoformat()
            raw_data["cache_dir"] = str(temp_root)
            short_config = replace(config, raw=raw)
            output = temp_root / f"{symbol}-{uuid4().hex}.csv"
            errors: list[str] = []
            try:
                returned = reference.download(
                    short_config,
                    instrument,
                    output,
                    cache_path=None,
                    cutoff=selected[-1].date,
                    proxy_mode=str(raw_data.get("proxy_mode", "system")),
                    network_errors=errors,
                    provider_metadata={},
                )
                reference_bars = load_cached_bars(Path(returned))
            except Exception as exc:
                item["error"] = _safe_error(exc)
                item["reason"] = "reference_provider_request_failed"
                if reference.is_transport_failure(exc):
                    provider_failures[audit_reference_name] = item["error"]
                if errors:
                    item["network_errors"] = [_safe_error(value) for value in errors[:4]]
                results.append(item)
                continue
            reference_by_date = {bar.date: bar for bar in reference_bars if bar.date <= cutoff}
            selected_by_date = {bar.date: bar for bar in selected}
            overlap = sorted(set(selected_by_date) & set(reference_by_date))
            item["overlap_sessions"] = len(overlap)
            item["reference_latest"] = max(reference_by_date).isoformat() if reference_by_date else None
            item["missing_reference_dates"] = [
                value.isoformat() for value in sorted(set(selected_by_date) - set(reference_by_date))
            ][:MAX_BREACHES_PER_SYMBOL]
            if len(overlap) < minimum_overlap:
                item.update(
                    status="insufficient_overlap",
                    reason="reference_history_does_not_cover_minimum_sessions",
                )
                results.append(item)
                continue
            breaches: list[dict[str, Any]] = []
            maximums: dict[str, float] = {}
            for on_date in overlap:
                primary = selected_by_date[on_date]
                secondary = reference_by_date[on_date]
                for field in ("open", "high", "low", "close", "volume", "amount"):
                    left = float(getattr(primary, field))
                    right = float(getattr(secondary, field))
                    absolute = abs(left - right)
                    relative = absolute / max(abs(left), abs(right), 1e-12)
                    maximums[field] = max(maximums.get(field, 0.0), relative)
                    if not _within_tolerance(field, left, right):
                        if len(breaches) < MAX_BREACHES_PER_SYMBOL:
                            breaches.append(
                                {
                                    "date": on_date.isoformat(),
                                    "field": field,
                                    "primary": left,
                                    "reference": right,
                                    "absolute": absolute,
                                    "relative": relative,
                                }
                            )
            item["max_deviation"] = maximums
            item["breaches"] = breaches
            if breaches:
                item.update(status="mismatch", reason="value_outside_tolerance")
            elif item["missing_reference_dates"]:
                item.update(status="insufficient_overlap", reason="reference_missing_recent_sessions")
            else:
                item["status"] = "matched"
            results.append(item)
    return results


def _within_tolerance(field: str, left: float, right: float) -> bool:
    if not (math.isfinite(left) and math.isfinite(right)):
        return False
    absolute = abs(left - right)
    relative = absolute / max(abs(left), abs(right), 1e-12)
    if field in {"open", "high", "low", "close"}:
        return absolute <= PRICE_ABSOLUTE_TOLERANCE or relative <= PRICE_RELATIVE_TOLERANCE
    if field == "volume":
        return relative <= VOLUME_RELATIVE_TOLERANCE
    return relative <= AMOUNT_RELATIVE_TOLERANCE


def _actual_provider(entry: object, configured_primary: str) -> str | None:
    """Infer the source that supplied one file from manifest route metadata."""

    if not isinstance(entry, Mapping):
        return None
    candidates = [
        entry.get("source_provider"),
        entry.get("source"),
        entry.get("cached_seed_source"),
    ]
    for candidate in candidates:
        value = str(candidate or "").strip().lower()
        if "tencent" in value:
            return "tencent"
        if "eastmoney" in value:
            return "eastmoney"
        if value == "network":
            return configured_primary
    return None


def _persist_result(
    config: AppConfig,
    manifest: dict[str, Any],
    result: dict[str, Any],
) -> dict[str, Any]:
    manifest_path = config.cache_dir / "manifest.json"
    try:
        result["input_manifest_sha256"] = sha256(manifest_path.read_bytes()).hexdigest()
    except OSError:
        result["input_manifest_sha256"] = None
    result["persisted"] = True
    result["audit_sha256"] = _sha256_json(result)
    updated = copy.deepcopy(manifest)
    updated["cross_source_check"] = result
    try:
        replace_manifest(config.cache_dir, updated)
    except Exception as exc:
        result["persisted"] = False
        result["persistence_error"] = _safe_error(exc)
        return _bounded_projection(result)
    result["persisted"] = True
    return _bounded_projection(result)


def _normalize_symbols(config: AppConfig, symbols: Iterable[str] | None) -> list[str]:
    configured = {item.symbol for item in config.instruments}
    if symbols is None:
        return sorted(configured)
    values = []
    for value in symbols:
        symbol = str(value).strip()
        if not symbol or symbol in values:
            continue
        if symbol not in configured:
            raise ValueError(f"Cross-check symbol is not configured: {symbol}")
        values.append(symbol)
        if len(values) >= MAX_SYMBOLS:
            break
    return values


def _bounded_int(value: Any, default: int, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        return default
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return min(max(parsed, minimum), maximum)


def _unpersisted_result(reason: str, exc: Exception | None = None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "status": "unavailable",
        "confidence": "not_available",
        "reason": reason,
        "persisted": False,
    }
    if exc is not None:
        result["error"] = _safe_error(exc)
    return result


def _verify_payload(
    manifest: Mapping[str, Any],
    payload: Mapping[str, Any],
    *,
    file_hashes: Mapping[str, str] | None,
) -> tuple[bool, str]:
    expected_dataset = cross_check_dataset_digest(manifest)
    if payload.get("dataset_sha256") != expected_dataset:
        return False, "audit_dataset_digest_mismatch"
    audit_sha = payload.get("audit_sha256")
    if not isinstance(audit_sha, str) or audit_sha != _sha256_json(payload):
        return False, "audit_digest_mismatch"
    if file_hashes is not None:
        files = manifest.get("files")
        if not isinstance(files, Mapping):
            return False, "manifest_files_invalid"
        for symbol, digest in file_hashes.items():
            entry = files.get(symbol)
            if not isinstance(entry, Mapping) or entry.get("sha256") != digest:
                return False, "audit_file_digest_mismatch"
    return True, ""


def _bounded_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    result = dict(value)
    symbols = result.get("symbols")
    if isinstance(symbols, list):
        result["symbols"] = symbols[:MAX_SYMBOLS]
    for key in ("error", "reason", "persistence_error"):
        if key in result and result[key] is not None:
            result[key] = str(result[key])[:MAX_ERROR_LENGTH]
    return result


def _sha256_json(value: Mapping[str, Any]) -> str:
    body = {key: value[key] for key in sorted(value) if key != "audit_sha256"}
    encoded = json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return sha256(encoded.encode("utf-8")).hexdigest()


def _safe_error(value: object) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text[:MAX_ERROR_LENGTH] or "unknown error"


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


__all__ = [
    "AMOUNT_RELATIVE_TOLERANCE",
    "DEFAULT_LOOKBACK_SESSIONS",
    "DEFAULT_MINIMUM_OVERLAP_SESSIONS",
    "PRICE_ABSOLUTE_TOLERANCE",
    "PRICE_RELATIVE_TOLERANCE",
    "SCHEMA_VERSION",
    "VOLUME_RELATIVE_TOLERANCE",
    "cross_check_dataset_digest",
    "cross_check_market_snapshot",
    "cross_source_projection",
]
