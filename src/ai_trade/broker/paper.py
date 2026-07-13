from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import shutil
from contextlib import contextmanager
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from uuid import uuid4

from ..config import AppConfig
from ..data.market import MarketData
from ..execution import Portfolio, execute_target_weights, portfolio_value
from ..strategy import MomentumTrendStrategy


def initialize_paper(
    config: AppConfig,
    cash: float | None = None,
    overwrite: bool = False,
) -> dict[str, object]:
    starting_cash = float(config.raw["paper"]["initial_cash"] if cash is None else cash)
    if not math.isfinite(starting_cash) or starting_cash <= 0:
        raise ValueError("Paper starting cash must be finite and positive")

    with _account_lock(config.paper_state_file):
        if config.paper_state_file.exists() and not overwrite:
            raise FileExistsError(f"Paper state already exists: {config.paper_state_file}")
        if overwrite:
            _archive_existing_account(config)
        state = {
            "version": 4,
            "account_id": uuid4().hex,
            "config_fingerprint": _config_fingerprint(config),
            "cash": starting_cash,
            "positions": {},
            "high_water_mark": starting_cash,
            "last_equity": starting_cash,
            "last_run_date": None,
            "pending_targets": None,
            "pending_signal_date": None,
            "cooldown_remaining": 0,
            "sessions_since_rebalance": config.strategy.rebalance_days,
        }
        _save_state(config.paper_state_file, state)
        return state


def run_paper(config: AppConfig, market: MarketData) -> dict[str, object]:
    with _account_lock(config.paper_state_file):
        state = _load_state(config.paper_state_file)
        _validate_state(state, config)
        latest_date = market.latest_date()
        last_run_raw = state.get("last_run_date")
        if last_run_raw == latest_date.isoformat():
            return _existing_report(config, state, latest_date)

        if last_run_raw is None:
            sessions = [latest_date]
        else:
            last_run = date.fromisoformat(str(last_run_raw))
            if last_run > latest_date:
                raise RuntimeError(
                    f"Paper state date {last_run} is after completed market date {latest_date}"
                )
            sessions = [value for value in market.calendar if last_run < value <= latest_date]
        if not sessions:
            raise RuntimeError("No completed market session is available to process")

        report: dict[str, object] | None = None
        for on_date in sessions:
            report = _process_session(config, market, state, on_date)
        if report is None:
            raise RuntimeError("Paper processing produced no report")
        return report


def paper_status(config: AppConfig) -> dict[str, object]:
    state = _load_state(config.paper_state_file)
    _validate_state(state, config)
    return state


def _process_session(
    config: AppConfig,
    market: MarketData,
    state: dict[str, object],
    on_date: date,
) -> dict[str, object]:
    portfolio = Portfolio(
        cash=float(state["cash"]),
        positions={key: int(value) for key, value in dict(state["positions"]).items()},
        high_water_mark=float(state["high_water_mark"]),
    )
    trades = []
    pending = state.get("pending_targets")
    signal_date = state.get("pending_signal_date")
    if pending is not None and signal_date and date.fromisoformat(str(signal_date)) < on_date:
        trades = execute_target_weights(
            portfolio,
            market,
            on_date,
            {key: float(value) for key, value in dict(pending).items()},
            config.costs,
            "Paper fill from prior signal",
            config.strategy.minimum_rebalance_weight,
        )

    equity = portfolio_value(portfolio, market, on_date, "close")
    portfolio.high_water_mark = max(portfolio.high_water_mark, equity)
    drawdown = equity / portfolio.high_water_mark - 1.0 if portfolio.high_water_mark else 0.0
    last_equity = float(state.get("last_equity", equity))
    daily_return = equity / last_equity - 1.0 if last_equity > 0 else 0.0
    cooldown = int(state.get("cooldown_remaining", 0))
    sessions_since = int(
        state.get("sessions_since_rebalance", config.strategy.rebalance_days)
    )
    next_targets: dict[str, float] | None = None
    next_signal_date: str | None = None

    risk_triggered = bool(portfolio.positions) and (
        drawdown <= -config.risk.max_portfolio_drawdown
        or daily_return <= -config.risk.max_daily_loss
    )
    if risk_triggered:
        next_targets = {}
        next_signal_date = on_date.isoformat()
        reason = "Paper risk stop"
        cooldown = config.risk.cooldown_days
        sessions_since = config.strategy.rebalance_days
    elif cooldown > 0:
        cooldown -= 1
        if portfolio.positions:
            next_targets = {}
            next_signal_date = on_date.isoformat()
        reason = "Paper risk cooldown"
        if cooldown == 0:
            portfolio.high_water_mark = equity
    else:
        sessions_since += 1
        if sessions_since >= config.strategy.rebalance_days:
            signal = MomentumTrendStrategy(config.strategy).generate(market, on_date)
            next_targets = signal.target_weights
            next_signal_date = on_date.isoformat()
            reason = signal.reason
            sessions_since = 0
        else:
            reason = (
                f"Hold; next scheduled rebalance in "
                f"{config.strategy.rebalance_days - sessions_since} sessions"
            )

    state.update(
        {
            "cash": portfolio.cash,
            "positions": portfolio.positions,
            "high_water_mark": portfolio.high_water_mark,
            "last_equity": equity,
            "last_run_date": on_date.isoformat(),
            "pending_targets": next_targets,
            "pending_signal_date": next_signal_date,
            "cooldown_remaining": cooldown,
            "sessions_since_rebalance": sessions_since,
        }
    )
    snapshot_id = _market_snapshot_id(market)
    _append_trades(config.paper_trades_file, str(state["account_id"]), trades)
    _append_equity(
        config.paper_equity_file,
        state,
        on_date,
        equity,
        drawdown,
        daily_return,
        snapshot_id,
    )
    _save_state(config.paper_state_file, state)
    return _paper_report(
        config,
        state,
        on_date,
        trades,
        reason,
        drawdown,
        daily_return,
        snapshot_id,
    )


