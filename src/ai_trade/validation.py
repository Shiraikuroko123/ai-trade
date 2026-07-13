from __future__ import annotations

import json
import math
import random
import statistics
from dataclasses import replace
from datetime import date
from pathlib import Path

from .backtest import BacktestEngine
from .config import AppConfig
from .data.market import MarketData
from .metrics import calculate_metrics
from .models import BacktestResult, EquityPoint


def run_robustness_validation(
    config: AppConfig,
    market: MarketData,
    bootstrap_samples: int = 1000,
    block_days: int = 20,
) -> dict[str, object]:
    if bootstrap_samples < 100:
        raise ValueError("bootstrap_samples must be at least 100")
    if block_days < 2:
        raise ValueError("block_days must be at least 2")

    baseline = BacktestEngine(config, market).run()
    bootstrap = moving_block_bootstrap(
        baseline.equity_curve,
        samples=bootstrap_samples,
        block_days=block_days,
    )
    cost_stress = _cost_stress(config, market, baseline)
    sensitivity = _parameter_sensitivity(config, market)
    regimes = _regime_stress(baseline)
    gates = _research_gates(bootstrap, cost_stress, sensitivity, regimes)
    return {
        "methodology": (
            "Independent rewrite inspired by Vibe-Trading validation concepts. "
            "Moving-block bootstrap preserves short-horizon dependence; cost, parameter, "
            "and historical-regime tests are diagnostic and are not profit guarantees."
        ),
        "selection_disclosure": (
            "The current liquidity threshold and risk-model defaults were compared on the "
            "available historical and walk-forward results. Those results are now development "
            "evidence, not an untouched holdout; future paper sessions are the next independent test."
        ),
        "data_snapshot": market.snapshot_metadata(),
        "baseline": baseline.metrics,
        "bootstrap": bootstrap,
        "cost_stress": cost_stress,
        "parameter_sensitivity": sensitivity,
        "regime_stress": regimes,
        "research_gates": gates,
    }


def moving_block_bootstrap(
    curve: list[EquityPoint],
    samples: int = 1000,
    block_days: int = 20,
    seed: int = 42,
) -> dict[str, object]:
    returns = [
        curve[index].equity / curve[index - 1].equity - 1.0
        for index in range(1, len(curve))
        if curve[index - 1].equity > 0
    ]
    if len(returns) < max(20, block_days):
        raise ValueError("Not enough returns for moving-block bootstrap")
    rng = random.Random(seed)
    sharpes: list[float] = []
    cagrs: list[float] = []
    drawdowns: list[float] = []
    for _ in range(samples):
        sampled: list[float] = []
        while len(sampled) < len(returns):
            start = rng.randrange(0, len(returns) - block_days + 1)
            sampled.extend(returns[start : start + block_days])
        sampled = sampled[: len(returns)]
        sharpes.append(_sharpe(sampled))
        cagrs.append(_annualized_return(sampled))
        drawdowns.append(_max_drawdown(sampled))

    return {
        "samples": samples,
        "block_days": block_days,
        "seed": seed,
        "observed_sharpe": _sharpe(returns),
        "sharpe_ci_95": [_percentile(sharpes, 0.025), _percentile(sharpes, 0.975)],
        "cagr_ci_95": [_percentile(cagrs, 0.025), _percentile(cagrs, 0.975)],
        "max_drawdown_median": statistics.median(drawdowns),
        "max_drawdown_5pct_worst": _percentile(drawdowns, 0.05),
        "probability_sharpe_positive": sum(value > 0 for value in sharpes) / samples,
        "probability_cagr_positive": sum(value > 0 for value in cagrs) / samples,
    }


