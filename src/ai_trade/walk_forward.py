from __future__ import annotations

import itertools
import json
import math
from dataclasses import replace
from datetime import date
from pathlib import Path

from .backtest import BacktestEngine
from .config import AppConfig
from .data.market import MarketData
from .metrics import calculate_metrics


def run_walk_forward(
    config: AppConfig,
    market: MarketData,
    train_days: int = 756,
    test_days: int = 252,
) -> dict[str, object]:
    start = date.fromisoformat(config.raw["backtest"]["start"])
    end = date.fromisoformat(config.raw["backtest"]["end"])
    calendar = [day for day in market.calendar if start <= day <= end]
    if len(calendar) <= train_days + 20:
        raise ValueError("Not enough history for walk-forward validation")

    candidates = list(
        itertools.product(
            [63, 126, 189],
            [100, 200],
            [1, 2, 3],
        )
    )
    segments: list[dict[str, object]] = []
    cursor = train_days
    while cursor < len(calendar) - 20:
        train_start = calendar[cursor - train_days]
        train_end = calendar[cursor - 1]
        test_start = calendar[cursor]
        test_end = calendar[min(cursor + test_days - 1, len(calendar) - 1)]
        best_score = -math.inf
        best_params: dict[str, int] | None = None
        best_train = None

        for lookback, trend, top_n in candidates:
            engine = BacktestEngine(config, market).with_settings(
                lookback_days=lookback,
                trend_sma_days=trend,
                top_n=top_n,
            )
            result = engine.run(train_start, train_end)
            score = result.metrics["sharpe"] + 0.20 * result.metrics["calmar"]
            if result.metrics["invested_days"] < 20:
                score -= 1.0
            if score > best_score:
                best_score = score
                best_params = {"lookback_days": lookback, "trend_sma_days": trend, "top_n": top_n}
                best_train = result.metrics

        if best_params is None or best_train is None:
            break
        segments.append(
            {
                "train_start": train_start.isoformat(),
                "train_end": train_end.isoformat(),
                "test_start": test_start.isoformat(),
                "test_end": test_end.isoformat(),
                "selected": best_params,
                "train_metrics": best_train,
            }
        )
        cursor += test_days

    if not segments:
        raise ValueError("Walk-forward validation produced no out-of-sample segments")

    settings_schedule = {
        date.fromisoformat(str(segment["test_start"])): replace(
            config.strategy, **segment["selected"]
        )
        for segment in segments
    }
    continuous = BacktestEngine(config, market).run_scheduled(
        date.fromisoformat(str(segments[0]["test_start"])),
        date.fromisoformat(str(segments[-1]["test_end"])),
        settings_schedule,
    )

    for segment in segments:
        segment_start = date.fromisoformat(str(segment["test_start"]))
        segment_end = date.fromisoformat(str(segment["test_end"]))
        curve = [
            point
            for point in continuous.equity_curve
            if segment_start <= point.date <= segment_end
        ]
        trades = [
            trade
            for trade in continuous.trades
            if segment_start <= trade.date <= segment_end
        ]
        segment["test_metrics"] = calculate_metrics(curve, trades)

    aggregate = {
        "segments": len(segments),
        "oos_total_return": continuous.metrics["total_return"],
        "oos_cagr": continuous.metrics["cagr"],
        "oos_sharpe": continuous.metrics["sharpe"],
        "oos_max_drawdown": continuous.metrics["max_drawdown"],
        "oos_turnover": continuous.metrics["turnover"],
        "oos_commissions": continuous.metrics["commissions"],
        "oos_transaction_costs": continuous.metrics["transaction_costs"],
        "benchmark_total_return": continuous.benchmark_metrics["total_return"],
        "benchmark_sharpe": continuous.benchmark_metrics["sharpe"],
        "positive_segments": sum(
            1 for segment in segments if float(segment["test_metrics"]["total_return"]) > 0
        ),
    }
    return {
        "methodology": (
            "Continuous out-of-sample account; holdings, costs, high-water mark, and risk "
            "cooldown persist across parameter-selection boundaries."
        ),
        "selection_disclosure": (
            "Current defaults were compared using these historical windows. Treat this as a "
            "development walk-forward report, not a pristine final holdout."
        ),
        "data_snapshot": market.snapshot_metadata(),
        "aggregate": aggregate,
        "segments": segments,
    }


def save_walk_forward(report: dict[str, object], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "walk_forward.json"
    md_path = output_dir / "walk_forward.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    aggregate = report["aggregate"]
    lines = [
        "# Walk-forward validation",
        "",
        f"{report['methodology']}",
        f"{report['selection_disclosure']}",
        "",
        f"- Latest completed session: {report['data_snapshot']['latest_common_session']}",
        f"- OOS segments: {aggregate['segments']}",
        f"- OOS total return: {aggregate['oos_total_return']:.2%}",
        f"- OOS CAGR: {aggregate['oos_cagr']:.2%}",
        f"- OOS Sharpe: {aggregate['oos_sharpe']:.2f}",
        f"- OOS max drawdown: {aggregate['oos_max_drawdown']:.2%}",
        f"- Benchmark total return: {aggregate['benchmark_total_return']:.2%}",
        f"- Positive segments: {aggregate['positive_segments']}",
        "",
        "| Test period | Parameters | Return | Sharpe | Max drawdown |",
        "|---|---|---:|---:|---:|",
    ]
    for segment in report["segments"]:
        metrics = segment["test_metrics"]
        selected = segment["selected"]
        params = f"LB {selected['lookback_days']}, SMA {selected['trend_sma_days']}, Top {selected['top_n']}"
        lines.append(
            f"| {segment['test_start']} to {segment['test_end']} | {params} | "
            f"{metrics['total_return']:.2%} | {metrics['sharpe']:.2f} | {metrics['max_drawdown']:.2%} |"
        )
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path
