from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from datetime import date, time
from pathlib import Path
from typing import Any

from .broker.base import (
    BROKER_ACCOUNT_ID_MAX_LENGTH,
    BROKER_ADAPTER_NAME_MAX_LENGTH,
)
from .json_utils import load_unique_json
from .models import CostSettings, Instrument, RiskSettings, StrategySettings
from .security import SecurityMaster


DEFAULT_BROKER_MAX_ORDER_NOTIONAL = 50_000.0
DEFAULT_BROKER_MAX_DAILY_NOTIONAL = 100_000.0
DEFAULT_AUTH_SESSION_HOURS = 8
DEFAULT_AUTH_MAX_FAILED_ATTEMPTS = 5
DEFAULT_AUTH_FAILURE_WINDOW_MINUTES = 15
DEFAULT_AUTH_LOCKOUT_MINUTES = 15
MAX_CONFIG_FILE_BYTES = 5 * 1024 * 1024
DEFAULT_RESEARCH_JOURNAL_DIR = "state/research_journal"
DEFAULT_MONITORING_DIR = "state/monitoring"


@dataclass(frozen=True)
class AppConfig:
    path: Path
    project_root: Path
    raw: dict[str, Any]
    instruments: tuple[Instrument, ...]
    strategy: StrategySettings
    risk: RiskSettings
    costs: CostSettings
    security_master: SecurityMaster
    universe_name: str
    minimum_listing_days: int

    def resolve(self, value: str) -> Path:
        candidate = Path(value)
        return candidate if candidate.is_absolute() else self.project_root / candidate

    @property
    def cache_dir(self) -> Path:
        return self.resolve(self.raw["data"]["cache_dir"])

    @property
    def reports_dir(self) -> Path:
        return self.resolve(self.raw["reports_dir"])

    @property
    def logs_dir(self) -> Path:
        return self.resolve(self.raw["logs_dir"])

    @property
    def paper_state_file(self) -> Path:
        return self.resolve(self.raw["paper"]["state_file"])

    @property
    def paper_trades_file(self) -> Path:
        return self.resolve(self.raw["paper"]["trades_file"])

    @property
    def paper_equity_file(self) -> Path:
        return self.resolve(self.raw["paper"]["equity_file"])

    @property
    def paper_rejections_file(self) -> Path:
        return self.resolve(
            self.raw["paper"].get("rejections_file", "state/paper_rejections.csv")
        )

    @property
    def research_journal_dir(self) -> Path:
        """Return the local, git-ignored root for immutable research notes."""
        return _research_journal_path(
            self.raw.get("research_journal", {}), self.project_root
        )

    @property
    def monitoring_dir(self) -> Path:
        """Return the local, owner-isolated root for research monitoring state."""
        return _state_child_path(
            self.raw.get("monitoring", {}),
            project_root=self.project_root,
            section="monitoring",
            default=DEFAULT_MONITORING_DIR,
        )

    @property
    def auth_enabled(self) -> bool:
        return bool(self.raw.get("auth", {}).get("enabled", True))

    @property
    def auth_users_file(self) -> Path:
        return self.resolve(
            self.raw.get("auth", {}).get("users_file", "state/beta_users.json")
        )

    @property
    def broker_reconciliation_file(self) -> Path:
        return self.resolve(
            self.raw.get("broker", {}).get(
                "reconciliation_file", "state/broker_reconciliation.csv"
            )
        )

    @property
    def broker_orders_file(self) -> Path:
        return self.resolve(
            self.raw.get("broker", {}).get("orders_file", "state/broker_orders.csv")
        )

    @property
    def broker_fills_file(self) -> Path:
        return self.resolve(
            self.raw.get("broker", {}).get("fills_file", "state/broker_fills.csv")
        )

    @property
    def broker_ledger_scope_file(self) -> Path:
        return self.resolve(
            self.raw.get("broker", {}).get(
                "ledger_scope_file", "state/broker_ledger_scope.json"
            )
        )

    @property
    def shadow_fills_file(self) -> Path:
        return self.resolve(
            self.raw.get("shadow_account", {}).get(
                "fills_file", "state/shadow_fills.csv"
            )
        )

    @property
    def shadow_imports_file(self) -> Path:
        return self.resolve(
            self.raw.get("shadow_account", {}).get(
                "imports_file", "state/shadow_imports.csv"
            )
        )

    @property
    def shadow_max_import_bytes(self) -> int:
        return int(
            self.raw.get("shadow_account", {}).get("max_import_bytes", 1_000_000)
        )

    @property
    def live_authorization_file(self) -> Path:
        return self.resolve(
            self.raw.get("broker", {}).get(
                "authorization_file", "state/live_authorization.json"
            )
        )

    @property
    def live_batch_approval_file(self) -> Path:
        return self.resolve(
            self.raw.get("broker", {}).get(
                "batch_approval_file", "state/live_batch_approval.json"
            )
        )

    @property
    def live_kill_switch_file(self) -> Path:
        return self.resolve(
            self.raw.get("broker", {}).get(
                "kill_switch_file", "state/LIVE_KILL_SWITCH"
            )
        )

    def active_symbols(self, on_date: date) -> tuple[str, ...]:
        return self.security_master.active_symbols(
            self.universe_name, on_date, self.minimum_listing_days
        )


