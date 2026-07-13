from __future__ import annotations

import csv
import html
import json
from pathlib import Path

from .models import BacktestResult


def save_backtest_report(result: BacktestResult, output_dir: Path) -> dict[str, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "summary": output_dir / "backtest_summary.json",
        "equity": output_dir / "equity_curve.csv",
        "trades": output_dir / "trades.csv",
        "signal": output_dir / "latest_signal.json",
        "html": output_dir / "backtest_report.html",
    }
    summary = {
        "metadata": result.metadata,
        "strategy_metrics": result.metrics,
        "benchmark_metrics": result.benchmark_metrics,
    }
    paths["summary"].write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")

    benchmark_by_date = {point.date: point for point in result.benchmark_curve}
    with paths["equity"].open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "strategy_equity", "cash", "drawdown", "benchmark_equity"])
        for point in result.equity_curve:
            benchmark = benchmark_by_date.get(point.date)
            writer.writerow(
                [
                    point.date.isoformat(),
                    f"{point.equity:.4f}",
                    f"{point.cash:.4f}",
                    f"{point.drawdown:.8f}",
                    f"{benchmark.equity:.4f}" if benchmark else "",
                ]
            )

    with paths["trades"].open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(
            [
                "date",
                "symbol",
                "side",
                "quantity",
                "price",
                "notional",
                "commission",
                "stamp_duty",
                "transfer_fee",
                "slippage_cost",
                "reason",
            ]
        )
        for trade in result.trades:
            writer.writerow(
                [
                    trade.date.isoformat(),
                    trade.symbol,
                    trade.side,
                    trade.quantity,
                    f"{trade.price:.6f}",
                    f"{trade.notional:.2f}",
                    f"{trade.commission:.2f}",
                    f"{trade.stamp_duty:.2f}",
                    f"{trade.transfer_fee:.2f}",
                    f"{trade.slippage_cost:.2f}",
                    trade.reason,
                ]
            )

    signal = result.latest_signal
    signal_payload = None
    if signal:
        signal_payload = {
            "date": signal.date.isoformat(),
            "target_weights": signal.target_weights,
            "reason": signal.reason,
            "ranking": [item.__dict__ for item in signal.ranked],
        }
    paths["signal"].write_text(json.dumps(signal_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    paths["html"].write_text(_build_html(result), encoding="utf-8")
    return paths


def _build_html(result: BacktestResult) -> str:
    strategy_values = [point.equity for point in result.equity_curve]
    benchmark_values = [point.equity for point in result.benchmark_curve]
    all_values = strategy_values + benchmark_values
    common_low = min(all_values) if all_values else 0.0
    common_high = max(all_values) if all_values else 1.0
    strategy_line = _svg_polyline(strategy_values, 900, 260, common_low, common_high)
    benchmark_line = _svg_polyline(benchmark_values, 900, 260, common_low, common_high)
    rows = "".join(
        f"<tr><td>{html.escape(_label(key))}</td><td>{_format_metric(key, value)}</td>"
        f"<td>{_format_metric(key, result.benchmark_metrics.get(key, 0.0))}</td></tr>"
        for key, value in result.metrics.items()
        if key in {
            "total_return", "cagr", "annual_volatility", "sharpe", "sortino",
            "max_drawdown", "calmar", "value_at_risk_95", "expected_shortfall_95",
            "worst_day", "monthly_win_ratio", "longest_drawdown_sessions", "turnover",
            "commissions",
            "stamp_duty",
            "transfer_fees",
            "slippage_cost",
            "transaction_costs",
        }
    )
    signal_html = "<p>No current position passed the filters.</p>"
    if result.latest_signal:
        signal_rows = "".join(
            f"<tr><td>{html.escape(item.symbol)}</td><td>{html.escape(item.name)}</td>"
            f"<td>{item.momentum:.2%}</td><td>{item.annual_volatility:.2%}</td>"
            f"<td>{item.average_amount / 1_000_000:.1f}m</td>"
            f"<td>{'Yes' if item.above_trend else 'No'}</td><td>{item.weight:.2%}</td></tr>"
            for item in result.latest_signal.ranked
        )
        signal_html = (
            f"<p>{html.escape(result.latest_signal.reason)}</p><table><thead><tr><th>Symbol</th><th>Name</th>"
            "<th>Momentum</th><th>Volatility</th><th>Avg amount</th><th>Above trend</th><th>Weight</th></tr></thead>"
            f"<tbody>{signal_rows}</tbody></table>"
        )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>AI Trade backtest report</title>
<style>
body{{font-family:Arial,"Microsoft YaHei",sans-serif;margin:32px;color:#172033;background:#f7f8fa}}
main{{max-width:1080px;margin:auto}} h1{{font-size:28px}} h2{{margin-top:32px}}
.notice{{padding:12px 16px;background:#fff4d6;border-left:4px solid #d99a00}}
.chart,.panel{{background:#fff;border:1px solid #dfe3e8;padding:18px;margin-top:16px}}
table{{border-collapse:collapse;width:100%;background:#fff}} th,td{{padding:9px;border-bottom:1px solid #e5e7eb;text-align:right}}
th:first-child,td:first-child,th:nth-child(2),td:nth-child(2){{text-align:left}} .legend{{display:flex;gap:20px;font-size:13px}}
.blue{{color:#0b6fa4}} .gray{{color:#8a94a3}}
</style></head><body><main>
<h1>AI Trade backtest report</h1>
<p class="notice">Research and paper-trading output only. Historical performance does not guarantee future profit. The current universe has survivorship bias, and forward-adjusted prices are a research approximation rather than point-in-time executable prices.</p>
<div class="panel"><strong>Period:</strong> {result.metadata['start']} to {result.metadata['end']} &nbsp; <strong>Initial cash:</strong> {result.metadata['initial_cash']:,.0f} &nbsp; <strong>Latest completed session:</strong> {result.metadata['data_snapshot']['latest_common_session']}</div>
<h2>Equity curve</h2><div class="chart"><div class="legend"><span class="blue">Strategy</span><span class="gray">Benchmark</span></div>
<svg viewBox="0 0 900 260" width="100%" role="img" aria-label="Equity curve"><polyline points="{benchmark_line}" fill="none" stroke="#9aa3af" stroke-width="2"/><polyline points="{strategy_line}" fill="none" stroke="#0b6fa4" stroke-width="3"/></svg></div>
<h2>Metrics</h2><table><thead><tr><th>Metric</th><th>Strategy</th><th>Benchmark</th></tr></thead><tbody>{rows}</tbody></table>
<h2>Latest signal</h2>{signal_html}
</main></body></html>"""


def _svg_polyline(
    values: list[float],
    width: int,
    height: int,
    low: float | None = None,
    high: float | None = None,
) -> str:
    if not values:
        return ""
    low = min(values) if low is None else low
    high = max(values) if high is None else high
    span = high - low or 1.0
    count = max(1, len(values) - 1)
    points = []
    for index, value in enumerate(values):
        x = index / count * width
        y = height - (value - low) / span * (height - 20) - 10
        points.append(f"{x:.2f},{y:.2f}")
    return " ".join(points)


def _format_metric(key: str, value: float) -> str:
    if key in {
        "total_return", "cagr", "annual_volatility", "max_drawdown", "value_at_risk_95",
        "expected_shortfall_95", "worst_day", "best_day", "monthly_win_ratio",
    }:
        return f"{value:.2%}"
    if key in {
        "commissions",
        "stamp_duty",
        "transfer_fees",
        "slippage_cost",
        "transaction_costs",
    }:
        return f"{value:,.2f}"
    return f"{value:.2f}"


def _label(key: str) -> str:
    return key.replace("_", " ").title()
