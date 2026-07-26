"""Deterministic consolidated research report projection.

The report is a read-time projection over evidence that already exists on
disk: backtest and walk-forward reports, the robustness validation report,
the paper account, and the factor/model/hypothesis research stores. It calls
no model, refreshes no provider, and mutates nothing except the single output
file the operator asked for. Missing evidence is reported explicitly as
unavailable instead of being padded or invented.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Callable

from . import __version__
from .config import AppConfig
from .json_utils import load_unique_json


SCHEMA_VERSION = 1
MAX_SOURCE_BYTES = 8 * 1024 * 1024

_AUTHORITY = (
    "本报告为 research_only 只读投影：不构成投资建议，不创建订单、"
    "不改变策略、不解锁任何权限；历史结果不代表未来收益。"
)


def generate_research_report(
    config: AppConfig, owner: str = "local-owner"
) -> dict[str, Any]:
    sections: list[dict[str, Any]] = []

    def add(section_id: str, title: str, builder: Callable[[], list[str]]) -> None:
        try:
            lines = builder()
            sections.append(
                {
                    "id": section_id,
                    "title": title,
                    "status": "available",
                    "lines": lines,
                }
            )
        except Exception as exc:  # noqa: BLE001 - report must fail soft per section
            sections.append(
                {
                    "id": section_id,
                    "title": title,
                    "status": "unavailable",
                    "lines": [f"证据不可用: {str(exc)[:200]}"],
                }
            )

    add("market", "行情快照", lambda: _market_section(config))
    add("backtest", "策略回测", lambda: _backtest_section(config))
    add("walk_forward", "滚动样本外验证", lambda: _walk_forward_section(config))
    add("validation", "稳健性验证", lambda: _validation_section(config))
    add("paper", "模拟账户", lambda: _paper_section(config))
    add("factors", "因子研究", lambda: _factor_section(config, owner))
    add("models", "模型研究", lambda: _model_section(config, owner))
    add("hypotheses", "假设与实验", lambda: _hypothesis_section(config, owner))

    body_fingerprint = sha256(
        json.dumps(sections, ensure_ascii=True, sort_keys=True).encode("utf-8")
    ).hexdigest()
    generated_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    markdown = _render_markdown(config, generated_at, sections, body_fingerprint)
    return {
        "schema_version": SCHEMA_VERSION,
        "generated_at": generated_at,
        "app_version": __version__,
        "sections": [
            {"id": item["id"], "title": item["title"], "status": item["status"]}
            for item in sections
        ],
        "content_fingerprint": body_fingerprint,
        "markdown": markdown,
        "safety": {
            "research_only": True,
            "creates_no_signal": True,
            "orders_created": False,
            "strategy_changed": False,
        },
    }


def write_research_report(
    config: AppConfig,
    output: str | Path | None = None,
    owner: str = "local-owner",
) -> dict[str, Any]:
    report = generate_research_report(config, owner)
    path = Path(output) if output is not None else (
        config.project_root / "reports" / "research_report.md"
    )
    if not path.is_absolute():
        path = config.project_root / path
    resolved = path.resolve()
    root = config.project_root.resolve()
    if root not in resolved.parents and resolved != root:
        raise ValueError("Research report output must stay inside the workspace")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(report["markdown"], encoding="utf-8")
    summary = {key: value for key, value in report.items() if key != "markdown"}
    summary["output"] = str(resolved)
    return summary


def _render_markdown(
    config: AppConfig,
    generated_at: str,
    sections: list[dict[str, Any]],
    fingerprint: str,
) -> str:
    lines = [
        "# AI Trade 研究报告",
        "",
        f"- 生成时间 (UTC): {generated_at}",
        f"- 软件版本: {__version__}",
        f"- 标的池: {config.universe_name}",
        f"- 内容指纹: `{fingerprint[:16]}`",
        "",
        f"> {_AUTHORITY}",
    ]
    for section in sections:
        marker = "" if section["status"] == "available" else "（不可用）"
        lines.extend(["", f"## {section['title']}{marker}", ""])
        lines.extend(section["lines"])
    lines.append("")
    return "\n".join(lines)


def _market_section(config: AppConfig) -> list[str]:
    from .data.market import MarketData

    market = MarketData(config, recover_snapshot=False)
    metadata = market.snapshot_metadata()
    manifest_hash = getattr(market, "manifest_sha256", None)
    return [
        f"- 数据提供方: {metadata.get('provider')}",
        f"- 最近共同完整交易日: {metadata.get('latest_common_session')}",
        f"- 完整会话截止: {metadata.get('completed_session_cutoff')}",
        f"- 证券数量: {len(metadata.get('symbols') or {})}",
        f"- manifest 指纹: `{(manifest_hash or '未提供')[:16]}`",
    ]


def _backtest_section(config: AppConfig) -> list[str]:
    value = _load_json(config.project_root / "reports" / "backtest_summary.json")
    strategy = value.get("strategy_metrics")
    benchmark = value.get("benchmark_metrics")
    metadata = value.get("metadata")
    if not isinstance(strategy, dict) or not isinstance(metadata, dict):
        raise RuntimeError("backtest_summary.json 结构不符合预期")
    lines = [
        f"- 区间: {metadata.get('start')} 至 {metadata.get('end')}",
        "",
        "| 指标 | 策略 | 基准 |",
        "|---|---:|---:|",
    ]
    benchmark = benchmark if isinstance(benchmark, dict) else {}
    for key, label, kind in (
        ("total_return", "总收益", "pct"),
        ("cagr", "年化收益", "pct"),
        ("sharpe", "Sharpe", "num"),
        ("max_drawdown", "最大回撤", "pct"),
        ("turnover", "换手（名义/均值）", "num"),
    ):
        lines.append(
            f"| {label} | {_number(strategy.get(key), kind)} "
            f"| {_number(benchmark.get(key), kind)} |"
        )
    rejections = metadata.get("order_rejection_count")
    if isinstance(rejections, int):
        lines.extend(["", f"- 拒单数量: {rejections}"])
    return lines


def _walk_forward_section(config: AppConfig) -> list[str]:
    value = _load_json(config.project_root / "reports" / "walk_forward.json")
    aggregate = value.get("aggregate")
    if not isinstance(aggregate, dict):
        raise RuntimeError("walk_forward.json 结构不符合预期")
    lines = [
        f"- 样本外分段: {aggregate.get('segments')}"
        f"（正收益分段 {aggregate.get('positive_segments')}）",
        f"- OOS 总收益: {_number(aggregate.get('oos_total_return'), 'pct')}",
        f"- OOS 年化: {_number(aggregate.get('oos_cagr'), 'pct')}",
        f"- OOS Sharpe: {_number(aggregate.get('oos_sharpe'), 'num')}",
        f"- OOS 最大回撤: {_number(aggregate.get('oos_max_drawdown'), 'pct')}",
    ]
    disclosure = value.get("selection_disclosure")
    if isinstance(disclosure, str) and disclosure.strip():
        lines.extend(["", f"> {disclosure.strip()}"])
    return lines


def _validation_section(config: AppConfig) -> list[str]:
    value = _load_json(config.project_root / "reports" / "validation_report.json")
    gates = value.get("research_gates")
    lines: list[str] = []
    if isinstance(gates, dict):
        checks = gates.get("checks")
        if isinstance(checks, dict) and checks:
            booleans = {
                str(key): bool(item)
                for key, item in checks.items()
                if isinstance(item, bool)
            }
            passed = sum(booleans.values())
            lines.append(f"- 研究门禁: {passed}/{len(booleans)} 通过")
            for label, ok in booleans.items():
                if not ok:
                    lines.append(f"- 未通过: {label[:120]}")
        elif isinstance(checks, list):
            passed = sum(
                bool(item.get("passed")) for item in checks if isinstance(item, dict)
            )
            lines.append(f"- 研究门禁: {passed}/{len(checks)} 通过")
            for item in checks:
                if isinstance(item, dict) and not item.get("passed"):
                    lines.append(
                        f"- 未通过: {str(item.get('label') or item.get('id'))[:120]}"
                    )
        status = gates.get("status")
        if isinstance(status, str) and status.strip():
            lines.append(f"- 门禁状态: {status.strip()[:160]}")
        if isinstance(gates.get("live_ready"), bool):
            lines.append(
                f"- live_ready: {'true' if gates['live_ready'] else 'false'}"
            )
    for key, label in (
        ("bootstrap", "Bootstrap"),
        ("cost_stress", "成本压力"),
        ("parameter_sensitivity", "参数敏感性"),
        ("regime_stress", "情景压力"),
    ):
        if key in value:
            lines.append(f"- 已包含证据: {label}")
    if not lines:
        raise RuntimeError("validation_report.json 结构不符合预期")
    return lines


def _paper_section(config: AppConfig) -> list[str]:
    value = _load_json(config.project_root / "state" / "paper_state.json")
    positions = value.get("positions")
    position_count = len(positions) if isinstance(positions, dict) else 0
    account = str(value.get("account_id") or "")
    return [
        f"- 账期: {account[:12] or '未知'}",
        f"- 最近推进日: {value.get('last_run_date')}",
        f"- 最近权益: {_number(value.get('last_equity'), 'plain')}",
        f"- 现金: {_number(value.get('cash'), 'plain')}",
        f"- 持仓标的数: {position_count}",
        f"- 风险冷却剩余: {value.get('cooldown_remaining')}",
    ]


def _factor_section(config: AppConfig, owner: str) -> list[str]:
    from .factor_lab import FactorLabEngine

    listing = FactorLabEngine(config).list(owner, limit=5)
    rows = listing.get("evaluations") or []
    if not rows:
        return ["- 尚无因子评估记录（factor-evaluate 可生成）"]
    lines = ["| 因子 | 截止 | 各期限 mean IC |", "|---|---|---|"]
    for row in rows:
        horizons = ", ".join(
            f"h{item['horizon']}={item['mean_ic']:+.3f}" for item in row["results"]
        )
        lines.append(f"| {row['factor_id']} | {row['as_of']} | {horizons} |")
    return lines


def _model_section(config: AppConfig, owner: str) -> list[str]:
    from .model_lab import ModelLabEngine

    listing = ModelLabEngine(config).list(owner, limit=5)
    rows = listing.get("evaluations") or []
    if not rows:
        return ["- 尚无模型评估记录（model-evaluate 可生成）"]
    lines = [
        "| 模型 | 期限 | mean IC | ICIR | 模型−最佳单因子 |",
        "|---|---:|---:|---:|---:|",
    ]
    for row in rows:
        lines.append(
            f"| {row['model_id']} | {row['horizon']} | {row['mean_ic']:+.4f} "
            f"| {row['ic_ir']:+.3f} | {row['model_minus_best_factor_ic']:+.4f} |"
        )
    return lines


def _hypothesis_section(config: AppConfig, owner: str) -> list[str]:
    from .hypothesis_lab import HypothesisLabEngine
    from .hypothesis_lab.runner import HypothesisExperimentRunner

    engine = HypothesisLabEngine(config)
    listing = engine.list(owner, limit=5)
    total = listing.get("summary", {}).get("total", 0)
    lines = [f"- 已登记假设: {total}"]
    runner = HypothesisExperimentRunner(config, engine.store, engine.strategy_lab)
    runs = runner.list_runs(owner, limit=5)
    run_rows = runs.get("runs") or []
    if run_rows:
        lines.extend(["", "| 运行 | 模式 | 判定 |", "|---|---|---|"])
        for row in run_rows:
            lines.append(
                f"| {row['run_id'][:14]} | {row['mode']} "
                f"| {row['verdict']['status']} |"
            )
    else:
        lines.append("- 尚无实验运行记录（hypothesis-run 可执行已登记假设）")
    return lines


def _load_json(path: Path) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(f"{path.name} 不存在")
    value = load_unique_json(path, max_bytes=MAX_SOURCE_BYTES)
    if not isinstance(value, dict):
        raise RuntimeError(f"{path.name} 必须是 JSON 对象")
    return value


def _number(value: Any, kind: str = "num") -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return "无"
    parsed = float(value)
    if kind == "pct":
        return f"{parsed:.2%}"
    if kind == "plain":
        return f"{parsed:,.2f}"
    return f"{parsed:.3f}"


__all__ = [
    "SCHEMA_VERSION",
    "generate_research_report",
    "write_research_report",
]
