"""Immutable public Level-1 five-level order-book snapshots."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import math
from pathlib import Path
import re
import time as time_module
from typing import Any, Mapping, Sequence
import urllib.parse
import urllib.request

from ..config import AppConfig
from ..json_utils import loads_unique_json
from ..models import Instrument
from .eastmoney import REQUEST_HEADERS, _open_request, _proxy_mode
from .evidence_io import DateRevisionSpec, ImmutableDateRevisionStore


SCHEMA_VERSION = 1
DATASET = "order_book"
ENDPOINT = "https://push2.eastmoney.com/api/qt/stock/get"
EASTMONEY_UT = "fa5fd1943c7b386f172d6893dbfba10b"
MAX_RESPONSE_BYTES = 512 * 1024
MAX_SYMBOLS = 100
MAX_QUERY_LIMIT = 500
CHINA_TIMEZONE = timezone(timedelta(hours=8))
_SYMBOL = re.compile(r"\d{6}\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
_AUTHORITY = {"research_only": True, "execution_authorized": False}

# Eastmoney returns f11-f40 only for its full Level-1 quote field bundle.
_REQUEST_FIELDS = (
    "f120,f121,f122,f174,f175,f59,f163,f43,f57,f58,f169,f170,f46,f44,"
    "f51,f168,f47,f164,f116,f60,f45,f52,f50,f48,f167,f117,f71,f161,"
    "f49,f530,f135,f136,f137,f138,f139,f141,f142,f144,f145,f147,f148,"
    "f140,f143,f146,f149,f55,f62,f162,f92,f173,f104,f105,f84,f85,f183,"
    "f184,f185,f186,f187,f188,f189,f190,f191,f192,f107,f111,f86,f177,"
    "f78,f110,f262,f263,f264,f267,f268,f255,f256,f257,f258,f127,f199,"
    "f128,f198,f259,f260,f261,f171,f277,f278,f279,f288,f152,f250,f251,"
    "f252,f253,f254,f269,f270,f271,f272,f273,f274,f275,f276,f265,f266,"
    "f289,f290,f286,f285,f292,f293,f294,f295"
)
_DEPTH_MAPPING = {
    "buy": (("f19", "f20"), ("f17", "f18"), ("f15", "f16"), ("f13", "f14"), ("f11", "f12")),
    "sell": (("f39", "f40"), ("f37", "f38"), ("f35", "f36"), ("f33", "f34"), ("f31", "f32")),
}


@dataclass(frozen=True)
class OrderBookQuery:
    trade_date: date | None = None
    symbol: str | None = None
    limit: int = 100
    include_revisions: bool = False


class OrderBookProviderError(RuntimeError):
    """Raised when a Level-1 quote response fails validation."""


def refresh_order_book(
    config: AppConfig,
    *,
    symbols: Sequence[str] | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    instruments = {item.symbol: item for item in config.instruments}
    selected = list(symbols) if symbols is not None else list(instruments)
    if not selected or len(selected) > MAX_SYMBOLS or len(set(selected)) != len(selected):
        raise ValueError(f"symbols must contain 1 to {MAX_SYMBOLS} unique items")
    resolved: list[Instrument] = []
    for symbol in selected:
        if not isinstance(symbol, str) or _SYMBOL.fullmatch(symbol) is None:
            raise ValueError("symbol must be a six-digit security code")
        try:
            resolved.append(instruments[symbol])
        except KeyError as exc:
            raise ValueError("symbol must be in the configured security master") from exc

    records: list[dict[str, Any]] = []
    errors: list[dict[str, str]] = []
    responses: list[dict[str, Any]] = []
    for instrument in resolved:
        try:
            payload, digest, response_bytes = _download(config, instrument)
            record = _parse_payload(payload, instrument, as_of=as_of)
            record["response_sha256"] = digest
            record["response_bytes"] = response_bytes
            records.append(record)
            responses.append(
                {
                    "symbol": instrument.symbol,
                    "response_sha256": digest,
                    "response_bytes": response_bytes,
                }
            )
        except (OrderBookProviderError, OSError, ValueError) as exc:
            errors.append(
                {
                    "symbol": instrument.symbol,
                    "code": "order_book_provider_error",
                    "message": str(exc)[:300],
                }
            )
    if not records:
        return _unavailable(
            OrderBookQuery(symbol=selected[0] if len(selected) == 1 else None),
            errors,
        )
    newest_date = max(date.fromisoformat(item["provider_date"]) for item in records)
    current_records: list[dict[str, Any]] = []
    for item in records:
        if date.fromisoformat(item["provider_date"]) != newest_date:
            errors.append(
                {
                    "symbol": item["symbol"],
                    "code": "order_book_stale_provider_date",
                    "message": (
                        f"Provider depth date {item['provider_date']} is older than "
                        f"the newest returned date {newest_date.isoformat()}."
                    ),
                }
            )
            continue
        current_records.append(item)
    records = current_records
    records.sort(key=lambda item: item["symbol"])
    responses.sort(key=lambda item: item["symbol"])
    trade_date = newest_date
    draft = {
        "schema_version": SCHEMA_VERSION,
        "dataset": DATASET,
        "available": True,
        "status": "partial" if errors or any(item["status"] == "partial" for item in records) else "current",
        "trade_date": trade_date.isoformat(),
        "retrieved_at": _now(),
        "source": {
            "provider": "eastmoney",
            "endpoint": ENDPOINT,
            "request_fields": _REQUEST_FIELDS.split(","),
            "depth_fields": [
                field
                for pairs in _DEPTH_MAPPING.values()
                for pair in pairs
                for field in pair
            ],
            "float_mode": "fltt=2,invt=2",
            "price_unit": "CNY",
            "raw_volume_unit": "lot",
            "normalized_volume_unit": "share",
            "volume_scaling": "provider lots * 100 shares",
            "certification": "public_level1_not_exchange_certified",
            "response_count": len(responses),
            "response_sha256": _fingerprint(responses),
            "responses": responses,
        },
        "records": records,
        "summary": {
            "requested_count": len(resolved),
            "returned_count": len(records),
            "complete_depth_count": sum(item["status"] == "complete" for item in records),
            "partial_depth_count": sum(item["status"] == "partial" for item in records),
            "error_count": len(errors),
        },
        "errors": errors,
        "warnings": [
            {
                "code": "public_level1_only",
                "message": "Five-level public quote depth is Level-1 research evidence, not Tick, full-depth, or Level-2 data.",
            },
            {
                "code": "not_exchange_certified",
                "message": "The snapshot is a third-party public quote and is not exchange-certified or suitable for execution decisions.",
            },
            {
                "code": "ephemeral_snapshot",
                "message": "Each revision records one observed snapshot; it is not a replayable order-event stream.",
            },
        ],
        "authority": dict(_AUTHORITY),
    }
    return OrderBookStore(config).publish(draft)


class OrderBookStore:
    def __init__(self, config_or_root: AppConfig | str | Path):
        if isinstance(config_or_root, AppConfig) or hasattr(
            config_or_root, "order_book_dir"
        ):
            root = getattr(config_or_root, "order_book_dir", None)
            if root is None:
                root = config_or_root.resolve("state/order_book")
        else:
            root = Path(config_or_root)
        self._store = ImmutableDateRevisionStore(
            Path(root),
            DateRevisionSpec(DATASET, "Order book", "order_book"),
            _validate_payload,
        )

    def publish(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        return self._store.publish(draft)

    def list(self, query: OrderBookQuery | None = None) -> dict[str, Any]:
        selected = query or OrderBookQuery()
        _validate_query(selected)
        latest = self._store.latest(
            selected.trade_date, include_revisions=selected.include_revisions
        )
        if latest is None:
            return _unavailable(selected, [])
        matched = list(latest["records"])
        if selected.symbol:
            matched = [item for item in matched if item["symbol"] == selected.symbol]
        latest["records"] = matched[: selected.limit]
        latest["summary"] = {
            **latest["summary"],
            "matched_count": len(matched),
            "returned_count": len(latest["records"]),
            "truncated": len(matched) > len(latest["records"]),
        }
        latest["filters"] = _query_payload(selected)
        return latest


def _download(
    config: AppConfig, instrument: Instrument
) -> tuple[dict[str, Any], str, int]:
    params = {
        "secid": _secid(instrument),
        "fields": _REQUEST_FIELDS,
        "fltt": "2",
        "invt": "2",
        "ut": EASTMONEY_UT,
    }
    request = urllib.request.Request(
        f"{ENDPOINT}?{urllib.parse.urlencode(params)}",
        headers=REQUEST_HEADERS,
        method="GET",
    )
    data_config = config.raw.get("data", {})
    timeout = int(data_config.get("timeout_seconds", 20))
    attempts = min(3, max(1, int(data_config.get("max_attempts", 3))))
    errors: list[str] = []
    for attempt in range(attempts):
        try:
            with _open_request(request, timeout, _proxy_mode(config)) as response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
            if len(raw) > MAX_RESPONSE_BYTES:
                raise OrderBookProviderError("order-book response is too large")
            value = loads_unique_json(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise OrderBookProviderError("order-book response is not an object")
            return value, sha256(raw).hexdigest(), len(raw)
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")
            if attempt + 1 < attempts:
                time_module.sleep(min(2.0, 0.25 * (2**attempt)))
    raise OrderBookProviderError(
        "order-book download failed: " + " | ".join(errors)
    )


def _parse_payload(
    payload: Mapping[str, Any], instrument: Instrument, *, as_of: datetime | None
) -> dict[str, Any]:
    if payload.get("rc") not in {None, 0}:
        raise OrderBookProviderError(
            f"Eastmoney order-book request returned rc={payload.get('rc')}"
        )
    data = payload.get("data")
    if not isinstance(data, dict) or str(data.get("f57")) != instrument.symbol:
        raise OrderBookProviderError("order-book response identity is invalid")
    name = _text(data.get("f58"), "security name")
    latest = _positive_number(data.get("f43"), "latest price")
    previous_close = _optional_positive(data.get("f60"))
    provider_timestamp = data.get("f86")
    if (
        isinstance(provider_timestamp, bool)
        or not isinstance(provider_timestamp, (int, float))
        or not math.isfinite(float(provider_timestamp))
        or int(provider_timestamp) != provider_timestamp
        or provider_timestamp <= 0
    ):
        raise OrderBookProviderError("provider timestamp is invalid")
    observed = datetime.fromtimestamp(int(provider_timestamp), timezone.utc).astimezone(
        CHINA_TIMEZONE
    )
    now = _china_now(as_of)
    if observed > now + timedelta(minutes=5) or now - observed > timedelta(days=14):
        raise OrderBookProviderError("provider timestamp is outside the freshness bound")

    levels: dict[str, list[dict[str, Any]]] = {}
    for side, mapping in _DEPTH_MAPPING.items():
        parsed: list[dict[str, Any]] = []
        for rank, (price_field, volume_field) in enumerate(mapping, start=1):
            price = _optional_positive(data.get(price_field))
            lots = _optional_nonnegative(data.get(volume_field))
            if price is None and lots not in {None, 0.0}:
                raise OrderBookProviderError(
                    f"{side} level {rank} has volume without a price"
                )
            raw_shares = None if lots is None else lots * 100
            if raw_shares is not None and not raw_shares.is_integer():
                raise OrderBookProviderError(
                    f"{side} level {rank} volume does not normalize to whole shares"
                )
            parsed.append(
                {
                    "rank": rank,
                    "price": price,
                    "volume_lots": lots,
                    "volume_shares": None if raw_shares is None else int(raw_shares),
                    "price_field": price_field,
                    "volume_field": volume_field,
                }
            )
        levels[side] = parsed
    bid = next((item["price"] for item in levels["buy"] if item["price"]), None)
    ask = next((item["price"] for item in levels["sell"] if item["price"]), None)
    if bid is None or ask is None:
        raise OrderBookProviderError("five-level depth fields are unavailable")
    if ask < bid:
        raise OrderBookProviderError("best ask is below best bid")
    complete_levels = sum(
        item["price"] is not None and item["volume_shares"] is not None
        for side in levels.values()
        for item in side
    )
    bid_volume = sum(item["volume_shares"] or 0 for item in levels["buy"])
    ask_volume = sum(item["volume_shares"] or 0 for item in levels["sell"])
    total_volume = bid_volume + ask_volume
    return {
        "symbol": instrument.symbol,
        "name": name,
        "market": instrument.market,
        "instrument_type": instrument.instrument_type,
        "status": "complete" if complete_levels == 10 else "partial",
        "provider_timestamp": int(provider_timestamp),
        "observed_at": observed.isoformat(),
        "provider_date": observed.date().isoformat(),
        "latest": latest,
        "previous_close": previous_close,
        "best_bid": bid,
        "best_ask": ask,
        "spread": round(ask - bid, 6),
        "mid_price": round((ask + bid) / 2, 6),
        "buy_levels": levels["buy"],
        "sell_levels": levels["sell"],
        "buy_volume_shares": bid_volume,
        "sell_volume_shares": ask_volume,
        "depth_imbalance": (
            None
            if total_volume == 0
            else round((bid_volume - ask_volume) / total_volume, 6)
        ),
        "complete_level_count": complete_levels,
    }


def _validate_payload(value: Mapping[str, Any]) -> None:
    if value.get("schema_version") != SCHEMA_VERSION or value.get("dataset") != DATASET:
        raise RuntimeError("order-book schema is invalid")
    try:
        trade_date = date.fromisoformat(str(value["trade_date"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError("order-book trade_date is invalid") from exc
    records = value.get("records")
    if not isinstance(records, list) or not records or len(records) > MAX_SYMBOLS:
        raise RuntimeError("order-book records are invalid")
    seen: set[str] = set()
    for item in records:
        if not isinstance(item, dict) or _SYMBOL.fullmatch(
            str(item.get("symbol", ""))
        ) is None:
            raise RuntimeError("order-book symbol is invalid")
        if item["symbol"] in seen:
            raise RuntimeError("order-book symbols are duplicated")
        seen.add(item["symbol"])
        _text(item.get("name"), "security name")
        if item.get("status") not in {"complete", "partial"}:
            raise RuntimeError("order-book status is invalid")
        observed = _timestamp(item.get("observed_at")).astimezone(CHINA_TIMEZONE)
        provider_date = date.fromisoformat(str(item.get("provider_date")))
        if provider_date != trade_date or observed.date() != provider_date:
            raise RuntimeError("order-book record date is inconsistent")
        complete_levels = 0
        volumes: dict[str, int] = {}
        for side in ("buy_levels", "sell_levels"):
            levels = item.get(side)
            if not isinstance(levels, list) or len(levels) != 5:
                raise RuntimeError("order-book levels are invalid")
            if [level.get("rank") for level in levels] != [1, 2, 3, 4, 5]:
                raise RuntimeError("order-book level ranks are invalid")
            expected_mapping = _DEPTH_MAPPING["buy" if side == "buy_levels" else "sell"]
            for level, (price_field, volume_field) in zip(
                levels, expected_mapping, strict=True
            ):
                _validate_optional_number(level.get("price"), positive=True)
                _validate_optional_number(level.get("volume_lots"), positive=False)
                shares = level.get("volume_shares")
                if shares is not None and (
                    isinstance(shares, bool)
                    or not isinstance(shares, int)
                    or shares < 0
                    or shares % 100 != 0
                ):
                    raise RuntimeError("order-book share volume is invalid")
                lots = level.get("volume_lots")
                if shares is not None and (
                    lots is None or not math.isclose(float(shares), float(lots) * 100)
                ):
                    raise RuntimeError("order-book lot/share volume is inconsistent")
                if level.get("price_field") != price_field or level.get(
                    "volume_field"
                ) != volume_field:
                    raise RuntimeError("order-book provider field mapping is invalid")
                complete_levels += level.get("price") is not None and shares is not None
            volumes[side] = sum(level.get("volume_shares") or 0 for level in levels)
        bid = item["buy_levels"][0].get("price")
        ask = item["sell_levels"][0].get("price")
        if bid is None or ask is None or ask < bid:
            raise RuntimeError("order-book best prices are invalid")
        total_volume = volumes["buy_levels"] + volumes["sell_levels"]
        expected_imbalance = (
            None
            if total_volume == 0
            else round(
                (volumes["buy_levels"] - volumes["sell_levels"]) / total_volume,
                6,
            )
        )
        expected_status = "complete" if complete_levels == 10 else "partial"
        if (
            item.get("best_bid") != bid
            or item.get("best_ask") != ask
            or item.get("spread") != round(ask - bid, 6)
            or item.get("mid_price") != round((ask + bid) / 2, 6)
            or item.get("buy_volume_shares") != volumes["buy_levels"]
            or item.get("sell_volume_shares") != volumes["sell_levels"]
            or item.get("depth_imbalance") != expected_imbalance
            or item.get("complete_level_count") != complete_levels
            or item.get("status") != expected_status
        ):
            raise RuntimeError("order-book derived fields are inconsistent")
    source = value.get("source")
    if not isinstance(source, dict) or source.get("certification") != (
        "public_level1_not_exchange_certified"
    ):
        raise RuntimeError("order-book source metadata is invalid")
    responses = source.get("responses")
    if not isinstance(responses, list) or source.get("response_count") != len(responses):
        raise RuntimeError("order-book responses are invalid")
    if source.get("response_sha256") != _fingerprint(responses):
        raise RuntimeError("order-book response fingerprint is invalid")
    for response in responses:
        if not isinstance(response, dict) or _FINGERPRINT.fullmatch(
            str(response.get("response_sha256", ""))
        ) is None:
            raise RuntimeError("order-book response evidence is invalid")
    if value.get("authority") != _AUTHORITY:
        raise RuntimeError("order-book authority is invalid")


def _validate_query(query: OrderBookQuery) -> None:
    if query.trade_date is not None and (
        not isinstance(query.trade_date, date)
        or isinstance(query.trade_date, datetime)
    ):
        raise ValueError("trade_date must be a date")
    if query.symbol is not None and _SYMBOL.fullmatch(query.symbol) is None:
        raise ValueError("symbol must be a six-digit security code")
    if isinstance(query.limit, bool) or not isinstance(query.limit, int) or not 1 <= query.limit <= MAX_QUERY_LIMIT:
        raise ValueError(f"limit must be between 1 and {MAX_QUERY_LIMIT}")


def _unavailable(
    query: OrderBookQuery, errors: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "dataset": DATASET,
        "available": False,
        "status": "unavailable",
        "trade_date": query.trade_date.isoformat() if query.trade_date else None,
        "records": [],
        "summary": {"returned_count": 0, "error_count": len(errors)},
        "errors": list(errors)
        or [
            {
                "code": "order_book_not_refreshed",
                "message": "No validated local Level-1 order-book snapshot is available.",
            }
        ],
        "warnings": [],
        "authority": dict(_AUTHORITY),
        "filters": _query_payload(query),
        "revisions": [],
    }


def _query_payload(query: OrderBookQuery) -> dict[str, Any]:
    return {
        "trade_date": query.trade_date.isoformat() if query.trade_date else None,
        "symbol": query.symbol,
        "limit": query.limit,
    }


def _secid(instrument: Instrument) -> str:
    market = {"SH": "1", "SZ": "0", "BJ": "0"}.get(
        instrument.market.strip().upper()
    )
    if market is None:
        raise ValueError(f"Unsupported order-book market: {instrument.market}")
    return f"{market}.{instrument.symbol}"


def _optional_number(value: Any) -> float | None:
    if value in {None, "", "-"}:
        return None
    if isinstance(value, bool):
        raise OrderBookProviderError("order-book numeric field is invalid")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise OrderBookProviderError("order-book numeric field is invalid") from exc
    if not math.isfinite(result):
        raise OrderBookProviderError("order-book numeric field is not finite")
    return result


def _positive_number(value: Any, label: str) -> float:
    result = _optional_number(value)
    if result is None or result <= 0:
        raise OrderBookProviderError(f"{label} is unavailable")
    return result


def _optional_positive(value: Any) -> float | None:
    result = _optional_number(value)
    return result if result is not None and result > 0 else None


def _optional_nonnegative(value: Any) -> float | None:
    result = _optional_number(value)
    if result is not None and result < 0:
        raise OrderBookProviderError("order-book volume is negative")
    return result


def _validate_optional_number(value: Any, *, positive: bool) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or (float(value) <= 0 if positive else float(value) < 0)
    ):
        raise RuntimeError("order-book normalized number is invalid")


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > 100:
        raise OrderBookProviderError(f"{label} is invalid")
    return value.strip()


def _timestamp(value: Any) -> datetime:
    if not isinstance(value, str):
        raise RuntimeError("order-book timestamp is invalid")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError("order-book timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise RuntimeError("order-book timestamp lacks a timezone")
    return parsed


def _china_now(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(CHINA_TIMEZONE)
    if value.tzinfo is None:
        return value.replace(tzinfo=CHINA_TIMEZONE)
    return value.astimezone(CHINA_TIMEZONE)


def _fingerprint(value: Any) -> str:
    import json

    return sha256(
        json.dumps(
            value,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


__all__ = [
    "OrderBookProviderError",
    "OrderBookQuery",
    "OrderBookStore",
    "refresh_order_book",
]