def load_config(path: str | Path) -> AppConfig:
    config_path = Path(path).resolve()
    raw = load_unique_json(config_path, max_bytes=MAX_CONFIG_FILE_BYTES)
    if not isinstance(raw, dict):
        raise ValueError("configuration must be a JSON object")
    project_root = config_path.parent.parent
    master_spec = raw.get("security_master")
    if master_spec:
        master_path = Path(str(master_spec["file"]))
        if not master_path.is_absolute():
            master_path = project_root / master_path
        security_master = SecurityMaster.load(master_path.resolve())
        universe_name = str(master_spec["universe"])
        minimum_listing_days = int(master_spec.get("minimum_listing_days", 0))
        benchmark = str(raw["strategy"]["benchmark"])
        instruments = security_master.required_instruments(universe_name, benchmark)
    else:
        security_master = SecurityMaster.from_legacy(raw["universe"])
        universe_name = "legacy"
        minimum_listing_days = 0
        instruments = security_master.required_instruments(
            universe_name, str(raw["strategy"]["benchmark"])
        )
    strategy = StrategySettings(**raw["strategy"])
    risk = RiskSettings(**raw["risk"])
    cost_values = dict(raw["costs"])
    cost_values["history"] = tuple(cost_values.get("history", ()))
    costs = CostSettings(**cost_values)
    _validate(
        raw,
        instruments,
        strategy,
        risk,
        costs,
        security_master,
        universe_name,
        minimum_listing_days,
        project_root,
    )
    return AppConfig(
        config_path,
        project_root,
        raw,
        instruments,
        strategy,
        risk,
        costs,
        security_master,
        universe_name,
        minimum_listing_days,
    )


