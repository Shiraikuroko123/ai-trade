from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, timedelta
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable

from .json_utils import load_unique_json
from .models import Instrument


MAX_SECURITY_MASTER_BYTES = 32 * 1024 * 1024


@dataclass(frozen=True)
class UniverseMembership:
    symbol: str
    start: date
    end: date | None = None

    def contains(self, on_date: date) -> bool:
        return self.start <= on_date and (self.end is None or on_date <= self.end)


@dataclass(frozen=True)
class TradingStatusPeriod:
    symbol: str
    start: date
    end: date | None
    status: str
    tradable: bool
    price_limit_pct: float | None = None

    def contains(self, on_date: date) -> bool:
        return self.start <= on_date and (self.end is None or on_date <= self.end)


@dataclass(frozen=True)
class TradingStatus:
    status: str
    tradable: bool
    price_limit_pct: float | None


class SecurityMaster:
    def __init__(
        self,
        instruments: Iterable[Instrument],
        universes: dict[str, tuple[UniverseMembership, ...]],
        status_periods: tuple[TradingStatusPeriod, ...] = (),
        metadata: dict[str, Any] | None = None,
        source_path: Path | None = None,
    ):
        values = tuple(instruments)
        self.instruments = {item.symbol: item for item in values}
        if len(values) != len(self.instruments):
            raise ValueError("Security master contains duplicate instrument symbols")
        self.universes = universes
        self.status_periods = status_periods
        self.metadata = metadata or {}
        self.source_path = source_path
        self._validate()

    @classmethod
    def load(cls, path: Path) -> "SecurityMaster":
        raw = load_unique_json(path, max_bytes=MAX_SECURITY_MASTER_BYTES)
        if not isinstance(raw, dict):
            raise ValueError("security master must be a JSON object")
        return cls.from_dict(raw, source_path=path)

    @classmethod
    def from_dict(
        cls, raw: dict[str, Any], source_path: Path | None = None
    ) -> "SecurityMaster":
        if int(raw.get("schema_version", 0)) != 1:
            raise ValueError("security master schema_version must be 1")
        instruments = tuple(_parse_instrument(value) for value in raw.get("instruments", []))
        universes = {
            name: tuple(_parse_membership(value) for value in values)
            for name, values in dict(raw.get("universes", {})).items()
        }
        status_periods = tuple(
            _parse_status(value) for value in raw.get("status_periods", [])
        )
        metadata = {
            "selection_method": raw.get("selection_method", "unspecified"),
            "provenance": raw.get("provenance", "unspecified"),
            "as_of": raw.get("as_of"),
        }
        return cls(instruments, universes, status_periods, metadata, source_path)

    @classmethod
    def from_legacy(cls, values: Iterable[dict[str, Any]]) -> "SecurityMaster":
        instruments = tuple(_parse_instrument(value) for value in values)
        memberships = tuple(
            UniverseMembership(item.symbol, item.listing_date or date.min, item.delisting_date)
            for item in instruments
        )
        return cls(
            instruments,
            {"legacy": memberships},
            metadata={
                "selection_method": "legacy_static_config",
                "provenance": "config.universe",
                "as_of": None,
            },
        )

    def required_instruments(self, universe: str, benchmark: str) -> tuple[Instrument, ...]:
        symbols = {item.symbol for item in self._memberships(universe)} | {benchmark}
        missing = sorted(symbol for symbol in symbols if symbol not in self.instruments)
        if missing:
            raise ValueError(f"Security master is missing required symbols: {missing}")
        return tuple(self.instruments[symbol] for symbol in sorted(symbols))

    def active_symbols(
        self,
        universe: str,
        on_date: date,
        minimum_listing_days: int = 0,
    ) -> tuple[str, ...]:
        result = []
        seen: set[str] = set()
        for membership in self._memberships(universe):
            if membership.symbol in seen:
                continue
            if not self.eligibility_reasons(
                universe, membership.symbol, on_date, minimum_listing_days
            ):
                result.append(membership.symbol)
                seen.add(membership.symbol)
        return tuple(result)

    def eligibility_reasons(
        self,
        universe: str,
        symbol: str,
        on_date: date,
        minimum_listing_days: int = 0,
    ) -> tuple[str, ...]:
        instrument = self.instruments.get(symbol)
        if instrument is None:
            return ("unknown_symbol",)
        reasons: list[str] = []
        if instrument.listing_date and on_date < instrument.listing_date:
            reasons.append("not_yet_listed")
        if instrument.delisting_date and on_date > instrument.delisting_date:
            reasons.append("delisted")
        memberships = [
            value
            for value in self._memberships(universe)
            if value.symbol == symbol and value.contains(on_date)
        ]
        if not memberships:
            reasons.append("outside_universe_membership")
        if (
            minimum_listing_days > 0
            and instrument.listing_date
            and on_date < instrument.listing_date + timedelta(days=minimum_listing_days)
        ):
            reasons.append("listing_seasoning")
        return tuple(reasons)

    def trading_status(self, symbol: str, on_date: date) -> TradingStatus:
        instrument = self.instruments[symbol]
        matches = [
            value
            for value in self.status_periods
            if value.symbol == symbol and value.contains(on_date)
        ]
        if not matches:
            return TradingStatus("normal", True, instrument.price_limit_pct)
        value = matches[-1]
        limit = (
            value.price_limit_pct
            if value.price_limit_pct is not None
            else instrument.price_limit_pct
        )
        return TradingStatus(value.status, value.tradable, limit)

    def snapshot(self, universe: str, on_date: date, minimum_listing_days: int) -> dict[str, Any]:
        members = {item.symbol for item in self._memberships(universe)}
        rows = []
        for symbol in sorted(members):
            instrument = self.instruments[symbol]
            reasons = self.eligibility_reasons(
                universe, symbol, on_date, minimum_listing_days
            )
            status = self.trading_status(symbol, on_date)
            rows.append(
                {
                    "symbol": symbol,
                    "name": instrument.name,
                    "instrument_type": instrument.instrument_type,
                    "asset_class": instrument.asset_class,
                    "sector": instrument.sector,
                    "listing_date": (
                        instrument.listing_date.isoformat() if instrument.listing_date else None
                    ),
                    "delisting_date": (
                        instrument.delisting_date.isoformat() if instrument.delisting_date else None
                    ),
                    "active": not reasons,
                    "eligibility_reasons": list(reasons),
                    "trading_status": status.status,
                    "tradable": status.tradable,
                }
            )
        return {
            "universe": universe,
            "date": on_date.isoformat(),
            "candidate_records": len(rows),
            "active_symbols": sum(bool(row["active"]) for row in rows),
            "minimum_listing_days": minimum_listing_days,
            "selection_method": self.metadata.get("selection_method"),
            "provenance": self.metadata.get("provenance"),
            "master_sha256": self.fingerprint(),
            "instruments": rows,
        }

    def fingerprint(self) -> str:
        payload = {
            "instruments": [_instrument_payload(value) for value in self.instruments.values()],
            "universes": {
                key: [_dated_payload(value) for value in values]
                for key, values in sorted(self.universes.items())
            },
            "status_periods": [_dated_payload(value) for value in self.status_periods],
            "metadata": self.metadata,
        }
        encoded = json.dumps(
            payload, ensure_ascii=True, sort_keys=True, separators=(",", ":")
        ).encode("ascii")
        return sha256(encoded).hexdigest()

    def _memberships(self, universe: str) -> tuple[UniverseMembership, ...]:
        if universe not in self.universes:
            raise ValueError(f"Unknown universe in security master: {universe!r}")
        return self.universes[universe]

    def _validate(self) -> None:
        if not self.instruments:
            raise ValueError("Security master must contain at least one instrument")
        if not self.universes:
            raise ValueError("Security master must define at least one universe")
        for name, memberships in self.universes.items():
            if not memberships:
                raise ValueError(f"Universe {name!r} must not be empty")
            for value in memberships:
                if value.symbol not in self.instruments:
                    raise ValueError(
                        f"Universe {name!r} references unknown symbol {value.symbol!r}"
                    )
                if value.end and value.end < value.start:
                    raise ValueError(f"Membership end precedes start for {value.symbol}")
            _validate_non_overlapping_periods(
                memberships, f"universe {name!r} membership"
            )
        for value in self.status_periods:
            if value.symbol not in self.instruments:
                raise ValueError(f"Status period references unknown symbol {value.symbol!r}")
            if value.end and value.end < value.start:
                raise ValueError(f"Status end precedes start for {value.symbol}")
            if value.price_limit_pct is not None and not 0 < value.price_limit_pct < 1:
                raise ValueError(f"Invalid status price limit for {value.symbol}")
        _validate_non_overlapping_periods(self.status_periods, "trading status")


