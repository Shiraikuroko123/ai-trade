from __future__ import annotations

import argparse
import importlib.resources
import json
import logging
import os
import sys
from dataclasses import asdict
from datetime import date
from getpass import getpass
from pathlib import Path

from .assistant import AssistantEngine
from .backtest import BacktestEngine
from .broker.live_guard import BrokerNotConfigured, require_live_confirmation
from .broker.paper import initialize_paper, paper_status, run_paper
from .broker.paper_audit import audit_paper, save_paper_audit
from .broker.probe import (
    available_broker_adapters,
    compare_configured_broker,
    probe_configured_broker,
)
from .config import AppConfig, load_config
from .data.eastmoney import download_universe
from .data.market import MarketData
from .diagnostics import diagnose
from .report import save_backtest_report
from .strategy import MomentumTrendStrategy
from .validation import run_robustness_validation, save_validation_report
from .walk_forward import run_walk_forward, save_walk_forward


_WEB_JOB_PROTOCOL_ENV = "AI_TRADE_WEB_JOB_PROTOCOL"
_CLOUD_BACKUP_EVENT_PREFIX = "@@AI_TRADE_CLOUD_BACKUP@@"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ai-trade", description="Auditable systematic research and paper trading"
    )
    parser.add_argument(
        "--config", default="config/default.json", help="Path to JSON configuration"
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    init = subparsers.add_parser("init", help="Create a standalone AI Trade workspace")
    init.add_argument("--directory", default=".", help="Target workspace directory")

    download = subparsers.add_parser("download", help="Download and cache market data")
    download.add_argument(
        "--force", action="store_true", help="Overwrite existing cache"
    )

    cloud_status = subparsers.add_parser(
        "cloud-status", help="Inspect this user's optional Cloudflare R2 backup"
    )
    cloud_status.add_argument(
        "--check",
        action="store_true",
        help="Verify access to the configured R2 namespace",
    )
    subparsers.add_parser(
        "cloud-backup", help="Upload a verified market-cache snapshot to Cloudflare R2"
    )
    cloud_list = subparsers.add_parser(
        "cloud-list",
        help="List market-cache snapshots in this installation's namespace",
    )
    cloud_list.add_argument("--limit", type=int, default=20)
    cloud_restore = subparsers.add_parser(
        "cloud-restore",
        help="Verify and restore a cloud snapshot into a new staging directory",
    )
    cloud_restore.add_argument("snapshot_id")
    cloud_restore.add_argument(
        "--directory",
        help=(
            "New destination directory (must not already exist); "
            "defaults to local/cloud-restore/<snapshot-id>"
        ),
    )

    backtest = subparsers.add_parser("backtest", help="Run historical backtest")
    backtest.add_argument("--start", help="YYYY-MM-DD")
    backtest.add_argument("--end", help="YYYY-MM-DD")

    walk = subparsers.add_parser(
        "walk-forward", help="Run rolling out-of-sample validation"
    )
    walk.add_argument("--train-days", type=int, default=756)
    walk.add_argument("--test-days", type=int, default=252)

    validate = subparsers.add_parser(
        "validate", help="Run robustness and stress validation"
    )
    validate.add_argument("--bootstrap-samples", type=int, default=1000)
    validate.add_argument("--block-days", type=int, default=20)

    signal = subparsers.add_parser("signal", help="Show the latest target weights")
    signal.add_argument("--refresh", action="store_true")

    assistant_analyze = subparsers.add_parser(
        "assistant-analyze", help="Run one research-only assistant analysis"
    )
    assistant_analyze.add_argument("--symbol", required=True)
    assistant_analyze.add_argument("--lookback", type=int, default=180)
    assistant_analyze.add_argument(
        "--mode", choices=("local", "model"), default="local"
    )

    paper_init = subparsers.add_parser(
        "paper-init", help="Initialize the paper account"
    )
    paper_init.add_argument("--cash", type=float)
    paper_init.add_argument("--overwrite", action="store_true")

    paper_run = subparsers.add_parser(
        "paper-run", help="Refresh data and process one paper session"
    )
    paper_run.add_argument("--no-refresh", action="store_true")

    subparsers.add_parser("paper-status", help="Show paper account state")
    subparsers.add_parser(
        "paper-audit", help="Audit forward paper performance and promotion gates"
    )
    universe = subparsers.add_parser(
        "universe-status", help="Inspect point-in-time universe eligibility"
    )
    universe.add_argument("--date", help="YYYY-MM-DD; defaults to today")
    subparsers.add_parser(
        "doctor", help="Check configuration, cache, and latest market date"
    )
    subparsers.add_parser(
        "live-check", help="Verify the live-trading guard; does not submit orders"
    )
    subparsers.add_parser(
        "broker-list", help="List installed broker adapter plugins"
    )
    subparsers.add_parser(
        "broker-probe",
        help="Read the configured sandbox broker without changing broker state",
    )
    subparsers.add_parser(
        "broker-compare",
        help="Compare the paper account with a read-only broker observation",
    )
    serve = subparsers.add_parser("serve", help="Start the local AI Trade workstation")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--no-open", action="store_true", help="Do not open a browser")
    serve.add_argument(
        "--owner-local",
        action="store_true",
        help="Bypass beta login for this loopback-only owner session",
    )
    beta_add = subparsers.add_parser(
        "beta-user-add", help="Add an account to the local beta whitelist"
    )
    beta_add.add_argument("username")
    beta_add.add_argument(
        "--replace", action="store_true", help="Replace an existing password"
    )
    subparsers.add_parser("beta-user-list", help="List local beta accounts")
    for action in ("enable", "disable", "remove"):
        command = subparsers.add_parser(
            f"beta-user-{action}", help=f"{action.title()} a local beta account"
        )
        command.add_argument("username")
        if action == "remove":
            command.add_argument(
                "--yes", action="store_true", help="Confirm permanent removal"
            )
    beta_export = subparsers.add_parser(
        "beta-users-export",
        help="Export a portable beta whitelist without plaintext passwords",
    )
    beta_export.add_argument("output")
    beta_import = subparsers.add_parser(
        "beta-users-import", help="Import a portable beta whitelist"
    )
    beta_import.add_argument("source")
    beta_import.add_argument(
        "--mode", choices=("reject", "merge", "replace"), default="reject"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "init":
            return _initialize_workspace(Path(args.directory))
        config = load_config(_resolve_config_path(args.config))
        _configure_logging(config)
        if args.command in {
            "beta-user-add",
            "beta-user-list",
            "beta-user-enable",
            "beta-user-disable",
            "beta-user-remove",
        }:
            from .web.auth import UserStore

            users = UserStore(config.auth_users_file)
            if args.command == "beta-user-add":
                password = getpass("内测密码: ")
                confirmation = getpass("再次输入: ")
                if password != confirmation:
                    raise ValueError("两次输入的密码不一致")
                user = users.add_user(args.username, password, replace=args.replace)
                print(json.dumps(asdict(user), ensure_ascii=False, indent=2))
                return 0
            if args.command == "beta-user-list":
                print(
                    json.dumps(
                        [asdict(user) for user in users.list_users()],
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            if args.command == "beta-user-enable":
                user = users.set_enabled(args.username, True)
                print(json.dumps(asdict(user), ensure_ascii=False, indent=2))
                return 0
            if args.command == "beta-user-disable":
                user = users.set_enabled(args.username, False)
                print(json.dumps(asdict(user), ensure_ascii=False, indent=2))
                return 0
            if args.command == "beta-user-remove":
                if not args.yes:
                    raise ValueError("Permanent removal requires --yes")
                if not users.remove_user(args.username):
                    raise ValueError("Beta user does not exist")
                print(json.dumps({"removed": args.username}, ensure_ascii=False))
                return 0
        if args.command in {"beta-users-export", "beta-users-import"}:
            from .web.auth import UserStore

            users = UserStore(config.auth_users_file)
            if args.command == "beta-users-export":
                output = Path(args.output).expanduser().resolve()
                count = users.export_users(output)
                print(
                    json.dumps(
                        {"output": str(output), "users": count},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            imported = users.import_users(args.source, mode=args.mode)
            print(
                json.dumps(
                    {"users": [asdict(user) for user in imported]},
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
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
        if args.command.startswith("cloud-"):
            from .cloud import (
                backup_market_cache,
                cloud_dependency_available,
                load_cloud_settings,
                tracked_r2_store,
            )

            settings = load_cloud_settings()
            if args.command == "cloud-status":
                status = settings.public_status()
                status["dependency_available"] = cloud_dependency_available()
                if args.check:
                    store = tracked_r2_store(config, settings)
                    store.check_connection()
                    status["connection"] = "ok"
                else:
                    status["connection"] = "not_checked"
                print(json.dumps(status, ensure_ascii=False, indent=2))
                return 0
            store = tracked_r2_store(config, settings)
            if args.command == "cloud-backup":
                _ensure_cache(config)
                result = backup_market_cache(config, store)
                public_keys = (
                    "snapshot_id",
                    "sha256",
                    "dataset_sha256",
                    "size",
                    "created_at",
                    "latest_common_session",
                    "skipped_duplicate",
                )
                print(
                    json.dumps(
                        {key: result[key] for key in public_keys if key in result},
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                return 0
            if args.command == "cloud-list":
                snapshots = store.list_snapshots(limit=args.limit)
                public = [
                    {key: value for key, value in item.items() if key != "object_key"}
                    for item in snapshots
                ]
                print(json.dumps({"snapshots": public}, ensure_ascii=False, indent=2))
                return 0
            destination = (
                Path(args.directory)
                if args.directory
                else config.project_root / "local" / "cloud-restore" / args.snapshot_id
            )
            restored = store.restore_snapshot(config, args.snapshot_id, destination)
            print(
                json.dumps(
                    {
                        "snapshot_id": args.snapshot_id,
                        "restored_to": str(restored),
                        "active_cache_unchanged": True,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 0
        if args.command == "download":
            paths = download_universe(config, force=args.force)
            _maybe_automatic_cloud_backup(config)
            print(
                json.dumps(
                    {key: str(value) for key, value in paths.items()},
                    ensure_ascii=False,
                    indent=2,
                )
            )
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
            print(
                json.dumps(
                    {
                        "aggregate": result["aggregate"],
                        "files": [str(path) for path in paths],
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
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
                _maybe_automatic_cloud_backup(config)
            else:
                _ensure_cache(config)
            market = MarketData(config)
            signal = MomentumTrendStrategy(config.strategy).generate(
                market, market.latest_date()
            )
            print(json.dumps(_signal_payload(signal), ensure_ascii=False, indent=2))
            return 0
        if args.command == "assistant-analyze":
            _ensure_cache(config)
            result = AssistantEngine(config).analyze(
                MarketData(config),
                args.symbol,
                lookback=args.lookback,
                mode=args.mode,
                user_id="local-owner",
            )
            print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
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
            _maybe_automatic_cloud_backup(config)
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
            diagnosis = diagnose(config, market)
            print(json.dumps(diagnosis, ensure_ascii=False, indent=2))
            return 0 if diagnosis["status"] == "OK" else 1
        if args.command == "broker-list":
            print(
                json.dumps(
                    available_broker_adapters(), ensure_ascii=False, indent=2
                )
            )
            return 0
        if args.command == "broker-probe":
            print(
                json.dumps(
                    probe_configured_broker(config), ensure_ascii=False, indent=2
                )
            )
            return 0
        if args.command == "broker-compare":
            print(
                json.dumps(
                    compare_configured_broker(config), ensure_ascii=False, indent=2
                )
            )
            return 0
        if args.command == "live-check":
            require_live_confirmation()
            raise BrokerNotConfigured(
                "No live-capable broker adapter is configured; read-only adapters "
                "cannot unlock live trading."
            )
        if args.command == "serve":
            from .web.server import serve_dashboard

            serve_dashboard(
                config,
                host=args.host,
                port=args.port,
                open_browser=not args.no_open,
                auth_enabled=False if args.owner_local else None,
            )
            return 0
    except Exception as exc:
        if getattr(args, "command", "").startswith("cloud-"):
            message = _safe_cloud_error(exc)
            logging.getLogger(__name__).error("Cloud command failed: %s", message)
            print(f"ERROR: {message}", file=sys.stderr)
        else:
            logging.getLogger(__name__).exception("Command failed")
            print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def _maybe_automatic_cloud_backup(config: AppConfig) -> None:
    try:
        from .cloud import (
            automatic_cloud_backup_enabled,
            backup_market_cache,
            load_cloud_settings,
            tracked_r2_store,
        )

        if not automatic_cloud_backup_enabled(config):
            return
        settings = load_cloud_settings()
        result = backup_market_cache(config, tracked_r2_store(config, settings))
        logging.getLogger(__name__).info(
            "Automatic cloud snapshot %s (%s)",
            result.get("snapshot_id", "completed"),
            "deduplicated" if result.get("skipped_duplicate") else "uploaded",
        )
        _emit_cloud_backup_event("succeeded")
    except Exception as exc:
        logging.getLogger(__name__).warning(
            "Automatic cloud backup failed; local data remains valid: %s",
            _safe_cloud_error(exc),
        )
        _emit_cloud_backup_event("failed")


def _emit_cloud_backup_event(status: str) -> None:
    if os.environ.get(_WEB_JOB_PROTOCOL_ENV) != "1":
        return
    if status not in {"succeeded", "failed"}:
        raise ValueError("Cloud backup event status is invalid")
    payload = json.dumps(
        {"schema_version": 1, "status": status},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    print(f"{_CLOUD_BACKUP_EVENT_PREFIX}{payload}", file=sys.stderr, flush=True)


def _ensure_cache(config: AppConfig) -> None:
    missing = [
        item.symbol
        for item in config.instruments
        if not (config.cache_dir / f"{item.symbol}.csv").exists()
    ]
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


def _safe_cloud_error(exc: Exception) -> str:
    from .cloud import safe_cloud_error

    return safe_cloud_error(exc)


def _backtest_console(result, paths: dict[str, Path]) -> str:
    payload = {
        "period": [result.metadata["start"], result.metadata["end"]],
        "strategy": result.metrics,
        "benchmark": result.benchmark_metrics,
        "latest_signal": _signal_payload(result.latest_signal)
        if result.latest_signal
        else None,
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


if __name__ == "__main__":
    raise SystemExit(main())
