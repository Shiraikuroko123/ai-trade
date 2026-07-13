from __future__ import annotations

import csv
import json
import threading
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .. import __version__
from ..broker.live_guard import evaluate_live_readiness
from ..broker.paper import paper_status
from ..broker.paper_audit import audit_paper
from ..config import AppConfig
from ..data.eastmoney import completed_session_cutoff
from ..data.market import MarketData
from ..diagnostics import diagnose
from ..strategy import MomentumTrendStrategy


class DashboardService:
    def __init__(self, config: AppConfig):
        self.config = config
        self._lock = threading.RLock()
        self._market: MarketData | None = None
        self._market_signature: tuple[tuple[str, int], ...] | None = None

    def overview(self) -> dict[str, Any]:
        backtest = self._json_report("backtest_summary.json") or {}
        walk = self._json_report("walk_forward.json") or {}
        validation = self._json_report("validation_report.json") or {}
        state = self._paper_state()
        market_issue = None
        try:
            market = self.market()
        except (OSError, RuntimeError, ValueError) as exc:
            market = None
            market_issue = self._market_issue(exc)

        if market is None:
            diagnosis = self._unavailable_diagnosis(market_issue)
            paper_audit = self._unavailable_paper_audit(
                market_issue["message"], "market_data_unavailable"
            )
            signal = None
            market_summary = {
                "available": False,
                "date": None,
                "provider": self.config.raw["data"]["provider"],
                "adjustment": self.config.raw["data"].get("adjustment"),
                "universe": diagnosis["point_in_time_universe"],
                "warnings": diagnosis["research_warnings"],
                "error": market_issue,
            }
        else:
            diagnosis = diagnose(self.config, market)
            paper_audit = self._paper_audit(market)
            signal = self._signal(market, state)
            market_summary = {
                "available": True,
                "date": market.latest_date().isoformat(),
                "provider": self.config.raw["data"]["provider"],
                "adjustment": self.config.raw["data"].get("adjustment"),
                "universe": diagnosis["point_in_time_universe"],
                "warnings": diagnosis["research_warnings"],
                "error": None,
            }
        report_statuses = self._report_statuses(
            {
                "backtest": backtest,
                "walk_forward": walk,
                "validation": validation,
            },
            market,
        )
        report_warnings = self._report_warnings(report_statuses)
        market_summary["warnings"] = [
            *market_summary["warnings"],
            *report_warnings,
        ]
        live = evaluate_live_readiness(self.config, paper_audit)
        return {
            "generated_at": _now(),
            "version": __version__,
            "errors": [market_issue] if market_issue else [],
            "market": market_summary,
            "paper": self._paper_summary(state, paper_audit),
            "signal": signal,
            "research": {
                "backtest": backtest.get("strategy_metrics"),
                "benchmark": backtest.get("benchmark_metrics"),
                "period": [
                    backtest.get("metadata", {}).get("start"),
                    backtest.get("metadata", {}).get("end"),
                ],
                "walk_forward": walk.get("aggregate"),
                "gates": validation.get("research_gates"),
                "reports": report_statuses,
            },
            "equity_curve": self._equity_curve(480),
            "live": live,
        }

    def research(self) -> dict[str, Any]:
        backtest = self._json_report("backtest_summary.json") or {}
        walk = self._json_report("walk_forward.json") or {}
        validation = self._json_report("validation_report.json") or {}
        market = None
        market_issue = None
        if backtest or walk or validation:
            try:
                market = self.market()
            except (OSError, RuntimeError, ValueError) as exc:
                market_issue = self._market_issue(exc)
        report_statuses = self._report_statuses(
            {
                "backtest": backtest,
                "walk_forward": walk,
                "validation": validation,
            },
            market,
        )
        return {
            "generated_at": _now(),
            "errors": [market_issue] if market_issue else [],
            "warnings": self._report_warnings(report_statuses),
            "reports": report_statuses,
            "configuration": self._research_configuration(),
            "backtest": {
                "metrics": backtest.get("strategy_metrics"),
                "benchmark": backtest.get("benchmark_metrics"),
                "metadata": backtest.get("metadata"),
                "equity_curve": self._equity_curve(720),
            },
            "walk_forward": {
                "aggregate": walk.get("aggregate"),
                "segments": walk.get("segments", []),
                "selection_disclosure": walk.get("selection_disclosure"),
            },
            "validation": {
                "baseline": validation.get("baseline"),
                "bootstrap": validation.get("bootstrap"),
                "cost_stress": validation.get("cost_stress", []),
                "parameter_sensitivity": validation.get("parameter_sensitivity"),
                "regime_stress": validation.get("regime_stress", []),
                "research_gates": validation.get("research_gates"),
                "selection_disclosure": validation.get("selection_disclosure"),
            },
        }

    def portfolio(self) -> dict[str, Any]:
        state = self._paper_state()
        if not state:
            return {
                "generated_at": _now(),
                "initialized": False,
                "positions": [],
                "pending_targets": [],
                "equity_curve": [],
            }
        market = self.market()
        equity = float(state.get("last_equity", 0))
        positions = []
        for symbol, raw_quantity in dict(state.get("positions", {})).items():
            quantity = int(raw_quantity)
            bar = market.latest_bar_on_or_before(symbol, market.latest_date())
            price = bar.close if bar else 0.0
            value = price * quantity
            instrument = market.instrument(symbol)
            positions.append(
                {
                    "symbol": symbol,
                    "name": instrument.name,
                    "quantity": quantity,
                    "price": price,
                    "market_value": value,
                    "weight": value / equity if equity > 0 else 0.0,
                    "asset_class": instrument.asset_class,
                    "sector": instrument.sector,
                }
            )
        pending = []
        for symbol, weight in dict(state.get("pending_targets") or {}).items():
            instrument = market.instrument(symbol)
            current = next(
                (value["weight"] for value in positions if value["symbol"] == symbol),
                0.0,
            )
            pending.append(
                {
                    "symbol": symbol,
                    "name": instrument.name,
                    "current_weight": current,
                    "target_weight": float(weight),
                    "difference": float(weight) - current,
                }
            )
        return {
            "generated_at": _now(),
            "initialized": True,
            "account_id": state.get("account_id"),
            "date": state.get("last_run_date"),
            "equity": equity,
            "cash": float(state.get("cash", 0)),
            "cash_weight": float(state.get("cash", 0)) / equity if equity > 0 else 0.0,
            "drawdown": (
                equity / float(state.get("high_water_mark", equity)) - 1.0
                if equity > 0 and float(state.get("high_water_mark", equity)) > 0
                else 0.0
            ),
            "cooldown_remaining": int(state.get("cooldown_remaining", 0)),
            "pending_signal_date": state.get("pending_signal_date"),
            "positions": positions,
            "pending_targets": pending,
            "equity_curve": self._paper_equity_curve(480),
        }

    def trading(self) -> dict[str, Any]:
        state = self._paper_state()
        market_issue = None
        try:
            market = self.market()
        except (OSError, RuntimeError, ValueError) as exc:
            market = None
            market_issue = self._market_issue(exc)
        if market is None:
            paper_audit = self._unavailable_paper_audit(
                market_issue["message"], "market_data_unavailable"
            )
        else:
            paper_audit = self._paper_audit(market)
        return {
            "generated_at": _now(),
            "errors": [market_issue] if market_issue else [],
            "paper_audit": paper_audit,
            "live": evaluate_live_readiness(self.config, paper_audit),
            "paper_trades": self._csv_rows(self.config.paper_trades_file, 200),
            "paper_rejections": self._csv_rows(self.config.paper_rejections_file, 200),
            "broker_orders": self._csv_rows(self.config.broker_orders_file, 200),
            "broker_fills": self._csv_rows(self.config.broker_fills_file, 200),
            "pending_targets": dict((state or {}).get("pending_targets") or {}),
        }

    def universe(self, on_date: date | None = None) -> dict[str, Any]:
        market_issue = None
        try:
            market = self.market()
        except (OSError, RuntimeError, ValueError) as exc:
            market = None
            market_issue = self._market_issue(exc)
        selected_date = on_date or (
            market.latest_date()
            if market is not None
            else completed_session_cutoff(
                market_close=self.config.raw["data"].get("market_close_time", "15:30")
            )
        )
        snapshot = self.config.security_master.snapshot(
            self.config.universe_name,
            selected_date,
            self.config.minimum_listing_days,
        )
        coverage = (
            diagnose(self.config, market)["coverage"]
            if market is not None
            else self._unavailable_diagnosis(market_issue)["coverage"]
        )
        for item in snapshot["instruments"]:
            item_coverage = dict(coverage.get(item["symbol"]) or {})
            bar = (
                market.latest_bar_on_or_before(item["symbol"], selected_date)
                if market is not None and item["symbol"] in market.symbols
                else None
            )
            item_coverage["cache_last"] = item_coverage.get("last")
            item_coverage["last"] = bar.date.isoformat() if bar else None
            item["coverage"] = item_coverage
            item["latest_close"] = bar.close if bar else None
            item["latest_bar_date"] = bar.date.isoformat() if bar else None
        snapshot["market_available"] = market is not None
        snapshot["errors"] = [market_issue] if market_issue else []
        return snapshot

    def system(self) -> dict[str, Any]:
        market_issue = None
        try:
            market = self.market()
            diagnosis = diagnose(self.config, market)
        except (OSError, RuntimeError, ValueError) as exc:
            market_issue = self._market_issue(exc)
            diagnosis = self._unavailable_diagnosis(market_issue)
        reports = []
        for path in sorted(self.config.reports_dir.glob("*")):
            if path.is_file() and path.name != ".gitkeep":
                stat = path.stat()
                reports.append(
                    {
                        "name": path.name,
                        "size": stat.st_size,
                        "updated_at": datetime.fromtimestamp(
                            stat.st_mtime, timezone.utc
                        ).isoformat(),
                    }
                )
        return {
            "generated_at": _now(),
            "errors": [market_issue] if market_issue else [],
            "diagnosis": diagnosis,
            "paths": {
                "project": str(self.config.project_root),
                "config": str(self.config.path),
                "cache": str(self.config.cache_dir),
                "reports": str(self.config.reports_dir),
                "logs": str(self.config.logs_dir),
            },
            "reports": reports,
            "broker": {
                "mode": self.config.raw.get("broker", {}).get("mode", "disabled"),
                "adapter": self.config.raw.get("broker", {}).get("adapter"),
                "account_configured": bool(
                    self.config.raw.get("broker", {}).get("account_id")
                ),
            },
        }

    def storage(self, *, refresh: bool = False) -> dict[str, Any]:
        from ..cloud import cloud_dashboard_status

        return cloud_dashboard_status(self.config, refresh=refresh)

    def save_storage_preferences(self, payload: dict[str, object]) -> dict[str, Any]:
        from ..cloud import save_cloud_dashboard_preferences

        return save_cloud_dashboard_preferences(self.config, payload)

    def market(self) -> MarketData:
        signature = tuple(
            sorted(
                (str(path), path.stat().st_mtime_ns)
                for path in self.config.cache_dir.glob("*")
                if path.is_file()
            )
        )
        with self._lock:
            if self._market is None or signature != self._market_signature:
                self._market = MarketData(self.config)
                self._market_signature = signature
            return self._market

    def _signal(
        self, market: MarketData, state: dict[str, Any] | None
    ) -> dict[str, Any]:
        equity = float((state or {}).get("last_equity", 0)) or None
        signal = MomentumTrendStrategy(self.config.strategy).generate(
            market, market.latest_date(), equity
        )
        return {
            "date": signal.date.isoformat(),
            "target_weights": signal.target_weights,
            "reason": signal.reason,
            "diagnostics": signal.diagnostics,
            "ranking": [value.__dict__ for value in signal.ranked],
        }

    def _paper_state(self) -> dict[str, Any] | None:
        try:
            return paper_status(self.config)
        except (FileNotFoundError, RuntimeError, ValueError):
            return None

    def _paper_audit(self, market: MarketData) -> dict[str, Any]:
        try:
            return audit_paper(self.config, market)
        except (FileNotFoundError, RuntimeError, ValueError) as exc:
            return self._unavailable_paper_audit(str(exc), "paper_account_unavailable")

    def _unavailable_paper_audit(self, message: str, status: str) -> dict[str, Any]:
        minimum = int(self.config.raw["paper"].get("minimum_promotion_sessions", 60))
        return {
            "sessions": 0,
            "minimum_promotion_sessions": minimum,
            "remaining_sessions": minimum,
            "integrity_errors": [message],
            "promotion_checks": {},
            "eligible_for_broker_sandbox": False,
            "live_ready": False,
            "status": status,
        }

    def _market_issue(self, exc: Exception) -> dict[str, str]:
        return {
            "code": "market_data_unavailable",
            "message": (
                "Market data is unavailable. Run the refresh-data action or execute "
                "'ai-trade download --force', then reload this page."
            ),
            "detail": str(exc),
            "recovery_action": "refresh-data",
            "recovery_command": "ai-trade download --force",
        }

    def _report_statuses(
        self,
        reports: dict[str, dict[str, Any]],
        market: MarketData | None,
    ) -> dict[str, dict[str, Any]]:
        specifications = {
            "backtest": ("backtest_summary.json", "backtest"),
            "walk_forward": ("walk_forward.json", "walk-forward"),
            "validation": ("validation_report.json", "validate"),
        }
        result = {}
        for name, (filename, action) in specifications.items():
            path = self.config.reports_dir / filename
            payload = reports.get(name) or {}
            if not path.exists():
                state = "missing"
                message = f"{filename} is missing; run the {action} action"
                current = None
            elif not payload:
                state = "invalid"
                message = f"{filename} is not valid JSON; rerun the {action} action"
                current = None
            elif market is None:
                state = "unverifiable"
                message = (
                    f"{filename} is available, but its data snapshot cannot be verified "
                    "until market data is available"
                )
                current = None
            else:
                matches = self._report_matches_market(name, payload, market)
                if matches is True:
                    state = "current"
                    message = "Report data snapshot matches the active market cache"
                    current = True
                elif matches is False:
                    state = "stale"
                    message = (
                        f"{filename} was generated from a different data snapshot; "
                        f"rerun the {action} action"
                    )
                    current = False
                else:
                    state = "unverifiable"
                    message = (
                        f"{filename} does not contain a verifiable data snapshot; "
                        f"rerun the {action} action"
                    )
                    current = None
            try:
                updated_at = (
                    datetime.fromtimestamp(
                        path.stat().st_mtime, timezone.utc
                    ).isoformat()
                    if path.exists()
                    else None
                )
            except OSError:
                updated_at = None
            result[name] = {
                "filename": filename,
                "available": bool(payload),
                "state": state,
                "current": current,
                "message": message,
                "recovery_action": action,
                "updated_at": updated_at,
            }
        return result

    def _report_matches_market(
        self,
        name: str,
        report: dict[str, Any],
        market: MarketData,
    ) -> bool | None:
        if name == "backtest":
            metadata = report.get("metadata")
            snapshot = (
                metadata.get("data_snapshot") if isinstance(metadata, dict) else None
            )
        else:
            snapshot = report.get("data_snapshot")
        if not isinstance(snapshot, dict):
            return None

        report_symbols = snapshot.get("symbols")
        if not isinstance(report_symbols, dict):
            return None
        report_hashes = {
            symbol: value.get("sha256")
            for symbol, value in report_symbols.items()
            if isinstance(value, dict)
        }
        if set(report_hashes) != set(market.file_hashes):
            return False
        if any(
            report_hashes.get(symbol) != digest
            for symbol, digest in market.file_hashes.items()
        ):
            return False
        if snapshot.get("provider") != self.config.raw["data"]["provider"]:
            return False
        if snapshot.get("adjustment") != self.config.raw["data"].get(
            "adjustment", "none"
        ):
            return False
        universe = snapshot.get("universe")
        if not isinstance(universe, dict):
            return None
        return (
            universe.get("name") == self.config.universe_name
            and universe.get("security_master_sha256")
            == self.config.security_master.fingerprint()
        )

    @staticmethod
    def _report_warnings(statuses: dict[str, dict[str, Any]]) -> list[str]:
        return [
            str(value["message"])
            for value in statuses.values()
            if value.get("state") != "current"
        ]

    def _research_configuration(self) -> dict[str, dict[str, Any]]:
        result = {}
        for name in ("strategy", "risk"):
            value = getattr(self.config, name, None)
            result[name] = asdict(value) if is_dataclass(value) else {}
        return result

    def _unavailable_diagnosis(self, issue: dict[str, str]) -> dict[str, Any]:
        coverage = {}
        missing = []
        for instrument in self.config.instruments:
            path = self.config.cache_dir / f"{instrument.symbol}.csv"
            exists = path.is_file()
            if not exists:
                missing.append(instrument.symbol)
            coverage[instrument.symbol] = {
                "name": instrument.name,
                "cache_exists": exists,
                "cache_file": str(path),
                "rows": None,
                "first": None,
                "last": None,
            }
        return {
            "status": "ERROR",
            "config": str(self.config.path),
            "completed_session_cutoff": None,
            "latest_market_date": None,
            "latest_common_market_date": None,
            "market_data_current": False,
            "market_data_lag_days": None,
            "universe_latest_dates_aligned": False,
            "point_in_time_universe": {
                "name": self.config.universe_name,
                "active_count": None,
                "loaded_instrument_count": len(self.config.instruments),
                "selection_method": self.config.security_master.metadata.get(
                    "selection_method"
                ),
                "security_master_sha256": self.config.security_master.fingerprint(),
            },
            "research_warnings": [issue["message"]],
            "coverage": coverage,
            "missing_cache_symbols": missing,
            "error": issue,
            "cache_manifest": {
                "available": False,
                "downloaded_at": None,
                "completed_through": None,
                "latest_common_session": None,
                "request_policy": None,
                "source_counts": {},
                "refresh_failures": [],
            },
            "live_trading": "DISABLED",
        }

    def _paper_summary(
        self,
        state: dict[str, Any] | None,
        audit: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "initialized": state is not None,
            "account_id": (state or {}).get("account_id"),
            "date": (state or {}).get("last_run_date"),
            "equity": (state or {}).get("last_equity"),
            "cash": (state or {}).get("cash"),
            "positions": (state or {}).get("positions", {}),
            "pending_targets": (state or {}).get("pending_targets") or {},
            "cooldown_remaining": (state or {}).get("cooldown_remaining", 0),
            "audit": audit,
        }

    def _json_report(self, name: str) -> dict[str, Any] | None:
        path = self.config.reports_dir / name
        if not path.exists():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def _equity_curve(self, maximum: int) -> list[dict[str, Any]]:
        rows = self._csv_rows(self.config.reports_dir / "equity_curve.csv")
        return _sample(rows, maximum)

    def _paper_equity_curve(self, maximum: int) -> list[dict[str, Any]]:
        rows = self._csv_rows(self.config.paper_equity_file)
        return _sample(rows, maximum)

    @staticmethod
    def _csv_rows(path: Path, limit: int | None = None) -> list[dict[str, str]]:
        if not path.exists():
            return []
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))
        except (OSError, csv.Error):
            return []
        return rows[-limit:] if limit else rows


def _sample(rows: list[dict[str, Any]], maximum: int) -> list[dict[str, Any]]:
    if len(rows) <= maximum:
        return rows
    step = (len(rows) - 1) / (maximum - 1)
    indexes = sorted({round(index * step) for index in range(maximum)})
    return [rows[index] for index in indexes]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