def _paper_report(
    config: AppConfig,
    state: dict[str, object],
    on_date: date,
    trades: list,
    reason: str,
    drawdown: float,
    daily_return: float,
    snapshot_id: str,
) -> dict[str, object]:
    report = {
        "account_id": state["account_id"],
        "date": on_date.isoformat(),
        "equity": state.get("last_equity"),
        "cash": state.get("cash"),
        "positions": state.get("positions"),
        "pending_targets": state.get("pending_targets"),
        "cooldown_remaining": state.get("cooldown_remaining"),
        "sessions_since_rebalance": state.get("sessions_since_rebalance"),
        "drawdown": drawdown,
        "daily_return": daily_return,
        "market_snapshot_id": snapshot_id,
        "trades": [_trade_payload(str(state["account_id"]), trade) for trade in trades],
        "reason": reason,
    }
    config.reports_dir.mkdir(parents=True, exist_ok=True)
    output = config.reports_dir / f"paper_{on_date.strftime('%Y%m%d')}.json"
    _atomic_write_json(output, report)
    return report


def _existing_report(
    config: AppConfig,
    state: dict[str, object],
    on_date: date,
) -> dict[str, object]:
    output = config.reports_dir / f"paper_{on_date.strftime('%Y%m%d')}.json"
    if output.exists():
        report = json.loads(output.read_text(encoding="utf-8"))
    else:
        report = {
            "account_id": state["account_id"],
            "date": on_date.isoformat(),
            "equity": state.get("last_equity"),
            "cash": state.get("cash"),
            "positions": state.get("positions"),
            "pending_targets": state.get("pending_targets"),
            "cooldown_remaining": state.get("cooldown_remaining"),
            "sessions_since_rebalance": state.get("sessions_since_rebalance"),
            "trades": [],
            "reason": "Existing state; daily report is missing",
        }
    return report | {"status": "already_processed"}


def _trade_payload(account_id: str, trade) -> dict[str, object]:
    payload = trade.__dict__ | {"date": trade.date.isoformat()}
    return {
        "account_id": account_id,
        "trade_id": _trade_id(account_id, trade),
        **payload,
    }


def _trade_id(account_id: str, trade) -> str:
    raw = "|".join(
        [
            account_id,
            trade.date.isoformat(),
            trade.symbol,
            trade.side,
            str(trade.quantity),
            f"{trade.price:.10f}",
            trade.reason,
        ]
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]


def _load_state(path: Path) -> dict[str, object]:
    if not path.exists():
        raise FileNotFoundError("Paper account is not initialized. Run paper-init first.")
    return json.loads(path.read_text(encoding="utf-8"))