def save_validation_report(report: dict[str, object], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "validation_report.json"
    markdown_path = output_dir / "validation_report.md"
    temporary = json_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(json_path)

    baseline = report["baseline"]
    bootstrap = report["bootstrap"]
    gates = report["research_gates"]
    lines = [
        "# AI Trade 稳健性验证",
        "",
        "该报告用于寻找策略失效证据，不构成收益承诺或实盘许可。",
        "当前流动性阈值和风险模型已参考这些历史结果，样本外数据因此也属于开发证据；下一份独立证据只能来自未来模拟盘。",
        "",
        f"- 数据截止：{report['data_snapshot']['latest_common_session']}",
        f"- 基准年化收益：{baseline['cagr']:.2%}",
        f"- 基准 Sharpe：{baseline['sharpe']:.2f}",
        f"- 基准最大回撤：{baseline['max_drawdown']:.2%}",
        f"- 研究状态：{gates['status']}",
        "- 实盘就绪：否",
        "",
        "## 移动区块自助法",
        "",
        f"- 样本数：{bootstrap['samples']}，区块长度：{bootstrap['block_days']} 个交易日",
        f"- Sharpe 95% 区间：{bootstrap['sharpe_ci_95'][0]:.2f} 至 {bootstrap['sharpe_ci_95'][1]:.2f}",
        f"- 年化收益 95% 区间：{bootstrap['cagr_ci_95'][0]:.2%} 至 {bootstrap['cagr_ci_95'][1]:.2%}",
        f"- 5% 尾部最大回撤：{bootstrap['max_drawdown_5pct_worst']:.2%}",
        f"- 年化收益为正概率：{bootstrap['probability_cagr_positive']:.1%}",
        "",
        "## 成本压力",
        "",
        "| 成本倍数 | 年化收益 | Sharpe | 最大回撤 | 成交成本 |",
        "|---:|---:|---:|---:|---:|",
    ]
    for row in report["cost_stress"]:
        lines.append(
            f"| {row['multiplier']:.0f}x | {row['cagr']:.2%} | {row['sharpe']:.2f} | "
            f"{row['max_drawdown']:.2%} | {row['commissions']:,.0f} |"
        )
    lines.extend(
        [
            "",
            "## 参数邻域",
            "",
            f"- 组合数：{report['parameter_sensitivity']['variants']}",
            f"- 年化收益为正比例：{report['parameter_sensitivity']['positive_cagr_ratio']:.1%}",
            f"- 年化收益范围：{report['parameter_sensitivity']['min_cagr']:.2%} 至 {report['parameter_sensitivity']['max_cagr']:.2%}",
            f"- Sharpe 中位数：{report['parameter_sensitivity']['median_sharpe']:.2f}",
            "",
            "## 历史压力区间",
            "",
            "| 区间 | 策略收益 | 基准收益 | 超额收益 | 最大回撤 |",
            "|---|---:|---:|---:|---:|",
        ]
    )
    for row in report["regime_stress"]:
        lines.append(
            f"| {row['name']} | {row['strategy_return']:.2%} | {row['benchmark_return']:.2%} | "
            f"{row['excess_return']:.2%} | {row['strategy_max_drawdown']:.2%} |"
        )
    lines.extend(["", "## 研究门槛", ""])
    for name, passed in gates["checks"].items():
        lines.append(f"- {name}: {'通过' if passed else '未通过'}")
    lines.extend(
        [
            "",
            "即使所有门槛通过，也只能说明历史证据相对稳定；仍需长期模拟盘、真实成交对账和独立数据源复核。",
        ]
    )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path


def _cost_stress(
    config: AppConfig,
    market: MarketData,
    baseline: BacktestResult,
) -> list[dict[str, float]]:
    results = []
    for multiplier in (1.0, 2.0, 3.0):
        if multiplier == 1.0:
            metrics = baseline.metrics
        else:
            stressed_costs = replace(
                config.costs,
                commission_bps=config.costs.commission_bps * multiplier,
                slippage_bps=config.costs.slippage_bps * multiplier,
                minimum_commission=config.costs.minimum_commission * multiplier,
            )
            metrics = BacktestEngine(replace(config, costs=stressed_costs), market).run().metrics
        results.append(
            {
                "multiplier": multiplier,
                "total_return": metrics["total_return"],
                "cagr": metrics["cagr"],
                "sharpe": metrics["sharpe"],
                "max_drawdown": metrics["max_drawdown"],
                "turnover": metrics["turnover"],
                "commissions": metrics["commissions"],
            }
        )
    return results


def _parameter_sensitivity(config: AppConfig, market: MarketData) -> dict[str, object]:
    base = config.strategy
    candidates = [
        {"lookback_days": max(21, base.lookback_days - 63)},
        {"lookback_days": base.lookback_days},
        {"lookback_days": base.lookback_days + 63},
        {"rebalance_days": max(5, base.rebalance_days // 2)},
        {"rebalance_days": base.rebalance_days},
        {"rebalance_days": base.rebalance_days * 2},
        {"top_n": max(1, base.top_n - 1)},
        {"top_n": base.top_n},
        {"top_n": min(len(config.instruments), base.top_n + 1)},
    ]
    rows = []
    seen: set[tuple[tuple[str, int], ...]] = set()
    for changes in candidates:
        key = tuple(sorted(changes.items()))
        if key in seen:
            continue
        seen.add(key)
        result = BacktestEngine(config, market).with_settings(**changes).run()
        rows.append(
            {
                "changes": changes,
                "cagr": result.metrics["cagr"],
                "sharpe": result.metrics["sharpe"],
                "max_drawdown": result.metrics["max_drawdown"],
                "turnover": result.metrics["turnover"],
            }
        )
    cagrs = [row["cagr"] for row in rows]
    sharpes = [row["sharpe"] for row in rows]
    return {
        "variants": len(rows),
        "positive_cagr_ratio": sum(value > 0 for value in cagrs) / len(cagrs),
        "min_cagr": min(cagrs),
        "median_cagr": statistics.median(cagrs),
        "max_cagr": max(cagrs),
        "median_sharpe": statistics.median(sharpes),
        "sharpe_std": statistics.pstdev(sharpes),
        "results": rows,
    }


def _regime_stress(result: BacktestResult) -> list[dict[str, object]]:
    periods = [
        ("2018 去杠杆与贸易摩擦", date(2018, 1, 1), date(2018, 12, 31)),
        ("2020 疫情冲击", date(2020, 1, 1), date(2020, 6, 30)),
        ("2022 全球紧缩", date(2022, 1, 1), date(2022, 12, 31)),
        ("2024 至今", date(2024, 1, 1), date.fromisoformat(result.metadata["end"])),
    ]
    rows = []
    for name, start, end in periods:
        strategy_curve = _slice_curve(result.equity_curve, start, end)
        benchmark_curve = _slice_curve(result.benchmark_curve, start, end)
        if len(strategy_curve) < 20 or len(benchmark_curve) < 20:
            continue
        trades = [trade for trade in result.trades if start <= trade.date <= end]
        strategy = calculate_metrics(strategy_curve, trades)
        benchmark = calculate_metrics(benchmark_curve)
        rows.append(
            {
                "name": name,
                "start": strategy_curve[0].date.isoformat(),
                "end": strategy_curve[-1].date.isoformat(),
                "strategy_return": strategy["total_return"],
                "benchmark_return": benchmark["total_return"],
                "excess_return": strategy["total_return"] - benchmark["total_return"],
                "strategy_sharpe": strategy["sharpe"],
                "strategy_max_drawdown": strategy["max_drawdown"],
            }
        )
    return rows


def _research_gates(
    bootstrap: dict[str, object],
    cost_stress: list[dict[str, float]],
    sensitivity: dict[str, object],
    regimes: list[dict[str, object]],
) -> dict[str, object]:
    checks = {
        "Bootstrap Sharpe 下界大于 0": float(bootstrap["sharpe_ci_95"][0]) > 0,
        "三倍成本下年化收益大于 0": cost_stress[-1]["cagr"] > 0,
        "至少 75% 参数邻域年化收益为正": float(sensitivity["positive_cagr_ratio"]) >= 0.75,
        "至少一半压力区间取得正超额": bool(regimes)
        and sum(float(row["excess_return"]) > 0 for row in regimes) >= math.ceil(len(regimes) / 2),
    }
    passed = sum(checks.values())
    status = (
        "相对稳健但已用于模型选择，等待未来验证"
        if passed == len(checks)
        else "存在脆弱项，继续模拟验证"
    )
    return {
        "status": status,
        "passed": passed,
        "total": len(checks),
        "checks": checks,
        "live_ready": False,
    }


def _slice_curve(curve: list[EquityPoint], start: date, end: date) -> list[EquityPoint]:
    selected = [point for point in curve if start <= point.date <= end]
    if not selected:
        return []
    high_water = selected[0].equity
    result = []
    for point in selected:
        high_water = max(high_water, point.equity)
        drawdown = point.equity / high_water - 1.0 if high_water > 0 else 0.0
        result.append(EquityPoint(point.date, point.equity, point.cash, drawdown))
    return result


def _sharpe(returns: list[float]) -> float:
    if len(returns) < 2:
        return 0.0
    deviation = statistics.pstdev(returns)
    return statistics.fmean(returns) / deviation * math.sqrt(252) if deviation > 0 else 0.0


def _annualized_return(returns: list[float]) -> float:
    growth = 1.0
    for value in returns:
        growth *= 1.0 + value
    if growth <= 0:
        return -1.0
    return growth ** (252.0 / len(returns)) - 1.0


def _max_drawdown(returns: list[float]) -> float:
    equity = 1.0
    peak = 1.0
    worst = 0.0
    for value in returns:
        equity *= 1.0 + value
        peak = max(peak, equity)
        worst = min(worst, equity / peak - 1.0)
    return worst


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction
