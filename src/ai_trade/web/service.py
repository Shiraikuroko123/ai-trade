from __future__ import annotations

import csv
import json
import math
import statistics
import threading
from collections import Counter, deque
from dataclasses import asdict, is_dataclass, replace
from datetime import date, datetime, timedelta, timezone
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
from ..data.capital_flow import CapitalFlowQuery, CapitalFlowStore
from ..data.cross_check import cross_source_projection
from ..data.eastmoney import completed_session_cutoff
from ..data.market import MarketData
from ..data.market_breadth import MarketBreadthQuery, MarketBreadthStore
from ..data.market_intelligence import DragonTigerQuery, DragonTigerStore
from ..data.intraday import IntradayQuery, IntradayStore
from ..data.valuation import ValuationQuery, ValuationStore
from ..data.news import NewsQuery, NewsStore
from ..diagnostics import diagnose
from ..json_utils import load_unique_json
from ..monitoring import MonitoringEngine
from ..research_digest import (
    DIGEST_KINDS,
    ResearchDigestBatchError,
    ResearchDigestDraft,
    ResearchDigestQuery,
    ResearchDigestStore,
    unavailable_research_digests,
)
from ..research_archive import (
    ResearchArchiveProjection,
    ResearchArchiveQuery,
    unavailable_research_archive,
)
from ..research_journal import (
    JOURNAL_CATEGORIES,
    JOURNAL_DECISIONS,
    JournalDraft,
    JournalQuery,
    ResearchJournalStore,
    unavailable_journal,
)
from ..strategy_lab import StrategyLabConflictError, StrategyLabEngine
from ..strategy import MomentumTrendStrategy
from .screener import ScreeningFilters, screen_rows


_STRATEGY_VALIDATION_LOCK = threading.Lock()
_MARKET_CHART_PERIODS = frozenset({"day", "week", "month"})
_MARKET_CHART_MIN_LIMIT = 60
_MARKET_CHART_MAX_LIMIT = 1500
_MARKET_CHART_MAX_TRADE_MARKERS = 500
_MARKET_CHART_MAX_EXCLUDED_DATES = 20
MAX_DASHBOARD_REPORT_BYTES = 8 * 1024 * 1024
SCREEN_SCHEMA_VERSION = 2

# Keep the definitions next to the calculations so the API can disclose the
# exact research vocabulary used by the table.  These are descriptive only;
# they do not change strategy or execution behavior.
SCREEN_METRIC_DEFINITIONS: dict[str, dict[str, str]] = {
    "momentum": {
        "label": "动量",
        "formula": "close[t-skip] / close[t-skip-lookback] - 1",
        "unit": "ratio",
        "window": "strategy.lookback_days, strategy.skip_days",
    },
    "annual_volatility": {
        "label": "年化波动",
        "formula": "stdev(daily_returns) * sqrt(252)",
        "unit": "ratio",
        "window": "strategy.volatility_days",
    },
    "average_amount": {
        "label": "20 日平均成交额",
        "formula": "mean(amount over the latest 20 completed bars)",
        "unit": "CNY",
        "window": "20 completed bars",
    },
    "trend": {
        "label": "趋势",
        "formula": "latest close compared with trend SMA",
        "unit": "categorical",
        "window": "strategy.trend_sma_days",
    },
    "coverage": {
        "label": "历史覆盖",
        "formula": "available bars / minimum required bars, capped at 100%",
        "unit": "percent",
        "window": "minimum research history",
    },
}