def _validate(
    raw: dict[str, Any],
    instruments: tuple[Instrument, ...],
    strategy: StrategySettings,
    risk: RiskSettings,
    costs: CostSettings,
    security_master: SecurityMaster,
    universe_name: str,
    minimum_listing_days: int,
    project_root: Path,
) -> None:
    symbols = [item.symbol for item in instruments]
    if not instruments:
        raise ValueError("Universe must not be empty")
    if len(symbols) != len(set(symbols)):
        raise ValueError("Universe contains duplicate symbols")
    if universe_name not in security_master.universes:
        raise ValueError(f"Unknown configured universe: {universe_name!r}")
    if minimum_listing_days < 0:
        raise ValueError("security_master.minimum_listing_days must be non-negative")
    # Provider names are resolved through the shared registry.  Keeping this
    # validation at configuration load time prevents a typo from silently
    # falling through to a local cache during a scheduled refresh.
    from .data.providers import registered_provider_names

    provider = raw["data"].get("provider", "eastmoney")
    supported_providers = set(registered_provider_names())
    if not isinstance(provider, str) or provider.strip().lower() not in supported_providers:
        supported = ", ".join(sorted(supported_providers))
        raise ValueError(
            f"data.provider must be one of {supported}; got {provider!r}"
        )
    raw["data"]["provider"] = provider.strip().lower()
    if raw["data"].get("adjustment", "forward") not in {"none", "forward", "backward"}:
        raise ValueError("data.adjustment must be none, forward, or backward")
    _validate_data_transport(raw["data"])
    try:
        data_start = date.fromisoformat(raw["data"]["start"])
        data_end = date.fromisoformat(raw["data"]["end"])
        backtest_start = date.fromisoformat(raw["backtest"]["start"])
        backtest_end = date.fromisoformat(raw["backtest"]["end"])
        market_close = time.fromisoformat(
            raw["data"].get("market_close_time", "15:30")
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"Configuration contains an invalid date or time: {exc}") from exc
    if data_start > data_end or backtest_start > backtest_end:
        raise ValueError("Configuration start date must not be after end date")
    if market_close.tzinfo is not None:
        raise ValueError("data.market_close_time must not include a timezone")
    for instrument in instruments:
        if not re.fullmatch(r"\d{6}", instrument.symbol):
            raise ValueError(f"Invalid six-digit symbol: {instrument.symbol!r}")
        if instrument.market not in {"SH", "SZ"}:
            raise ValueError(f"Unsupported market for {instrument.symbol}: {instrument.market!r}")
        if not isinstance(instrument.lot_size, int) or instrument.lot_size <= 0:
            raise ValueError(f"lot_size must be a positive integer for {instrument.symbol}")
        if instrument.instrument_type not in {"ETF", "STOCK", "INDEX", "FUND"}:
            raise ValueError(
                f"Unsupported instrument_type for {instrument.symbol}: "
                f"{instrument.instrument_type!r}"
            )
        if instrument.listing_date and instrument.delisting_date:
            if instrument.delisting_date < instrument.listing_date:
                raise ValueError(f"delisting_date precedes listing_date for {instrument.symbol}")
        if instrument.price_limit_pct is not None and not 0 < instrument.price_limit_pct < 1:
            raise ValueError(f"price_limit_pct must be in (0, 1) for {instrument.symbol}")
        if not math.isfinite(instrument.tick_size) or instrument.tick_size <= 0:
            raise ValueError(f"tick_size must be positive for {instrument.symbol}")
    if strategy.benchmark not in symbols:
        raise ValueError("Benchmark must be part of the universe")
    for name in (
        "rebalance_days",
        "lookback_days",
        "trend_sma_days",
        "volatility_days",
    ):
        if getattr(strategy, name) <= 0:
            raise ValueError(f"strategy.{name} must be positive")
    if strategy.skip_days < 0:
        raise ValueError("strategy.skip_days must be non-negative")
    if strategy.top_n < 1 or strategy.top_n > len(symbols):
        raise ValueError("top_n is outside the universe size")
    if strategy.max_position_weight <= 0 or strategy.max_position_weight > 1:
        raise ValueError("max_position_weight must be in (0, 1]")
    if strategy.minimum_cash_weight < 0 or strategy.minimum_cash_weight >= 1:
        raise ValueError("minimum_cash_weight must be in [0, 1)")
    if strategy.target_annual_volatility < 0:
        raise ValueError("target_annual_volatility must be non-negative")
    if strategy.covariance_days < 0:
        raise ValueError("strategy.covariance_days must be non-negative")
    if strategy.covariance_shrinkage < 0 or strategy.covariance_shrinkage > 1:
        raise ValueError("strategy.covariance_shrinkage must be in [0, 1]")
    if not math.isfinite(strategy.minimum_average_amount) or strategy.minimum_average_amount < 0:
        raise ValueError("strategy.minimum_average_amount must be finite and non-negative")
    if strategy.minimum_rebalance_weight < 0 or strategy.minimum_rebalance_weight >= 1:
        raise ValueError("strategy.minimum_rebalance_weight must be in [0, 1)")
    if strategy.weighting_method not in {"inverse_volatility", "risk_parity"}:
        raise ValueError(
            "strategy.weighting_method must be inverse_volatility or risk_parity"
        )
    if strategy.risk_model not in {"conservative_sum", "covariance"}:
        raise ValueError("strategy.risk_model must be conservative_sum or covariance")
    for name in ("max_asset_class_weight", "max_sector_weight"):
        value = getattr(strategy, name)
        if value <= 0 or value > 1:
            raise ValueError(f"strategy.{name} must be in (0, 1]")
    if not math.isfinite(strategy.capacity_reference_cash) or strategy.capacity_reference_cash < 0:
        raise ValueError("strategy.capacity_reference_cash must be finite and non-negative")
    if (
        strategy.max_average_amount_participation <= 0
        or strategy.max_average_amount_participation > 1
    ):
        raise ValueError("strategy.max_average_amount_participation must be in (0, 1]")
    if strategy.capacity_days <= 0:
        raise ValueError("strategy.capacity_days must be positive")
    if risk.max_portfolio_drawdown <= 0 or risk.max_portfolio_drawdown >= 1:
        raise ValueError("max_portfolio_drawdown must be in (0, 1)")
    if risk.max_daily_loss <= 0 or risk.max_daily_loss >= 1:
        raise ValueError("max_daily_loss must be in (0, 1)")
    if risk.cooldown_days < 0:
        raise ValueError("cooldown_days must be non-negative")
    _validate_costs(costs)
    backtest_cash = float(raw["backtest"]["initial_cash"])
    paper_cash = float(raw["paper"]["initial_cash"])
    if not math.isfinite(backtest_cash) or backtest_cash <= 0:
        raise ValueError("initial_cash must be positive")
    if not math.isfinite(paper_cash) or paper_cash <= 0:
        raise ValueError("paper.initial_cash must be positive")
    if int(raw["paper"].get("minimum_promotion_sessions", 60)) < 20:
        raise ValueError("paper.minimum_promotion_sessions must be at least 20")
    _validate_auth(raw.get("auth", {}))
    _validate_broker(raw.get("broker", {}), project_root=project_root)
    _validate_shadow_account(raw.get("shadow_account", {}))
    _validate_research_journal(
        raw.get("research_journal", {}), project_root=project_root
    )
    _state_child_path(
        raw.get("monitoring", {}),
        project_root=project_root,
        section="monitoring",
        default=DEFAULT_MONITORING_DIR,
    )