def _parse_instrument(raw: dict[str, Any]) -> Instrument:
    value = dict(raw)
    for key in ("listing_date", "delisting_date"):
        if value.get(key):
            value[key] = date.fromisoformat(str(value[key]))
        else:
            value[key] = None
    return Instrument(**value)


def _parse_membership(raw: dict[str, Any]) -> UniverseMembership:
    return UniverseMembership(
        symbol=str(raw["symbol"]),
        start=date.fromisoformat(str(raw["start"])),
        end=date.fromisoformat(str(raw["end"])) if raw.get("end") else None,
    )


def _parse_status(raw: dict[str, Any]) -> TradingStatusPeriod:
    tradable = raw.get("tradable", True)
    if not isinstance(tradable, bool):
        raise ValueError("status_periods.tradable must be a JSON boolean")
    return TradingStatusPeriod(
        symbol=str(raw["symbol"]),
        start=date.fromisoformat(str(raw["start"])),
        end=date.fromisoformat(str(raw["end"])) if raw.get("end") else None,
        status=str(raw.get("status", "restricted")),
        tradable=tradable,
        price_limit_pct=(
            float(raw["price_limit_pct"])
            if raw.get("price_limit_pct") is not None
            else None
        ),
    )


def _instrument_payload(value: Instrument) -> dict[str, Any]:
    payload = asdict(value)
    for key in ("listing_date", "delisting_date"):
        if payload[key] is not None:
            payload[key] = payload[key].isoformat()
    return payload


def _dated_payload(value: Any) -> dict[str, Any]:
    payload = asdict(value)
    for key in ("start", "end"):
        if payload.get(key) is not None:
            payload[key] = payload[key].isoformat()
    return payload


def _validate_non_overlapping_periods(values: Iterable[Any], label: str) -> None:
    grouped: dict[str, list[Any]] = {}
    for value in values:
        grouped.setdefault(value.symbol, []).append(value)
    for symbol, periods in grouped.items():
        ordered = sorted(periods, key=lambda value: value.start)
        for previous, current in zip(ordered, ordered[1:]):
            if previous.end is None or current.start <= previous.end:
                raise ValueError(f"Overlapping {label} periods for {symbol}")
