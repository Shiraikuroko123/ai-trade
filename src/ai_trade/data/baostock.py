"""Bounded BaoStock daily bars for provisional recent-data reconciliation.

BaoStock is an optional, anonymous public reference route.  The upstream data
provenance, service level, and redistribution authorization are not verified,
so this adapter can never supply the strategy snapshot or independently
confirm it.  It only normalizes a short response for a conservative numerical
cross-check.
"""

from __future__ import annotations

import csv
import io
import math
from contextlib import redirect_stderr, redirect_stdout
from datetime import date
from importlib import import_module
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..config import AppConfig
from ..models import Bar, Instrument


OPTIONAL_EXTRA = "baostock"
SUCCESS_CODE = "0"
MAX_REFERENCE_RANGE_DAYS = 100
MAX_ROWS = 80
SHARES_PER_LOT = 100.0
FIELDS = (
    "date",
    "code",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "amount",
    "adjustflag",
)
TRANSPORT_ERROR_CODES = {
    "10002001",
    "10002002",
    "10002003",
    "10002004",
    "10002005",
    "10002006",
    "10002007",
    "10002008",
}


class BaoStockDownloadError(RuntimeError):
    """A bounded BaoStock client failure with an explicit provider code."""

    def __init__(self, stage: str, error_code: str, message: str):
        detail = _safe_text(message)
        super().__init__(f"BaoStock {stage} failed with code {error_code}: {detail}")
        self.stage = stage
        self.error_code = error_code


def download_instrument(
    config: AppConfig,
    instrument: Instrument,
    output_path: Path,
    *,
    cutoff: date,
    proxy_mode: str | None = None,
    provider_metadata: dict[str, object] | None = None,
) -> Path:
    """Download one recent reference window and stage a normalized CSV."""

    adjustment = str(config.raw["data"].get("adjustment", "forward"))
    adjustflag = _adjustflag(adjustment)
    start, end = _request_range(config, instrument, cutoff)
    code = _provider_code(instrument)
    configured_proxy_mode = _proxy_mode(config, proxy_mode)
    client = _load_client()

    login_attempted = False
    try:
        login_attempted = True
        login_result = _quiet_call(client.login)
        _require_success(login_result, "login")
        query_result = _quiet_call(
            client.query_history_k_data_plus,
            code,
            ",".join(FIELDS),
            start_date=start.isoformat(),
            end_date=end.isoformat(),
            frequency="d",
            adjustflag=adjustflag,
        )
        rows = _result_rows(query_result)
    except BaoStockDownloadError:
        raise
    except Exception as exc:
        raise BaoStockDownloadError(
            "client",
            "exception",
            f"{type(exc).__name__}: {exc}",
        ) from exc
    finally:
        if login_attempted:
            try:
                _quiet_call(client.logout)
            except Exception:
                # Logout cannot make a fully validated observation false.  The
                # optional client owns its process-global session and socket.
                pass

    bars = _parse_rows(
        rows,
        code=code,
        start=start,
        end=end,
        cutoff=cutoff,
        adjustflag=adjustflag,
    )
    _write_bars(output_path, bars)
    if provider_metadata is not None:
        provider_metadata.update(
            {
                "source_provider": "baostock_public_api",
                "source_mode": "anonymous_bounded_reference",
                "provider_transport": "direct_tcp_client",
                "configured_proxy_mode": configured_proxy_mode,
                "proxy_applied": False,
                "adjustflag": adjustflag,
                "adjustment_semantics": (
                    "provider_declared_forward" if adjustment == "forward" else "unadjusted"
                ),
                "volume_unit": "lots_100_shares_normalized_from_shares",
                "amount_unit": "cny",
                "comparison_fields": list(FIELDS[2:8]),
                "usage_scope": "provisional_reference_only",
                "authorization_status": "upstream_and_redistribution_unverified",
            }
        )
    return output_path


