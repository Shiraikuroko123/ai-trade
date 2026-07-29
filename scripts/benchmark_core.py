"""Measure deterministic local compute paths without writing research evidence."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from datetime import datetime, timezone
from time import perf_counter
from typing import Any, Callable

from ai_trade.backtest import BacktestEngine
from ai_trade.config import load_config
from ai_trade.data.market import MarketData
from ai_trade.validation import run_robustness_validation
from ai_trade.walk_forward import run_walk_forward


def _measure(action: Callable[[], Any]) -> tuple[Any, float]:
    started = perf_counter()
    result = action()
    return result, perf_counter() - started


def _fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=str,
    ).encode("ascii")
    return hashlib.sha256(encoded).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Benchmark verified local market, backtest, and validation kernels"
    )
    parser.add_argument("--config", default="config/default.json")
    parser.add_argument("--train-days", type=int, default=756)
    parser.add_argument("--test-days", type=int, default=252)
    parser.add_argument("--bootstrap-samples", type=int, default=1000)
    parser.add_argument("--block-days", type=int, default=20)
    args = parser.parse_args()

    config = load_config(args.config)
    market, market_seconds = _measure(lambda: MarketData(config))
    backtest, backtest_seconds = _measure(
        lambda: BacktestEngine(config, market).run()
    )
    walk_forward, walk_forward_seconds = _measure(
        lambda: run_walk_forward(
            config,
            market,
            train_days=args.train_days,
            test_days=args.test_days,
        )
    )
    validation, validation_seconds = _measure(
        lambda: run_robustness_validation(
            config,
            market,
            bootstrap_samples=args.bootstrap_samples,
            block_days=args.block_days,
        )
    )

    payload = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime": {
            "python": platform.python_version(),
            "implementation": platform.python_implementation(),
            "platform": platform.platform(),
        },
        "data": {
            "symbols": len(market.symbols),
            "calendar_sessions": len(market.calendar),
            "latest_common_session": market.latest_common_session.isoformat(),
            "manifest_sha256": market.manifest_sha256,
        },
        "parameters": {
            "train_days": args.train_days,
            "test_days": args.test_days,
            "bootstrap_samples": args.bootstrap_samples,
            "block_days": args.block_days,
        },
        "seconds": {
            "market_load": round(market_seconds, 6),
            "backtest": round(backtest_seconds, 6),
            "walk_forward": round(walk_forward_seconds, 6),
            "validation": round(validation_seconds, 6),
            "total": round(
                market_seconds
                + backtest_seconds
                + walk_forward_seconds
                + validation_seconds,
                6,
            ),
        },
        "result_fingerprints": {
            "backtest_metrics": _fingerprint(backtest.metrics),
            "walk_forward_aggregate": _fingerprint(walk_forward["aggregate"]),
            "validation": _fingerprint(
                {
                    "baseline": validation["baseline"],
                    "bootstrap": validation["bootstrap"],
                    "research_gates": validation["research_gates"],
                }
            ),
        },
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
