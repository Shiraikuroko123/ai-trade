from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from typing import Any

from ..config import AppConfig
from ..feature_store.schema import json_fingerprint


@dataclass(frozen=True)
class TradeCostEstimate:
    symbol: str
    side: str
    notional: float
    commission: float
    slippage: float
    stamp_duty: float
    transfer_fee: float

    @property
    def total(self) -> float:
        return self.commission + self.slippage + self.stamp_duty + self.transfer_fee

    @property
    def marginal_bps(self) -> float:
        return self.total / self.notional * 10_000.0 if self.notional > 0 else 0.0

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["total"] = self.total
        result["marginal_bps"] = self.marginal_bps
        return result


class TransactionCostModel:
    """Use the same dated China-market fee schedule as execution."""

    def __init__(self, config: AppConfig):
        self.config = config

    def estimate(
        self,
        symbol: str,
        *,
        on_date: date,
        current_weight: float,
        target_weight: float,
        equity: float,
    ) -> TradeCostEstimate:
        delta = target_weight - current_weight
        side = "BUY" if delta > 0 else "SELL" if delta < 0 else "HOLD"
        notional = abs(delta) * equity
        if notional <= 0:
            return TradeCostEstimate(symbol, side, 0.0, 0.0, 0.0, 0.0, 0.0)
        try:
            instrument = self.config.security_master.instruments[symbol]
        except KeyError as exc:
            raise ValueError(f"Unknown cost-model symbol: {symbol}") from exc
        schedule = self.config.costs.for_instrument(instrument, on_date)
        commission = max(
            schedule.minimum_commission,
            notional * schedule.commission_bps / 10_000.0,
        )
        slippage = notional * schedule.slippage_bps / 10_000.0
        stamp_duty = (
            notional * schedule.sell_stamp_duty_bps / 10_000.0
            if side == "SELL"
            else 0.0
        )
        transfer_fee = notional * schedule.transfer_fee_bps / 10_000.0
        return TradeCostEstimate(
            symbol,
            side,
            notional,
            commission,
            slippage,
            stamp_duty,
            transfer_fee,
        )

    def assumptions(self, on_date: date) -> dict[str, Any]:
        schedules = {}
        for symbol, instrument in sorted(self.config.security_master.instruments.items()):
            schedule = self.config.costs.for_instrument(instrument, on_date)
            schedules[symbol] = {
                "commission_bps": schedule.commission_bps,
                "slippage_bps": schedule.slippage_bps,
                "minimum_commission": schedule.minimum_commission,
                "sell_stamp_duty_bps": schedule.sell_stamp_duty_bps,
                "transfer_fee_bps": schedule.transfer_fee_bps,
            }
        payload = {"on_date": on_date.isoformat(), "schedules": schedules}
        return {**payload, "fingerprint": json_fingerprint(payload)}


__all__ = ["TradeCostEstimate", "TransactionCostModel"]