def _load_client() -> Any:
    try:
        client = import_module("baostock")
    except ImportError as exc:
        raise RuntimeError(
            "BaoStock reference provider requires the optional "
            f"ai-trade[{OPTIONAL_EXTRA}] dependency"
        ) from exc
    for name in ("login", "logout", "query_history_k_data_plus"):
        if not callable(getattr(client, name, None)):
            raise RuntimeError(f"BaoStock client is missing required callable {name}")
    return client


def _quiet_call(call: Any, *args: object, **kwargs: object) -> Any:
    # BaoStock prints login and network messages directly.  Keep CLI JSON and
    # audit output machine-readable; structured result codes remain available.
    with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
        return call(*args, **kwargs)


def _result_rows(result: object) -> list[dict[str, object]]:
    _require_success(result, "history query")
    raw_fields = getattr(result, "fields", None)
    if not isinstance(raw_fields, (list, tuple)) or tuple(raw_fields) != FIELDS:
        raise RuntimeError("BaoStock response fields do not match the request")
    next_row = getattr(result, "next", None)
    get_row_data = getattr(result, "get_row_data", None)
    if not callable(next_row) or not callable(get_row_data):
        raise RuntimeError("BaoStock response iterator is invalid")

    rows: list[dict[str, object]] = []
    while True:
        has_next = _quiet_call(next_row)
        if not isinstance(has_next, bool):
            raise RuntimeError("BaoStock response iterator returned a non-boolean value")
        if not has_next:
            break
        raw_row = _quiet_call(get_row_data)
        if not isinstance(raw_row, (list, tuple)) or len(raw_row) != len(FIELDS):
            raise RuntimeError("BaoStock response row shape is invalid")
        rows.append(dict(zip(FIELDS, raw_row, strict=True)))
        if len(rows) > MAX_ROWS:
            raise RuntimeError("BaoStock response row count exceeds the reference limit")
    _require_success(result, "history query")
    return rows


def _require_success(result: object, stage: str) -> None:
    code = getattr(result, "error_code", None)
    if code == SUCCESS_CODE:
        return
    normalized = str(code) if code is not None else "invalid_result"
    message = getattr(result, "error_msg", "invalid provider result")
    raise BaoStockDownloadError(stage, normalized, str(message))


def _parse_rows(
    rows: Sequence[Mapping[str, object]],
    *,
    code: str,
    start: date,
    end: date,
    cutoff: date,
    adjustflag: str,
) -> list[Bar]:
    parsed: list[Bar] = []
    seen_dates: set[date] = set()
    for index, row in enumerate(rows):
        if set(row) != set(FIELDS):
            raise RuntimeError(f"BaoStock row fields are invalid at row {index}")
        if row.get("code") != code:
            raise RuntimeError("BaoStock symbol identity does not match the request")
        if row.get("adjustflag") != adjustflag:
            raise RuntimeError("BaoStock adjustment flag does not match the request")
        on_date = _trade_date(row.get("date"))
        if on_date in seen_dates:
            raise RuntimeError("BaoStock returned duplicate trading dates")
        if not start <= on_date <= end or on_date > cutoff:
            raise RuntimeError("BaoStock row date is outside the request")
        seen_dates.add(on_date)
        bar = Bar(
            date=on_date,
            open=_positive(row.get("open"), "open"),
            close=_positive(row.get("close"), "close"),
            high=_positive(row.get("high"), "high"),
            low=_positive(row.get("low"), "low"),
            volume=_nonnegative(row.get("volume"), "volume") / SHARES_PER_LOT,
            amount=_nonnegative(row.get("amount"), "amount"),
        )
        _validate_bar(bar, index)
        parsed.append(bar)
    parsed.sort(key=lambda item: item.date)
    if not parsed:
        raise RuntimeError("BaoStock returned no completed daily bars")
    return parsed