class DashboardService:
    def __init__(self, config: AppConfig):
        self.config = config
        self._lock = threading.RLock()
        self._market: MarketData | None = None
        self._market_signature: tuple[tuple[str, int], ...] | None = None
        self._assistant: AssistantEngine | None = None
        self._strategy_lab: StrategyLabEngine | None = None
        self._research_journal: ResearchJournalStore | None = None
        self._research_archive: ResearchArchiveProjection | None = None
        self._research_digests: ResearchDigestStore | None = None
        self._monitoring: MonitoringEngine | None = None

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
                "freshness": {
                    "status": diagnosis.get("status", "ERROR"),
                    "completed_session_cutoff": diagnosis.get(
                        "completed_session_cutoff"
                    ),
                    "latest_common_market_date": diagnosis.get(
                        "latest_common_market_date"
                    ),
                    "lag_calendar_days": diagnosis.get("market_data_lag_days"),
                    "current": diagnosis.get("market_data_current", False),
                },
                "provenance": diagnosis.get("cache_manifest", {}),
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
                "freshness": {
                    "status": diagnosis["status"],
                    "completed_session_cutoff": diagnosis[
                        "completed_session_cutoff"
                    ],
                    "latest_common_market_date": diagnosis[
                        "latest_common_market_date"
                    ],
                    "lag_calendar_days": diagnosis["market_data_lag_days"],
                    "current": diagnosis["market_data_current"],
                },
                "provenance": diagnosis["cache_manifest"],
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

    def research(
        self,
        *,
        owner_id: str = "local-owner",
        journal_query: JournalQuery | None = None,
    ) -> dict[str, Any]:
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
        try:
            journal = self.research_journal(
                owner_id=owner_id,
                journal_query=journal_query,
            )
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            journal = unavailable_journal(str(exc))
        try:
            archives = self.research_archive(owner_id=owner_id)
        except (AttributeError, OSError, RuntimeError, ValueError, KeyError) as exc:
            archives = unavailable_research_archive(str(exc))
        try:
            digests = self.research_digests(owner_id=owner_id)
        except (AttributeError, OSError, RuntimeError, ValueError, KeyError) as exc:
            digests = unavailable_research_digests(str(exc))
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
            "journal": journal,
            "archives": archives,
            "digests": digests,
        }

    def research_archive(
        self,
        *,
        owner_id: str = "local-owner",
        query: ResearchArchiveQuery | None = None,
    ) -> dict[str, Any]:
        query = query or ResearchArchiveQuery()
        try:
            state = self._paper_state()
            account_id = str((state or {}).get("account_id") or "")
            config_fingerprint = str(
                (state or {}).get("config_fingerprint") or ""
            )
            if not account_id:
                return unavailable_research_archive(
                    "Paper account is not initialized. Run paper-init before reviewing "
                    "historical snapshots.",
                    code="paper_account_unavailable",
                    recovery_action="paper-init",
                    query=query,
                )
            projection = self._research_archive_projection()
            calendar = None
            try:
                market = self.market(recover_snapshot=False)
                calendar = getattr(market, "calendar", None)
            except (AttributeError, OSError, RuntimeError, ValueError):
                calendar = None
            return projection.build(
                owner_id,
                account_id=account_id,
                config_fingerprint=config_fingerprint,
                query=query,
                market_calendar=calendar,
            )
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return unavailable_research_archive(str(exc), query=query)

    def research_digests(
        self,
        *,
        owner_id: str = "local-owner",
        query: ResearchDigestQuery | None = None,
    ) -> dict[str, Any]:
        """Read the immutable daily/weekly digest ledger for one owner epoch."""

        query = query or ResearchDigestQuery()
        try:
            account_id, _config_fingerprint = self._research_account_context()
            if not account_id:
                return unavailable_research_digests(
                    "Paper account is not initialized. Run paper-init before generating "
                    "persistent research digests.",
                    code="paper_account_unavailable",
                    recovery_action="paper-init",
                    query=query,
                )
            return self._research_digest_store().list(owner_id, account_id, query)
        except (AttributeError, OSError, RuntimeError, TypeError, ValueError) as exc:
            return unavailable_research_digests(str(exc), query=query)

    def generate_research_digests(
        self,
        *,
        owner_id: str = "local-owner",
        actor: str = "local-owner",
        trigger: str = "manual",
        kind: str = "all",
        on_date: date | None = None,
        week_start: date | None = None,
    ) -> dict[str, Any]:
        """Materialize the current read-only archive projection as revisions.

        This method only reads the authoritative paper/report/journal evidence
        and appends immutable digest records. It never refreshes providers or
        invokes strategy, accounting, broker, or order code.
        """

        if kind not in {"all", *DIGEST_KINDS}:
            raise ValueError("Research digest kind must be all, daily, or weekly")
        if on_date is not None and week_start is not None:
            raise ValueError("Research digest date and week cannot be combined")
        query_kind = "daily" if kind == "daily" else "weekly" if kind == "weekly" else "all"
        projection_query = ResearchArchiveQuery(
            kind=query_kind,
            on_date=on_date,
            week_start=week_start,
            limit=52,
        )
        account_id, config_fingerprint = self._research_account_context()
        if not account_id:
            return unavailable_research_digests(
                "Paper account is not initialized. Run paper-init before generating "
                "persistent research digests.",
                code="paper_account_unavailable",
                recovery_action="paper-init",
                query=ResearchDigestQuery(
                    kind=kind if kind in {"all", *DIGEST_KINDS} else "all",
                    period_start=on_date or week_start,
                ),
            )
        projection = self._research_archive_projection()
        calendar = None
        try:
            market = self.market(recover_snapshot=False)
            calendar = getattr(market, "calendar", None)
        except (AttributeError, OSError, RuntimeError, ValueError):
            calendar = None
        projected = projection.build(
            owner_id,
            account_id=account_id,
            config_fingerprint=config_fingerprint,
            query=projection_query,
            market_calendar=calendar,
        )
        if not projected.get("available"):
            return unavailable_research_digests(
                str(
                    (projected.get("errors") or [{}])[0].get(
                        "message", "Research archive projection is unavailable."
                    )
                ),
                code="research_archive_unavailable",
                query=ResearchDigestQuery(
                    kind=kind,
                    period_start=on_date or week_start,
                ),
            )

        store = self._research_digest_store()
        account_fingerprint = store.account_id(account_id)
        generated_on = datetime.now(timezone(timedelta(hours=8))).date()
        prepared: list[tuple[str, date, dict[str, Any], ResearchDigestDraft]] = []
        projected_items = (
            *(("daily", item) for item in projected.get("daily", [])),
            *(("weekly", item) for item in projected.get("weekly", [])),
        )
        for item_kind, raw_item in projected_items:
            if not isinstance(raw_item, dict):
                raise ValueError("Research archive projection item is invalid")
            period = _research_digest_period(raw_item, item_kind)
            item = _research_digest_payload(
                raw_item,
                item_kind,
                period_start=period,
                generated_on=generated_on,
                calendar_verified=calendar is not None,
            )
            source_fingerprint, evidence = _digest_source_fingerprints(
                item, item_kind
            )
            source = {
                "fingerprint": source_fingerprint,
                "evidence_fingerprints": evidence,
                "calendar_fingerprint": _research_calendar_fingerprint(
                    calendar,
                    kind=item_kind,
                    period_start=period,
                ),
                "config_fingerprint": config_fingerprint,
                "account_fingerprint": account_fingerprint,
            }
            prepared.append(
                (
                    item_kind,
                    period,
                    item,
                    ResearchDigestDraft(
                        kind=item_kind,
                        period_start=period,
                        payload=item,
                        source=source,
                        config_fingerprint=config_fingerprint,
                        actor=actor,
                        trigger=trigger,
                    ),
                )
            )

        write_results = []
        batch_error: ResearchDigestBatchError | None = None
        if prepared:
            try:
                write_results = store.append_many_with_results(
                    owner_id,
                    account_id,
                    [item[3] for item in prepared],
                )
            except ResearchDigestBatchError as exc:
                write_results = list(exc.results)
                batch_error = exc
        writes = [
            {
                "kind": item_kind,
                "period_start": period.isoformat(),
                "created": result.created,
                "reused": result.reused,
                "digest": result.digest,
            }
            for (item_kind, period, _payload, _draft), result in zip(
                prepared[: len(write_results)], write_results
            )
        ]
        errors = (
            [
                {
                    "code": "research_digest_batch_partial",
                    "message": str(batch_error),
                    "recovery_action": "archive-generate",
                }
            ]
            if batch_error is not None
            else []
        )
        has_provisional = any(
            payload.get("status") == "provisional"
            for _kind, _period, payload, _draft in prepared
        )
        projected_status = projected.get("status", "empty")
        response_status = (
            "partial"
            if batch_error is not None
            else "provisional"
            if has_provisional and projected_status == "current"
            else projected_status
        )
        return {
            "schema_version": 1,
            "available": bool(writes) or not prepared or batch_error is None,
            "status": response_status,
            "generated_at": _now(),
            "errors": errors,
            "projection": {
                "status": projected.get("status"),
                "errors": projected.get("errors", []),
                "summary": projected.get("summary", {}),
                "filters": projected.get("filters", {}),
            },
            "summary": {
                "requested": len(prepared),
                "completed": len(writes),
                "written": sum(1 for item in writes if item["created"]),
                "reused": sum(1 for item in writes if item["reused"]),
                "daily": sum(1 for item in writes if item["kind"] == "daily"),
                "weekly": sum(1 for item in writes if item["kind"] == "weekly"),
            },
            "writes": writes,
            "authority": {
                "research_only": True,
                "execution_authorized": False,
                "strategy_changed": False,
                "paper_account_changed": False,
                "broker_permissions_changed": False,
            },
        }

    def research_journal(
        self,
        *,
        owner_id: str = "local-owner",
        journal_query: JournalQuery | None = None,
    ) -> dict[str, Any]:
        query = journal_query or JournalQuery()
        result = self._research_journal_store().list(owner_id, query)
        result["options"] = {
            "categories": list(JOURNAL_CATEGORIES),
            "decisions": list(JOURNAL_DECISIONS),
            "symbols": [
                str(item.symbol)
                for item in getattr(self.config, "instruments", ())
                if getattr(item, "symbol", None)
            ],
        }
        return result

    def append_research_journal(
        self,
        *,
        owner_id: str,
        actor: str,
        draft: JournalDraft,
    ) -> dict[str, Any]:
        configured_symbols = {
            str(item.symbol)
            for item in getattr(self.config, "instruments", ())
            if getattr(item, "symbol", None)
        }
        if draft.symbol is not None and configured_symbols and draft.symbol not in configured_symbols:
            raise ValueError("symbol is not part of the configured instrument universe")
        return self._research_journal_store().append(
            owner_id,
            draft,
            actor=actor,
            market_evidence=self._journal_market_evidence(),
            strategy_evidence=self._journal_strategy_evidence(owner_id),
        )

    def portfolio(self) -> dict[str, Any]:
        state = self._paper_state()
        if not state:
            return {
                "generated_at": _now(),
                "initialized": False,
                "positions": [],
                "pending_targets": [],
                "equity_curve": [],
                "valuation_available": False,
                "valuation_status": "uninitialized",
            }
        equity = float(state.get("last_equity", 0))
        instrument_by_symbol = {
            item.symbol: item for item in getattr(self.config, "instruments", ())
        }
        market_issue = None
        try:
            market = self.market()
        except (OSError, RuntimeError, ValueError) as exc:
            market = None
            market_issue = self._market_issue(exc)

        freshness = None
        valuation_status = "unavailable"
        valuation_errors: list[dict[str, str]] = []
        latest_market_date = None
        if market is not None:
            freshness = _portfolio_market_freshness(self.config, market)
            latest_market_date = _safe_market_date(market, "latest_date")
            if freshness and freshness.get("status") == "OK":
                valuation_status = "current"
            elif freshness and freshness.get("current") is False:
                valuation_status = "stale"
            else:
                valuation_status = "needs_review"

        positions = []
        for symbol, raw_quantity in dict(state.get("positions", {})).items():
            quantity = int(raw_quantity)
            instrument = instrument_by_symbol.get(symbol)
            if market is None:
                positions.append(
                    {
                        "symbol": symbol,
                        "name": instrument.name if instrument else symbol,
                        "quantity": quantity,
                        "price": None,
                        "market_value": None,
                        "weight": None,
                        "asset_class": instrument.asset_class if instrument else None,
                        "sector": instrument.sector if instrument else None,
                        "valuation_status": "unavailable",
                        "valuation_error": "market_unavailable",
                    }
                )
                continue
            market_instrument = instrument
            try:
                market_instrument = market.instrument(symbol)
            except (AttributeError, KeyError, RuntimeError, ValueError):
                pass
            bar = None
            if latest_market_date is not None:
                try:
                    bar = market.latest_bar_on_or_before(symbol, latest_market_date)
                except (AttributeError, KeyError, RuntimeError, ValueError):
                    bar = None
            close = _finite_float(getattr(bar, "close", None))
            if bar is None:
                valuation_error = "missing_completed_bar"
            elif close is None or close <= 0:
                valuation_error = "invalid_close"
            else:
                valuation_error = None
            if valuation_error:
                valuation_errors.append(
                    {
                        "code": valuation_error,
                        "symbol": symbol,
                        "message": (
                            "No validated completed close is available for this position; "
                            "refresh the market snapshot before using price-derived values."
                        ),
                        "recovery_action": "refresh-data",
                    }
                )
            price = close if valuation_error is None else None
            market_value = price * quantity if price is not None else None
            positions.append(
                {
                    "symbol": symbol,
                    "name": market_instrument.name if market_instrument else symbol,
                    "quantity": quantity,
                    "price": price,
                    "market_value": market_value,
                    "weight": (
                        market_value / equity
                        if market_value is not None and equity > 0
                        else None
                    ),
                    "asset_class": (
                        market_instrument.asset_class if market_instrument else None
                    ),
                    "sector": market_instrument.sector if market_instrument else None,
                    "valuation_status": "valued" if valuation_error is None else "unavailable",
                    "valuation_error": valuation_error,
                }
            )
        pending = []
        for symbol, weight in dict(state.get("pending_targets") or {}).items():
            instrument = instrument_by_symbol.get(symbol)
            current = next(
                (value["weight"] for value in positions if value["symbol"] == symbol),
                None if market is None else 0.0,
            )
            target_weight = float(weight)
            pending.append(
                {
                    "symbol": symbol,
                    "name": instrument.name if instrument else symbol,
                    "current_weight": current,
                    "target_weight": target_weight,
                    "difference": None if current is None else target_weight - current,
                }
            )
        cash = float(state.get("cash", 0))
        high_water_mark = float(state.get("high_water_mark", equity))
        if valuation_errors:
            valuation_status = "partial"
        errors = [market_issue] if market_issue else []
        errors.extend(valuation_errors)
        valuation_date = (
            freshness.get("date")
            if isinstance(freshness, dict) and freshness.get("date")
            else _date_text(latest_market_date)
        )
        return {
            "generated_at": _now(),
            "initialized": True,
            "account_id": state.get("account_id"),
            "date": state.get("last_run_date"),
            "equity": equity,
            "cash": cash,
            "cash_weight": cash / equity if equity > 0 else 0.0,
            "drawdown": (
                equity / high_water_mark - 1.0
                if equity > 0 and high_water_mark > 0
                else 0.0
            ),
            "cooldown_remaining": int(state.get("cooldown_remaining", 0)),
            "pending_signal_date": state.get("pending_signal_date"),
            "positions": positions,
            "pending_targets": pending,
            "equity_curve": self._paper_equity_curve(480),
            "valuation_available": market is not None and not valuation_errors,
            "valuation_status": valuation_status,
            "valuation_date": valuation_date,
            "market_freshness": freshness,
            "errors": errors,
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
        snapshot["cross_source_check"] = (
            cross_source_projection(
                getattr(market, "manifest", None)
                if isinstance(getattr(market, "manifest", None), dict)
                else None,
                file_hashes=getattr(market, "file_hashes", None),
            )
            if market is not None
            else {
                "status": "not_run",
                "confidence": "not_available",
                "valid": False,
                "reason": "market_data_unavailable",
            }
        )
        snapshot["errors"] = [market_issue] if market_issue else []
        snapshot["generated_at"] = _now()
        return snapshot

    def screen_universe(
        self,
        on_date: date | None = None,
        filters: ScreeningFilters | None = None,
    ) -> dict[str, Any]:
        """Return a deterministic multi-instrument research screen.

        The base universe snapshot and all derived metrics come from the same
        completed-session cache.  This route is deliberately read-only: a
        missing or stale cache is represented in the response instead of
        triggering a refresh or silently filling values.
        """

        selected_filters = filters or ScreeningFilters()
        snapshot = self.universe(on_date)
        selected_date = date.fromisoformat(str(snapshot["date"]))
        market = None
        if snapshot.get("market_available"):
            try:
                market = self.market(recover_snapshot=False)
            except (OSError, RuntimeError, ValueError):
                # Keep the original universe response and expose unavailable
                # metrics rather than making the screen non-deterministic.
                market = None

        rows: list[dict[str, Any]] = []
        for item in snapshot.get("instruments", []):
            row = dict(item)
            if market is None:
                row.update(_unavailable_screen_metrics(item))
            else:
                row.update(self._screen_metrics(item, market, selected_date))
            rows.append(row)

        screened, counts = screen_rows(rows, selected_filters)
        quality_counts = {
            status: sum(1 for row in rows if row.get("data_status") == status)
            for status in ("complete", "stale", "insufficient_history", "missing")
        }
        if market is None:
            status = "unavailable"
        elif not screened:
            status = "empty"
        elif quality_counts["complete"] < len(rows):
            status = "partial"
        else:
            status = "ok"
        empty_reason = None
        if not screened:
            empty_reason = (
                "market_data_unavailable"
                if market is None
                else "no_instruments_match_filters"
            )
        snapshot_id = _screen_snapshot_id(self, selected_date, market)
        generated_at = _now()
        quality_summary = _screen_quality_summary(
            rows, _screen_required_history(self.config)
        )
        source_summary = _screen_source_summary(rows)
        returned_source_summary = _screen_source_summary(screened)
        screen_payload = {
            "schema_version": SCREEN_SCHEMA_VERSION,
            "status": status,
            "filters": selected_filters.as_dict(),
            "counts": counts,
            "quality_counts": quality_counts,
            "data_quality": quality_summary,
            "source_summary": source_summary,
            "returned_source_summary": returned_source_summary,
            "metric_definitions": SCREEN_METRIC_DEFINITIONS,
            "minimum_history_bars": _screen_required_history(self.config),
            "snapshot_id": snapshot_id,
            "filter_fingerprint": _screen_filter_fingerprint(
                selected_filters
            ),
            "data_date": selected_date.isoformat(),
            "completed_session_cutoff": (
                market.completed_through.isoformat()
                if market is not None
                and getattr(market, "completed_through", None) is not None
                else None
            ),
            "latest_common_session": (
                market.latest_common_session.isoformat()
                if market is not None
                and getattr(market, "latest_common_session", None) is not None
                else None
            ),
            "empty_reason": empty_reason,
            "warnings": _screen_warnings(rows, snapshot),
        }
        result = {
            **snapshot,
            "instruments": screened,
            "screen": screen_payload,
        }
        # ``universe()`` records its own read timestamp.  The screen has extra
        # metric work, so publish a timestamp after that work has completed.
        result["generated_at"] = generated_at
        return result

    def monitoring(self, *, owner_id: str) -> dict[str, Any]:
        engine = self._monitoring_engine()
        try:
            market = self.market(recover_snapshot=False)
        except (OSError, RuntimeError, ValueError):
            market = None
        return engine.status(owner_id, market=market)

    def monitoring_create_watchlist(
        self,
        *,
        owner_id: str,
        actor: str,
        name: str,
        expected_revision: int | None,
    ) -> dict[str, Any]:
        engine = self._monitoring_engine()
        engine.store.profile(owner_id).create_watchlist(
            name,
            actor=actor,
            expected_revision=expected_revision,
        )
        return self.monitoring(owner_id=owner_id)

    def monitoring_watchlist_action(
        self,
        *,
        owner_id: str,
        actor: str,
        watchlist_id: str,
        action: str,
        expected_revision: int | None,
        symbol: str | None = None,
        name: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        if action == "add_symbol" and symbol is not None and symbol not in {
            item.symbol for item in self.config.instruments
        }:
            raise ValueError("symbol must be in the configured security master")
        engine = self._monitoring_engine()
        engine.store.profile(owner_id).mutate_watchlist(
            watchlist_id,
            action=action,
            actor=actor,
            expected_revision=expected_revision,
            symbol=symbol,
            name=name,
            enabled=enabled,
        )
        return self.monitoring(owner_id=owner_id)

    def monitoring_create_rule(
        self,
        *,
        owner_id: str,
        actor: str,
        rule: dict[str, Any],
        expected_revision: int | None,
    ) -> dict[str, Any]:
        if rule.get("symbol") not in {item.symbol for item in self.config.instruments}:
            raise ValueError("symbol must be in the configured security master")
        engine = self._monitoring_engine()
        engine.store.profile(owner_id).create_rule(
            rule,
            actor=actor,
            expected_revision=expected_revision,
        )
        return self.monitoring(owner_id=owner_id)

    def monitoring_rule_action(
        self,
        *,
        owner_id: str,
        actor: str,
        rule_id: str,
        action: str,
        expected_revision: int | None,
        patch: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if patch and patch.get("symbol") not in (None, *(
            item.symbol for item in self.config.instruments
        )):
            raise ValueError("symbol must be in the configured security master")
        engine = self._monitoring_engine()
        engine.store.profile(owner_id).mutate_rule(
            rule_id,
            action=action,
            actor=actor,
            expected_revision=expected_revision,
            patch=patch,
        )
        return self.monitoring(owner_id=owner_id)

    def monitoring_scan(self, *, owner_id: str, actor: str) -> dict[str, Any]:
        engine = self._monitoring_engine()
        try:
            market = self.market(recover_snapshot=False)
        except (OSError, RuntimeError, ValueError):
            market = None
        scan = engine.scan(owner_id, actor=actor, market=market)
        return {**self.monitoring(owner_id=owner_id), "scan_result": scan}

    def monitoring_alert_action(
        self,
        *,
        owner_id: str,
        actor: str,
        alert_id: str,
        action: str,
        note: str,
        snooze_until: str | None,
        expected_state_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        engine = self._monitoring_engine()
        engine.store.profile(owner_id).alert_action(
            alert_id,
            action=action,
            actor=actor,
            note=note,
            snooze_until=snooze_until,
            expected_state_fingerprint=expected_state_fingerprint,
        )
        return self.monitoring(owner_id=owner_id)

    def monitoring_notification_action(
        self,
        *,
        owner_id: str,
        actor: str,
        notification_id: str,
        action: str,
        expected_state_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        engine = self._monitoring_engine()
        engine.store.profile(owner_id).notification_action(
            notification_id,
            action=action,
            actor=actor,
            expected_state_fingerprint=expected_state_fingerprint,
        )
        return self.monitoring(owner_id=owner_id)

    def _screen_metrics(
        self,
        item: dict[str, Any],
        market: MarketData,
        selected_date: date,
    ) -> dict[str, Any]:
        symbol = str(item["symbol"])
        settings = self.config.strategy
        required = _screen_required_history(self.config)
        history_limit = min(max(required + 20, 20), 2500)
        try:
            history = list(market.history(symbol, selected_date, history_limit))
        except (AttributeError, KeyError, RuntimeError, ValueError):
            history = []
        latest = history[-1] if history else None
        closes = [
            float(value.close)
            for value in history
            if _positive_finite(getattr(value, "close", None))
        ]
        latest_close = _positive_finite(getattr(latest, "close", None))
        latest_date = getattr(latest, "date", None)
        if latest is None:
            data_status = "missing"
        elif latest_date != selected_date:
            data_status = "stale"
        elif len(history) < required:
            data_status = "insufficient_history"
        else:
            data_status = "complete"

        momentum = None
        lookback = int(settings.lookback_days)
        skip = int(settings.skip_days)
        momentum_end = len(closes) - 1 - skip
        momentum_start = momentum_end - lookback
        if momentum_start >= 0 and momentum_end >= 0 and closes[momentum_start] > 0:
            momentum = closes[momentum_end] / closes[momentum_start] - 1.0

        trend_sma = None
        trend = "MIXED"
        trend_days = int(settings.trend_sma_days)
        if latest_close is not None and len(closes) >= trend_days:
            trend_sma = statistics.fmean(closes[-trend_days:])
            if latest_close > trend_sma:
                trend = "UP"
            elif latest_close < trend_sma:
                trend = "DOWN"

        volatility = None
        volatility_days = int(settings.volatility_days)
        volatility_closes = closes[-(volatility_days + 1) :]
        returns = _screen_returns(volatility_closes)
        if len(returns) > 1:
            volatility = statistics.stdev(returns) * math.sqrt(252.0)

        amount_values = [
            float(value.amount)
            for value in history[-min(20, len(history)) :]
            if _nonnegative_finite(getattr(value, "amount", None))
        ]
        average_amount = statistics.fmean(amount_values) if amount_values else None
        coverage = item.get("coverage") or {}
        coverage_rows = coverage.get("rows")
        try:
            coverage_rows = int(coverage_rows)
        except (TypeError, ValueError):
            coverage_rows = len(history)
        coverage_percent = min(100.0, max(0.0, coverage_rows / required * 100.0))
        manifest_file = _screen_manifest_file(market, symbol)
        lag_days = (
            max(0, (selected_date - latest_date).days)
            if latest_date is not None
            else None
        )
        return {
            "data_status": data_status,
            "history_bars": len(history),
            "history_ready": len(history) >= required,
            "coverage_percent": round(coverage_percent, 1),
            "data_lag_days": lag_days,
            "momentum": momentum,
            "trend": trend,
            "trend_sma": trend_sma,
            "annual_volatility": volatility,
            "average_amount": average_amount,
            "source": _bounded_manifest_text(manifest_file.get("source")),
            "source_provider": _bounded_manifest_text(
                manifest_file.get("source_provider")
            ),
            "file_sha256": _bounded_manifest_text(
                (getattr(market, "file_hashes", {}) or {}).get(symbol)
            ),
        }

    def market_intelligence(
        self,
        query: DragonTigerQuery | None = None,
    ) -> dict[str, Any]:
        """Read the local dragon-tiger ledger without refreshing any provider."""

        selected = query or DragonTigerQuery()
        store_query = replace(selected, include_revisions=True)
        cutoff = None
        try:
            market = self.market(recover_snapshot=False)
            candidate = getattr(market, "completed_through", None)
            if isinstance(candidate, date) and not isinstance(candidate, datetime):
                cutoff = candidate
        except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError):
            # The ledger remains useful when the ordinary market cache is absent or
            # invalid; in that case the store reports freshness without a cutoff.
            cutoff = None
        result = DragonTigerStore(self.config).list(
            store_query,
            completed_session_cutoff=cutoff,
        )
        result["generated_at"] = _now()
        return result

    def market_breadth(
        self,
        query: MarketBreadthQuery | None = None,
    ) -> dict[str, Any]:
        """Read local sector/breadth evidence without refreshing a provider."""

        selected = query or MarketBreadthQuery()
        store_query = replace(selected, include_revisions=True)
        cutoff = None
        try:
            market = self.market(recover_snapshot=False)
            candidate = getattr(market, "completed_through", None)
            if isinstance(candidate, date) and not isinstance(candidate, datetime):
                cutoff = candidate
        except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError):
            cutoff = None
        result = MarketBreadthStore(self.config).list(
            store_query,
            completed_session_cutoff=cutoff,
        )
        result["generated_at"] = _now()
        return result

    def capital_flow(
        self,
        query: CapitalFlowQuery | None = None,
    ) -> dict[str, Any]:
        """Read local board capital-flow evidence without refreshing a provider."""

        selected = query or CapitalFlowQuery()
        store_query = replace(selected, include_revisions=True)
        cutoff = None
        try:
            market = self.market(recover_snapshot=False)
            candidate = getattr(market, "completed_through", None)
            if isinstance(candidate, date) and not isinstance(candidate, datetime):
                cutoff = candidate
        except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError):
            cutoff = None
        result = CapitalFlowStore(self.config).list(
            store_query,
            completed_session_cutoff=cutoff,
        )
        result["generated_at"] = _now()
        return result

    def intraday(self, query: IntradayQuery) -> dict[str, Any]:
        """Read validated local minute evidence without refreshing a provider."""

        result = IntradayStore(self.config).list(query)
        result["generated_at"] = _now()
        return result

    def valuation(self, query: ValuationQuery | None = None) -> dict[str, Any]:
        """Read current valuation evidence without contacting a provider."""

        result = ValuationStore(self.config).list(query)
        result["generated_at"] = _now()
        return result

    def news(self, query: NewsQuery | None = None) -> dict[str, Any]:
        """Read validated local news/announcement evidence without network I/O."""

        result = NewsStore(self.config).list(query)
        result["generated_at"] = _now()
        return result

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
        cross_source_check = cross_source_projection(
            manifest,
            file_hashes=market.file_hashes,
        )
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
        if cross_source_check.get("status") in {"failed", "invalid"}:
            warnings.append(
                "Independent provider reconciliation found a conflict or invalid audit; "
                "review the cross-source evidence before relying on this snapshot."
            )
        elif cross_source_check.get("status") in {"warning", "unavailable", "not_run"}:
            warnings.append(
                "Independent provider reconciliation has not confirmed this snapshot."
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
                "cross_source_check": cross_source_check,
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
            "cross_source_check": diagnosis.get("cache_manifest", {}).get(
                "cross_source_check"
            ),
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

    def _research_journal_store(self) -> ResearchJournalStore:
        with self._lock:
            if self._research_journal is None:
                root = getattr(self.config, "research_journal_dir", None)
                if root is None:
                    project_root = getattr(self.config, "project_root", None)
                    if project_root is not None:
                        root = Path(project_root) / "state" / "research_journal"
                    else:
                        reports_dir = Path(getattr(self.config, "reports_dir"))
                        root = reports_dir.parent / "state" / "research_journal"
                self._research_journal = ResearchJournalStore(root)
            return self._research_journal

    def _research_archive_projection(self) -> ResearchArchiveProjection:
        with self._lock:
            if self._research_archive is None:
                self._research_archive = ResearchArchiveProjection(
                    self.config.reports_dir,
                    self.config.paper_equity_file,
                    self._research_journal_store(),
                )
            return self._research_archive

    def _research_digest_store(self) -> ResearchDigestStore:
        with self._lock:
            if self._research_digests is None:
                root = getattr(self.config, "research_digest_dir", None)
                if root is None:
                    project_root = getattr(self.config, "project_root", None)
                    if project_root is not None:
                        root = Path(project_root) / "state" / "research_digests"
                    else:
                        reports_dir = Path(getattr(self.config, "reports_dir"))
                        root = reports_dir.parent / "state" / "research_digests"
                self._research_digests = ResearchDigestStore(root)
            return self._research_digests

    def _research_account_context(self) -> tuple[str, str]:
        state = self._paper_state()
        return (
            str((state or {}).get("account_id") or ""),
            str((state or {}).get("config_fingerprint") or ""),
        )

    def _monitoring_engine(self) -> MonitoringEngine:
        with self._lock:
            if self._monitoring is None:
                self._monitoring = MonitoringEngine(self.config)
            return self._monitoring

    def _journal_market_evidence(self) -> dict[str, Any]:
        try:
            market = self.market()
            metadata = market.snapshot_metadata()
            if not isinstance(metadata, dict):
                raise ValueError("market snapshot metadata is unavailable")
            selected_date = metadata.get("latest_common_session") or metadata.get(
                "latest_benchmark_session"
            )
            if selected_date is None:
                selected_date = market.latest_date().isoformat()
            selected_date = date.fromisoformat(str(selected_date)).isoformat()
            return {
                "available": True,
                "date": selected_date,
                "fingerprint": _journal_evidence_fingerprint(metadata),
            }
        except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError):
            return {"available": False, "date": None, "fingerprint": None}

    def _journal_strategy_evidence(self, owner_id: str) -> dict[str, Any]:
        try:
            active = self._strategy_lab_engine().summary(owner_id)["active"]
            return {
                "available": True,
                "candidate_id": active.get("candidate_id"),
                "fingerprint": active["fingerprint"],
                "lifecycle_state": active["lifecycle_state"],
            }
        except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError):
            return {
                "available": False,
                "candidate_id": None,
                "fingerprint": None,
                "lifecycle_state": None,
            }

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
            "provenance": {
                "manifest_available": False,
                "cross_source_check": {
                    "status": "not_run",
                    "confidence": "not_available",
                    "valid": False,
                    "reason": "manifest_unavailable",
                },
            },
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
                "cross_source_check": {
                    "status": "not_run",
                    "confidence": "not_available",
                    "valid": False,
                    "reason": "manifest_unavailable",
                },
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
            value = load_unique_json(path, max_bytes=MAX_DASHBOARD_REPORT_BYTES)
        except (OSError, UnicodeError, ValueError):
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


def _screen_required_history(config: AppConfig) -> int:
    settings = config.strategy
    return max(
        int(settings.lookback_days) + int(settings.skip_days) + 1,
        int(settings.trend_sma_days),
        int(settings.volatility_days) + 1,
        21,
    )


def _screen_returns(values: list[float]) -> list[float]:
    return [
        values[index] / values[index - 1] - 1.0
        for index in range(1, len(values))
        if values[index - 1] > 0 and values[index] > 0
    ]


def _positive_finite(value: Any) -> float | None:
    parsed = _finite_float(value)
    return parsed if parsed is not None and parsed > 0 else None


def _nonnegative_finite(value: Any) -> float | None:
    parsed = _finite_float(value)
    return parsed if parsed is not None and parsed >= 0 else None


def _unavailable_screen_metrics(item: dict[str, Any]) -> dict[str, Any]:
    coverage = item.get("coverage") or {}
    rows = coverage.get("rows")
    try:
        rows = max(0, int(rows))
    except (TypeError, ValueError):
        rows = 0
    return {
        "data_status": "missing",
        "history_bars": rows,
        "history_ready": False,
        "coverage_percent": 0.0,
        "data_lag_days": None,
        "momentum": None,
        "trend": "MIXED",
        "trend_sma": None,
        "annual_volatility": None,
        "average_amount": None,
        "source": None,
        "source_provider": None,
        "file_sha256": None,
    }


def _screen_manifest_file(market: Any, symbol: str) -> dict[str, Any]:
    manifest = getattr(market, "manifest", None)
    if not isinstance(manifest, dict):
        return {}
    return _manifest_symbol_entry(manifest, symbol)


def _screen_snapshot_id(
    service: DashboardService, selected_date: date, market: MarketData | None
) -> str:
    values = {
        "date": selected_date.isoformat(),
        "security_master_sha256": service.config.security_master.fingerprint(),
        "manifest_sha256": getattr(market, "manifest_sha256", None)
        if market is not None
        else None,
        "files": sorted(
            (getattr(market, "file_hashes", {}) or {}).items()
            if market is not None
            else []
        ),
    }
    digest = sha256(
        json.dumps(values, ensure_ascii=True, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"screen-{selected_date.isoformat()}-{digest[:16]}"


def _screen_filter_fingerprint(filters: ScreeningFilters) -> str:
    digest = sha256(
        json.dumps(
            filters.as_dict(),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return f"filter-{digest[:16]}"


def _screen_warnings(
    rows: list[dict[str, Any]], snapshot: dict[str, Any]
) -> list[str]:
    warnings: list[str] = []
    errors = snapshot.get("errors")
    if isinstance(errors, list):
        warnings.extend(
            str(value.get("message"))
            for value in errors
            if isinstance(value, dict) and value.get("message")
        )
    stale = sum(row.get("data_status") == "stale" for row in rows)
    missing = sum(row.get("data_status") == "missing" for row in rows)
    insufficient = sum(
        row.get("data_status") == "insufficient_history" for row in rows
    )
    if stale:
        warnings.append(f"{stale} instruments are behind the selected completed session")
    if missing:
        warnings.append(f"{missing} instruments have no validated bars")
    if insufficient:
        warnings.append(f"{insufficient} instruments lack the minimum research history")
    fallback = sum(1 for row in rows if _screen_is_fallback_source(row))
    if fallback:
        warnings.append(
            f"{fallback} instruments use a network or validated local fallback source"
        )
    unknown_source = sum(
        1
        for row in rows
        if not (_bounded_manifest_text(row.get("source_provider"))
                or _bounded_manifest_text(row.get("source")))
    )
    if unknown_source:
        warnings.append(f"{unknown_source} instruments have no source provider metadata")
    return warnings


def _screen_is_fallback_source(row: dict[str, Any]) -> bool:
    source = " ".join(
        value.lower()
        for value in (
            _bounded_manifest_text(row.get("source_provider")),
            _bounded_manifest_text(row.get("source")),
        )
        if value
    )
    return "fallback" in source or "validated_local" in source


def _screen_source_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    counts: Counter[str] = Counter()
    fallback_by_provider: Counter[str] = Counter()
    fallback_count = 0
    for row in rows:
        provider = _bounded_manifest_text(row.get("source_provider"))
        source = _bounded_manifest_text(row.get("source"))
        key = provider or source or "unknown"
        counts[key] += 1
        is_fallback = _screen_is_fallback_source(row)
        fallback_count += int(is_fallback)
        fallback_by_provider[key] += int(is_fallback)
    total = len(rows)
    providers = [
        {
            "provider": provider,
            "count": count,
            "percent": round(count / total * 100.0, 1) if total else 0.0,
            "fallback": bool(fallback_by_provider.get(provider)),
        }
        for provider, count in sorted(
            counts.items(), key=lambda item: (-item[1], item[0])
        )
    ]
    return {
        "instrument_count": total,
        "fallback_count": fallback_count,
        "unknown_count": counts.get("unknown", 0),
        "providers": providers,
    }


def _screen_quality_summary(
    rows: list[dict[str, Any]], minimum_history: int
) -> dict[str, Any]:
    total = len(rows)
    status_counts = Counter(
        str(row.get("data_status") or "unknown")
        for row in rows
    )
    coverage_values = [
        value
        for row in rows
        if (value := _finite_float(row.get("coverage_percent"))) is not None
    ]
    lag_values = [
        value
        for row in rows
        if (value := _finite_float(row.get("data_lag_days"))) is not None
    ]
    dates = sorted(
        {
            str(row.get("latest_bar_date"))
            for row in rows
            if row.get("latest_bar_date")
        }
    )

    def percentage(count: int) -> float:
        return round(count / total * 100.0, 1) if total else 0.0

    def summary(values: list[float]) -> dict[str, float | None]:
        if not values:
            return {"minimum": None, "median": None, "maximum": None}
        return {
            "minimum": round(min(values), 1),
            "median": round(statistics.median(values), 1),
            "maximum": round(max(values), 1),
        }

    return {
        "row_count": total,
        "minimum_history_bars": minimum_history,
        "status_counts": {
            key: status_counts.get(key, 0)
            for key in ("complete", "stale", "insufficient_history", "missing")
        },
        "complete_percent": percentage(status_counts.get("complete", 0)),
        "history_ready_percent": round(
            sum(bool(row.get("history_ready")) for row in rows)
            / total
            * 100.0,
            1,
        )
        if total
        else 0.0,
        "coverage_percent": summary(coverage_values),
        "lag_days": summary(lag_values),
        "latest_bar_dates": dates,
    }


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


def _journal_evidence_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
        default=lambda item: item.isoformat()
        if isinstance(item, (date, datetime))
        else str(item),
    ).encode("ascii")
    return sha256(encoded).hexdigest()


def _research_calendar_fingerprint(
    value: Any,
    *,
    kind: str,
    period_start: date,
) -> str | None:
    if kind == "daily" or value is None:
        return None
    period_end = period_start + timedelta(days=6)
    dates: list[str] = []
    try:
        for item in value:
            if isinstance(item, date) and not isinstance(item, datetime):
                parsed = item
            elif isinstance(item, str):
                parsed = date.fromisoformat(item)
            else:
                return None
            if period_start <= parsed <= period_end:
                dates.append(parsed.isoformat())
    except (TypeError, ValueError):
        return None
    normalized = sorted(set(dates))
    if not normalized:
        return None
    return _journal_evidence_fingerprint(normalized)


def _research_digest_period(item: dict[str, Any], kind: str) -> date:
    field = "as_of_date" if kind == "daily" else "week_start"
    other = "week_start" if kind == "daily" else "as_of_date"
    value = item.get(field)
    if other in item or not isinstance(value, str):
        raise ValueError("Research archive projection period is ambiguous")
    try:
        period = date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError("Research archive projection period is invalid") from exc
    if period.isoformat() != value:
        raise ValueError("Research archive projection period is not canonical")
    if kind == "weekly":
        if period.weekday() != 0:
            raise ValueError("Research archive weekly period must be an ISO Monday")
        expected_end = (period + timedelta(days=6)).isoformat()
        if item.get("week_end") != expected_end:
            raise ValueError("Research archive weekly period end is invalid")
    return period


def _research_digest_payload(
    item: dict[str, Any],
    kind: str,
    *,
    period_start: date,
    generated_on: date,
    calendar_verified: bool,
) -> dict[str, Any]:
    payload = dict(item)
    if (
        kind == "weekly"
        and payload.get("status") == "current"
        and (
            not calendar_verified
            or period_start + timedelta(days=6) >= generated_on
        )
    ):
        payload["status"] = "provisional"
        payload["status_detail"] = (
            "The market calendar is unavailable; weekly finalization cannot be "
            "verified."
            if not calendar_verified
            else "The ISO week is still open; a later generation will append the "
            "finalized revision."
        )
    return payload


def _digest_source_fingerprints(
    item: dict[str, Any], kind: str
) -> tuple[str, list[str]]:
    nested = item.get("source")
    key = "evidence_fingerprint" if kind == "daily" else "weekly_fingerprint"
    candidate = nested.get(key) if isinstance(nested, dict) else None
    if not _is_sha256(candidate):
        candidate = None
    if candidate is None:
        candidate = _journal_evidence_fingerprint(
            {key: nested, "period": item.get("as_of_date") or item.get("week_start")}
        )
    evidence: list[str] = []
    if kind == "weekly" and isinstance(nested, dict):
        raw_evidence = nested.get("evidence_fingerprints")
        if isinstance(raw_evidence, list):
            for value in raw_evidence:
                if _is_sha256(value) and value not in evidence:
                    evidence.append(value)
    if not evidence:
        evidence = [candidate]
    return candidate, evidence


def _is_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != 64:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return value == value.lower()


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


def _portfolio_market_freshness(config: AppConfig, market: Any) -> dict[str, Any]:
    """Return a small, defensive freshness projection for the portfolio view.

    The portfolio route must remain useful while a cache is being repaired. A
    lightweight market double used by integrations may not implement the full
    diagnostics protocol, so the projection falls back to the dates available
    on the market reader instead of turning an otherwise readable ledger into
    a server error.
    """
    try:
        diagnosis = diagnose(config, market)
    except (AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError):
        latest = _date_text(_safe_market_date(market, "latest_date"))
        completed = _date_text(getattr(market, "completed_through", None))
        common = _date_text(getattr(market, "latest_common_session", None)) or latest
        lag = None
        if completed and common:
            try:
                lag = max(0, (date.fromisoformat(completed) - date.fromisoformat(common)).days)
            except ValueError:
                lag = None
        manifest = getattr(market, "manifest", None)
        current = bool(completed and common and common >= completed)
        return {
            "status": "OK" if current and isinstance(manifest, dict) else "WARNING",
            "date": common,
            "completed_session_cutoff": completed,
            "lag_calendar_days": lag,
            "current": current,
            "manifest_available": isinstance(manifest, dict),
        }
    return {
        "status": diagnosis["status"],
        "date": diagnosis["latest_common_market_date"],
        "completed_session_cutoff": diagnosis["completed_session_cutoff"],
        "lag_calendar_days": diagnosis["market_data_lag_days"],
        "current": diagnosis["market_data_current"],
        "manifest_available": diagnosis["cache_manifest"]["available"],
    }


def _safe_market_date(market: Any, method_name: str) -> date | None:
    method = getattr(market, method_name, None)
    if not callable(method):
        return None
    try:
        value = method()
    except (OSError, RuntimeError, ValueError):
        return None
    return value if isinstance(value, date) else None


def _date_text(value: date | None) -> str | None:
    return value.isoformat() if isinstance(value, date) else None


def _finite_float(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None
