from __future__ import annotations

import csv
import json
import math
import threading
from collections import deque
from dataclasses import asdict, is_dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Any

from .. import __version__
from ..assistant import AssistantEngine
from ..broker.base import BrokerEnvironment
from ..broker.ledger import recover_order_lifecycle
from ..broker.live_guard import (
    broker_configuration_fingerprint,
    evaluate_live_readiness,
)
from ..broker.paper import paper_status
from ..broker.paper_audit import audit_paper
from ..broker.shadow import import_shadow_csv, shadow_account_status
from ..broker.scope import create_broker_ledger_scope
from ..config import AppConfig
from ..data.eastmoney import completed_session_cutoff
from ..data.market import MarketData
from ..diagnostics import diagnose
from ..strategy_lab import StrategyLabConflictError, StrategyLabEngine
from ..strategy import MomentumTrendStrategy


_STRATEGY_VALIDATION_LOCK = threading.Lock()
_MARKET_CHART_PERIODS = frozenset({"day", "week", "month"})
_MARKET_CHART_MIN_LIMIT = 60
_MARKET_CHART_MAX_LIMIT = 1500
_MARKET_CHART_MAX_TRADE_MARKERS = 500
_MARKET_CHART_MAX_EXCLUDED_DATES = 20


class DashboardService:
    def __init__(self, config: AppConfig):
        self.config = config
        self._lock = threading.RLock()
        self._market: MarketData | None = None
        self._market_signature: tuple[tuple[str, int], ...] | None = None
        self._assistant: AssistantEngine | None = None
        self._strategy_lab: StrategyLabEngine | None = None

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
        live = _public_live_readiness(self.config, paper_audit, market)
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

    def trading(self, *, owner_id: str = "local-owner") -> dict[str, Any]:
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
        paper_trades = self._csv_rows(self.config.paper_trades_file, 200)
        broker_lifecycle = recover_order_lifecycle(
            self.config.broker_orders_file,
            self.config.broker_fills_file,
            scope_path=self.config.broker_ledger_scope_file,
            expected_scope=self._broker_ledger_scope(),
        )
        broker_fills = list(broker_lifecycle.pop("fills", []))
        return {
            "generated_at": _now(),
            "errors": [market_issue] if market_issue else [],
            "paper_audit": paper_audit,
            "live": _public_live_readiness(self.config, paper_audit, market),
            "paper_trades": paper_trades,
            "paper_rejections": self._csv_rows(self.config.paper_rejections_file, 200),
            "broker_fills": broker_fills[:200],
            "broker_lifecycle": broker_lifecycle,
            "pending_targets": dict((state or {}).get("pending_targets") or {}),
            "shadow_account": self._shadow_account(owner_id, state),
        }

    def _broker_ledger_scope(self):
        broker = self.config.raw.get("broker", {})
        mode = broker.get("mode", "disabled")
        adapter = broker.get("adapter")
        account_id = broker.get("account_id")
        if mode not in {"sandbox", "live"} or not adapter or not account_id:
            return None
        return create_broker_ledger_scope(
            adapter=str(adapter),
            account_id=str(account_id),
            environment=BrokerEnvironment(mode),
            config_fingerprint=broker_configuration_fingerprint(self.config),
            orders_path=self.config.broker_orders_file,
            fills_path=self.config.broker_fills_file,
        )

    def import_shadow_account(
        self,
        *,
        owner_id: str,
        source_label: str,
        account_alias: str,
        csv_content: bytes,
    ) -> dict[str, Any]:
        result = import_shadow_csv(
            self.config.shadow_fills_file,
            self.config.shadow_imports_file,
            owner_id=owner_id,
            source_label=source_label,
            account_alias=account_alias,
            content=csv_content,
            max_bytes=self.config.shadow_max_import_bytes,
        )
        return {
            "generated_at": _now(),
            "import_result": result,
            "shadow_account": self._shadow_account(owner_id, self._paper_state()),
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

    def market_chart(
        self, *, symbol: str, period: str = "day", limit: int = 240
    ) -> dict[str, Any]:
        instruments = {item.symbol: item for item in self.config.instruments}
        if symbol not in instruments:
            raise ValueError("symbol must be an instrument in the configured universe")
        if period not in _MARKET_CHART_PERIODS:
            raise ValueError("period must be day, week, or month")
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not _MARKET_CHART_MIN_LIMIT <= limit <= _MARKET_CHART_MAX_LIMIT
        ):
            raise ValueError(
                f"limit must be an integer between {_MARKET_CHART_MIN_LIMIT} "
                f"and {_MARKET_CHART_MAX_LIMIT}"
            )

        instrument = instruments[symbol]
        try:
            market = self.market(recover_snapshot=False)
        except (OSError, RuntimeError, ValueError) as exc:
            return self._unavailable_market_chart(instrument, period, exc)
        if symbol not in market.symbols:
            return self._unavailable_market_chart(
                instrument,
                period,
                RuntimeError("The validated cache snapshot omits the requested symbol"),
            )

        daily_bars = market.symbols[symbol].bars
        aggregate_bars = _aggregate_market_chart_bars(daily_bars, period)
        selected = aggregate_bars[-limit:]
        if not selected:
            return self._unavailable_market_chart(
                instrument,
                period,
                RuntimeError("No completed bars are available for the requested symbol"),
            )

        latest_daily_date = daily_bars[-1].date
        completed_cutoff = market.completed_through
        stale = latest_daily_date < completed_cutoff
        lag_days = max(0, (completed_cutoff - latest_daily_date).days)
        manifest = market.manifest if isinstance(market.manifest, dict) else None
        manifest_file = _manifest_symbol_entry(manifest, symbol)
        warnings = []
        if stale:
            warnings.append(
                f"Data ends on {latest_daily_date.isoformat()}, before the completed-session "
                f"cutoff {completed_cutoff.isoformat()}. Refresh data or verify an exchange "
                "holiday."
            )
        if manifest is None:
            warnings.append(
                "The cache manifest is missing, so snapshot provenance cannot be verified."
            )
        excluded_dates = market.excluded_dates.get(symbol, [])
        if excluded_dates:
            warnings.append(
                "One or more cache rows after the completed-session cutoff were excluded."
            )

        trade_markers, markers_truncated = self._paper_trade_markers(
            symbol=symbol,
            period=period,
            selected_bars=selected,
        )
        response_bars = [_market_chart_bar_payload(value) for value in selected]
        snapshot_digest = _market_chart_snapshot_digest(market)
        snapshot_id = (
            f"market-{market.latest_common_session.isoformat()}-"
            f"{snapshot_digest[:12]}"
        )
        return {
            "generated_at": _now(),
            "available": True,
            "symbol": symbol,
            "name": instrument.name,
            "instrument": {
                "symbol": symbol,
                "name": instrument.name,
                "market": instrument.market,
                "instrument_type": instrument.instrument_type,
                "asset_class": instrument.asset_class,
                "currency": instrument.currency,
            },
            "period": period,
            "adjustment": self.config.raw["data"].get("adjustment", "none"),
            "provider": self.config.raw["data"]["provider"],
            "provenance": {
                "manifest_available": manifest is not None,
                "downloaded_at": _bounded_manifest_text(
                    manifest.get("downloaded_at") if manifest else None
                ),
                "source": _bounded_manifest_text(manifest_file.get("source")),
                "source_provider": _bounded_manifest_text(
                    manifest_file.get("source_provider")
                ),
                "source_mode": _bounded_manifest_text(
                    manifest_file.get("source_mode")
                ),
                "amount_quality": _bounded_manifest_text(
                    manifest_file.get("amount_quality")
                ),
            },
            "data_date": latest_daily_date.isoformat(),
            "snapshot": {
                "id": snapshot_id,
                "snapshot_id": snapshot_id,
                "dataset_sha256": snapshot_digest,
                "completed_session_cutoff": completed_cutoff.isoformat(),
                "latest_common_session": market.latest_common_session.isoformat(),
                "manifest_sha256": getattr(market, "manifest_sha256", None),
                "file_sha256": market.file_hashes[symbol],
                "symbol_file_sha256": market.file_hashes[symbol],
                "security_master_sha256": self.config.security_master.fingerprint(),
            },
            "bars": response_bars,
            "summary": _market_chart_summary(response_bars),
            "trade_markers": trade_markers,
            "diagnostics": {
                "status": "stale" if stale else "warning" if warnings else "ok",
                "missing": False,
                "stale": stale,
                "lag_calendar_days": lag_days,
                "completed_session_cutoff": completed_cutoff.isoformat(),
                "latest_completed_bar": latest_daily_date.isoformat(),
                "excluded_incomplete_count": len(excluded_dates),
                "excluded_incomplete_dates": [
                    value.isoformat()
                    for value in excluded_dates[-_MARKET_CHART_MAX_EXCLUDED_DATES:]
                ],
                "trade_markers_truncated": markers_truncated,
                "warnings": warnings,
            },
        }

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

    def assistant(self, *, user_id: str) -> dict[str, Any]:
        market = self.market()
        available = set(market.symbols)
        engine = self._assistant_engine()
        return {
            "status": engine.status(),
            "instruments": [
                {"symbol": item.symbol, "name": item.name}
                for item in self.config.instruments
                if item.symbol in available
            ],
            "defaults": {
                "symbol": self.config.strategy.benchmark,
                "lookback": 180,
                "mode": "local",
            },
            "history": engine.history(user_id, limit=20),
        }

    def assistant_analyze(
        self,
        *,
        symbol: str,
        lookback: int,
        mode: str,
        user_id: str,
    ) -> dict[str, Any]:
        return self._assistant_engine().analyze(
            self.market(),
            symbol,
            lookback=lookback,
            mode=mode,
            user_id=user_id,
        )

    def strategy_lab(self, *, owner_id: str) -> dict[str, Any]:
        engine = self._strategy_lab_engine()
        result = engine.summary(owner_id)
        result["parameter_schema"] = engine.parameter_schema()
        return result

    def strategy_lab_candidate(
        self, *, candidate_id: str, owner_id: str
    ) -> dict[str, Any]:
        return self._strategy_lab_engine().get_candidate(owner_id, candidate_id)

    def strategy_lab_create_manual(
        self,
        *,
        changes: dict[str, Any],
        title: str,
        hypothesis: str,
        reason: str,
        owner_id: str,
        actor: str,
    ) -> dict[str, Any]:
        return self._strategy_lab_engine().create_manual_candidate(
            owner_id,
            changes,
            title,
            hypothesis,
            reason,
            actor=actor,
        )

    def strategy_lab_propose(
        self,
        *,
        title: str,
        hypothesis: str,
        objective: str,
        owner_id: str,
        actor: str,
    ) -> dict[str, Any]:
        return self._strategy_lab_engine().propose_local_ai_candidate(
            owner_id,
            title,
            hypothesis,
            objective,
            actor=actor,
        )

    def strategy_lab_validate(
        self, *, candidate_id: str, owner_id: str, actor: str
    ) -> dict[str, Any]:
        if not _STRATEGY_VALIDATION_LOCK.acquire(blocking=False):
            raise StrategyLabConflictError(
                "已有策略验证正在运行；当前服务一次只能验证一个候选，请稍后重试"
            )
        try:
            return self._strategy_lab_engine().validate_candidate(
                owner_id,
                candidate_id,
                self.market(),
                actor=actor,
            )
        finally:
            _STRATEGY_VALIDATION_LOCK.release()

    def strategy_lab_approve(
        self, *, candidate_id: str, note: str, owner_id: str, actor: str
    ) -> dict[str, Any]:
        return self._strategy_lab_engine().approve_candidate(
            owner_id,
            candidate_id,
            approved_by=actor,
            note=note,
        )

    def strategy_lab_export(
        self, *, candidate_id: str, owner_id: str, actor: str
    ) -> dict[str, Any]:
        return self._strategy_lab_engine().export_paper_config(
            owner_id,
            candidate_id,
            actor=actor,
        )

    def strategy_lab_activate(
        self, *, candidate_id: str, note: str, owner_id: str, actor: str
    ) -> dict[str, Any]:
        return self._strategy_lab_engine().activate_candidate(
            owner_id,
            candidate_id,
            activated_by=actor,
            note=note,
        )

    def strategy_lab_monitor(self, *, owner_id: str, actor: str) -> dict[str, Any]:
        if not _STRATEGY_VALIDATION_LOCK.acquire(blocking=False):
            raise StrategyLabConflictError(
                "已有策略回测任务正在运行；当前服务一次只允许一项策略验证或监控"
            )
        try:
            return self._strategy_lab_engine().monitor_active_candidate(
                owner_id,
                self.market(),
                actor=actor,
            )
        finally:
            _STRATEGY_VALIDATION_LOCK.release()

    def strategy_lab_lifecycle(
        self,
        *,
        action: str,
        note: str,
        expected_active_candidate_id: str,
        expected_active_fingerprint: str,
        monitor_id: str | None,
        owner_id: str,
        actor: str,
    ) -> dict[str, Any]:
        engine = self._strategy_lab_engine()
        common = {
            "actor": actor,
            "expected_active_candidate_id": expected_active_candidate_id,
            "expected_active_fingerprint": expected_active_fingerprint,
            "note": note,
            "monitor_id": monitor_id,
        }
        if action == "suspend":
            return engine.suspend_active_candidate(owner_id, **common)
        if action == "resume":
            return engine.resume_active_candidate(owner_id, **common)
        if action == "retire":
            return engine.retire_active_candidate(owner_id, **common)
        raise ValueError("Unknown strategy lifecycle action")

    def strategy_lab_rollback(
        self,
        *,
        note: str,
        expected_active_candidate_id: str,
        expected_active_fingerprint: str,
        owner_id: str,
        actor: str,
    ) -> dict[str, Any]:
        return self._strategy_lab_engine().rollback(
            owner_id,
            rolled_back_by=actor,
            expected_active_candidate_id=expected_active_candidate_id,
            expected_active_fingerprint=expected_active_fingerprint,
            note=note,
        )

    def save_storage_preferences(self, payload: dict[str, object]) -> dict[str, Any]:
        from ..cloud import save_cloud_dashboard_preferences

        return save_cloud_dashboard_preferences(self.config, payload)

    def market(self, *, recover_snapshot: bool = True) -> MarketData:
        signature = tuple(
            sorted(
                (str(path), path.stat().st_mtime_ns)
                for path in self.config.cache_dir.glob("*")
                if path.is_file()
            )
        )
        with self._lock:
            if self._market is None or signature != self._market_signature:
                self._market = MarketData(
                    self.config, recover_snapshot=recover_snapshot
                )
                self._market_signature = signature
            return self._market

    def _assistant_engine(self) -> AssistantEngine:
        with self._lock:
            if self._assistant is None:
                self._assistant = AssistantEngine(self.config)
            return self._assistant

    def _strategy_lab_engine(self) -> StrategyLabEngine:
        with self._lock:
            if self._strategy_lab is None:
                self._strategy_lab = StrategyLabEngine(self.config)
            return self._strategy_lab

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

    def _shadow_account(
        self, owner_id: str, state: dict[str, Any] | None
    ) -> dict[str, Any]:
        account_id = str((state or {}).get("account_id") or "")
        expected = [
            row
            for row in self._csv_rows(self.config.paper_trades_file)
            if not account_id or row.get("account_id") == account_id
        ]
        status = shadow_account_status(
            self.config.shadow_fills_file,
            self.config.shadow_imports_file,
            owner_id=owner_id,
            expected_trades=expected,
        )
        status["max_import_bytes"] = self.config.shadow_max_import_bytes
        return status

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

    def _unavailable_market_chart(
        self, instrument: Any, period: str, exc: Exception
    ) -> dict[str, Any]:
        issue = self._market_issue(exc)
        completed_cutoff = completed_session_cutoff(
            market_close=self.config.raw["data"].get("market_close_time", "15:30")
        ).isoformat()
        return {
            "generated_at": _now(),
            "available": False,
            "symbol": instrument.symbol,
            "name": instrument.name,
            "instrument": {
                "symbol": instrument.symbol,
                "name": instrument.name,
                "market": instrument.market,
                "instrument_type": instrument.instrument_type,
                "asset_class": instrument.asset_class,
                "currency": instrument.currency,
            },
            "period": period,
            "adjustment": self.config.raw["data"].get("adjustment", "none"),
            "provider": self.config.raw["data"]["provider"],
            "provenance": {"manifest_available": False},
            "data_date": None,
            "snapshot": {
                "id": None,
                "snapshot_id": None,
                "dataset_sha256": None,
                "completed_session_cutoff": completed_cutoff,
                "latest_common_session": None,
                "manifest_sha256": None,
                "file_sha256": None,
                "symbol_file_sha256": None,
                "security_master_sha256": self.config.security_master.fingerprint(),
            },
            "bars": [],
            "summary": None,
            "trade_markers": [],
            "diagnostics": {
                "status": "missing",
                "missing": True,
                "stale": None,
                "lag_calendar_days": None,
                "completed_session_cutoff": completed_cutoff,
                "latest_completed_bar": None,
                "excluded_incomplete_count": 0,
                "excluded_incomplete_dates": [],
                "trade_markers_truncated": False,
                "code": issue["code"],
                "message": issue["message"],
                "detail": issue["detail"][:1000],
                "recovery_action": issue["recovery_action"],
                "recovery_command": issue["recovery_command"],
                "warnings": [issue["message"]],
            },
        }

    def _paper_trade_markers(
        self,
        *,
        symbol: str,
        period: str,
        selected_bars: list[dict[str, Any]],
    ) -> tuple[list[dict[str, Any]], bool]:
        state = self._paper_state()
        account_id = str((state or {}).get("account_id") or "")
        path = self.config.paper_trades_file
        if not account_id or not path.exists() or path.is_symlink():
            return [], False
        bar_dates = {
            _market_chart_period_key(value["date"], period): value["date"]
            for value in selected_bars
        }
        required = {
            "account_id",
            "trade_id",
            "date",
            "symbol",
            "side",
            "quantity",
            "price",
        }
        markers: deque[dict[str, Any]] = deque(
            maxlen=_MARKET_CHART_MAX_TRADE_MARKERS
        )
        accepted = 0
        try:
            with path.open("r", encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                if not reader.fieldnames or not required.issubset(reader.fieldnames):
                    return [], False
                for row in reader:
                    if row.get("account_id") != account_id or row.get("symbol") != symbol:
                        continue
                    try:
                        trade_date = date.fromisoformat(str(row["date"]))
                        side = str(row["side"])
                        quantity = int(row["quantity"])
                        price = float(row["price"])
                    except (KeyError, TypeError, ValueError):
                        continue
                    bar_date = bar_dates.get(_market_chart_period_key(trade_date, period))
                    trade_id = str(row["trade_id"])
                    if (
                        bar_date is None
                        or side not in {"BUY", "SELL"}
                        or len(trade_id) != 24
                        or any(value not in "0123456789abcdef" for value in trade_id)
                        or quantity <= 0
                        or not math.isfinite(price)
                        or price <= 0
                    ):
                        continue
                    accepted += 1
                    markers.append(
                        {
                            "trade_id": trade_id,
                            "date": trade_date.isoformat(),
                            "bar_date": bar_date.isoformat(),
                            "side": side,
                            "quantity": quantity,
                            "price": price,
                        }
                    )
        except (OSError, csv.Error):
            return [], False
        return list(markers), accepted > _MARKET_CHART_MAX_TRADE_MARKERS

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


def _aggregate_market_chart_bars(
    bars: list[Any], period: str
) -> list[dict[str, Any]]:
    groups: list[list[Any]] = []
    for bar in bars:
        key = _market_chart_period_key(bar.date, period)
        if not groups or _market_chart_period_key(groups[-1][0].date, period) != key:
            groups.append([bar])
        else:
            groups[-1].append(bar)

    result = []
    for values in groups:
        try:
            volume = math.fsum(float(value.volume) for value in values)
            amount = math.fsum(float(value.amount) for value in values)
        except OverflowError as exc:
            raise RuntimeError(
                "Aggregated market chart contains a non-finite value"
            ) from exc
        numbers = {
            "open": float(values[0].open),
            "high": max(float(value.high) for value in values),
            "low": min(float(value.low) for value in values),
            "close": float(values[-1].close),
            "volume": volume,
            "amount": amount,
        }
        if not all(math.isfinite(value) for value in numbers.values()):
            raise RuntimeError("Aggregated market chart contains a non-finite value")
        result.append({"date": values[-1].date, **numbers})
    return result


def _market_chart_period_key(value: date, period: str) -> object:
    if period == "day":
        return value
    if period == "week":
        calendar = value.isocalendar()
        return calendar.year, calendar.week
    if period == "month":
        return value.year, value.month
    raise ValueError("period must be day, week, or month")


def _market_chart_bar_payload(value: dict[str, Any]) -> dict[str, Any]:
    return {
        "date": value["date"].isoformat(),
        "open": value["open"],
        "high": value["high"],
        "low": value["low"],
        "close": value["close"],
        "volume": value["volume"],
        "amount": value["amount"],
    }


def _market_chart_summary(bars: list[dict[str, Any]]) -> dict[str, Any]:
    latest = bars[-1]
    previous_close = bars[-2]["close"] if len(bars) > 1 else None
    change = latest["close"] - previous_close if previous_close is not None else None
    change_percent = change / previous_close if previous_close is not None else None
    if change is not None and not math.isfinite(change):
        raise RuntimeError("Market chart summary change is non-finite")
    if change_percent is not None and not math.isfinite(change_percent):
        raise RuntimeError("Market chart summary change percentage is non-finite")
    return {
        "latest_date": latest["date"],
        "latest_open": latest["open"],
        "latest_high": latest["high"],
        "latest_low": latest["low"],
        "latest_close": latest["close"],
        "previous_close": previous_close,
        "change": change,
        "change_percent": change_percent,
        "volume": latest["volume"],
        "amount": latest["amount"],
        "bar_count": len(bars),
    }


def _manifest_symbol_entry(
    manifest: dict[str, Any] | None, symbol: str
) -> dict[str, Any]:
    files = manifest.get("files") if manifest else None
    if not isinstance(files, dict):
        return {}
    value = files.get(symbol)
    return value if isinstance(value, dict) else {}


def _bounded_manifest_text(value: object, maximum: int = 128) -> str | None:
    if not isinstance(value, str) or not value or len(value) > maximum:
        return None
    return value


def _market_chart_snapshot_digest(market: Any) -> str:
    values = [
        f"manifest:{getattr(market, 'manifest_sha256', None) or 'missing'}",
        *(f"{symbol}:{digest}" for symbol, digest in sorted(market.file_hashes.items())),
    ]
    return sha256("|".join(values).encode("utf-8")).hexdigest()


def _sample(rows: list[dict[str, Any]], maximum: int) -> list[dict[str, Any]]:
    if len(rows) <= maximum:
        return rows
    step = (len(rows) - 1) / (maximum - 1)
    indexes = sorted({round(index * step) for index in range(maximum)})
    return [rows[index] for index in indexes]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public_live_readiness(
    config: AppConfig,
    paper_audit: dict[str, Any],
    market: MarketData | None,
) -> dict[str, Any]:
    readiness = evaluate_live_readiness(
        config,
        paper_audit,
        completed_market_date=market.latest_date() if market is not None else None,
    )
    for field in ("account_id", "kill_switch_file", "batch_approval_file"):
        readiness.pop(field, None)
    return readiness