def _validate_data_transport(data: dict[str, Any]) -> None:
    fallback_provider = data.get("fallback_provider", "tencent")
    from .data.providers import registered_provider_names

    supported_providers = set(registered_provider_names())
    if (
        not isinstance(fallback_provider, str)
        or fallback_provider.strip().lower()
        not in supported_providers | {"none"}
    ):
        supported = ", ".join(sorted(supported_providers | {"none"}))
        raise ValueError(
            f"data.fallback_provider must be one of {supported}; "
            f"got {fallback_provider!r}"
        )
    data["fallback_provider"] = fallback_provider.strip().lower()
    provider = data.get("provider", "eastmoney")
    if (
        isinstance(provider, str)
        and fallback_provider.strip().lower() == provider.strip().lower()
    ):
        raise ValueError("data.fallback_provider must differ from data.provider")
    proxy_mode = data.get("proxy_mode", "system")
    if not isinstance(proxy_mode, str) or proxy_mode not in {"system", "direct"}:
        raise ValueError("data.proxy_mode must be system or direct")
    integer_limits = {
        "timeout_seconds": (1, 120, 20),
        "max_attempts": (1, 10, 4),
        "eastmoney_max_attempts": (1, 10, data.get("max_attempts", 4)),
    }
    for name, (minimum, maximum, default) in integer_limits.items():
        value = data.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"data.{name} must be an integer")
        if not minimum <= value <= maximum:
            raise ValueError(
                f"data.{name} must be between {minimum} and {maximum}"
            )

    numeric_limits = {
        "request_interval_seconds": (0.0, 60.0, 2.0),
        "request_jitter_seconds": (0.0, 10.0, 0.5),
        "failure_cooldown_seconds": (0.0, 300.0, 20.0),
        "retry_base_seconds": (0.0, 60.0, 1.0),
        "retry_max_seconds": (0.0, 300.0, 8.0),
        "retry_jitter_seconds": (0.0, 10.0, 0.5),
    }
    parsed: dict[str, float] = {}
    for name, (minimum, maximum, default) in numeric_limits.items():
        value = data.get(name, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"data.{name} must be numeric")
        parsed[name] = float(value)
        if not math.isfinite(parsed[name]) or not minimum <= parsed[name] <= maximum:
            raise ValueError(
                f"data.{name} must be finite and between {minimum} and {maximum}"
            )
    if parsed["retry_max_seconds"] < parsed["retry_base_seconds"]:
        raise ValueError("data.retry_max_seconds must not be below retry_base_seconds")


