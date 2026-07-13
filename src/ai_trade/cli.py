from __future__ import annotations

import argparse
import importlib.resources
import json
import logging
import sys
from datetime import date
from pathlib import Path

from .backtest import BacktestEngine
from .broker.live_guard import BrokerNotConfigured, require_live_confirmation
from .broker.paper import initialize_paper, paper_status, run_paper
from .broker.paper_audit import audit_paper, save_paper_audit
from .config import AppConfig, load_config
from .data.eastmoney import download_universe
from .data.market import MarketData
from .report import save_backtest_report
from .strategy import MomentumTrendStrategy
from .validation import run_robustness_validation, save_validation_report
from .walk_forward import run_walk_forward, save_walk_forward


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-trade", description="Auditable systematic research and paper trading"
    )
    parser.add_argument("--config", default="config/default.json", help="Path to JSON configuration")
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Create a standalone AI Trade workspace")
    init.add_argument("--directory", default=".", help="Target workspace directory")

    download = subparsers.add_parser("download", help="Download and cache market data")
    download.add_argument("--force", action="store_true", help="Overwrite existing cache")

    backtest = subparsers.add_parser("backtest", help="Run historical backtest")
    backtest.add_argument("--start", help="YYYY-MM-DD")
    backtest.add_argument("--end", help="YYYY-MM-DD")

    walk = subparsers.add_parser("walk-forward", help="Run rolling out-of-sample validation")
    walk.add_argument("--train-days", type=int, default=756)
    walk.add_argument("--test-days", type=int, default=252)

    validate = subparsers.add_parser("validate", help="Run robustness and stress validation")
    validate.add_argument("--bootstrap-samples", type=int, default=1000)
    validate.add_argument("--block-days", type=int, default=20)

    signal = subparsers.add_parser("signal", help="Show the latest target weights")
    signal.add_argument("--refresh", action="store_true")

    paper_init = subparsers.add_parser("paper-init", help="Initialize the paper account")
    paper_init.add_argument("--cash", type=float)
    paper_init.add_argument("--overwrite", action="store_true")

    paper_run = subparsers.add_parser("paper-run", help="Refresh data and process one paper session")
    paper_run.add_argument("--no-refresh", action="store_true")

    subparsers.add_parser("paper-status", help="Show paper account state")
    subparsers.add_parser("paper-audit", help="Audit forward paper performance and promotion gates")
    universe = subparsers.add_parser(
        "universe-status", help="Inspect point-in-time universe eligibility"
    )
    universe.add_argument("--date", help="YYYY-MM-DD; defaults to today")
    subparsers.add_parser("doctor", help="Check configuration, cache, and latest market date")
    subparsers.add_parser("live-check", help="Verify the live-trading guard; does not submit orders")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            return _initialize_workspace(Path(args.directory))
        config = load_config(_resolve_config_path(args.config))
        _configure_logging(config)
        if args.command == "universe-status":
            on_date = date.fromisoformat(args.date) if args.date else date.today()
            print(
                json.dumps(
                    config.security_master.snapshot(
                        config.universe_name, on_date, config.minimum_listing_days
                    ),
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "download":
            paths = download_universe(config, force=args.force)
            print(json.dumps({key: str(value) for key, value in paths.items()}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "backtest":
            _ensure_cache(config)
            market = MarketData(config)
            result = BacktestEngine(config, market).run(
                date.fromisoformat(args.start) if args.start else None,
                date.fromisoformat(args.end) if args.end else None,
            )
            paths = save_backtest_report(result, config.reports_dir)
            print(_backtest_console(result, paths))
            return 0
        if args.command == "walk-forward":
            _ensure_cache(config)
            market = MarketData(config)
            result = run_walk_forward(config, market, args.train_days, args.test_days)
            paths = save_walk_forward(result, config.reports_dir)
            print(json.dumps({"aggregate": result["aggregate"], "files": [str(path) for path in paths]}, ensure_ascii=False, indent=2))
            return 0
        if args.command == "validate":
            _ensure_cache(config)
            market = MarketData(config)
            result = run_robustness_validation(
                config,
                market,
                bootstrap_samples=args.bootstrap_samples,
                block_days=args.block_days,
            )
            paths = save_validation_report(result, config.reports_dir)
            print(
                json.dumps(
                    {
                        "research_gates": result["research_gates"],
                        "bootstrap": result["bootstrap"],
                        "files": [str(path) for path in paths],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "signal":
            if args.refresh:
                download_universe(config, force=True)
            else:
                _ensure_cache(config)
            market = MarketData(config)
            signal = MomentumTrendStrategy(config.strategy).generate(market, market.latest_date())
            print(json.dumps(_signal_payload(signal), ensure_ascii=False, indent=2))
            return 0
        if args.command == "paper-init":
            state = initialize_paper(config, args.cash, args.overwrite)
            print(json.dumps(state, ensure_ascii=False, indent=2))
            return 0
        if args.command == "paper-run":
            if not args.no_refresh:
                download_universe(config, force=True)
            else:
                _ensure_cache(config)
            report = run_paper(config, MarketData(config))
            print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
            return 0
        if args.command == "paper-status":
            print(json.dumps(paper_status(config), ensure_ascii=False, indent=2))
            return 0
        if args.command == "paper-audit":
            _ensure_cache(config)
            report = audit_paper(config, MarketData(config))
            paths = save_paper_audit(report, config.reports_dir)
            print(
                json.dumps(
                    report | {"files": [str(path) for path in paths]},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0 if not report["integrity_errors"] else 1
        if args.command == "doctor":
            _ensure_cache(config)
            market = MarketData(config)
            diagnosis = _doctor(config, market)
            print(json.dumps(diagnosis, ensure_ascii=False, indent=2))
            return 0 if diagnosis["status"] == "OK" else 1
        if args.command == "live-check":
            require_live_confirmation()
            raise BrokerNotConfigured(
                "Live guard is open, but no broker adapter is configured. Select a broker before live use."
            )
    except Exception as exc:
        logging.getLogger(__name__).exception("Command failed")
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def _ensure_cache(config: AppConfig) -> None:
    missing = [item.symbol for item in config.instruments if not (config.cache_dir / f"{item.symbol}.csv").exists()]
    if missing:
        download_universe(config, force=False)


def _resolve_config_path(value: str) -> Path:
    requested = Path(value)
    if requested.exists() or requested.is_absolute() or value != "config/default.json":
        return requested
    project_default = Path(__file__).resolve().parents[2] / "config" / "default.json"
    return project_default if project_default.exists() else requested


def _initialize_workspace(directory: Path) -> int:
    root = directory.expanduser().resolve()
    config_path = root / "config" / "default.json"
    if config_path.exists():
        raise FileExistsError(f"Workspace configuration already exists: {config_path}")
    config_path.parent.mkdir(parents=True, exist_ok=True)
    resource = importlib.resources.files("ai_trade").joinpath("default_config.json")
    config_path.write_text(resource.read_text(encoding="utf-8"), encoding="utf-8")
    master_resource = importlib.resources.files("ai_trade").joinpath(
        "default_security_master.json"
    )
    (root / "config" / "security_master.json").write_text(
        master_resource.read_text(encoding="utf-8"), encoding="utf-8"
    )
    for relative in ("data/cache", "reports", "state", "logs"):
        path = root / relative
        path.mkdir(parents=True, exist_ok=True)
        (path / ".gitkeep").touch(exist_ok=True)
    print(json.dumps({"workspace": str(root), "config": str(config_path)}, indent=2))
    return 0


def _configure_logging(config: AppConfig) -> None:
    config.logs_dir.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(config.logs_dir / "ai_trade.log", encoding="utf-8"),
            logging.StreamHandler(),
        ],
    )


def _backtest_console(result, paths: dict[str, Path]) -> str:
    payload = {
        "period": [result.metadata["start"], result.metadata["end"]],
        "strategy": result.metrics,
        "benchmark": result.benchmark_metrics,
        "latest_signal": _signal_payload(result.latest_signal) if result.latest_signal else None,
        "report": str(paths["html"]),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _signal_payload(signal) -> dict[str, object]:
    return {
        "date": signal.date.isoformat(),
        "target_weights": signal.target_weights,
        "reason": signal.reason,
        "diagnostics": signal.diagnostics,
        "ranking": [item.__dict__ for item in signal.ranked],
    }


def _doctor(config: AppConfig, market: MarketData) -> dict[str, object]:
    coverage = {}
    latest_dates = []
    active_symbols = set(market.active_symbols(market.latest_date()))
    active_symbols.add(config.strategy.benchmark)
    for symbol, item in market.symbols.items():
        if symbol in active_symbols:
            latest_dates.append(item.bars[-1].date)
        coverage[symbol] = {
            "name": item.instrument.name,
            "active": symbol in active_symbols,
            "rows": len(item.bars),
            "first": item.bars[0].date.isoformat(),
            "last": item.bars[-1].date.isoformat(),
            "sha256": market.file_hashes[symbol],
            "excluded_incomplete_dates": [
                value.isoformat() for value in market.excluded_dates[symbol]
            ],
        }
    aligned = len(set(latest_dates)) == 1
    research_warnings = []
    if config.security_master.metadata.get("selection_method") == "curated_static":
        research_warnings.append(
            "Default universe is curated_static and does not remove survivorship bias"
        )
    if config.raw["data"].get("adjustment") != "none":
        research_warnings.append(
            "Adjusted bars are still used for simulated execution; raw prices and corporate "
            "actions are not yet separated"
        )
    return {
        "status": "OK" if aligned else "WARNING",
        "config": str(config.path),
        "completed_session_cutoff": market.completed_through.isoformat(),
        "latest_market_date": market.latest_date().isoformat(),
        "universe_latest_dates_aligned": aligned,
        "point_in_time_universe": {
            "name": config.universe_name,
            "active_count": len(market.active_symbols(market.latest_date())),
            "loaded_instrument_count": len(config.instruments),
            "selection_method": config.security_master.metadata.get("selection_method"),
            "security_master_sha256": config.security_master.fingerprint(),
        },
        "research_warnings": research_warnings,
        "coverage": coverage,
        "live_trading": "DISABLED",
    }


if __name__ == "__main__":
    raise SystemExit(main())
