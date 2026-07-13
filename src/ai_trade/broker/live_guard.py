from __future__ import annotations

import os


LIVE_CONFIRMATION = "I_ACCEPT_LIVE_TRADING_RISK"


def require_live_confirmation() -> None:
    if os.environ.get("AI_TRADE_LIVE_CONFIRMATION") != LIVE_CONFIRMATION:
        raise RuntimeError(
            "Live trading is disabled. Set AI_TRADE_LIVE_CONFIRMATION only after paper validation, "
            "broker review, and an explicit user confirmation."
        )


class BrokerNotConfigured(RuntimeError):
    pass