def _validate_costs(costs: CostSettings) -> None:
    names = (
        "commission_bps",
        "slippage_bps",
        "minimum_commission",
        "sell_stamp_duty_bps",
        "transfer_fee_bps",
    )
    schedules: list[dict[str, Any]] = [
        {name: getattr(costs, name) for name in names},
        *costs.by_instrument_type.values(),
        *costs.history,
    ]
    allowed_values = set(names)
    for instrument_type, schedule in costs.by_instrument_type.items():
        unknown = set(schedule) - allowed_values
        if unknown:
            raise ValueError(
                f"Unknown cost fields for {instrument_type}: {sorted(unknown)}"
            )
    for schedule in costs.history:
        unknown = set(schedule) - allowed_values - {"instrument_type", "start", "end"}
        if unknown:
            raise ValueError(f"Unknown cost history fields: {sorted(unknown)}")
    for schedule in schedules:
        for name in names:
            if name not in schedule:
                continue
            value = float(schedule[name])
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"Trading cost {name} must be finite and non-negative")
    periods: dict[str, list[tuple[date, date | None]]] = {}
    for value in costs.history:
        instrument_type = str(value.get("instrument_type", ""))
        if not instrument_type:
            raise ValueError("Each cost history row requires instrument_type")
        try:
            start = date.fromisoformat(str(value["start"]))
            end = date.fromisoformat(str(value["end"])) if value.get("end") else None
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"Invalid cost history period: {exc}") from exc
        if end and end < start:
            raise ValueError("Cost history end must not precede start")
        periods.setdefault(instrument_type, []).append((start, end))
    for instrument_type, values in periods.items():
        ordered = sorted(values)
        for previous, current in zip(ordered, ordered[1:]):
            previous_end = previous[1]
            if previous_end is None or current[0] <= previous_end:
                raise ValueError(f"Overlapping cost history for {instrument_type}")


def _validate_auth(value: dict[str, Any]) -> None:
    if not isinstance(value, dict):
        raise ValueError("auth must be an object")
    if not isinstance(value.get("enabled", True), bool):
        raise ValueError("auth.enabled must be true or false")
    users_file = value.get("users_file", "state/beta_users.json")
    if not isinstance(users_file, str) or not users_file.strip():
        raise ValueError("auth.users_file must be a non-empty path")
    raw_hours = value.get("session_hours", DEFAULT_AUTH_SESSION_HOURS)
    if isinstance(raw_hours, bool):
        raise ValueError("auth.session_hours must be between 1 and 24")
    hours = float(raw_hours)
    if not math.isfinite(hours) or not 1 <= hours <= 24:
        raise ValueError("auth.session_hours must be between 1 and 24")
    integer_limits = {
        "max_failed_attempts": (DEFAULT_AUTH_MAX_FAILED_ATTEMPTS, 3, 20),
        "failure_window_minutes": (
            DEFAULT_AUTH_FAILURE_WINDOW_MINUTES,
            1,
            1440,
        ),
        "lockout_minutes": (DEFAULT_AUTH_LOCKOUT_MINUTES, 1, 1440),
    }
    for name, (default, minimum, maximum) in integer_limits.items():
        raw_value = value.get(name, default)
        if isinstance(raw_value, bool) or not isinstance(raw_value, int):
            raise ValueError(f"auth.{name} must be an integer")
        if not minimum <= raw_value <= maximum:
            raise ValueError(
                f"auth.{name} must be between {minimum} and {maximum}"
            )