def _request_range(
    config: AppConfig, instrument: Instrument, cutoff: date
) -> tuple[date, date]:
    configured_start = date.fromisoformat(config.raw["data"]["start"])
    configured_end = date.fromisoformat(config.raw["data"]["end"])
    start = max(configured_start, instrument.listing_date or configured_start)
    end = min(configured_end, cutoff, instrument.delisting_date or cutoff)
    if start > end:
        raise RuntimeError(
            f"BaoStock reference range is empty for {instrument.symbol}: {start}..{end}"
        )
    if (end - start).days > MAX_REFERENCE_RANGE_DAYS:
        raise RuntimeError(
            "BaoStock is reference-only and accepts at most "
            f"{MAX_REFERENCE_RANGE_DAYS + 1} calendar days per request"
        )
    return start, end


def _provider_code(instrument: Instrument) -> str:
    kind = instrument.instrument_type.strip().upper()
    if kind not in {"ETF", "STOCK"}:
        raise RuntimeError(
            "BaoStock reference bars do not support instrument type "
            f"{instrument.instrument_type!r}"
        )
    market = instrument.market.strip().lower()
    if market not in {"sh", "sz"}:
        raise RuntimeError(f"BaoStock has no code mapping for market {instrument.market!r}")
    return f"{market}.{instrument.symbol}"


def _adjustflag(adjustment: str) -> str:
    if adjustment == "forward":
        return "2"
    if adjustment == "none":
        return "3"
    raise RuntimeError("BaoStock reference bars support only none or forward adjustment")


def _proxy_mode(config: AppConfig, explicit: str | None) -> str:
    value: object = (
        explicit if explicit is not None else config.raw["data"].get("proxy_mode", "system")
    )
    if not isinstance(value, str) or value.strip().lower() not in {"system", "direct"}:
        raise ValueError("BaoStock configured proxy mode must be system or direct")
    return value.strip().lower()


def _trade_date(value: object) -> date:
    if not isinstance(value, str):
        raise RuntimeError("BaoStock date is invalid")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError("BaoStock date is invalid") from exc


def _positive(value: object, label: str) -> float:
    result = _number(value, label)
    if result <= 0:
        raise RuntimeError(f"BaoStock {label} is not positive")
    return result


def _nonnegative(value: object, label: str) -> float:
    result = _number(value, label)
    if result < 0:
        raise RuntimeError(f"BaoStock {label} is negative")
    return result


def _number(value: object, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise RuntimeError(f"BaoStock {label} is invalid")
    try:
        result = float(value)
    except ValueError as exc:
        raise RuntimeError(f"BaoStock {label} is invalid") from exc
    if not math.isfinite(result):
        raise RuntimeError(f"BaoStock {label} is not finite")
    return result


def _validate_bar(bar: Bar, index: int) -> None:
    if bar.high < max(bar.open, bar.close, bar.low) or bar.low > min(
        bar.open, bar.close, bar.high
    ):
        raise RuntimeError(f"BaoStock OHLC relationship is invalid at row {index}")
    if bar.volume < 0 or bar.amount < 0:
        raise RuntimeError(f"BaoStock volume or amount is invalid at row {index}")


def _write_bars(path: Path, bars: Sequence[Bar]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(
                ["date", "open", "close", "high", "low", "volume", "amount", "amplitude"]
            )
            for bar in bars:
                writer.writerow(
                    [
                        bar.date.isoformat(),
                        _format(bar.open),
                        _format(bar.close),
                        _format(bar.high),
                        _format(bar.low),
                        _format(bar.volume),
                        _format(bar.amount),
                        "",
                    ]
                )
        from .eastmoney import load_cached_bars

        load_cached_bars(temporary)
        temporary.replace(path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _format(value: float) -> str:
    return format(value, ".15g")


def _safe_text(value: object) -> str:
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    return text[:300] or "unknown error"


def is_transport_failure(error: Exception) -> bool:
    return isinstance(error, BaoStockDownloadError) and (
        error.error_code in TRANSPORT_ERROR_CODES
    )


__all__ = [
    "BaoStockDownloadError",
    "FIELDS",
    "MAX_REFERENCE_RANGE_DAYS",
    "MAX_ROWS",
    "OPTIONAL_EXTRA",
    "download_instrument",
    "is_transport_failure",
]
