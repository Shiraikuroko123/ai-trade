"""Persistent, research-only watchlists, rules, scans, and alert evidence.

The monitoring layer is deliberately downstream of :class:`MarketData`.  It
never refreshes a provider and it never writes strategy, accounting, risk, or
broker state.  Configuration revisions, scan records, alert triggers, and
alert actions are immutable JSON records scoped to the authenticated owner.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import statistics
import tempfile
from threading import Lock, RLock
from typing import Any, Iterator, Mapping
from uuid import uuid4

from .json_utils import load_unique_json


SCHEMA_VERSION = 1
MAX_CONFIG_REVISIONS = 1_000
MAX_WATCHLISTS = 50
MAX_SYMBOLS_PER_WATCHLIST = 500
MAX_TOTAL_SYMBOLS = 2_000
MAX_RULES = 500
MAX_ALERTS = 5_000
MAX_ACTIONS = 10_000
MAX_SCANS = 2_000
MAX_RECORD_BYTES = 512 * 1024
MAX_SCAN_BYTES = 2 * 1024 * 1024
MAX_STAGING_FILES = 64
MAX_STAGING_BYTES = 16 * 1024 * 1024
MAX_PUBLIC_ALERTS = 200
DEFAULT_ALERT_LIMIT = 100

WATCHLIST_ID = re.compile(r"watch_[0-9a-f]{32}\Z")
RULE_ID = re.compile(r"rule_[0-9a-f]{32}\Z")
ALERT_ID = re.compile(r"alert_[0-9a-f]{32}\Z")
ACTION_ID = re.compile(r"action_[0-9a-f]{32}\Z")
SCAN_ID = re.compile(r"scan_[0-9a-f]{32}\Z")
FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
PROFILE_ID = FINGERPRINT
SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:=+-]{0,63}\Z")
CONFIG_ID = re.compile(r"config_[0-9a-f]{32}\Z")
STAGING_TEMP_NAME = re.compile(
    r"\.(?:revision_[0-9]{8}\.json|alert_[0-9a-f]{32}\.json|"
    r"action_[0-9a-f]{32}\.json|scan_[0-9a-f]{32}\.json|"
    r"\.scan-transaction\.json)\.[A-Za-z0-9_-]+\.tmp\Z"
)

RULE_SEVERITIES = frozenset({"info", "warning", "critical"})
ALERT_STATUSES = frozenset({"open", "acknowledged", "snoozed", "dismissed"})
ALERT_ACTIONS = frozenset({"acknowledge", "dismiss", "reopen", "snooze", "unsnooze"})
ALERT_ACTION_FROM = {
    "acknowledge": frozenset({"open", "snoozed"}),
    "dismiss": frozenset({"open", "acknowledged", "snoozed"}),
    "reopen": frozenset({"acknowledged", "dismissed"}),
    "snooze": frozenset({"open"}),
    "unsnooze": frozenset({"snoozed"}),
}

# These formulas are intentionally server-side and versioned.  A rule that is
# not listed here cannot be persisted, which prevents a UI-only indicator from
# silently becoming a scheduled alert.
RULE_TYPE_METADATA: dict[str, dict[str, Any]] = {
    "close_above": {
        "label": "收盘价高于",
        "metric": "close",
        "operator": "gte",
        "operator_label": "≥",
        "unit": "CNY",
        "threshold_required": True,
        "window_default": None,
        "formula": "latest completed close",
    },
    "close_below": {
        "label": "收盘价低于",
        "metric": "close",
        "operator": "lte",
        "operator_label": "≤",
        "unit": "CNY",
        "threshold_required": True,
        "window_default": None,
        "formula": "latest completed close",
    },
    "daily_return_above": {
        "label": "单日涨跌幅高于",
        "metric": "daily_return",
        "operator": "gte",
        "operator_label": "≥",
        "unit": "ratio",
        "threshold_required": True,
        "window_default": None,
        "formula": "close[t] / close[t-1] - 1",
    },
    "daily_return_below": {
        "label": "单日涨跌幅低于",
        "metric": "daily_return",
        "operator": "lte",
        "operator_label": "≤",
        "unit": "ratio",
        "threshold_required": True,
        "window_default": None,
        "formula": "close[t] / close[t-1] - 1",
    },
    "volume_ratio_above": {
        "label": "成交量比率高于",
        "metric": "volume_ratio",
        "operator": "gte",
        "operator_label": "≥",
        "unit": "ratio",
        "threshold_required": True,
        "window_default": 20,
        "formula": "volume[t] / mean(volume[t-window:t])",
    },
    "volume_ratio_below": {
        "label": "成交量比率低于",
        "metric": "volume_ratio",
        "operator": "lte",
        "operator_label": "≤",
        "unit": "ratio",
        "threshold_required": True,
        "window_default": 20,
        "formula": "volume[t] / mean(volume[t-window:t])",
    },
    "ema_cross_above": {
        "label": "短期 EMA 上穿长期 EMA",
        "metric": "ema_cross",
        "operator": "cross_up",
        "operator_label": "上穿",
        "unit": "boolean",
        "threshold_required": False,
        "window_default": 12,
        "comparison_window_default": 26,
        "formula": "EMA(short)[t-1] ≤ EMA(long)[t-1] and EMA(short)[t] > EMA(long)[t]",
    },
    "ema_cross_below": {
        "label": "短期 EMA 下穿长期 EMA",
        "metric": "ema_cross",
        "operator": "cross_down",
        "operator_label": "下穿",
        "unit": "boolean",
        "threshold_required": False,
        "window_default": 12,
        "comparison_window_default": 26,
        "formula": "EMA(short)[t-1] ≥ EMA(long)[t-1] and EMA(short)[t] < EMA(long)[t]",
    },
    "rsi_above": {
        "label": "RSI 高于",
        "metric": "rsi",
        "operator": "gte",
        "operator_label": "≥",
        "unit": "number",
        "threshold_required": True,
        "window_default": 14,
        "formula": "Wilder RSI over completed closes",
    },
    "rsi_below": {
        "label": "RSI 低于",
        "metric": "rsi",
        "operator": "lte",
        "operator_label": "≤",
        "unit": "number",
        "threshold_required": True,
        "window_default": 14,
        "formula": "Wilder RSI over completed closes",
    },
    "atr_percent_above": {
        "label": "ATR/收盘占比高于",
        "metric": "atr_percent",
        "operator": "gte",
        "operator_label": "≥",
        "unit": "ratio",
        "threshold_required": True,
        "window_default": 14,
        "formula": "Wilder ATR(window) / latest close",
    },
    "data_stale": {
        "label": "数据滞后交易日数达到",
        "metric": "stale_days",
        "operator": "gte",
        "operator_label": "≥",
        "unit": "sessions",
        "threshold_required": True,
        "window_default": None,
        "formula": "completed trading sessions after the latest symbol bar",
    },
}

_CONFIG_FIELDS = frozenset(
    {
        "schema_version",
        "config_id",
        "revision",
        "owner",
        "created_at",
        "actor",
        "action",
        "parent_fingerprint",
        "watchlists",
        "rules",
        "fingerprint",
    }
)
_WATCHLIST_FIELDS = frozenset(
    {"watchlist_id", "name", "enabled", "symbols", "created_at", "updated_at"}
)
_RULE_FIELDS = frozenset(
    {
        "rule_id",
        "watchlist_id",
        "symbol",
        "rule_type",
        "threshold",
        "window",
        "comparison_window",
        "operator",
        "cooldown_sessions",
        "severity",
        "enabled",
        "created_at",
        "updated_at",
    }
)
_ALERT_FIELDS = frozenset(
    {
        "schema_version",
        "alert_id",
        "owner",
        "created_at",
        "scan_id",
        "snapshot_id",
        "manifest_sha256",
        "snapshot_evidence_fingerprint",
        "config_revision",
        "config_fingerprint",
        "rule_id",
        "rule_fingerprint",
        "watchlist_id",
        "symbol",
        "rule_type",
        "rule_label",
        "formula",
        "operator",
        "operator_label",
        "threshold",
        "observed_value",
        "observed_text",
        "data_date",
        "completed_session_cutoff",
        "source",
        "source_file_sha256",
        "evidence_fingerprint",
        "severity",
        "status",
        "triggered_at",
        "fingerprint",
    }
)
_ACTION_FIELDS = frozenset(
    {
        "schema_version",
        "action_id",
        "owner",
        "alert_id",
        "created_at",
        "sequence",
        "actor",
        "action",
        "from_status",
        "to_status",
        "note",
        "snooze_until",
        "alert_fingerprint",
        "fingerprint",
    }
)
_SCAN_FIELDS = frozenset(
    {
        "schema_version",
        "scan_id",
        "owner",
        "created_at",
        "started_at",
        "finished_at",
        "sequence",
        "actor",
        "status",
        "config_revision",
        "config_fingerprint",
        "snapshot_id",
        "manifest_sha256",
        "snapshot_evidence_fingerprint",
        "data_date",
        "completed_session_cutoff",
        "latest_common_session",
        "source_summary",
        "rule_states",
        "triggered_alert_ids",
        "suppressed",
        "exclusions",
        "error",
        "authority",
        "fingerprint",
    }
)
_SCAN_TRANSACTION_FIELDS = frozenset(
    {
        "schema_version",
        "owner",
        "created_at",
        "scan_id",
        "scan_fingerprint",
        "alerts",
        "fingerprint",
    }
)

_AUTHORITY = {
    "research_only": True,
    "execution_authorized": False,
    "strategy_changed": False,
    "paper_account_changed": False,
    "broker_permissions_changed": False,
}

_LOCKS_GUARD = Lock()
_LOCKS: dict[str, "_OwnerLockState"] = {}


class MonitoringConflictError(RuntimeError):
    """An optimistic configuration or alert-state update lost a race."""


class MonitoringCapacityError(RuntimeError):
    pass


class _OwnerLockState:
    def __init__(self) -> None:
        self.thread_lock = RLock()
        self.depth = 0


class MonitoringStore:
    """Owner-scoped immutable record store for monitoring state."""

    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def owner_id(self, owner: str) -> str:
        normalized = _normalize_owner(owner)
        from hashlib import sha256

        return sha256(normalized.encode("utf-8")).hexdigest()

    def profile(self, owner: str) -> "MonitoringProfile":
        return MonitoringProfile(self, self.owner_id(owner))

    def profile_by_id(self, profile_id: str) -> "MonitoringProfile":
        _valid_profile_id(profile_id)
        return MonitoringProfile(self, profile_id)

    def profile_ids(self) -> list[str]:
        users = self.root / "users"
        if users.is_symlink():
            raise RuntimeError("Monitoring user directory must not be a symbolic link")
        if not users.exists():
            return []
        if not users.is_dir():
            raise RuntimeError("Monitoring users path must be a directory")
        values = []
        for path in users.iterdir():
            if path.is_symlink() or not path.is_dir() or not PROFILE_ID.fullmatch(path.name):
                continue
            values.append(path.name)
        return sorted(values)

    @contextmanager
    def _owner_lock(self, profile_id: str) -> Iterator[None]:
        _valid_profile_id(profile_id)
        users = self.root / "users"
        if users.is_symlink():
            raise RuntimeError("Monitoring user directory must not be a symbolic link")
        if users.exists() and not users.is_dir():
            raise RuntimeError("Monitoring users path must be a directory")
        directory = users / profile_id
        if directory.is_symlink():
            raise RuntimeError("Monitoring profile must not be a symbolic link")
        if directory.exists() and not directory.is_dir():
            raise RuntimeError("Monitoring profile path must be a directory")
        key = os.path.normcase(str(directory))
        with _LOCKS_GUARD:
            state = _LOCKS.setdefault(key, _OwnerLockState())
        with state.thread_lock:
            if state.depth:
                state.depth += 1
                try:
                    yield
                finally:
                    state.depth -= 1
                return
            with _file_lock(directory / ".owner.lock"):
                state.depth = 1
                try:
                    yield
                finally:
                    state.depth = 0


class MonitoringProfile:
    def __init__(self, store: MonitoringStore, profile_id: str):
        self.store = store
        self.profile_id = _valid_profile_id(profile_id)

    @property
    def directory(self) -> Path:
        return self.store.root / "users" / self.profile_id

    def current(self) -> dict[str, Any]:
        with self.store._owner_lock(self.profile_id):
            self._recover_scan_transaction_unlocked()
            return self._current_unlocked()

    def create_watchlist(
        self, name: str, *, actor: str, expected_revision: int | None = None
    ) -> dict[str, Any]:
        name = _bounded_text(name, "watchlist name", 80)
        with self.store._owner_lock(self.profile_id):
            current = self._current_unlocked()
            _check_revision(current, expected_revision)
            if len(current["watchlists"]) >= MAX_WATCHLISTS:
                raise MonitoringCapacityError("watchlist limit reached")
            now = _now()
            watchlist = {
                "watchlist_id": f"watch_{uuid4().hex}",
                "name": name,
                "enabled": True,
                "symbols": [],
                "created_at": now,
                "updated_at": now,
            }
            config = self._write_config_unlocked(
                current,
                [*current["watchlists"], watchlist],
                current["rules"],
                action="watchlist_created",
                actor=actor,
            )
            return _public_config(config)

    def mutate_watchlist(
        self,
        watchlist_id: str,
        *,
        action: str,
        actor: str,
        expected_revision: int | None = None,
        symbol: str | None = None,
        name: str | None = None,
        enabled: bool | None = None,
    ) -> dict[str, Any]:
        _valid_watchlist_id(watchlist_id)
        if action not in {"add_symbol", "remove_symbol", "rename", "set_enabled", "delete"}:
            raise ValueError("Unsupported watchlist action")
        if symbol is not None:
            _valid_symbol(symbol)
        with self.store._owner_lock(self.profile_id):
            current = self._current_unlocked()
            _check_revision(current, expected_revision)
            watchlists = [dict(item) for item in current["watchlists"]]
            rules = [dict(item) for item in current["rules"]]
            index = next(
                (index for index, item in enumerate(watchlists) if item["watchlist_id"] == watchlist_id),
                None,
            )
            if index is None:
                raise KeyError(watchlist_id)
            item = watchlists[index]
            if action == "delete":
                watchlists.pop(index)
                rules = [rule for rule in rules if rule["watchlist_id"] != watchlist_id]
            elif action == "add_symbol":
                if symbol is None:
                    raise ValueError("symbol is required")
                if symbol not in item["symbols"]:
                    if len(item["symbols"]) >= MAX_SYMBOLS_PER_WATCHLIST:
                        raise MonitoringCapacityError("watchlist symbol limit reached")
                    item["symbols"] = [*item["symbols"], symbol]
                item["updated_at"] = _now()
            elif action == "remove_symbol":
                if symbol is None:
                    raise ValueError("symbol is required")
                item["symbols"] = [value for value in item["symbols"] if value != symbol]
                rules = [
                    rule
                    for rule in rules
                    if not (rule["watchlist_id"] == watchlist_id and rule["symbol"] == symbol)
                ]
                item["updated_at"] = _now()
            elif action == "rename":
                item["name"] = _bounded_text(name, "watchlist name", 80)
                item["updated_at"] = _now()
            else:
                if not isinstance(enabled, bool):
                    raise ValueError("enabled must be a boolean")
                item["enabled"] = enabled
                item["updated_at"] = _now()
            config = self._write_config_unlocked(
                current,
                watchlists,
                rules,
                action=f"watchlist_{action}",
                actor=actor,
            )
            return _public_config(config)

    def create_rule(
        self,
        rule: Mapping[str, Any],
        *,
        actor: str,
        expected_revision: int | None = None,
    ) -> dict[str, Any]:
        normalized = _normalize_rule(rule, require_id=False)
        with self.store._owner_lock(self.profile_id):
            current = self._current_unlocked()
            _check_revision(current, expected_revision)
            if len(current["rules"]) >= MAX_RULES:
                raise MonitoringCapacityError("monitoring rule limit reached")
            if not any(item["watchlist_id"] == normalized["watchlist_id"] for item in current["watchlists"]):
                raise KeyError(normalized["watchlist_id"])
            watchlist = next(item for item in current["watchlists"] if item["watchlist_id"] == normalized["watchlist_id"])
            if normalized["symbol"] not in watchlist["symbols"]:
                raise ValueError("symbol must be in the selected watchlist")
            normalized["rule_id"] = f"rule_{uuid4().hex}"
            now = _now()
            normalized["created_at"] = now
            normalized["updated_at"] = now
            config = self._write_config_unlocked(
                current,
                current["watchlists"],
                [*current["rules"], normalized],
                action="rule_created",
                actor=actor,
            )
            return _public_config(config)

    def mutate_rule(
        self,
        rule_id: str,
        *,
        action: str,
        actor: str,
        expected_revision: int | None = None,
        patch: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        _valid_rule_id(rule_id)
        if action not in {"update", "delete"}:
            raise ValueError("Unsupported rule action")
        with self.store._owner_lock(self.profile_id):
            current = self._current_unlocked()
            _check_revision(current, expected_revision)
            rules = [dict(item) for item in current["rules"]]
            index = next((i for i, item in enumerate(rules) if item["rule_id"] == rule_id), None)
            if index is None:
                raise KeyError(rule_id)
            if action == "delete":
                rules.pop(index)
            else:
                original = rules[index]
                merged = {**original, **dict(patch or {})}
                merged["rule_id"] = rule_id
                merged["created_at"] = original["created_at"]
                merged["updated_at"] = _now()
                normalized = _normalize_rule(merged, require_id=True)
                if not any(item["watchlist_id"] == normalized["watchlist_id"] for item in current["watchlists"]):
                    raise KeyError(normalized["watchlist_id"])
                watchlist = next(item for item in current["watchlists"] if item["watchlist_id"] == normalized["watchlist_id"])
                if normalized["symbol"] not in watchlist["symbols"]:
                    raise ValueError("symbol must be in the selected watchlist")
                rules[index] = normalized
            config = self._write_config_unlocked(
                current,
                current["watchlists"],
                rules,
                action=f"rule_{action}",
                actor=actor,
            )
            return _public_config(config)

    def alerts(self, *, limit: int = DEFAULT_ALERT_LIMIT) -> list[dict[str, Any]]:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PUBLIC_ALERTS:
            raise ValueError(f"limit must be between 1 and {MAX_PUBLIC_ALERTS}")
        with self.store._owner_lock(self.profile_id):
            self._verify_integrity_unlocked()
            public = self._project_alerts_unlocked()
        return public[:limit]

    def alert_summary(self) -> dict[str, Any]:
        with self.store._owner_lock(self.profile_id):
            self._verify_integrity_unlocked()
            alerts = self._project_alerts_unlocked()
        unresolved = [
            item for item in alerts if item["status"] in {"open", "snoozed"}
        ]
        severity_counts = {key: 0 for key in RULE_SEVERITIES}
        for item in unresolved:
            severity_counts[item["severity"]] += 1
        return {
            "unresolved_count": len(unresolved),
            "severity_counts": severity_counts,
            "stale_count": sum(
                1 for item in unresolved if item["rule_type"] == "data_stale"
            ),
        }

    def status_parts(
        self, *, limit: int = DEFAULT_ALERT_LIMIT
    ) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, Any], dict[str, Any] | None]:
        """Read config, alert projection, summary, and latest scan atomically."""
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_PUBLIC_ALERTS:
            raise ValueError(f"limit must be between 1 and {MAX_PUBLIC_ALERTS}")
        with self.store._owner_lock(self.profile_id):
            self._verify_integrity_unlocked()
            config = self._current_unlocked()
            projected = self._project_alerts_unlocked()
            unresolved = [
                item for item in projected if item["status"] in {"open", "snoozed"}
            ]
            severity_counts = {key: 0 for key in RULE_SEVERITIES}
            for item in unresolved:
                severity_counts[item["severity"]] += 1
            scans = self._list_records_unlocked("scans", _SCAN_FIELDS, MAX_SCANS)
            latest = (
                max(scans, key=lambda item: (item["sequence"], item["scan_id"]))
                if scans
                else None
            )
            public_scan = dict(latest) if latest else None
            if public_scan:
                public_scan.pop("owner", None)
            return (
                config,
                projected[:limit],
                {
                    "unresolved_count": len(unresolved),
                    "severity_counts": severity_counts,
                    "stale_count": sum(
                        1 for item in unresolved if item["rule_type"] == "data_stale"
                    ),
                },
                public_scan,
            )

    def verify_integrity(self) -> None:
        """Validate immutable cross-record references for this owner."""
        with self.store._owner_lock(self.profile_id):
            self._verify_integrity_unlocked()

    def _verify_integrity_unlocked(self) -> None:
        self._recover_scan_transaction_unlocked()
        configurations = self._configuration_records_unlocked()
        scans = self._list_records_unlocked("scans", _SCAN_FIELDS, MAX_SCANS)
        alerts = self._list_records_unlocked("alerts", _ALERT_FIELDS, MAX_ALERTS)
        actions = self._list_records_unlocked("actions", _ACTION_FIELDS, MAX_ACTIONS)
        config_by_revision = {item["revision"]: item for item in configurations}
        by_scan = {item["scan_id"]: item for item in scans}
        by_alert = {item["alert_id"]: item for item in alerts}
        ordered_scans = sorted(
            scans, key=lambda item: (item["sequence"], item["scan_id"])
        )
        if [item["sequence"] for item in ordered_scans] != list(
            range(1, len(ordered_scans) + 1)
        ):
            raise RuntimeError("Monitoring scan sequence is not contiguous")

        referenced_alerts: set[str] = set()
        for scan in ordered_scans:
            _validate_scan_evidence_binding(scan)
            configuration = config_by_revision.get(scan["config_revision"])
            if (
                configuration is None
                or configuration["fingerprint"] != scan["config_fingerprint"]
            ):
                raise RuntimeError(
                    "Monitoring scan configuration binding is invalid"
                )
            rules = {item["rule_id"]: item for item in configuration["rules"]}
            for rule_id, state in scan["rule_states"].items():
                rule = rules.get(rule_id)
                if (
                    rule is None
                    or state["rule_fingerprint"] != _rule_fingerprint(rule)
                ):
                    raise RuntimeError(
                        "Monitoring scan rule-state binding is invalid"
                    )
            for alert_id in scan["triggered_alert_ids"]:
                if alert_id in referenced_alerts:
                    raise RuntimeError("Monitoring alert is referenced by multiple scans")
                referenced_alerts.add(alert_id)
                alert = by_alert.get(alert_id)
                if alert is None or alert["scan_id"] != scan["scan_id"]:
                    raise RuntimeError("Monitoring scan-to-alert reference is invalid")
                if alert_id != _alert_id(scan["scan_id"], alert["rule_id"]):
                    raise RuntimeError("Monitoring alert id binding is invalid")
                rule = rules.get(alert["rule_id"])
                if rule is None:
                    raise RuntimeError(
                        "Monitoring alert references a missing historical rule"
                    )
                _validate_alert_evidence_binding(alert, scan, rule)
        for alert in alerts:
            if alert["scan_id"] not in by_scan:
                raise RuntimeError("Monitoring alert references a missing scan")
            if alert["alert_id"] not in referenced_alerts:
                raise RuntimeError("Monitoring alert is not referenced by its scan")
        actions_by_alert: dict[str, list[dict[str, Any]]] = {}
        for action in actions:
            if action["alert_id"] not in by_alert:
                raise RuntimeError("Monitoring action references a missing alert")
            actions_by_alert.setdefault(action["alert_id"], []).append(action)
        for alert in alerts:
            _validated_action_chain(
                alert, actions_by_alert.get(alert["alert_id"], [])
            )

    def _project_alerts_unlocked(self) -> list[dict[str, Any]]:
        records = self._list_records_unlocked("alerts", _ALERT_FIELDS, MAX_ALERTS)
        actions = self._list_records_unlocked("actions", _ACTION_FIELDS, MAX_ACTIONS)
        by_alert: dict[str, list[dict[str, Any]]] = {}
        for action in actions:
            by_alert.setdefault(action["alert_id"], []).append(action)
        public: list[dict[str, Any]] = []
        for record in records:
            current = dict(record)
            alert_actions = _validated_action_chain(
                record, by_alert.get(record["alert_id"], [])
            )
            for action in alert_actions:
                current["status"] = action["to_status"]
                current["last_action"] = _public_action(action)
                current["snooze_until"] = action.get("snooze_until")
            current["state_fingerprint"] = _alert_state_fingerprint(
                record, alert_actions
            )
            current.pop("owner", None)
            public.append(current)
        public.sort(key=lambda item: (item["triggered_at"], item["alert_id"]), reverse=True)
        return public

    def alert_action(
        self,
        alert_id: str,
        *,
        action: str,
        actor: str,
        note: str = "",
        snooze_until: str | None = None,
        expected_state_fingerprint: str | None = None,
    ) -> dict[str, Any]:
        _valid_alert_id(alert_id)
        if action not in ALERT_ACTIONS:
            raise ValueError("Unsupported alert action")
        note = _bounded_optional_text(note, "note", 1_000)
        if snooze_until is not None:
            parsed_snooze = _parse_iso_date(snooze_until, "snooze_until")
            if action == "snooze" and parsed_snooze < date.today():
                raise ValueError("snooze_until must not be in the past")
        with self.store._owner_lock(self.profile_id):
            self._verify_integrity_unlocked()
            alerts = self._list_records_unlocked("alerts", _ALERT_FIELDS, MAX_ALERTS)
            alert = next((item for item in alerts if item["alert_id"] == alert_id), None)
            if alert is None:
                raise KeyError(alert_id)
            actions = self._list_records_unlocked("actions", _ACTION_FIELDS, MAX_ACTIONS)
            if len(actions) >= MAX_ACTIONS:
                raise MonitoringCapacityError("monitoring action limit reached")
            related = _validated_action_chain(
                alert,
                [item for item in actions if item["alert_id"] == alert_id],
            )
            current_fingerprint = _alert_state_fingerprint(alert, related)
            if expected_state_fingerprint is not None and (
                not isinstance(expected_state_fingerprint, str)
                or not FINGERPRINT.fullmatch(expected_state_fingerprint)
            ):
                raise ValueError(
                    "expected_state_fingerprint must be a lowercase SHA-256 fingerprint"
                )
            if (
                expected_state_fingerprint is not None
                and expected_state_fingerprint != current_fingerprint
            ):
                raise MonitoringConflictError(
                    "alert state changed; reload before writing"
                )
            status = alert["status"]
            for item in related:
                status = item["to_status"]
            if status not in ALERT_ACTION_FROM[action]:
                raise MonitoringConflictError(
                    f"cannot {action} an alert in {status} state"
                )
            if action == "acknowledge":
                target = "acknowledged"
            elif action == "dismiss":
                target = "dismissed"
            elif action == "reopen":
                target = "open"
            elif action == "snooze":
                if snooze_until is None:
                    raise ValueError("snooze_until is required")
                target = "snoozed"
            else:
                target = "open"
                snooze_until = None
            if status == target and action not in {"snooze", "unsnooze"}:
                raise MonitoringConflictError("alert is already in the requested state")
            record = {
                "schema_version": SCHEMA_VERSION,
                "action_id": f"action_{uuid4().hex}",
                "owner": self.profile_id,
                "alert_id": alert_id,
                "created_at": _now(),
                "sequence": len(related) + 1,
                "actor": _bounded_text(actor, "actor", 80),
                "action": action,
                "from_status": status,
                "to_status": target,
                "note": note,
                "snooze_until": snooze_until,
                "alert_fingerprint": alert["fingerprint"],
            }
            record["fingerprint"] = _fingerprint({k: v for k, v in record.items() if k != "fingerprint"})
            action_path = self.directory / "actions" / f"{record['action_id']}.json"
            _validate_action_record(record, action_path)
            _atomic_create_json(
                action_path,
                record,
                _ACTION_FIELDS,
                MAX_RECORD_BYTES,
            )
            result = dict(alert)
            result["status"] = target
            result["last_action"] = _public_action(record)
            result["state_fingerprint"] = _alert_state_fingerprint(
                alert, [*related, record]
            )
            result.pop("owner", None)
            return result

    def expire_snoozes(self, *, as_of: date, actor: str) -> int:
        """Append auditable unsnooze actions whose review date has arrived."""
        if not isinstance(as_of, date):
            raise ValueError("as_of must be a date")
        changed = 0
        with self.store._owner_lock(self.profile_id):
            # Expiry is also a public mutator.  Run the same recovery and
            # cross-record checks as every other entry point before projecting
            # alert state, so a stale transaction marker cannot be bypassed.
            self._verify_integrity_unlocked()
            for alert in self._project_alerts_unlocked():
                until = alert.get("snooze_until")
                if alert.get("status") != "snoozed" or not until:
                    continue
                if _parse_iso_date(until, "snooze_until") > as_of:
                    continue
                self.alert_action(
                    alert["alert_id"],
                    action="unsnooze",
                    actor=actor,
                    note=f"暂缓日期 {until} 已到，扫描自动重新打开",
                    expected_state_fingerprint=alert.get("state_fingerprint"),
                )
                changed += 1
        return changed

    def latest_scan(self) -> dict[str, Any] | None:
        with self.store._owner_lock(self.profile_id):
            self._verify_integrity_unlocked()
            records = self._list_records_unlocked("scans", _SCAN_FIELDS, MAX_SCANS)
        if not records:
            return None
        records.sort(key=lambda item: (item["sequence"], item["scan_id"]), reverse=True)
        value = dict(records[0])
        value.pop("owner", None)
        return value

    def _latest_rule_states_unlocked(self) -> dict[str, dict[str, Any]]:
        records = self._list_records_unlocked("scans", _SCAN_FIELDS, MAX_SCANS)
        states: dict[str, dict[str, Any]] = {}
        for scan in sorted(
            records, key=lambda item: (item["sequence"], item["scan_id"]), reverse=True
        ):
            if scan["status"] == "failed":
                continue
            for rule_id, state in scan["rule_states"].items():
                states.setdefault(rule_id, state)
        return states

    def scan_record(self, scan_id: str) -> dict[str, Any] | None:
        _valid_scan_id(scan_id)
        with self.store._owner_lock(self.profile_id):
            self._verify_integrity_unlocked()
            path = self.directory / "scans" / f"{scan_id}.json"
            if not path.is_file() or path.is_symlink():
                return None
            record = _read_record(path, _SCAN_FIELDS, MAX_SCAN_BYTES)
            if record["owner"] != self.profile_id:
                raise RuntimeError("Monitoring scan owner binding does not match")
            _validate_scan_record(record, path)
            record.pop("owner", None)
            return record

    def _current_unlocked(self) -> dict[str, Any]:
        self._recover_scan_transaction_unlocked()
        records = self._configuration_records_unlocked()
        return _public_config(records[-1]) if records else _empty_config()

    def _configuration_records_unlocked(self) -> list[dict[str, Any]]:
        self._assert_owner_directory()
        directory = self.directory / "configurations"
        if directory.is_symlink():
            raise RuntimeError("Monitoring configurations must not be a symbolic link")
        if not directory.exists():
            return []
        paths = sorted(directory.iterdir())
        for path in paths:
            if (
                path.is_symlink()
                or not path.is_file()
                or not re.fullmatch(r"revision_[0-9]{8}\.json", path.name)
            ):
                raise RuntimeError("Monitoring configuration filename is invalid")
        if len(paths) > MAX_CONFIG_REVISIONS:
            raise MonitoringCapacityError("monitoring configuration revision limit reached")
        if not paths:
            return []
        records = [_read_record(path, _CONFIG_FIELDS, MAX_RECORD_BYTES) for path in paths]
        records.sort(key=lambda item: item["revision"])
        previous: dict[str, Any] | None = None
        for path, record in zip(sorted(paths, key=lambda item: int(item.stem.removeprefix("revision_"))), records):
            if record["owner"] != self.profile_id:
                raise RuntimeError("Monitoring configuration owner binding does not match")
            if not isinstance(record["revision"], int) or isinstance(record["revision"], bool) or record["revision"] < 1:
                raise RuntimeError("Monitoring configuration revision is invalid")
            if path.stem != f"revision_{record['revision']:08d}":
                raise RuntimeError("Monitoring configuration filename does not match revision")
            if not isinstance(record.get("config_id"), str) or not CONFIG_ID.fullmatch(record["config_id"]):
                raise RuntimeError("Monitoring configuration id is invalid")
            _valid_timestamp(record["created_at"], "configuration created_at")
            _bounded_text(record["actor"], "actor", 80)
            _bounded_text(record["action"], "action", 80)
            if record["revision"] == 1 and record["parent_fingerprint"] is not None:
                raise RuntimeError("First monitoring configuration cannot have a parent")
            if previous is None and record["revision"] != 1:
                raise RuntimeError("Monitoring configuration revision chain must start at one")
            if record["revision"] > 1 and (
                not isinstance(record["parent_fingerprint"], str)
                or not FINGERPRINT.fullmatch(record["parent_fingerprint"])
            ):
                raise RuntimeError("Monitoring configuration parent fingerprint is invalid")
            normalized_watchlists = _normalize_watchlists(record["watchlists"])
            normalized_rules = _normalize_rules(record["rules"], normalized_watchlists)
            if normalized_watchlists != record["watchlists"] or normalized_rules != record["rules"]:
                raise RuntimeError("Monitoring configuration contains non-canonical records")
            if previous is not None:
                if record["revision"] != previous["revision"] + 1:
                    raise RuntimeError("Monitoring configuration revision chain has a gap")
                if record["parent_fingerprint"] != previous["fingerprint"]:
                    raise RuntimeError("Monitoring configuration parent fingerprint mismatch")
            previous = record
        return records

    def _write_config_unlocked(
        self,
        current: Mapping[str, Any],
        watchlists: list[Mapping[str, Any]],
        rules: list[Mapping[str, Any]],
        *,
        action: str,
        actor: str,
    ) -> dict[str, Any]:
        normalized_watchlists = _normalize_watchlists(watchlists)
        normalized_rules = _normalize_rules(rules, normalized_watchlists)
        if sum(len(item["symbols"]) for item in normalized_watchlists) > MAX_TOTAL_SYMBOLS:
            raise MonitoringCapacityError("total monitored symbol limit reached")
        revision = int(current["revision"]) + 1
        if revision > MAX_CONFIG_REVISIONS:
            raise MonitoringCapacityError(
                "monitoring configuration revision limit reached"
            )
        record = {
            "schema_version": SCHEMA_VERSION,
            "config_id": f"config_{uuid4().hex}",
            "revision": revision,
            "owner": self.profile_id,
            "created_at": _now(),
            "actor": _bounded_text(actor, "actor", 80),
            "action": _bounded_text(action, "action", 80),
            "parent_fingerprint": current["fingerprint"] if current["revision"] else None,
            "watchlists": normalized_watchlists,
            "rules": normalized_rules,
        }
        record["fingerprint"] = _fingerprint({k: v for k, v in record.items() if k != "fingerprint"})
        _atomic_create_json(
            self.directory / "configurations" / f"revision_{revision:08d}.json",
            record,
            _CONFIG_FIELDS,
            MAX_RECORD_BYTES,
        )
        return _public_config(record)

    def _list_records_unlocked(
        self, kind: str, fields: frozenset[str], maximum: int
    ) -> list[dict[str, Any]]:
        self._assert_owner_directory()
        directory = self.directory / kind
        if directory.is_symlink():
            raise RuntimeError(f"Monitoring {kind} must not be a symbolic link")
        if not directory.exists():
            return []
        if not directory.is_dir():
            raise RuntimeError(f"Monitoring {kind} path must be a directory")
        paths: list[Path] = []
        for path in directory.iterdir():
            if path.is_symlink():
                raise RuntimeError(f"Monitoring {kind} records must not be symbolic links")
            if not path.is_file() or path.suffix != ".json":
                raise RuntimeError(f"Unexpected monitoring {kind} entry: {path.name}")
            paths.append(path)
        if len(paths) > maximum:
            raise MonitoringCapacityError(f"monitoring {kind} record limit reached")
        records = []
        for path in paths:
            record = _read_record(path, fields, MAX_SCAN_BYTES if kind == "scans" else MAX_RECORD_BYTES)
            if record.get("owner") != self.profile_id:
                raise RuntimeError(f"Monitoring {kind} owner binding does not match")
            if kind == "alerts":
                _validate_alert_record(record, path)
            elif kind == "actions":
                _validate_action_record(record, path)
            elif kind == "scans":
                _validate_scan_record(record, path)
            records.append(record)
        return records

    def _assert_owner_directory(self) -> None:
        users = self.store.root / "users"
        directory = self.directory
        if users.is_symlink() or directory.is_symlink():
            raise RuntimeError("Monitoring owner paths must not be symbolic links")
        if users.exists() and not users.is_dir():
            raise RuntimeError("Monitoring users path must be a directory")
        if directory.exists() and not directory.is_dir():
            raise RuntimeError("Monitoring profile path must be a directory")

    def _scan_by_id_unlocked(self, scan_id: str) -> dict[str, Any] | None:
        _valid_scan_id(scan_id)
        path = self.directory / "scans" / f"{scan_id}.json"
        if not path.is_file() or path.is_symlink():
            return None
        record = _read_record(path, _SCAN_FIELDS, MAX_SCAN_BYTES)
        if record["owner"] != self.profile_id:
            raise RuntimeError("Monitoring scan owner binding does not match")
        _validate_scan_record(record, path)
        return record

    def _successful_scan_unlocked(
        self, snapshot_id: str, config_fingerprint: str
    ) -> dict[str, Any] | None:
        matches = [
            record
            for record in self._list_records_unlocked(
                "scans", _SCAN_FIELDS, MAX_SCANS
            )
            if record["status"] == "succeeded"
            and record["snapshot_id"] == snapshot_id
            and record["config_fingerprint"] == config_fingerprint
        ]
        if not matches:
            return None
        return max(matches, key=lambda item: (item["sequence"], item["scan_id"]))

    def _latest_scan_unlocked(self) -> dict[str, Any] | None:
        records = self._list_records_unlocked("scans", _SCAN_FIELDS, MAX_SCANS)
        if not records:
            return None
        return max(records, key=lambda item: (item["sequence"], item["scan_id"]))

    def _latest_alert_for_rule_unlocked(
        self, rule_id: str, rule_fingerprint: str | None = None
    ) -> dict[str, Any] | None:
        records = [
            item
            for item in self._list_records_unlocked("alerts", _ALERT_FIELDS, MAX_ALERTS)
            if item["rule_id"] == rule_id
            and (
                rule_fingerprint is None
                or item["rule_fingerprint"] == rule_fingerprint
            )
        ]
        if not records:
            return None
        scans = self._list_records_unlocked("scans", _SCAN_FIELDS, MAX_SCANS)
        sequence_by_scan = {item["scan_id"]: item["sequence"] for item in scans}
        return max(
            records,
            key=lambda item: (sequence_by_scan.get(item["scan_id"], 0), item["alert_id"]),
        )

    def _write_alert_unlocked(
        self, alert_id: str, record: Mapping[str, Any]
    ) -> bool:
        path = self.directory / "alerts" / f"{alert_id}.json"
        _validate_record_payload(record, _ALERT_FIELDS)
        _validate_alert_record(record, path)
        if record["owner"] != self.profile_id:
            raise RuntimeError("Monitoring alert owner binding does not match")
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("Monitoring alert target must be a regular file")
            existing = _read_record(path, _ALERT_FIELDS, MAX_RECORD_BYTES)
            _validate_alert_record(existing, path)
            volatile = {"created_at", "triggered_at", "fingerprint"}
            if (
                {key: value for key, value in existing.items() if key not in volatile}
                != {key: value for key, value in record.items() if key not in volatile}
            ):
                raise RuntimeError("Existing monitoring alert content does not match")
            return False
        count = len(self._list_records_unlocked("alerts", _ALERT_FIELDS, MAX_ALERTS))
        if count >= MAX_ALERTS:
            raise MonitoringCapacityError("monitoring alert limit reached")
        _atomic_create_json(path, record, _ALERT_FIELDS, MAX_RECORD_BYTES)
        return True

    def _rollback_alert_unlocked(
        self, alert_id: str, expected_fingerprint: str
    ) -> None:
        """Remove an uncommitted alert when its parent scan cannot be written."""
        path = self.directory / "alerts" / f"{alert_id}.json"
        if path.is_symlink():
            raise RuntimeError("Cannot roll back a symbolic-link monitoring alert")
        if not path.exists():
            return
        if not path.is_file():
            raise RuntimeError("Cannot roll back a non-file monitoring alert")
        existing = _read_record(path, _ALERT_FIELDS, MAX_RECORD_BYTES)
        if existing["fingerprint"] != expected_fingerprint:
            raise RuntimeError("Cannot roll back a changed monitoring alert")
        _unlink_file_durable(path)

    def _write_scan_unlocked(self, record: Mapping[str, Any]) -> None:
        path = self.directory / "scans" / f"{record['scan_id']}.json"
        _validate_record_payload(record, _SCAN_FIELDS)
        _validate_scan_record(record, path)
        if record["owner"] != self.profile_id:
            raise RuntimeError("Monitoring scan owner binding does not match")
        if path.exists():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError("Monitoring scan target must be a regular file")
            existing = _read_record(path, _SCAN_FIELDS, MAX_SCAN_BYTES)
            if existing["fingerprint"] != record["fingerprint"]:
                raise RuntimeError("Existing monitoring scan content does not match")
            return
        count = len(self._list_records_unlocked("scans", _SCAN_FIELDS, MAX_SCANS))
        if count >= MAX_SCANS:
            raise MonitoringCapacityError("monitoring scan limit reached")
        _atomic_create_json(path, record, _SCAN_FIELDS, MAX_SCAN_BYTES)

    def _begin_scan_transaction_unlocked(
        self,
        scan: Mapping[str, Any],
        pending_alerts: list[tuple[str, Mapping[str, Any]]],
    ) -> None:
        if len(pending_alerts) > MAX_RULES:
            raise MonitoringCapacityError("monitoring scan alert limit reached")
        path = self.directory / ".scan-transaction.json"
        if path.exists() or path.is_symlink():
            self._recover_scan_transaction_unlocked()
        if path.exists() or path.is_symlink():
            raise RuntimeError("Monitoring scan transaction is already in progress")
        record = {
            "schema_version": SCHEMA_VERSION,
            "owner": self.profile_id,
            "created_at": _now(),
            "scan_id": scan["scan_id"],
            "scan_fingerprint": scan["fingerprint"],
            "alerts": [
                {"alert_id": alert_id, "fingerprint": alert["fingerprint"]}
                for alert_id, alert in pending_alerts
            ],
        }
        record["fingerprint"] = _fingerprint(
            {key: value for key, value in record.items() if key != "fingerprint"}
        )
        _validate_record_payload(record, _SCAN_TRANSACTION_FIELDS)
        _atomic_replace_json(
            path, record, _SCAN_TRANSACTION_FIELDS, MAX_RECORD_BYTES
        )

    def _finish_scan_transaction_unlocked(self) -> None:
        path = self.directory / ".scan-transaction.json"
        if path.is_symlink():
            raise RuntimeError("Monitoring scan transaction must not be a symbolic link")
        if path.exists():
            if not path.is_file():
                raise RuntimeError("Monitoring scan transaction must be a regular file")
            _unlink_file_durable(path)

    def _recover_scan_transaction_unlocked(self) -> None:
        """Complete or roll back a scan interrupted between file publishes."""
        self._cleanup_staging_unlocked()
        path = self.directory / ".scan-transaction.json"
        if path.is_symlink():
            raise RuntimeError("Monitoring scan transaction must not be a symbolic link")
        if not path.exists():
            return
        if not path.is_file():
            raise RuntimeError("Monitoring scan transaction is invalid")
        marker = _read_record(path, _SCAN_TRANSACTION_FIELDS, MAX_RECORD_BYTES)
        if marker["owner"] != self.profile_id:
            raise RuntimeError("Monitoring scan transaction owner binding is invalid")
        _valid_timestamp(marker["created_at"], "scan transaction created_at")
        _valid_scan_id(marker["scan_id"])
        if not isinstance(marker["scan_fingerprint"], str) or not FINGERPRINT.fullmatch(
            marker["scan_fingerprint"]
        ):
            raise RuntimeError("Monitoring scan transaction fingerprint is invalid")
        expected: dict[str, str] = {}
        if not isinstance(marker["alerts"], list):
            raise RuntimeError("Monitoring scan transaction alerts are invalid")
        if len(marker["alerts"]) > MAX_RULES:
            raise MonitoringCapacityError("Monitoring scan transaction alert limit reached")
        for item in marker["alerts"]:
            if not isinstance(item, dict) or set(item) != {"alert_id", "fingerprint"}:
                raise RuntimeError("Monitoring scan transaction alert entry is invalid")
            alert_id = _valid_alert_id(item["alert_id"])
            fingerprint = item["fingerprint"]
            if not isinstance(fingerprint, str) or not FINGERPRINT.fullmatch(fingerprint):
                raise RuntimeError("Monitoring scan transaction alert fingerprint is invalid")
            if alert_id in expected:
                raise RuntimeError("Monitoring scan transaction has duplicate alerts")
            expected[alert_id] = fingerprint

        scan_path = self.directory / "scans" / f"{marker['scan_id']}.json"
        if scan_path.is_symlink():
            raise RuntimeError("Monitoring scan transaction scan is a symbolic link")
        if scan_path.exists() and not scan_path.is_file():
            raise RuntimeError("Monitoring scan transaction scan is invalid")
        if scan_path.is_file():
            scan = _read_record(scan_path, _SCAN_FIELDS, MAX_SCAN_BYTES)
            _validate_scan_record(scan, scan_path)
            if (
                scan["owner"] != self.profile_id
                or scan["fingerprint"] != marker["scan_fingerprint"]
            ):
                raise RuntimeError("Monitoring scan transaction scan binding is invalid")
            triggered = scan["triggered_alert_ids"]
            if len(triggered) != len(set(triggered)) or set(triggered) != set(expected):
                raise RuntimeError("Monitoring scan transaction alert set is invalid")
            present: dict[str, Path] = {}
            missing = False
            for alert_id, fingerprint in expected.items():
                alert_path = self.directory / "alerts" / f"{alert_id}.json"
                if alert_path.is_symlink():
                    raise RuntimeError("Monitoring scan transaction alert is a symbolic link")
                if not alert_path.exists():
                    missing = True
                    continue
                if not alert_path.is_file():
                    raise RuntimeError("Monitoring scan transaction alert must be a regular file")
                alert = _read_record(alert_path, _ALERT_FIELDS, MAX_RECORD_BYTES)
                _validate_alert_record(alert, alert_path)
                if (
                    alert["owner"] != self.profile_id
                    or alert["fingerprint"] != fingerprint
                    or alert["scan_id"] != marker["scan_id"]
                ):
                    raise RuntimeError("Monitoring scan transaction alert binding is invalid")
                present[alert_id] = alert_path
            if missing:
                # A scan with only part of its alerts is not a committed
                # result.  Roll back the whole transaction when it is still
                # the newest scan; this prevents a permanent integrity deadlock.
                self._rollback_incomplete_scan_transaction_unlocked(
                    path, scan, expected, present
                )
                return
            _unlink_file_durable(path)
            return

        self._assert_transaction_has_no_actions_unlocked(set(expected))
        for alert_id, fingerprint in expected.items():
            alert_path = self.directory / "alerts" / f"{alert_id}.json"
            if not alert_path.exists():
                continue
            if alert_path.is_symlink():
                raise RuntimeError("Monitoring scan transaction alert is a symbolic link")
            alert = _read_record(alert_path, _ALERT_FIELDS, MAX_RECORD_BYTES)
            _validate_alert_record(alert, alert_path)
            if (
                alert["owner"] != self.profile_id
                or alert["fingerprint"] != fingerprint
                or alert["scan_id"] != marker["scan_id"]
            ):
                raise RuntimeError("Monitoring scan transaction rollback binding is invalid")
            _unlink_file_durable(alert_path)
        _unlink_file_durable(path)

    def _rollback_incomplete_scan_transaction_unlocked(
        self,
        marker_path: Path,
        scan: Mapping[str, Any],
        expected: Mapping[str, str],
        present: Mapping[str, Path],
    ) -> None:
        scans = self._list_records_unlocked("scans", _SCAN_FIELDS, MAX_SCANS)
        latest = max(scans, key=lambda item: (item["sequence"], item["scan_id"]), default=None)
        if latest is None or latest["scan_id"] != scan["scan_id"]:
            raise RuntimeError("Cannot roll back a non-tail monitoring scan transaction")
        self._assert_transaction_has_no_actions_unlocked(set(expected))
        scan_path = self.directory / "scans" / f"{scan['scan_id']}.json"
        if scan_path.is_symlink() or not scan_path.is_file():
            raise RuntimeError("Monitoring scan transaction scan changed during rollback")
        _unlink_file_durable(scan_path)
        for alert_path in present.values():
            if alert_path.is_symlink() or not alert_path.is_file():
                raise RuntimeError("Monitoring scan transaction alert changed during rollback")
            _unlink_file_durable(alert_path)
        _unlink_file_durable(marker_path)

    def _assert_transaction_has_no_actions_unlocked(self, alert_ids: set[str]) -> None:
        if not alert_ids:
            return
        actions = self._list_records_unlocked("actions", _ACTION_FIELDS, MAX_ACTIONS)
        if any(action["alert_id"] in alert_ids for action in actions):
            raise RuntimeError("Monitoring scan transaction has committed alert actions")

    def _cleanup_staging_unlocked(self) -> None:
        staging = self.directory / ".staging"
        if staging.is_symlink():
            raise RuntimeError("Monitoring staging directory must not be a symbolic link")
        if not staging.exists():
            return
        if not staging.is_dir():
            raise RuntimeError("Monitoring staging path must be a directory")
        entries = list(staging.iterdir())
        total_bytes = 0
        for path in entries:
            if path.is_symlink():
                raise RuntimeError("Monitoring staging entries must not be symbolic links")
            if not path.is_file() or not STAGING_TEMP_NAME.fullmatch(path.name):
                raise RuntimeError(f"Unexpected monitoring staging entry: {path.name}")
            try:
                total_bytes += path.stat().st_size
            except OSError as exc:
                raise RuntimeError("Cannot inspect monitoring staging entry") from exc
        over_limit = len(entries) > MAX_STAGING_FILES or total_bytes > MAX_STAGING_BYTES
        for path in entries:
            _unlink_file_durable(path)
        if over_limit:
            raise MonitoringCapacityError("Monitoring staging residue exceeded the recovery limit")


class MonitoringEngine:
    """Evaluate immutable watchlist rules against one validated MarketData snapshot."""

    def __init__(self, config: Any, store: MonitoringStore | None = None):
        self.config = config
        root = getattr(config, "monitoring_dir", None)
        if root is None:
            root = Path(config.project_root) / "state" / "monitoring"
        self.store = store or MonitoringStore(root)

    def status(self, owner: str, *, market: Any | None = None) -> dict[str, Any]:
        profile = self.store.profile(owner)
        config, alerts, alert_summary, scan = profile.status_parts(
            limit=MAX_PUBLIC_ALERTS
        )
        snapshot = self.snapshot(market)
        return {
            "schema_version": SCHEMA_VERSION,
            "generated_at": _now(),
            "authority": dict(_AUTHORITY),
            "configuration": config,
            "watchlists": config["watchlists"],
            "rules": config["rules"],
            "rule_types": _public_rule_types(),
            "instruments": [
                {"symbol": item.symbol, "name": item.name, "asset_class": item.asset_class, "sector": item.sector}
                for item in self.config.instruments
            ],
            "snapshot": snapshot,
            "scan": scan or {"status": "not_run", "data_date": None, "error": None},
            "alerts": alerts,
            "summary": {
                "watchlist_count": len(config["watchlists"]),
                "symbol_count": len({symbol for item in config["watchlists"] for symbol in item["symbols"]}),
                "enabled_rule_count": sum(1 for item in config["rules"] if item["enabled"]),
                "rule_count": len(config["rules"]),
                **alert_summary,
            },
            "empty_state": _empty_state(config, scan, alerts),
        }

    def scan(self, owner: str, *, actor: str = "scheduler", market: Any | None = None) -> dict[str, Any]:
        profile = self.store.profile(owner)
        return self.scan_profile(profile, actor=actor, market=market)

    def scan_all_profiles(
        self,
        *,
        actor: str = "scheduler",
        market: Any | None = None,
        allowed_profile_ids: set[str] | None = None,
    ) -> list[dict[str, Any]]:
        values = []
        for profile_id in self.store.profile_ids():
            if allowed_profile_ids is not None and profile_id not in allowed_profile_ids:
                continue
            profile = self.store.profile_by_id(profile_id)
            try:
                current = profile.current()
            except (OSError, RuntimeError, ValueError) as exc:
                values.append(
                    {
                        "profile_id": profile_id,
                        "status": "failed",
                        "scan_id": None,
                        "error": _normalize_scan_error(
                            {"code": "profile_unavailable", "message": str(exc)}
                        ),
                        "authority": dict(_AUTHORITY),
                    }
                )
                continue
            if not current["watchlists"] or not any(rule["enabled"] for rule in current["rules"]):
                continue
            try:
                result = self.scan_profile(profile, actor=actor, market=market)
                values.append({"profile_id": profile_id, **result})
            except (OSError, RuntimeError, ValueError, KeyError, TypeError) as exc:
                values.append(
                    {
                        "profile_id": profile_id,
                        "status": "failed",
                        "scan_id": None,
                        "error": _normalize_scan_error(
                            {"code": "profile_scan_failed", "message": str(exc)}
                        ),
                        "authority": dict(_AUTHORITY),
                    }
                )
        return values

    def scan_profile(self, profile: MonitoringProfile, *, actor: str, market: Any | None = None) -> dict[str, Any]:
        started_at = _now()
        if market is None:
            try:
                from .data.market import MarketData

                market = MarketData(self.config, recover_snapshot=False)
                snapshot = self.snapshot(market)
            except (OSError, RuntimeError, ValueError) as exc:
                snapshot = {
                    "available": False,
                    "data_date": None,
                    "snapshot_id": None,
                    "error": {
                        "code": "market_unavailable",
                        "message": str(exc),
                    },
                }
        else:
            snapshot = self.snapshot(market)
        with self.store._owner_lock(profile.profile_id):
            profile._verify_integrity_unlocked()
            current = profile._current_unlocked()
            enabled_rules = [rule for rule in current["rules"] if rule["enabled"]]
            if not enabled_rules:
                return {
                    "status": "no_rules",
                    "scan_id": None,
                    "config_revision": current["revision"],
                    "config_fingerprint": current["fingerprint"],
                    "data_date": snapshot.get("data_date"),
                    "triggered_alert_ids": [],
                    "suppressed": [],
                    "exclusions": [],
                }
            if not snapshot["available"]:
                previous = profile._latest_scan_unlocked()
                scan = self._failed_scan_record(
                    profile,
                    current,
                    actor=actor,
                    started_at=started_at,
                    sequence=int(previous["sequence"]) + 1 if previous else 1,
                    error=snapshot.get("error"),
                )
                profile._write_scan_unlocked(scan)
                result = dict(scan)
                result.pop("owner", None)
                result["reused"] = False
                return result
            profile.expire_snoozes(
                as_of=date.fromisoformat(snapshot["completed_session_cutoff"]),
                actor=actor,
            )
            completed = profile._successful_scan_unlocked(
                snapshot["snapshot_id"], current["fingerprint"]
            )
            if completed is not None:
                value = dict(completed)
                value.pop("owner", None)
                value["reused"] = True
                return value
            scan_id = _scan_id(
                profile.profile_id, snapshot["snapshot_id"], current["fingerprint"]
            )
            existing = profile._scan_by_id_unlocked(scan_id)
            if existing is not None:
                # Failed and partial attempts are immutable evidence, not
                # cache hits. Retrying after a provider/data repair gets a
                # new attempt ID and can produce a complete result.
                scan_id = f"scan_{uuid4().hex}"
            previous = profile._latest_scan_unlocked()
            scan_sequence = int(previous["sequence"]) + 1 if previous else 1
            previous_states = profile._latest_rule_states_unlocked()
            triggered: list[str] = []
            suppressed: list[dict[str, Any]] = []
            exclusions: list[dict[str, Any]] = []
            rule_states: dict[str, dict[str, Any]] = {}
            source_counts: dict[str, int] = {}
            pending_alerts: list[tuple[str, dict[str, Any]]] = []
            try:
                for rule in enabled_rules:
                    watchlist = next((item for item in current["watchlists"] if item["watchlist_id"] == rule["watchlist_id"]), None)
                    if watchlist is None or not watchlist["enabled"] or rule["symbol"] not in watchlist["symbols"]:
                        exclusions.append({"rule_id": rule["rule_id"], "symbol": rule["symbol"], "code": "watchlist_disabled", "message": "watchlist is disabled or no longer contains the symbol"})
                        continue
                    result = self._evaluate_rule(rule, market, snapshot["data_date"], snapshot["completed_session_cutoff"])
                    source = result.get("source") or "unknown"
                    source_counts[source] = source_counts.get(source, 0) + 1
                    if result.get("exclusion"):
                        exclusions.append({"rule_id": rule["rule_id"], "symbol": rule["symbol"], **result["exclusion"]})
                        continue
                    rule_states[rule["rule_id"]] = {
                        "rule_fingerprint": _rule_fingerprint(rule),
                        "triggered": bool(result.get("triggered")),
                        "observed_value": result.get("observed_value"),
                        "data_date": snapshot["data_date"],
                    }
                    if not result.get("triggered"):
                        continue
                    previous_state = previous_states.get(rule["rule_id"], {})
                    same_rule = previous_state.get("rule_fingerprint") == _rule_fingerprint(rule)
                    if same_rule and previous_state.get("triggered"):
                        suppressed.append({"rule_id": rule["rule_id"], "symbol": rule["symbol"], "reason": "condition_still_true"})
                        continue
                    last_alert = profile._latest_alert_for_rule_unlocked(
                        rule["rule_id"], _rule_fingerprint(rule)
                    )
                    cooldown = int(rule["cooldown_sessions"])
                    if (
                        last_alert
                        and _sessions_since(
                            last_alert.get("data_date"),
                            snapshot["data_date"],
                            getattr(market, "calendar", []),
                        )
                        <= cooldown
                    ):
                        suppressed.append({"rule_id": rule["rule_id"], "symbol": rule["symbol"], "reason": "cooldown", "cooldown_sessions": cooldown})
                        continue
                    alert_id = _alert_id(scan_id, rule["rule_id"])
                    record = self._alert_record(profile, rule, result, snapshot, scan_id, current)
                    pending_alerts.append((alert_id, record))
                    triggered.append(alert_id)
            except (ArithmeticError, AttributeError, KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
                failed = self._failed_scan_record(
                    profile,
                    current,
                    actor=actor,
                    started_at=started_at,
                    sequence=scan_sequence,
                    error={"code": "rule_evaluation_failed", "message": str(exc)},
                    scan_id=scan_id,
                    snapshot=snapshot,
                    rule_states=rule_states,
                    triggered_alert_ids=[],
                    suppressed=suppressed,
                    exclusions=exclusions,
                    source_counts=source_counts,
                )
                profile._write_scan_unlocked(failed)
                result = dict(failed)
                result.pop("owner", None)
                result["reused"] = False
                return result
            status = "succeeded" if not exclusions else "partial"
            scan = {
                "schema_version": SCHEMA_VERSION,
                "scan_id": scan_id,
                "owner": profile.profile_id,
                "created_at": _now(),
                "started_at": started_at,
                "finished_at": _now(),
                "sequence": scan_sequence,
                "actor": _bounded_text(actor, "actor", 80),
                "status": status,
                "config_revision": current["revision"],
                "config_fingerprint": current["fingerprint"],
                "snapshot_id": snapshot["snapshot_id"],
                "manifest_sha256": snapshot.get("manifest_sha256"),
                "snapshot_evidence_fingerprint": snapshot[
                    "evidence_fingerprint"
                ],
                "data_date": snapshot["data_date"],
                "completed_session_cutoff": snapshot["completed_session_cutoff"],
                "latest_common_session": snapshot["latest_common_session"],
                "source_summary": {"providers": [{"provider": key, "rule_count": value} for key, value in sorted(source_counts.items())]},
                "rule_states": rule_states,
                "triggered_alert_ids": triggered,
                "suppressed": suppressed,
                "exclusions": exclusions,
                "error": None,
                "authority": dict(_AUTHORITY),
            }
            scan["fingerprint"] = _fingerprint({k: v for k, v in scan.items() if k != "fingerprint"})
            written_alert_ids: list[str] = []
            created_alerts: list[tuple[str, str]] = []
            transaction_started = bool(pending_alerts)
            if transaction_started:
                profile._begin_scan_transaction_unlocked(scan, pending_alerts)
            try:
                for alert_id, record in pending_alerts:
                    if profile._write_alert_unlocked(alert_id, record):
                        created_alerts.append((alert_id, record["fingerprint"]))
                    written_alert_ids.append(alert_id)
            except (OSError, RuntimeError, ValueError, MonitoringCapacityError) as exc:
                for alert_id, fingerprint in reversed(created_alerts):
                    profile._rollback_alert_unlocked(alert_id, fingerprint)
                if transaction_started:
                    profile._finish_scan_transaction_unlocked()
                written_alert_ids = []
                failure_exclusions = [
                    *exclusions,
                    {
                        "code": "alert_write_failed",
                        "message": str(exc),
                    },
                ]
                failed = self._failed_scan_record(
                    profile,
                    current,
                    actor=actor,
                    started_at=started_at,
                    sequence=scan_sequence,
                    error={"code": "alert_write_failed", "message": str(exc)},
                    scan_id=scan_id,
                    snapshot=snapshot,
                    rule_states=rule_states,
                    triggered_alert_ids=written_alert_ids,
                    suppressed=suppressed,
                    exclusions=failure_exclusions,
                    source_counts=source_counts,
                )
                profile._write_scan_unlocked(failed)
                result = dict(failed)
                result.pop("owner", None)
                result["reused"] = False
                return result
            try:
                profile._write_scan_unlocked(scan)
            except (OSError, RuntimeError, ValueError, MonitoringCapacityError):
                for alert_id, fingerprint in reversed(created_alerts):
                    profile._rollback_alert_unlocked(alert_id, fingerprint)
                if transaction_started:
                    profile._finish_scan_transaction_unlocked()
                raise
            if transaction_started:
                profile._finish_scan_transaction_unlocked()
            result = dict(scan)
            result.pop("owner", None)
            result["reused"] = False
            return result

    def _failed_scan_record(
        self,
        profile: MonitoringProfile,
        config: Mapping[str, Any],
        *,
        actor: str,
        started_at: str,
        sequence: int,
        error: Any,
        scan_id: str | None = None,
        snapshot: Mapping[str, Any] | None = None,
        rule_states: Mapping[str, Mapping[str, Any]] | None = None,
        triggered_alert_ids: list[str] | None = None,
        suppressed: list[Mapping[str, Any]] | None = None,
        exclusions: list[Mapping[str, Any]] | None = None,
        source_counts: Mapping[str, int] | None = None,
    ) -> dict[str, Any]:
        if triggered_alert_ids:
            raise ValueError("failed scans cannot publish triggered alerts")
        normalized_error = _normalize_scan_error(error)
        evidence = snapshot if snapshot and snapshot.get("available") else {}
        record = {
            "schema_version": SCHEMA_VERSION,
            "scan_id": scan_id or f"scan_{uuid4().hex}",
            "owner": profile.profile_id,
            "created_at": _now(),
            "started_at": started_at,
            "finished_at": _now(),
            "sequence": sequence,
            "actor": _bounded_text(actor, "actor", 80),
            "status": "failed",
            "config_revision": config["revision"],
            "config_fingerprint": config["fingerprint"],
            "snapshot_id": evidence.get("snapshot_id"),
            "manifest_sha256": evidence.get("manifest_sha256"),
            "snapshot_evidence_fingerprint": evidence.get(
                "evidence_fingerprint"
            ),
            "data_date": evidence.get("data_date"),
            "completed_session_cutoff": evidence.get("completed_session_cutoff"),
            "latest_common_session": evidence.get("latest_common_session"),
            "source_summary": {
                "providers": [
                    {"provider": key, "rule_count": value}
                    for key, value in sorted((source_counts or {}).items())
                ]
            },
            "rule_states": {
                key: dict(value) for key, value in (rule_states or {}).items()
            },
            "triggered_alert_ids": [],
            "suppressed": [dict(item) for item in (suppressed or [])],
            "exclusions": [dict(item) for item in (exclusions or [])],
            "error": normalized_error,
            "authority": dict(_AUTHORITY),
        }
        record["fingerprint"] = _fingerprint(
            {key: value for key, value in record.items() if key != "fingerprint"}
        )
        return record

    def snapshot(self, market: Any | None) -> dict[str, Any]:
        if market is None:
            try:
                from .data.market import MarketData

                market = MarketData(self.config, recover_snapshot=False)
            except (OSError, RuntimeError, ValueError) as exc:
                return {"available": False, "data_date": None, "snapshot_id": None, "error": {"code": "market_unavailable", "message": str(exc)}}
        try:
            data_date = getattr(market, "latest_common_session", None) or market.latest_date()
            cutoff = getattr(market, "completed_through", None) or data_date
            metadata = market.snapshot_metadata() if hasattr(market, "snapshot_metadata") else {}
            digest = _fingerprint(metadata)
            snapshot_id = f"market-{data_date.isoformat()}-{digest[:12]}"
            manifest = getattr(market, "manifest", None)
            files = manifest.get("files", {}) if isinstance(manifest, dict) else {}
            providers: dict[str, int] = {}
            for symbol in getattr(market, "symbols", {}):
                entry = files.get(symbol, {}) if isinstance(files, dict) else {}
                provider = str(entry.get("source_provider") or entry.get("source") or "unknown")
                providers[provider] = providers.get(provider, 0) + 1
            return {
                "available": True,
                "data_date": data_date.isoformat(),
                "completed_session_cutoff": cutoff.isoformat(),
                "latest_common_session": data_date.isoformat(),
                "snapshot_id": snapshot_id,
                "manifest_sha256": getattr(market, "manifest_sha256", None),
                "providers": [{"provider": key, "instrument_count": value} for key, value in sorted(providers.items())],
                "evidence_fingerprint": digest,
            }
        except (AttributeError, KeyError, TypeError, ValueError, OSError, RuntimeError) as exc:
            return {"available": False, "data_date": None, "snapshot_id": None, "error": {"code": "snapshot_invalid", "message": str(exc)}}

    def _evaluate_rule(self, rule: Mapping[str, Any], market: Any, data_date: str, cutoff: str) -> dict[str, Any]:
        symbol = rule["symbol"]
        # A stale-data rule must inspect the symbol up to the completed
        # cutoff, while other rules intentionally use the common snapshot
        # date so their cross-symbol evidence remains comparable.
        selected_date = date.fromisoformat(
            cutoff if rule["rule_type"] == "data_stale" else data_date
        )
        window = int(rule["window"]) if rule.get("window") is not None else None
        comparison = int(rule["comparison_window"]) if rule.get("comparison_window") is not None else None
        history_count = max(window or 1, comparison or 1, 30) * 3 + 5
        try:
            history = list(market.history(symbol, selected_date, history_count))
        except (AttributeError, KeyError, RuntimeError, ValueError) as exc:
            return {"exclusion": {"code": "history_unavailable", "message": str(exc)}, "source": "unknown"}
        history = [bar for bar in history if getattr(bar, "date", None) <= selected_date]
        latest = history[-1] if history else None
        source, file_hash = _source_for(market, symbol)
        if latest is None:
            return {"exclusion": {"code": "missing_bar", "message": "no completed bar is available for this symbol"}, "source": source}
        closes = [float(getattr(bar, "close")) for bar in history if _positive(getattr(bar, "close", None))]
        if len(closes) != len(history):
            return {"exclusion": {"code": "invalid_close", "message": "one or more bars has an invalid close"}, "source": source}
        kind = rule["rule_type"]
        observed: float | int | None = None
        triggered = False
        if kind in {"close_above", "close_below"}:
            observed = closes[-1]
            triggered = _compare(observed, rule["threshold"], rule["operator"])
        elif kind in {"daily_return_above", "daily_return_below"}:
            if len(closes) < 2:
                return {"exclusion": {"code": "insufficient_history", "message": "at least two completed closes are required"}, "source": source}
            observed = closes[-1] / closes[-2] - 1.0
            triggered = _compare(observed, rule["threshold"], rule["operator"])
        elif kind in {"volume_ratio_above", "volume_ratio_below"}:
            values = [
                _nonnegative(getattr(bar, "volume", None)) for bar in history
            ]
            if any(value is None for value in values):
                return {
                    "exclusion": {
                        "code": "invalid_volume",
                        "message": "one or more bars has an invalid volume",
                    },
                    "source": source,
                }
            numeric_values = [float(value) for value in values]
            if len(numeric_values) < int(rule["window"]) + 1:
                return {"exclusion": {"code": "insufficient_history", "message": f"at least {int(rule['window']) + 1} valid volume bars are required"}, "source": source}
            baseline = statistics.fmean(numeric_values[-int(rule["window"]) - 1:-1])
            if baseline <= 0:
                return {"exclusion": {"code": "invalid_volume_baseline", "message": "volume baseline is not positive"}, "source": source}
            observed = numeric_values[-1] / baseline
            triggered = _compare(observed, rule["threshold"], rule["operator"])
        elif kind in {"ema_cross_above", "ema_cross_below"}:
            short = int(rule["window"])
            long = int(rule["comparison_window"])
            if len(closes) < long + 2:
                return {"exclusion": {"code": "insufficient_history", "message": f"at least {long + 2} completed closes are required"}, "source": source}
            short_values = _ema_series(closes, short)
            long_values = _ema_series(closes, long)
            previous_short, current_short = short_values[-2], short_values[-1]
            previous_long, current_long = long_values[-2], long_values[-1]
            observed = current_short - current_long
            triggered = (previous_short <= previous_long and current_short > current_long) if rule["operator"] == "cross_up" else (previous_short >= previous_long and current_short < current_long)
        elif kind in {"rsi_above", "rsi_below"}:
            period = int(rule["window"])
            value = _wilder_rsi(closes, period)
            if value is None:
                return {"exclusion": {"code": "insufficient_history", "message": f"at least {period + 1} completed closes are required"}, "source": source}
            observed = value
            triggered = _compare(observed, rule["threshold"], rule["operator"])
        elif kind == "atr_percent_above":
            period = int(rule["window"])
            value = _wilder_atr_percent(history, period)
            if value is None:
                return {"exclusion": {"code": "insufficient_history", "message": f"at least {period + 1} completed OHLC bars are required"}, "source": source}
            observed = value
            triggered = _compare(observed, rule["threshold"], rule["operator"])
        elif kind == "data_stale":
            latest_date = getattr(latest, "date", None)
            observed = _sessions_since(
                latest_date.isoformat(),
                cutoff,
                getattr(market, "calendar", []),
            )
            triggered = _compare(observed, rule["threshold"], rule["operator"])
        meta = RULE_TYPE_METADATA[kind]
        observed_text = _format_observed(observed, meta["unit"])
        return {
            "triggered": bool(triggered),
            "observed_value": observed,
            "observed_text": observed_text,
            "source": source,
            "source_file_sha256": file_hash,
        }

    def _alert_record(self, profile: MonitoringProfile, rule: Mapping[str, Any], result: Mapping[str, Any], snapshot: Mapping[str, Any], scan_id: str, config: Mapping[str, Any]) -> dict[str, Any]:
        meta = RULE_TYPE_METADATA[rule["rule_type"]]
        evidence = {
            "snapshot_id": snapshot["snapshot_id"],
            "manifest_sha256": snapshot.get("manifest_sha256"),
            "source_file_sha256": result.get("source_file_sha256"),
            "rule_id": rule["rule_id"],
            "rule_fingerprint": _rule_fingerprint(rule),
            "observed_value": result.get("observed_value"),
            "threshold": rule.get("threshold"),
            "data_date": snapshot["data_date"],
        }
        evidence_fingerprint = _fingerprint(evidence)
        record = {
            "schema_version": SCHEMA_VERSION,
            "alert_id": _alert_id(scan_id, rule["rule_id"]),
            "owner": profile.profile_id,
            "created_at": _now(),
            "scan_id": scan_id,
            "snapshot_id": snapshot["snapshot_id"],
            "manifest_sha256": snapshot.get("manifest_sha256"),
            "snapshot_evidence_fingerprint": snapshot.get(
                "evidence_fingerprint"
            ),
            "config_revision": config["revision"],
            "config_fingerprint": config["fingerprint"],
            "rule_id": rule["rule_id"],
            "rule_fingerprint": _rule_fingerprint(rule),
            "watchlist_id": rule["watchlist_id"],
            "symbol": rule["symbol"],
            "rule_type": rule["rule_type"],
            "rule_label": meta["label"],
            "formula": meta["formula"],
            "operator": rule["operator"],
            "operator_label": meta["operator_label"],
            "threshold": rule.get("threshold"),
            "observed_value": result.get("observed_value"),
            "observed_text": result.get("observed_text"),
            "data_date": snapshot["data_date"],
            "completed_session_cutoff": snapshot["completed_session_cutoff"],
            "source": result.get("source") or "unknown",
            "source_file_sha256": result.get("source_file_sha256"),
            "evidence_fingerprint": evidence_fingerprint,
            "severity": rule["severity"],
            "status": "open",
            "triggered_at": _now(),
        }
        record["fingerprint"] = _fingerprint({k: v for k, v in record.items() if k != "fingerprint"})
        return record


def _empty_config() -> dict[str, Any]:
    body = {"schema_version": SCHEMA_VERSION, "revision": 0, "watchlists": [], "rules": []}
    return {**body, "fingerprint": _fingerprint(body), "config_id": None, "created_at": None, "actor": None, "action": None}


def _public_config(value: Mapping[str, Any]) -> dict[str, Any]:
    result = {key: value for key, value in value.items() if key != "owner"}
    result["watchlists"] = [dict(item) for item in value.get("watchlists", [])]
    result["rules"] = [dict(item) for item in value.get("rules", [])]
    return result


def _public_action(value: Mapping[str, Any]) -> dict[str, Any]:
    return {key: item for key, item in value.items() if key not in {"owner", "alert_fingerprint", "fingerprint"}}


def _alert_state_fingerprint(
    alert: Mapping[str, Any], actions: list[Mapping[str, Any]]
) -> str:
    return _fingerprint(
        {
            "alert_fingerprint": alert["fingerprint"],
            "action_fingerprints": [item["fingerprint"] for item in actions],
        }
    )


def _validated_action_chain(
    alert: Mapping[str, Any], actions: list[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    ordered = sorted((dict(item) for item in actions), key=lambda item: item["sequence"])
    expected_status = alert["status"]
    for sequence, action in enumerate(ordered, start=1):
        if action["sequence"] != sequence:
            raise RuntimeError("Monitoring alert action sequence is not contiguous")
        if action["alert_fingerprint"] != alert["fingerprint"]:
            raise RuntimeError("Monitoring alert action binding does not match")
        if action["from_status"] != expected_status:
            raise RuntimeError("Monitoring alert action state chain does not match")
        expected_status = action["to_status"]
    return ordered


def _validate_scan_evidence_binding(scan: Mapping[str, Any]) -> None:
    snapshot_id = scan["snapshot_id"]
    if snapshot_id is None:
        return
    evidence_fingerprint = scan["snapshot_evidence_fingerprint"]
    expected_snapshot_id = (
        f"market-{scan['data_date']}-{evidence_fingerprint[:12]}"
    )
    if snapshot_id != expected_snapshot_id:
        raise RuntimeError("Monitoring scan snapshot fingerprint binding is invalid")
    if scan["latest_common_session"] != scan["data_date"]:
        raise RuntimeError("Monitoring scan common-session binding is invalid")
    if date.fromisoformat(scan["completed_session_cutoff"]) < date.fromisoformat(
        scan["data_date"]
    ):
        raise RuntimeError("Monitoring scan completed-session cutoff is invalid")
    if any(
        state["data_date"] != scan["data_date"]
        for state in scan["rule_states"].values()
    ):
        raise RuntimeError("Monitoring scan rule-state date binding is invalid")


def _validate_alert_evidence_binding(
    alert: Mapping[str, Any], scan: Mapping[str, Any], rule: Mapping[str, Any]
) -> None:
    scan_fields = (
        "snapshot_id",
        "manifest_sha256",
        "snapshot_evidence_fingerprint",
        "config_revision",
        "config_fingerprint",
        "data_date",
        "completed_session_cutoff",
    )
    if any(alert[field] != scan[field] for field in scan_fields):
        raise RuntimeError("Monitoring alert snapshot/config binding is invalid")

    metadata = RULE_TYPE_METADATA[rule["rule_type"]]
    expected_rule_fields = {
        "rule_fingerprint": _rule_fingerprint(rule),
        "watchlist_id": rule["watchlist_id"],
        "symbol": rule["symbol"],
        "rule_type": rule["rule_type"],
        "rule_label": metadata["label"],
        "formula": metadata["formula"],
        "operator": rule["operator"],
        "operator_label": metadata["operator_label"],
        "threshold": rule["threshold"],
        "severity": rule["severity"],
    }
    if any(alert[field] != expected for field, expected in expected_rule_fields.items()):
        raise RuntimeError("Monitoring alert historical-rule binding is invalid")

    state = scan["rule_states"].get(rule["rule_id"])
    if (
        state is None
        or not state["triggered"]
        or state["rule_fingerprint"] != alert["rule_fingerprint"]
        or state["observed_value"] != alert["observed_value"]
        or state["data_date"] != alert["data_date"]
    ):
        raise RuntimeError("Monitoring alert rule-state binding is invalid")
    if alert["observed_text"] != _format_observed(
        alert["observed_value"], metadata["unit"]
    ):
        raise RuntimeError("Monitoring alert observed-text binding is invalid")

    expected_evidence = _fingerprint(
        {
            "snapshot_id": alert["snapshot_id"],
            "manifest_sha256": alert["manifest_sha256"],
            "source_file_sha256": alert["source_file_sha256"],
            "rule_id": alert["rule_id"],
            "rule_fingerprint": alert["rule_fingerprint"],
            "observed_value": alert["observed_value"],
            "threshold": alert["threshold"],
            "data_date": alert["data_date"],
        }
    )
    if alert["evidence_fingerprint"] != expected_evidence:
        raise RuntimeError("Monitoring alert evidence fingerprint is invalid")


def _public_rule_types() -> list[dict[str, Any]]:
    return [{"rule_type": key, **{field: value for field, value in meta.items()}} for key, meta in RULE_TYPE_METADATA.items()]


def _empty_state(config: Mapping[str, Any], scan: Mapping[str, Any] | None, alerts: list[Mapping[str, Any]]) -> dict[str, Any]:
    if not config["watchlists"]:
        return {"code": "no_watchlists", "label": "尚未建立监控列表", "recovery_action": "create_watchlist"}
    if not config["rules"]:
        return {"code": "no_rules", "label": "已有标的但尚未建立规则", "recovery_action": "create_rule"}
    if scan is None:
        return {"code": "not_scanned", "label": "尚未运行扫描", "recovery_action": "run_scan"}
    if scan.get("status") == "failed":
        return {"code": "failed", "label": "最近一次扫描失败，需先修复数据", "recovery_action": "run_scan"}
    if scan.get("status") == "partial":
        return {"code": "partial", "label": "扫描部分完成，存在数据排除", "recovery_action": "review_exclusions"}
    if not alerts:
        return {"code": "no_alerts", "label": "已扫描，当前没有触发告警", "recovery_action": "review_rules"}
    return {"code": "ready", "label": "监控证据可供复核", "recovery_action": "review_alerts"}


def _normalize_watchlists(values: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if len(values) > MAX_WATCHLISTS:
        raise MonitoringCapacityError("watchlist limit reached")
    result = []
    seen: set[str] = set()
    for value in values:
        if not isinstance(value, Mapping):
            raise ValueError("watchlist must be an object")
        if set(value) != _WATCHLIST_FIELDS:
            raise ValueError("watchlist schema is invalid")
        watchlist_id = _valid_watchlist_id(value.get("watchlist_id"))
        if watchlist_id in seen:
            raise ValueError("duplicate watchlist id")
        seen.add(watchlist_id)
        symbols = value.get("symbols")
        if not isinstance(symbols, list) or len(symbols) > MAX_SYMBOLS_PER_WATCHLIST:
            raise ValueError("watchlist symbols are invalid")
        normalized_symbols = sorted({_valid_symbol(item) for item in symbols})
        result.append({
            "watchlist_id": watchlist_id,
            "name": _bounded_text(value.get("name"), "watchlist name", 80),
            "enabled": _strict_bool(value.get("enabled"), "watchlist enabled"),
            "symbols": normalized_symbols,
            "created_at": _valid_timestamp(value.get("created_at"), "watchlist created_at"),
            "updated_at": _valid_timestamp(value.get("updated_at"), "watchlist updated_at"),
        })
    result.sort(key=lambda item: (item["name"].casefold(), item["watchlist_id"]))
    return result


def _normalize_rules(values: list[Mapping[str, Any]], watchlists: list[Mapping[str, Any]]) -> list[dict[str, Any]]:
    if len(values) > MAX_RULES:
        raise MonitoringCapacityError("monitoring rule limit reached")
    watchlist_map = {item["watchlist_id"]: item for item in watchlists}
    result = []
    seen: set[str] = set()
    for value in values:
        rule = _normalize_rule(value, require_id=True)
        if rule["rule_id"] in seen:
            raise ValueError("duplicate monitoring rule id")
        seen.add(rule["rule_id"])
        watchlist = watchlist_map.get(rule["watchlist_id"])
        if watchlist is None or rule["symbol"] not in watchlist["symbols"]:
            raise ValueError("rule symbol must belong to its watchlist")
        result.append(rule)
    result.sort(key=lambda item: (item["watchlist_id"], item["symbol"], item["rule_id"]))
    return result


def _normalize_rule(value: Mapping[str, Any], *, require_id: bool) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("monitoring rule must be an object")
    allowed = _RULE_FIELDS if require_id else _RULE_FIELDS - {"rule_id", "created_at", "updated_at", "operator"}
    unknown = set(value) - allowed
    if unknown:
        raise ValueError("Unsupported monitoring rule fields: " + ", ".join(sorted(unknown)))
    if require_id and set(value) != _RULE_FIELDS:
        raise ValueError("monitoring rule schema is invalid")
    rule_id = value.get("rule_id")
    if require_id:
        _valid_rule_id(rule_id)
    elif rule_id is not None:
        _valid_rule_id(rule_id)
    watchlist_id = _valid_watchlist_id(value.get("watchlist_id"))
    symbol = _valid_symbol(value.get("symbol"))
    rule_type = value.get("rule_type")
    if rule_type not in RULE_TYPE_METADATA:
        raise ValueError("Unsupported monitoring rule type")
    meta = RULE_TYPE_METADATA[rule_type]
    threshold = value.get("threshold")
    if meta["threshold_required"]:
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or not math.isfinite(float(threshold)):
            raise ValueError("threshold must be a finite number")
        threshold = float(threshold)
        if rule_type.startswith("close_") and threshold <= 0:
            raise ValueError("close threshold must be positive")
        if rule_type.startswith("daily_return_") and not -1 <= threshold <= 10:
            raise ValueError("daily return threshold must be between -1 and 10")
        if rule_type.startswith("rsi_") and not 0 <= threshold <= 100:
            raise ValueError("RSI threshold must be between 0 and 100")
        if rule_type == "atr_percent_above" and not 0 <= threshold <= 10:
            raise ValueError("ATR percentage threshold must be between 0 and 10")
        if rule_type == "data_stale" and (
            threshold < 0 or threshold > 365 or not threshold.is_integer()
        ):
            raise ValueError("stale threshold must be a whole number between 0 and 365 days")
        if rule_type.startswith("volume_ratio") and threshold < 0:
            raise ValueError("volume ratio threshold must be non-negative")
    else:
        threshold = None
    window = value.get("window")
    if window is None:
        window = meta.get("window_default")
    comparison = value.get("comparison_window")
    if comparison is None:
        comparison = meta.get("comparison_window_default")
    if window is not None:
        window = _bounded_int(window, "window", 2, 500)
    if comparison is not None:
        comparison = _bounded_int(comparison, "comparison_window", 2, 1_000)
    if rule_type.startswith("ema_cross"):
        if comparison is None or window is None or window >= comparison:
            raise ValueError("EMA short window must be smaller than long window")
    elif comparison is not None:
        raise ValueError("comparison_window is only valid for EMA cross rules")
    cooldown = _bounded_int(value.get("cooldown_sessions", 1), "cooldown_sessions", 0, 250)
    severity = value.get("severity", "warning")
    if severity not in RULE_SEVERITIES:
        raise ValueError("severity must be info, warning, or critical")
    enabled = _strict_bool(value.get("enabled", True), "rule enabled")
    created_at = _valid_timestamp(value.get("created_at", _now()), "rule created_at")
    updated_at = _valid_timestamp(value.get("updated_at", created_at), "rule updated_at")
    return {
        "rule_id": rule_id,
        "watchlist_id": watchlist_id,
        "symbol": symbol,
        "rule_type": rule_type,
        "threshold": threshold,
        "window": window,
        "comparison_window": comparison,
        "operator": meta["operator"],
        "cooldown_sessions": cooldown,
        "severity": severity,
        "enabled": enabled,
        "created_at": created_at,
        "updated_at": updated_at,
    }


def _rule_fingerprint(rule: Mapping[str, Any]) -> str:
    return _fingerprint({key: rule.get(key) for key in ("rule_id", "watchlist_id", "symbol", "rule_type", "threshold", "window", "comparison_window", "operator", "cooldown_sessions", "severity", "enabled")})


def _scan_id(profile_id: str, snapshot_id: str, config_fingerprint: str) -> str:
    return f"scan_{_fingerprint({'profile': profile_id, 'snapshot': snapshot_id, 'config': config_fingerprint})[:32]}"


def _alert_id(scan_id: str, rule_id: str) -> str:
    return f"alert_{_fingerprint({'scan': scan_id, 'rule': rule_id})[:32]}"


def _compare(value: float | int, threshold: float, operator: str) -> bool:
    return value >= threshold if operator == "gte" else value <= threshold


def _ema_series(values: list[float], period: int) -> list[float]:
    alpha = 2.0 / (period + 1.0)
    result = [values[0]]
    for value in values[1:]:
        result.append(alpha * value + (1.0 - alpha) * result[-1])
    return result


def _wilder_rsi(values: list[float], period: int) -> float | None:
    if len(values) < period + 1:
        return None
    gains = [max(0.0, values[index] - values[index - 1]) for index in range(1, len(values))]
    losses = [max(0.0, values[index - 1] - values[index]) for index in range(1, len(values))]
    average_gain = statistics.fmean(gains[:period])
    average_loss = statistics.fmean(losses[:period])
    for index in range(period, len(gains)):
        average_gain = (average_gain * (period - 1) + gains[index]) / period
        average_loss = (average_loss * (period - 1) + losses[index]) / period
    if average_loss == 0:
        return 100.0 if average_gain > 0 else 50.0
    return 100.0 - 100.0 / (1.0 + average_gain / average_loss)


def _wilder_atr_percent(history: list[Any], period: int) -> float | None:
    if len(history) < period + 1:
        return None
    true_ranges: list[float] = []
    for index in range(1, len(history)):
        current = history[index]
        previous = history[index - 1]
        high = _positive(getattr(current, "high", None))
        low = _positive(getattr(current, "low", None))
        previous_close = _positive(getattr(previous, "close", None))
        close = _positive(getattr(current, "close", None))
        if None in {high, low, previous_close, close}:
            return None
        true_ranges.append(max(high - low, abs(high - previous_close), abs(low - previous_close)))
    atr = statistics.fmean(true_ranges[:period])
    for value in true_ranges[period:]:
        atr = (atr * (period - 1) + value) / period
    latest_close = _positive(getattr(history[-1], "close", None))
    return atr / latest_close if latest_close else None


def _source_for(market: Any, symbol: str) -> tuple[str, str | None]:
    manifest = getattr(market, "manifest", None)
    files = manifest.get("files", {}) if isinstance(manifest, dict) else {}
    entry = files.get(symbol, {}) if isinstance(files, dict) else {}
    source = str(entry.get("source_provider") or entry.get("source") or "unknown")
    digest = (getattr(market, "file_hashes", {}) or {}).get(symbol)
    return source, digest if isinstance(digest, str) else None


def _sessions_since(previous: str | None, current: str, calendar: Any) -> int:
    if not previous:
        return 10_000
    try:
        values = [item.isoformat() if hasattr(item, "isoformat") else str(item) for item in calendar]
        return max(0, values.index(current) - values.index(previous))
    except (ValueError, TypeError):
        try:
            return max(0, (date.fromisoformat(current) - date.fromisoformat(previous)).days)
        except ValueError:
            return 10_000


def _format_observed(value: float | int | None, unit: str) -> str:
    if value is None:
        return "不可用"
    if unit == "ratio":
        return f"{float(value) * 100:.2f}%"
    if unit == "CNY":
        return f"¥{float(value):.4f}"
    if unit == "sessions":
        return f"{int(value)} 个交易日"
    if unit == "boolean":
        return f"差值 {float(value):+.6f}"
    return f"{float(value):.2f}"


def _normalize_scan_error(value: Any) -> dict[str, str]:
    source = value if isinstance(value, Mapping) else {}
    code = str(source.get("code") or "scan_failed").strip().lower()
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", code):
        code = "scan_failed"
    message = " ".join(str(source.get("message") or "Market snapshot unavailable").split())
    if not message:
        message = "Market snapshot unavailable"
    return {"code": code, "message": message[:1_000]}


def _normalize_owner(owner: str) -> str:
    if not isinstance(owner, str) or not owner.strip() or len(owner.strip()) > 200:
        raise ValueError("Monitoring owner must be a non-empty string of at most 200 characters")
    return owner.strip().casefold()


def _valid_profile_id(value: Any) -> str:
    if not isinstance(value, str) or not PROFILE_ID.fullmatch(value):
        raise ValueError("Monitoring profile id is invalid")
    return value


def _valid_watchlist_id(value: Any) -> str:
    if not isinstance(value, str) or not WATCHLIST_ID.fullmatch(value):
        raise ValueError("watchlist id is invalid")
    return value


def _valid_rule_id(value: Any) -> str:
    if not isinstance(value, str) or not RULE_ID.fullmatch(value):
        raise ValueError("rule id is invalid")
    return value


def _valid_alert_id(value: Any) -> str:
    if not isinstance(value, str) or not ALERT_ID.fullmatch(value):
        raise ValueError("alert id is invalid")
    return value


def _valid_scan_id(value: Any) -> str:
    if not isinstance(value, str) or not SCAN_ID.fullmatch(value):
        raise ValueError("scan id is invalid")
    return value


def _valid_symbol(value: Any) -> str:
    if not isinstance(value, str) or value != value.strip() or not SYMBOL.fullmatch(value):
        raise ValueError("symbol is invalid")
    return value


def _bounded_text(value: Any, field: str, maximum: int) -> str:
    if not isinstance(value, str) or value != value.strip() or not value or len(value) > maximum or "\x00" in value:
        raise ValueError(f"{field} must contain between 1 and {maximum} trimmed characters")
    return value


def _bounded_optional_text(value: Any, field: str, maximum: int) -> str:
    if value in (None, ""):
        return ""
    return _bounded_text(value, field, maximum)


def _strict_bool(value: Any, field: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{field} must be a boolean")
    return value


def _bounded_int(value: Any, field: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{field} must be an integer between {minimum} and {maximum}")
    return value


def _valid_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso_date(value: str, field: str) -> date:
    try:
        parsed = date.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be an ISO calendar date") from exc
    if parsed.isoformat() != value:
        raise ValueError(f"{field} must use YYYY-MM-DD format")
    return parsed


def _positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _nonnegative(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number >= 0 else None


def _check_revision(current: Mapping[str, Any], expected: int | None) -> None:
    if expected is not None and (isinstance(expected, bool) or not isinstance(expected, int) or expected < 0):
        raise ValueError("expected_revision must be a non-negative integer")
    if expected is not None and expected != current["revision"]:
        raise MonitoringConflictError("monitoring configuration changed; reload before writing")


def _fingerprint(value: Mapping[str, Any]) -> str:
    from hashlib import sha256

    encoded = json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")
    return sha256(encoded).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _read_record(path: Path, fields: frozenset[str], maximum: int) -> dict[str, Any]:
    if path.is_symlink():
        raise RuntimeError(f"Monitoring record must not be a symbolic link: {path}")
    try:
        value = load_unique_json(path, max_bytes=maximum)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Invalid monitoring record: {path}: {exc}") from exc
    if not isinstance(value, dict) or set(value) != fields:
        raise RuntimeError(f"Invalid monitoring record schema: {path}")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise RuntimeError(f"Unsupported monitoring record schema: {path}")
    fingerprint = value.get("fingerprint")
    if not isinstance(fingerprint, str) or not FINGERPRINT.fullmatch(fingerprint):
        raise RuntimeError(f"Monitoring record fingerprint is invalid: {path}")
    if _fingerprint({key: item for key, item in value.items() if key != "fingerprint"}) != fingerprint:
        raise RuntimeError(f"Monitoring record fingerprint does not match: {path}")
    return value


def _validate_record_payload(value: Mapping[str, Any], fields: frozenset[str]) -> None:
    """Validate the envelope before an immutable record reaches the filesystem."""
    if set(value) != fields:
        raise ValueError("Monitoring record schema is invalid")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported monitoring record schema")
    fingerprint = value.get("fingerprint")
    if not isinstance(fingerprint, str) or not FINGERPRINT.fullmatch(fingerprint):
        raise ValueError("Monitoring record fingerprint is invalid")
    expected = _fingerprint(
        {key: item for key, item in value.items() if key != "fingerprint"}
    )
    if expected != fingerprint:
        raise ValueError("Monitoring record fingerprint does not match")


def _validate_alert_record(value: Mapping[str, Any], path: Path) -> None:
    try:
        alert_id = _valid_alert_id(value["alert_id"])
        if path.stem != alert_id:
            raise ValueError("filename does not match alert_id")
        _valid_scan_id(value["scan_id"])
        _valid_rule_id(value["rule_id"])
        _valid_watchlist_id(value["watchlist_id"])
        _valid_symbol(value["symbol"])
        _valid_timestamp(value["created_at"], "created_at")
        _valid_timestamp(value["triggered_at"], "triggered_at")
        _parse_iso_date(value["data_date"], "data_date")
        _parse_iso_date(
            value["completed_session_cutoff"], "completed_session_cutoff"
        )
        if value["rule_type"] not in RULE_TYPE_METADATA:
            raise ValueError("rule_type is unsupported")
        if value["severity"] not in RULE_SEVERITIES or value["status"] != "open":
            raise ValueError("severity or initial status is invalid")
        if value["operator"] != RULE_TYPE_METADATA[value["rule_type"]]["operator"]:
            raise ValueError("operator does not match rule type")
        for field in (
            "config_fingerprint",
            "rule_fingerprint",
            "evidence_fingerprint",
            "snapshot_evidence_fingerprint",
        ):
            if not isinstance(value[field], str) or not FINGERPRINT.fullmatch(
                value[field]
            ):
                raise ValueError(f"{field} is invalid")
        for field in ("manifest_sha256", "source_file_sha256"):
            source_hash = value[field]
            if source_hash is not None and (
                not isinstance(source_hash, str)
                or not FINGERPRINT.fullmatch(source_hash)
            ):
                raise ValueError(f"{field} is invalid")
        if (
            isinstance(value["config_revision"], bool)
            or not isinstance(value["config_revision"], int)
            or value["config_revision"] < 1
        ):
            raise ValueError("config_revision is invalid")
        for field in ("threshold", "observed_value"):
            item = value[field]
            if item is not None and (
                isinstance(item, bool)
                or not isinstance(item, (int, float))
                or not math.isfinite(float(item))
            ):
                raise ValueError(f"{field} is invalid")
        _bounded_text(value["rule_label"], "rule_label", 120)
        _bounded_text(value["formula"], "formula", 500)
        _bounded_text(value["operator_label"], "operator_label", 20)
        _bounded_text(value["observed_text"], "observed_text", 120)
        _bounded_text(value["source"], "source", 120)
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid monitoring alert record: {path}: {exc}") from exc


def _validate_action_record(value: Mapping[str, Any], path: Path) -> None:
    try:
        action_id = value["action_id"]
        if not isinstance(action_id, str) or not ACTION_ID.fullmatch(action_id):
            raise ValueError("action_id is invalid")
        if path.stem != action_id:
            raise ValueError("filename does not match action_id")
        _valid_alert_id(value["alert_id"])
        _valid_timestamp(value["created_at"], "created_at")
        _bounded_int(value["sequence"], "sequence", 1, MAX_ACTIONS)
        _bounded_text(value["actor"], "actor", 80)
        if value["action"] not in ALERT_ACTIONS:
            raise ValueError("action is unsupported")
        if value["from_status"] not in ALERT_STATUSES or value["to_status"] not in ALERT_STATUSES:
            raise ValueError("alert status transition is invalid")
        if value["from_status"] not in ALERT_ACTION_FROM[value["action"]]:
            raise ValueError("alert action is not valid from its source state")
        expected_target = "open" if value["action"] in {"reopen", "unsnooze"} else {
            "acknowledge": "acknowledged",
            "dismiss": "dismissed",
            "snooze": "snoozed",
        }.get(value["action"])
        if value["to_status"] != expected_target:
            raise ValueError("alert action target state is invalid")
        if not isinstance(value["alert_fingerprint"], str) or not FINGERPRINT.fullmatch(
            value["alert_fingerprint"]
        ):
            raise ValueError("alert_fingerprint is invalid")
        _bounded_optional_text(value["note"], "note", 1_000)
        if value["action"] == "snooze":
            _parse_iso_date(value["snooze_until"], "snooze_until")
        elif value["snooze_until"] is not None:
            raise ValueError("snooze_until is only valid for snooze")
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid monitoring alert action: {path}: {exc}") from exc


def _validate_scan_record(value: Mapping[str, Any], path: Path) -> None:
    try:
        scan_id = _valid_scan_id(value["scan_id"])
        if path.stem != scan_id:
            raise ValueError("filename does not match scan_id")
        for field in ("created_at", "started_at", "finished_at"):
            _valid_timestamp(value[field], field)
        if value["status"] not in {"succeeded", "partial", "failed"}:
            raise ValueError("status is invalid")
        _bounded_int(value["sequence"], "sequence", 1, MAX_SCANS)
        _bounded_text(value["actor"], "actor", 80)
        if value["authority"] != _AUTHORITY:
            raise ValueError("authority boundary is invalid")
        if (
            isinstance(value["config_revision"], bool)
            or not isinstance(value["config_revision"], int)
            or value["config_revision"] < 1
        ):
            raise ValueError("config_revision is invalid")
        if not isinstance(value["config_fingerprint"], str) or not FINGERPRINT.fullmatch(
            value["config_fingerprint"]
        ):
            raise ValueError("config_fingerprint is invalid")
        for field in ("manifest_sha256", "snapshot_evidence_fingerprint"):
            item = value[field]
            if item is not None and (
                not isinstance(item, str) or not FINGERPRINT.fullmatch(item)
            ):
                raise ValueError(f"{field} is invalid")
        if value["status"] == "failed":
            if value["snapshot_id"] is None:
                for field in (
                    "manifest_sha256",
                    "snapshot_evidence_fingerprint",
                    "data_date",
                    "completed_session_cutoff",
                    "latest_common_session",
                ):
                    if value[field] is not None:
                        raise ValueError(f"{field} must be null without a snapshot")
            else:
                if not isinstance(value["snapshot_id"], str) or not value["snapshot_id"]:
                    raise ValueError("snapshot_id is invalid")
                if not isinstance(value["snapshot_evidence_fingerprint"], str):
                    raise ValueError("snapshot evidence fingerprint is required")
                _parse_iso_date(value["data_date"], "data_date")
                _parse_iso_date(value["completed_session_cutoff"], "completed_session_cutoff")
                _parse_iso_date(value["latest_common_session"], "latest_common_session")
            if value["error"] != _normalize_scan_error(value["error"]):
                raise ValueError("failed scan error schema is invalid")
        else:
            if not isinstance(value["snapshot_id"], str) or not value["snapshot_id"]:
                raise ValueError("snapshot_id is invalid")
            if not isinstance(value["snapshot_evidence_fingerprint"], str):
                raise ValueError("snapshot evidence fingerprint is required")
            _parse_iso_date(value["data_date"], "data_date")
            _parse_iso_date(
                value["completed_session_cutoff"], "completed_session_cutoff"
            )
            _parse_iso_date(value["latest_common_session"], "latest_common_session")
        if not isinstance(value["rule_states"], dict):
            raise ValueError("rule_states must be an object")
        for rule_id, state in value["rule_states"].items():
            _valid_rule_id(rule_id)
            if not isinstance(state, dict) or set(state) != {
                "rule_fingerprint",
                "triggered",
                "observed_value",
                "data_date",
            }:
                raise ValueError("rule state schema is invalid")
            if not isinstance(state["triggered"], bool):
                raise ValueError("rule trigger state is invalid")
            if not isinstance(state["rule_fingerprint"], str) or not FINGERPRINT.fullmatch(
                state["rule_fingerprint"]
            ):
                raise ValueError("rule fingerprint is invalid")
            if value["status"] == "failed" and value["snapshot_id"] is None:
                raise ValueError("snapshot-less failed scan cannot contain rule states")
            _parse_iso_date(state["data_date"], "rule state data_date")
            observed = state["observed_value"]
            if observed is not None and (
                isinstance(observed, bool)
                or not isinstance(observed, (int, float))
                or not math.isfinite(float(observed))
            ):
                raise ValueError("rule state observed_value is invalid")
        if not all(isinstance(item, str) and ALERT_ID.fullmatch(item) for item in value["triggered_alert_ids"]):
            raise ValueError("triggered alert ids are invalid")
        for field in ("suppressed", "exclusions"):
            if not isinstance(value[field], list):
                raise ValueError(f"{field} must be a list")
        if value["status"] == "failed" and value["triggered_alert_ids"]:
            raise ValueError("failed scans cannot publish triggered alerts")
        if value["status"] == "succeeded" and value["exclusions"]:
            raise ValueError("succeeded scans cannot contain exclusions")
        if value["status"] == "partial" and not value["exclusions"]:
            raise ValueError("partial scans must contain exclusions")
        source_summary = value["source_summary"]
        if not isinstance(source_summary, dict) or not isinstance(
            source_summary.get("providers"), list
        ):
            raise ValueError("source_summary schema is invalid")
        if value["status"] != "failed" and value["error"] is not None:
            raise ValueError("successful or partial scan error must be null")
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid monitoring scan record: {path}: {exc}") from exc


def _atomic_create_json(path: Path, value: Mapping[str, Any], fields: frozenset[str], maximum: int) -> None:
    _validate_record_payload(value, fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"Monitoring record target must not be a symbolic link: {path}")
    staging = path.parent.parent / ".staging"
    if staging.is_symlink():
        raise RuntimeError("Monitoring staging directory must not be a symbolic link")
    staging.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=staging
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if temporary.stat().st_size > maximum:
            raise ValueError(f"Monitoring record exceeds {maximum} bytes")
        try:
            if os.name == "nt":
                _windows_move_file(temporary, path, replace=False)
            else:
                os.link(temporary, path)
        except OSError as exc:
            if not (isinstance(exc, FileExistsError) or getattr(exc, "winerror", None) == 183):
                raise
            raise FileExistsError(f"Immutable monitoring record already exists: {path.stem}") from exc
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_replace_json(
    path: Path, value: Mapping[str, Any], fields: frozenset[str], maximum: int
) -> None:
    """Atomically replace a small recovery marker from an owner-local staging dir."""
    _validate_record_payload(value, fields)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise RuntimeError(f"Monitoring record target must not be a symbolic link: {path}")
    staging = path.parent / ".staging"
    if staging.is_symlink():
        raise RuntimeError("Monitoring staging directory must not be a symbolic link")
    staging.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=staging
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if temporary.stat().st_size > maximum:
            raise ValueError(f"Monitoring record exceeds {maximum} bytes")
        if os.name == "nt":
            _windows_move_file(temporary, path, replace=True)
        else:
            os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    """Persist a directory entry where the host supports directory fsync."""
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _windows_move_file(source: Path, target: Path, *, replace: bool) -> None:
    """Publish a same-volume file with write-through semantics on Windows."""
    if os.name != "nt":
        raise RuntimeError("Windows move helper called on a non-Windows host")
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file_ex = kernel32.MoveFileExW
    move_file_ex.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move_file_ex.restype = wintypes.BOOL
    flags = 0x00000008  # MOVEFILE_WRITE_THROUGH
    if replace:
        flags |= 0x00000001  # MOVEFILE_REPLACE_EXISTING
    if not move_file_ex(str(source), str(target), flags):
        error = ctypes.get_last_error()
        if not replace and error in {80, 183}:
            raise FileExistsError(error, ctypes.FormatError(error), str(target))
        raise OSError(error, ctypes.FormatError(error), str(target))


def _unlink_file_durable(path: Path) -> None:
    """Remove one regular file and flush its parent directory where possible."""
    if path.is_symlink():
        raise RuntimeError(f"Monitoring file must not be a symbolic link: {path}")
    if not path.exists():
        return
    if not path.is_file():
        raise RuntimeError(f"Monitoring path must be a regular file: {path}")
    path.unlink()
    _fsync_directory(path.parent)


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _validate_profile_id(value: str) -> str:
    return _valid_profile_id(value)


__all__ = [
    "ALERT_ACTIONS",
    "ALERT_STATUSES",
    "DEFAULT_ALERT_LIMIT",
    "MonitoringConflictError",
    "MonitoringEngine",
    "MonitoringStore",
    "RULE_SEVERITIES",
    "RULE_TYPE_METADATA",
    "SCHEMA_VERSION",
]
