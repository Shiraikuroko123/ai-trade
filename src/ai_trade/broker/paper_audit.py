from __future__ import annotations

import csv
import json
import math
from datetime import date
from pathlib import Path

from ..config import AppConfig
from ..data.market import MarketData
from ..metrics import calculate_metrics
from ..models import EquityPoint
from .paper import paper_status


def audit_paper(config: AppConfig, market: MarketData) -> dict[str, object]:
    state = paper_status(config)
    rows, errors = _load_equity_rows(config.paper_equity_file)
    account_id = str(state["account_id"])
    account_rows = [row for row in rows if row["account_id"] == account_id]
    if len(account_rows) != len(rows):
        errors.append("equity ledger contains rows from another account epoch")
    if not account_rows:
        errors.append("equity ledger has no rows for the active account")

    curve = _curve_from_rows(account_rows, errors)
    if account_rows:
        latest = account_rows[-1]
        if latest["date"] != state.get("last_run_date"):
            errors.append("state last_run_date does not match equity ledger")
        if not math.isclose(
            float(latest["equity"]), float(state["last_equity"]), rel_tol=0, abs_tol=1e-5
        ):
            errors.append("state equity does not match equity ledger")
        if latest["config_fingerprint"] != state["config_fingerprint"]:
            errors.append("equity ledger configuration fingerprint mismatch")

    metrics = calculate_metrics(curve)
    benchmark_curve = _benchmark_curve(config, market, curve)
    benchmark_metrics = calculate_metrics(benchmark_curve)
    minimum_sessions = int(config.raw["paper"].get("minimum_promotion_sessions", 60))
    enough_history = len(curve) >= minimum_sessions
    checks = {
        "ledger_integrity": not errors,
        "minimum_forward_sessions": enough_history,
        "drawdown_within_limit": metrics["max_drawdown"]
        >= -config.risk.max_portfolio_drawdown,
        "positive_forward_sharpe": enough_history and metrics["sharpe"] > 0,
        "nonnegative_excess_return": enough_history
        and metrics["total_return"] >= benchmark_metrics["total_return"],
    }
    eligible = all(checks.values())
    return {
        "account_id": account_id,
        "config_fingerprint": state["config_fingerprint"],
        "sessions": len(curve),
        "minimum_promotion_sessions": minimum_sessions,
        "remaining_sessions": max(0, minimum_sessions - len(curve)),
        "period": [
            curve[0].date.isoformat() if curve else None,
            curve[-1].date.isoformat() if curve else None,
        ],
        "metrics": metrics,
        "benchmark_metrics": benchmark_metrics,
        "integrity_errors": errors,
        "promotion_checks": checks,
        "eligible_for_broker_sandbox": eligible,
        "live_ready": False,
        "status": (
            "eligible_for_broker_sandbox_review"
            if eligible
            else "collecting_independent_forward_evidence"
        ),
    }


def save_paper_audit(report: dict[str, object], output_dir: Path) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "paper_audit.json"
    markdown_path = output_dir / "paper_audit.md"
    temporary = json_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(json_path)

    metrics = report["metrics"]
    benchmark = report["benchmark_metrics"]
    lines = [
        "# AI Trade 前向模拟审计",
        "",
        f"- 账户：`{report['account_id']}`",
        f"- 状态：`{report['status']}`",
        f"- 已完成交易日：{report['sessions']} / {report['minimum_promotion_sessions']}",
        f"- 距券商沙盒评估：{report['remaining_sessions']} 个交易日",
        f"- 模拟累计收益：{metrics['total_return']:.2%}",
        f"- 基准累计收益：{benchmark['total_return']:.2%}",
        f"- 模拟 Sharpe：{metrics['sharpe']:.2f}",
        f"- 模拟最大回撤：{metrics['max_drawdown']:.2%}",
        "- 实盘就绪：否",
        "",
        "## 晋级检查",
        "",
    ]
    for name, passed in report["promotion_checks"].items():
        lines.append(f"- {name}: {'通过' if passed else '未通过'}")
    lines.extend(["", "## 完整性错误", ""])
    if report["integrity_errors"]:
        lines.extend(f"- {error}" for error in report["integrity_errors"])
    else:
        lines.append("- 无")
    lines.extend(
        [
            "",
            "达到 60 个交易日只允许进入券商沙盒复核，不会自动解锁真实下单。",
        ]
    )
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, markdown_path


def _load_equity_rows(path: Path) -> tuple[list[dict[str, str]], list[str]]:
    errors: list[str] = []
    if not path.exists():
        return [], [f"missing equity ledger: {path}"]
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {
            "account_id", "session_id", "date", "equity", "cash", "drawdown",
            "daily_return", "config_fingerprint", "market_snapshot_id",
        }
        if not reader.fieldnames or not required.issubset(reader.fieldnames):
            return [], ["equity ledger schema is invalid"]
        rows = list(reader)
    if len({row["session_id"] for row in rows}) != len(rows):
        errors.append("equity ledger contains duplicate session_id values")
    return rows, errors


def _curve_from_rows(
    rows: list[dict[str, str]], errors: list[str]
) -> list[EquityPoint]:
    curve = []
    previous_date: date | None = None
    high_water = 0.0
    for index, row in enumerate(rows, start=2):
        try:
            on_date = date.fromisoformat(row["date"])
            equity = float(row["equity"])
            cash = float(row["cash"])
            if not all(math.isfinite(value) for value in (equity, cash)) or equity <= 0:
                raise ValueError("equity must be positive and values finite")
        except (KeyError, ValueError) as exc:
            errors.append(f"invalid equity ledger row {index}: {exc}")
            continue
        if previous_date is not None and on_date <= previous_date:
            errors.append(f"equity ledger dates are not strictly increasing at row {index}")
        previous_date = on_date
        high_water = max(high_water, equity)
        curve.append(
            EquityPoint(on_date, equity, cash, equity / high_water - 1.0)
        )
    return curve


def _benchmark_curve(
    config: AppConfig,
    market: MarketData,
    curve: list[EquityPoint],
) -> list[EquityPoint]:
    if not curve:
        return []
    symbol = config.strategy.benchmark
    first_bar = market.bar(symbol, curve[0].date)
    if first_bar is None or first_bar.close <= 0:
        return []
    initial = curve[0].equity
    high_water = initial
    result = []
    for point in curve:
        bar = market.bar(symbol, point.date) or market.latest_bar_on_or_before(
            symbol, point.date
        )
        if bar is None:
            continue
        equity = initial * bar.close / first_bar.close
        high_water = max(high_water, equity)
        result.append(EquityPoint(point.date, equity, 0.0, equity / high_water - 1.0))
    return result