def _validate_state(state: dict[str, object], config: AppConfig) -> None:
    if int(state.get("version", 0)) != 4:
        raise RuntimeError(
            "Unsupported paper state version. Reinitialize with paper-init --overwrite to archive it."
        )
    required = {
        "account_id", "config_fingerprint", "cash", "positions", "high_water_mark",
        "last_equity",
    }
    missing = sorted(required - set(state))
    if missing:
        raise RuntimeError(f"Paper state is missing fields: {missing}")
    expected = _config_fingerprint(config)
    if state["config_fingerprint"] != expected:
        raise RuntimeError(
            "Paper configuration changed after account initialization. Review the changes and "
            "start a new archived epoch with paper-init --overwrite."
        )


def _config_fingerprint(config: AppConfig) -> str:
    payload = {
        "strategy": asdict(config.strategy),
        "risk": asdict(config.risk),
        "costs": asdict(config.costs),
        "universe": [asdict(item) for item in getattr(config, "instruments", ())],
        "data": {
            key: config.raw.get("data", {}).get(key)
            for key in ("provider", "adjustment", "market_close_time")
        },
    }
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode("ascii")).hexdigest()


def _save_state(path: Path, state: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write_json(path, state)


def _atomic_write_json(path: Path, value: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _append_trades(path: Path, account_id: str, trades: list) -> None:
    if not trades:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    existing_ids: set[str] = set()
    if path.exists():
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "trade_id" not in reader.fieldnames:
                raise RuntimeError(
                    f"Legacy paper trade ledger must be archived before reuse: {path}"
                )
            existing_ids = {str(row["trade_id"]) for row in reader}
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow(
                [
                    "account_id", "trade_id", "date", "symbol", "side", "quantity",
                    "price", "notional", "commission", "reason",
                ]
            )
        for trade in trades:
            trade_id = _trade_id(account_id, trade)
            if trade_id in existing_ids:
                continue
            writer.writerow(
                [
                    account_id, trade_id, trade.date.isoformat(), trade.symbol, trade.side,
                    trade.quantity, f"{trade.price:.6f}", f"{trade.notional:.2f}",
                    f"{trade.commission:.2f}", trade.reason,
                ]
            )


def _append_equity(
    path: Path,
    state: dict[str, object],
    on_date: date,
    equity: float,
    drawdown: float,
    daily_return: float,
    snapshot_id: str,
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    account_id = str(state["account_id"])
    session_id = hashlib.sha256(
        f"{account_id}|{on_date.isoformat()}|{state['config_fingerprint']}".encode("utf-8")
    ).hexdigest()[:24]
    existing_ids: set[str] = set()
    if path.exists():
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or "session_id" not in reader.fieldnames:
                raise RuntimeError(
                    f"Legacy paper equity ledger must be archived before reuse: {path}"
                )
            existing_ids = {str(row["session_id"]) for row in reader}
    if session_id in existing_ids:
        return
    exists = path.exists()
    with path.open("a", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        if not exists:
            writer.writerow(
                [
                    "account_id", "session_id", "date", "equity", "cash", "drawdown",
                    "daily_return", "positions", "pending_targets", "config_fingerprint",
                    "market_snapshot_id",
                ]
            )
        writer.writerow(
            [
                account_id,
                session_id,
                on_date.isoformat(),
                f"{equity:.6f}",
                f"{float(state['cash']):.6f}",
                f"{drawdown:.10f}",
                f"{daily_return:.10f}",
                json.dumps(state["positions"], ensure_ascii=False, sort_keys=True),
                json.dumps(state["pending_targets"], ensure_ascii=False, sort_keys=True),
                state["config_fingerprint"],
                snapshot_id,
            ]
        )


def _market_snapshot_id(market: MarketData) -> str:
    payload = "|".join(
        f"{symbol}:{digest}" for symbol, digest in sorted(market.file_hashes.items())
    )
    return hashlib.sha256(payload.encode("ascii")).hexdigest()[:24]


def _archive_existing_account(config: AppConfig) -> None:
    paths = [config.paper_state_file, config.paper_trades_file, config.paper_equity_file]
    paths.extend(config.reports_dir.glob("paper_????????.json"))
    existing = [path for path in paths if path.exists()]
    if not existing:
        return
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive = config.paper_state_file.parent / "archive" / stamp
    archive.mkdir(parents=True, exist_ok=False)
    for path in existing:
        shutil.move(str(path), archive / path.name)


@contextmanager
def _account_lock(state_path: Path):
    state_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with lock_path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise RuntimeError("Another paper account process is already running") from exc
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