def _validate_broker(
    value: dict[str, Any],
    *,
    project_root: Path | None = None,
) -> None:
    mode = value.get("mode", "disabled")
    if mode not in {"disabled", "sandbox", "live"}:
        raise ValueError("broker.mode must be disabled, sandbox, or live")
    if mode in {"sandbox", "live"}:
        for name, maximum in (
            ("adapter", BROKER_ADAPTER_NAME_MAX_LENGTH),
            ("account_id", BROKER_ACCOUNT_ID_MAX_LENGTH),
        ):
            configured = value.get(name)
            if (
                not isinstance(configured, str)
                or not configured
                or configured.strip() != configured
                or len(configured) > maximum
                or not configured.isprintable()
            ):
                raise ValueError(
                    f"broker.{name} must be canonical text of at most {maximum} "
                    f"characters in {mode} mode"
                )
    raw_minimum = value.get("sandbox_minimum_reconciliations", 20)
    if isinstance(raw_minimum, bool) or not isinstance(raw_minimum, int):
        raise ValueError("broker.sandbox_minimum_reconciliations must be an integer")
    minimum = raw_minimum
    if minimum < 5:
        raise ValueError("broker.sandbox_minimum_reconciliations must be at least 5")
    defaults = {
        "max_order_notional": DEFAULT_BROKER_MAX_ORDER_NOTIONAL,
        "max_daily_notional": DEFAULT_BROKER_MAX_DAILY_NOTIONAL,
    }
    for name, default in defaults.items():
        raw_amount = value.get(name, default)
        if isinstance(raw_amount, bool):
            raise ValueError(f"broker.{name} must be finite and positive")
        amount = float(raw_amount)
        if not math.isfinite(amount) or amount <= 0:
            raise ValueError(f"broker.{name} must be finite and positive")
    if float(value.get("max_daily_notional", defaults["max_daily_notional"])) < float(
        value.get("max_order_notional", defaults["max_order_notional"])
    ):
        raise ValueError("broker.max_daily_notional must cover at least one maximum order")
    path_defaults = {
        "reconciliation_file": "state/broker_reconciliation.csv",
        "orders_file": "state/broker_orders.csv",
        "fills_file": "state/broker_fills.csv",
        "ledger_scope_file": "state/broker_ledger_scope.json",
        "authorization_file": "state/live_authorization.json",
        "batch_approval_file": "state/live_batch_approval.json",
        "kill_switch_file": "state/LIVE_KILL_SWITCH",
    }
    configured_paths: dict[str, str] = {}
    root = (project_root or Path.cwd()).resolve()
    for name, default_path in path_defaults.items():
        raw_path = value.get(name, default_path)
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError(f"broker.{name} must be a non-empty path")
        candidate = Path(raw_path)
        resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (root / candidate).resolve()
        )
        normalized = os.path.normcase(str(resolved))
        previous = configured_paths.get(normalized)
        if previous is not None:
            raise ValueError(f"broker.{name} must differ from broker.{previous}")
        configured_paths[normalized] = name


def _validate_shadow_account(value: dict[str, Any]) -> None:
    raw_maximum = value.get("max_import_bytes", 1_000_000)
    if isinstance(raw_maximum, bool) or not isinstance(raw_maximum, int):
        raise ValueError("shadow_account.max_import_bytes must be an integer")
    if not 1_024 <= raw_maximum <= 5_000_000:
        raise ValueError(
            "shadow_account.max_import_bytes must be between 1024 and 5000000"
        )
    configured: set[str] = set()
    for name, default in (
        ("fills_file", "state/shadow_fills.csv"),
        ("imports_file", "state/shadow_imports.csv"),
    ):
        raw_path = value.get(name, default)
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ValueError(f"shadow_account.{name} must be a non-empty path")
        normalized = str(Path(raw_path)).casefold()
        if normalized in configured:
            raise ValueError("shadow account ledger paths must be different")
        configured.add(normalized)


def _validate_research_journal(
    value: Any,
    *,
    project_root: Path,
) -> None:
    _research_journal_path(value, project_root)


def _research_journal_path(value: Any, project_root: Path) -> Path:
    return _state_child_path(
        value,
        project_root=project_root,
        section="research_journal",
        default=DEFAULT_RESEARCH_JOURNAL_DIR,
    )


def _state_child_path(
    value: Any,
    *,
    project_root: Path,
    section: str,
    default: str,
) -> Path:
    if not isinstance(value, dict):
        raise ValueError(f"{section} must be an object")
    raw_path = value.get("root_dir", default)
    if (
        not isinstance(raw_path, str)
        or not raw_path
        or raw_path != raw_path.strip()
    ):
        raise ValueError(f"{section}.root_dir must be a non-empty path")
    candidate = Path(raw_path)
    try:
        resolved = (
            candidate.resolve()
            if candidate.is_absolute()
            else (project_root / candidate).resolve()
        )
        state_root = (project_root / "state").resolve()
        relative = resolved.relative_to(state_root)
    except (OSError, RuntimeError, ValueError) as exc:
        raise ValueError(
            f"{section}.root_dir must resolve inside the workspace state directory"
        ) from exc
    if not relative.parts:
        raise ValueError(
            f"{section}.root_dir must be a child of the workspace state directory"
        )
    return resolved
