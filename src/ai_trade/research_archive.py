from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import csv
from hashlib import sha256
import json
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from .json_utils import loads_unique_json
from .research_journal import ResearchJournalStore


SCHEMA_VERSION = 1
ARCHIVE_KINDS = frozenset({"all", "daily", "weekly", "monthly"})
ARCHIVE_DEFAULT_LIMIT = 12
ARCHIVE_MAX_LIMIT = 52
MAX_EQUITY_LEDGER_BYTES = 16 * 1024 * 1024
MAX_EQUITY_ROWS = 10_000
MAX_PAPER_REPORT_BYTES = 2 * 1024 * 1024
MAX_PAPER_REPORTS = 10_000
MAX_ARCHIVE_ERRORS = 100
MAX_ARCHIVE_JOURNAL_REFS = 2_000

_REPORT_NAME = re.compile(r"paper_(\d{8})\.json\Z")
_HEX_24 = re.compile(r"[0-9a-f]{24}\Z")
_HEX_64 = re.compile(r"[0-9a-f]{64}\Z")
_SYMBOL = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:=+-]{0,63}\Z")
_EQUITY_FIELDS = (
    "account_id",
    "session_id",
    "date",
    "equity",
    "cash",
    "drawdown",
    "daily_return",
    "positions",
    "pending_targets",
    "config_fingerprint",
    "market_snapshot_id",
)
_REPORT_FIELDS = frozenset(
    {
        "account_id",
        "date",
        "equity",
        "cash",
        "positions",
        "pending_targets",
        "cooldown_remaining",
        "sessions_since_rebalance",
        "drawdown",
        "daily_return",
        "market_snapshot_id",
        "trades",
        "order_rejections",
        "reason",
    }
)
_AUTHORITY = {
    "research_only": True,
    "execution_authorized": False,
    "strategy_changed": False,
    "paper_account_changed": False,
    "broker_permissions_changed": False,
}


class _JournalExcluded(Exception):
    """Internal control flow for an explicitly unavailable journal projection."""


@dataclass(frozen=True)
class ResearchArchiveQuery:
    kind: str = "all"
    on_date: date | None = None
    week_start: date | None = None
    month_start: date | None = None
    limit: int = ARCHIVE_DEFAULT_LIMIT


@dataclass(frozen=True)
class _EquityRow:
    account_id: str
    session_id: str
    on_date: date
    equity: float
    cash: float
    drawdown: float
    daily_return: float
    positions: dict[str, int]
    pending_targets: dict[str, float] | None
    config_fingerprint: str
    market_snapshot_id: str
    row_fingerprint: str


@dataclass(frozen=True)
class _PaperReport:
    on_date: date
    account_id: str
    payload: dict[str, Any]
    file_sha256: str


class ResearchArchiveProjection:
    """Build a read-only daily and weekly view from authoritative local evidence."""

    def __init__(
        self,
        reports_dir: str | Path,
        equity_file: str | Path,
        journal: ResearchJournalStore,
    ) -> None:
        self.reports_dir = Path(reports_dir)
        self.equity_file = Path(equity_file)
        self.journal = journal

    def build(
        self,
        owner: str,
        *,
        account_id: str | None,
        config_fingerprint: str | None,
        query: ResearchArchiveQuery | None = None,
        market_calendar: Sequence[date] | None = None,
        include_journal: bool = True,
    ) -> dict[str, Any]:
        query = query or ResearchArchiveQuery()
        _validate_query(query)
        errors: list[dict[str, str]] = []
        if not account_id:
            return unavailable_research_archive(
                "The paper account is not initialized or could not be validated.",
                code="paper_account_unavailable",
                recovery_action="paper-init",
                query=query,
            )
        account_id = _bounded_text(account_id, "account_id", 256)
        if not config_fingerprint:
            return unavailable_research_archive(
                "The active paper configuration could not be validated.",
                code="paper_configuration_unavailable",
                recovery_action="paper-audit",
                query=query,
            )
        config_fingerprint = _pattern_text(
            config_fingerprint,
            "config_fingerprint",
            _HEX_64,
        )

        equity_rows: list[_EquityRow] = []
        try:
            equity_rows = _load_equity_rows(
                self.equity_file,
                account_id,
                config_fingerprint,
            )
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            errors.append(
                _error(
                    "paper_equity_invalid",
                    str(exc),
                    "paper-audit",
                )
            )

        reports, report_errors = self._load_reports(account_id)
        errors.extend(report_errors)
        evidence_dates = {row.on_date for row in equity_rows} | set(reports)
        selected_dates = _selected_dates(evidence_dates, query)
        journal_records: list[dict[str, Any]] = []
        journal_error: dict[str, str] | None = None
        try:
            if not include_journal:
                journal_error = _error(
                    "research_journal_epoch_binding_unavailable",
                    "Research journal entries are not bound to a paper account epoch and are excluded from archived epoch projections.",
                    "review-active-journal-separately",
                )
                errors.append(journal_error)
                raise _JournalExcluded
            if query.on_date is not None:
                lower = upper = query.on_date
            elif query.week_start is not None:
                lower = query.week_start
                upper = query.week_start + timedelta(days=6)
            elif query.month_start is not None:
                lower = query.month_start
                upper = _month_end(query.month_start)
            else:
                # The journal is independent evidence. Read its bounded,
                # owner-scoped store across the full calendar so a note added
                # after the latest paper run is not silently omitted.
                lower = date.min
                upper = date.max
            journal_records = self.journal.entries_between(owner, lower, upper)
            evidence_dates.update(
                date.fromisoformat(str(item["research_date"]))
                for item in journal_records
            )
            selected_dates = _selected_dates(evidence_dates, query)
        except _JournalExcluded:
            pass
        except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
            journal_error = _error(
                "research_journal_unavailable",
                str(exc),
                "retry",
            )
            errors.append(journal_error)

        equity_by_date = {row.on_date: row for row in equity_rows}
        journal_by_date: dict[date, list[dict[str, Any]]] = defaultdict(list)
        for item in journal_records:
            journal_by_date[date.fromisoformat(str(item["research_date"]))].append(
                item
            )

        daily = [
            _daily_record(
                on_date,
                equity_by_date.get(on_date),
                reports.get(on_date),
                journal_by_date.get(on_date, []),
            )
            for on_date in sorted(selected_dates, reverse=True)
        ]
        weekly = _weekly_records(daily, market_calendar)
        monthly = _monthly_records(daily, market_calendar)
        if query.week_start is not None:
            weekly = [
                item for item in weekly if item["week_start"] == query.week_start.isoformat()
            ]
        if query.month_start is not None:
            monthly = [
                item
                for item in monthly
                if item["month_start"] == query.month_start.isoformat()
            ]

        if query.kind == "daily":
            weekly = []
            monthly = []
        elif query.kind == "weekly":
            daily = []
            monthly = []
        elif query.kind == "monthly":
            daily = []
            weekly = []

        daily = daily[: query.limit]
        weekly = weekly[: query.limit]
        monthly = monthly[: query.limit]
        snapshots = [
            {
                "as_of_date": item["as_of_date"],
                "status": item["status"],
                "equity": item["equity"],
                "cash": item["cash"],
                "positions": item["positions"],
                "pending_targets": item["pending_targets"],
                "market_snapshot_id": item["market_snapshot_id"],
                "valuation_status": "ledger_only",
                "price_derived_values_available": False,
                "source_fingerprint": item["source"]["evidence_fingerprint"],
            }
            for item in daily
            if item["equity"] is not None
            and item["source"]["equity_session_id"] is not None
        ]
        status = _projection_status(daily, weekly, monthly, errors)
        return {
            "schema_version": SCHEMA_VERSION,
            "available": status != "unavailable",
            "status": status,
            "generated_at": _now(),
            "filters": _query_payload(query),
            "account_fingerprint": sha256(account_id.encode("utf-8")).hexdigest(),
            "summary": {
                "daily_count": len(daily),
                "weekly_count": len(weekly),
                "monthly_count": len(monthly),
                "snapshot_count": len(snapshots),
                "source_daily_reports": len(reports),
                "source_equity_sessions": len(equity_rows),
                "journal_available": journal_error is None,
            },
            "daily": daily,
            "weekly": weekly,
            "monthly": monthly,
            "snapshots": snapshots,
            "authority": dict(_AUTHORITY),
            "errors": errors,
        }

    def _load_reports(
        self, account_id: str
    ) -> tuple[dict[date, _PaperReport], list[dict[str, str]]]:
        reports: dict[date, _PaperReport] = {}
        errors: list[dict[str, str]] = []
        if self.reports_dir.is_symlink():
            raise RuntimeError("Paper reports directory must not be a symbolic link")
        if not self.reports_dir.exists():
            return reports, errors
        if not self.reports_dir.is_dir():
            raise RuntimeError("Paper reports path must be a directory")
        paths: list[Path] = []
        for path in self.reports_dir.glob("paper_????????.json"):
            if _REPORT_NAME.fullmatch(path.name) is None:
                continue
            if path.is_symlink():
                continue
            if path.is_file():
                if len(paths) >= MAX_PAPER_REPORTS:
                    return {}, [
                        _error(
                            "paper_reports_too_many",
                            "Paper reports exceed the supported file-count limit.",
                            "paper-audit",
                        )
                    ]
                paths.append(path)
        invalid_count = 0
        for path in sorted(paths):
            match = _REPORT_NAME.fullmatch(path.name)
            try:
                if match is None:
                    continue
                on_date = datetime.strptime(match.group(1), "%Y%m%d").date()
                report = _load_paper_report(path, on_date, account_id)
            except (OSError, UnicodeError, ValueError, RuntimeError) as exc:
                invalid_count += 1
                if len(errors) < MAX_ARCHIVE_ERRORS - 1:
                    errors.append(
                        _error(
                            "paper_report_invalid",
                            f"{path.name}: {exc}",
                            "paper-run",
                        )
                    )
                continue
            if on_date in reports:
                errors.append(
                    _error(
                        "paper_report_conflict",
                        f"More than one report resolved to {on_date.isoformat()}.",
                        "paper-audit",
                    )
                )
                continue
            reports[on_date] = report
        if invalid_count >= MAX_ARCHIVE_ERRORS:
            errors.append(
                _error(
                    "paper_report_errors_truncated",
                    f"{invalid_count} paper reports failed validation; only the first "
                    f"{MAX_ARCHIVE_ERRORS - 1} errors are listed.",
                    "paper-audit",
                )
            )
        return reports, errors


def unavailable_research_archive(
    message: str,
    *,
    code: str = "research_archive_unavailable",
    recovery_action: str = "retry",
    query: ResearchArchiveQuery | None = None,
) -> dict[str, Any]:
    query = query or ResearchArchiveQuery()
    return {
        "schema_version": SCHEMA_VERSION,
        "available": False,
        "status": "unavailable",
        "generated_at": _now(),
        "filters": _query_payload(query),
        "account_fingerprint": None,
        "summary": {
            "daily_count": 0,
            "weekly_count": 0,
            "monthly_count": 0,
            "snapshot_count": 0,
            "source_daily_reports": 0,
            "source_equity_sessions": 0,
            "journal_available": False,
        },
        "daily": [],
        "weekly": [],
        "monthly": [],
        "snapshots": [],
        "authority": dict(_AUTHORITY),
        "errors": [_error(code, message, recovery_action)],
    }


def _load_equity_rows(
    path: Path,
    account_id: str,
    config_fingerprint: str,
) -> list[_EquityRow]:
    if path.is_symlink():
        raise RuntimeError("Paper equity ledger must not be a symbolic link")
    if not path.exists():
        return []
    if not path.is_file():
        raise RuntimeError("Paper equity ledger path must be a file")
    if path.stat().st_size > MAX_EQUITY_LEDGER_BYTES:
        raise RuntimeError("Paper equity ledger exceeds the supported size")
    rows: list[_EquityRow] = []
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if tuple(reader.fieldnames or ()) != _EQUITY_FIELDS:
                raise RuntimeError("Paper equity ledger schema is invalid")
            for index, raw in enumerate(reader, start=2):
                if index > MAX_EQUITY_ROWS + 1:
                    raise RuntimeError("Paper equity ledger contains too many rows")
                row = _parse_equity_row(raw, index)
                if row.account_id != account_id:
                    raise RuntimeError(
                        "Paper equity ledger contains rows from another account epoch"
                    )
                if row.config_fingerprint != config_fingerprint:
                    raise RuntimeError(
                        "Paper equity ledger does not match the active configuration"
                    )
                rows.append(row)
    except csv.Error as exc:
        raise RuntimeError("Paper equity ledger CSV is malformed") from exc
    if len({row.session_id for row in rows}) != len(rows):
        raise RuntimeError("Paper equity ledger contains duplicate session IDs")
    if any(current.on_date >= following.on_date for current, following in zip(rows, rows[1:])):
        raise RuntimeError("Paper equity ledger dates are not strictly increasing")
    if len({row.config_fingerprint for row in rows}) > 1:
        raise RuntimeError("Paper equity ledger mixes configuration fingerprints")
    return rows


def _parse_equity_row(raw: Mapping[str, str], index: int) -> _EquityRow:
    if set(raw) != set(_EQUITY_FIELDS):
        raise RuntimeError(f"Invalid paper equity row {index}: column count is invalid")
    try:
        account_id = _bounded_text(raw.get("account_id"), "account_id", 256)
        session_id = _pattern_text(raw.get("session_id"), "session_id", _HEX_24)
        on_date = _iso_date(raw.get("date"), "date")
        equity = _finite_number(raw.get("equity"), "equity", minimum=0, strict=True)
        cash = _finite_number(raw.get("cash"), "cash", minimum=0)
        drawdown = _finite_number(
            raw.get("drawdown"), "drawdown", minimum=-1, maximum=0
        )
        daily_return = _finite_number(raw.get("daily_return"), "daily_return")
        if daily_return <= -1:
            raise ValueError("daily_return must be greater than -1")
        positions = _positions(loads_unique_json(str(raw.get("positions", ""))))
        pending_targets = _targets(
            loads_unique_json(str(raw.get("pending_targets", "")))
        )
        config_fingerprint = _pattern_text(
            raw.get("config_fingerprint"), "config_fingerprint", _HEX_64
        )
        market_snapshot_id = _bounded_text(
            raw.get("market_snapshot_id"), "market_snapshot_id", 128
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid paper equity row {index}: {exc}") from exc
    canonical = {
        "account_id": account_id,
        "session_id": session_id,
        "date": on_date.isoformat(),
        "equity": equity,
        "cash": cash,
        "drawdown": drawdown,
        "daily_return": daily_return,
        "positions": positions,
        "pending_targets": pending_targets,
        "config_fingerprint": config_fingerprint,
        "market_snapshot_id": market_snapshot_id,
    }
    return _EquityRow(
        account_id=account_id,
        session_id=session_id,
        on_date=on_date,
        equity=equity,
        cash=cash,
        drawdown=drawdown,
        daily_return=daily_return,
        positions=positions,
        pending_targets=pending_targets,
        config_fingerprint=config_fingerprint,
        market_snapshot_id=market_snapshot_id,
        row_fingerprint=_fingerprint(canonical),
    )


def _load_paper_report(path: Path, on_date: date, account_id: str) -> _PaperReport:
    with path.open("rb") as handle:
        content = handle.read(MAX_PAPER_REPORT_BYTES + 1)
    if len(content) > MAX_PAPER_REPORT_BYTES:
        raise RuntimeError("Paper report exceeds the supported size")
    payload = loads_unique_json(content.decode("utf-8"))
    if not isinstance(payload, dict) or set(payload) != _REPORT_FIELDS:
        raise RuntimeError("Paper report schema is invalid")
    try:
        report_account = _bounded_text(payload.get("account_id"), "account_id", 256)
        report_date = _iso_date(payload.get("date"), "date")
        if report_date != on_date:
            raise ValueError("report date does not match its filename")
        if report_account != account_id:
            raise ValueError("report belongs to another paper account epoch")
        normalized = dict(payload)
        normalized["equity"] = _finite_number(
            payload.get("equity"), "equity", minimum=0, strict=True
        )
        normalized["cash"] = _finite_number(payload.get("cash"), "cash", minimum=0)
        normalized["drawdown"] = _finite_number(
            payload.get("drawdown"), "drawdown", minimum=-1, maximum=0
        )
        normalized["daily_return"] = _finite_number(
            payload.get("daily_return"), "daily_return"
        )
        if normalized["daily_return"] <= -1:
            raise ValueError("daily_return must be greater than -1")
        normalized["positions"] = _positions(payload.get("positions"))
        normalized["pending_targets"] = _targets(payload.get("pending_targets"))
        normalized["market_snapshot_id"] = _bounded_text(
            payload.get("market_snapshot_id"), "market_snapshot_id", 128
        )
        normalized["reason"] = _bounded_text(payload.get("reason"), "reason", 2_000)
        normalized["cooldown_remaining"] = _nonnegative_int(
            payload.get("cooldown_remaining"), "cooldown_remaining"
        )
        normalized["sessions_since_rebalance"] = _nonnegative_int(
            payload.get("sessions_since_rebalance"), "sessions_since_rebalance"
        )
        normalized["trades"] = _bounded_list(payload.get("trades"), "trades", 5_000)
        normalized["order_rejections"] = _bounded_list(
            payload.get("order_rejections"), "order_rejections", 5_000
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"Invalid paper report: {exc}") from exc
    return _PaperReport(
        on_date=on_date,
        account_id=report_account,
        payload=normalized,
        file_sha256=sha256(content).hexdigest(),
    )


def _daily_record(
    on_date: date,
    equity: _EquityRow | None,
    report: _PaperReport | None,
    journal_entries: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    issue = None
    if equity is not None and report is not None:
        issue = _report_equity_issue(report.payload, equity)
    if equity is None and report is None:
        status = "journal_only"
    elif report is None:
        status = "missing_report"
    elif equity is None:
        status = "unbound_report"
    elif issue is not None:
        status = "evidence_mismatch"
    else:
        status = "current"
    source = report.payload if report is not None else None
    positions = (
        equity.positions
        if equity is not None
        else dict(source.get("positions", {})) if source is not None else {}
    )
    targets = (
        equity.pending_targets
        if equity is not None
        else source.get("pending_targets") if source is not None else None
    )
    evidence = {
        "equity_row_fingerprint": equity.row_fingerprint if equity else None,
        "report_file_sha256": report.file_sha256 if report else None,
        "journal_entry_fingerprints": [
            str(item["entry_fingerprint"]) for item in journal_entries
        ],
    }
    evidence_fingerprint = _fingerprint(evidence)
    return {
        "as_of_date": on_date.isoformat(),
        "status": status,
        "status_detail": issue,
        "equity": equity.equity if equity else source.get("equity") if source else None,
        "cash": equity.cash if equity else source.get("cash") if source else None,
        "drawdown": (
            equity.drawdown if equity else source.get("drawdown") if source else None
        ),
        "daily_return": (
            equity.daily_return
            if equity
            else source.get("daily_return") if source else None
        ),
        "positions": [
            {"symbol": symbol, "quantity": quantity}
            for symbol, quantity in sorted(positions.items())
        ],
        "pending_targets": [
            {"symbol": symbol, "weight": weight}
            for symbol, weight in sorted((targets or {}).items())
        ],
        "market_snapshot_id": (
            equity.market_snapshot_id
            if equity
            else source.get("market_snapshot_id") if source else None
        ),
        "trades_count": len(source.get("trades", [])) if source else 0,
        "rejections_count": len(source.get("order_rejections", [])) if source else 0,
        "reason": source.get("reason") if source else None,
        "journal": _journal_summary(journal_entries),
        "source": {
            "equity_session_id": equity.session_id if equity else None,
            "equity_row_fingerprint": equity.row_fingerprint if equity else None,
            "config_fingerprint": equity.config_fingerprint if equity else None,
            "report_file_sha256": report.file_sha256 if report else None,
            "evidence_fingerprint": evidence_fingerprint,
        },
        "authority": dict(_AUTHORITY),
    }


def _report_equity_issue(payload: Mapping[str, Any], row: _EquityRow) -> str | None:
    if not math.isclose(float(payload["equity"]), row.equity, rel_tol=0, abs_tol=1e-5):
        return "Daily report equity does not match the authoritative equity ledger."
    if not math.isclose(float(payload["cash"]), row.cash, rel_tol=0, abs_tol=1e-5):
        return "Daily report cash does not match the authoritative equity ledger."
    if payload["positions"] != row.positions:
        return "Daily report positions do not match the authoritative equity ledger."
    if payload["pending_targets"] != row.pending_targets:
        return "Daily report targets do not match the authoritative equity ledger."
    if not math.isclose(
        float(payload["drawdown"]), row.drawdown, rel_tol=0, abs_tol=1e-8
    ):
        return "Daily report drawdown does not match the authoritative equity ledger."
    if not math.isclose(
        float(payload["daily_return"]), row.daily_return, rel_tol=0, abs_tol=1e-8
    ):
        return "Daily report return does not match the authoritative equity ledger."
    if payload["market_snapshot_id"] != row.market_snapshot_id:
        return "Daily report market snapshot does not match the equity ledger."
    return None


def _weekly_records(
    daily: Sequence[Mapping[str, Any]],
    market_calendar: Sequence[date] | None,
) -> list[dict[str, Any]]:
    groups: dict[date, list[Mapping[str, Any]]] = defaultdict(list)
    for item in daily:
        on_date = date.fromisoformat(str(item["as_of_date"]))
        week_start = on_date - timedelta(days=on_date.weekday())
        groups[week_start].append(item)
    calendar = set(market_calendar or ())
    result: list[dict[str, Any]] = []
    for week_start, items in groups.items():
        items = sorted(items, key=lambda value: str(value["as_of_date"]))
        paper_items = [
            item
            for item in items
            if item["source"]["equity_session_id"] is not None
        ]
        current = [item for item in paper_items if item["status"] == "current"]
        week_end = week_start + timedelta(days=6)
        expected_dates = sorted(
            value for value in calendar if week_start <= value <= week_end
        )
        included_dates = {
            date.fromisoformat(str(item["as_of_date"])) for item in paper_items
        }
        missing_sessions = [
            value.isoformat() for value in expected_dates if value not in included_dates
        ]
        expected_date_set = set(expected_dates)
        unexpected_sessions = (
            sorted(
                value.isoformat()
                for value in included_dates
                if value not in expected_date_set
            )
            if calendar
            else []
        )
        non_journal_items = [item for item in items if item["status"] != "journal_only"]
        if not paper_items:
            status = "journal_only" if not non_journal_items else "partial"
        elif (
            len(current) != len(paper_items)
            or any(
                item["status"] not in {"current", "journal_only"}
                for item in items
            )
            or missing_sessions
            or unexpected_sessions
        ):
            status = "partial"
        else:
            status = "current"
        first_equity = paper_items[0]["equity"] if paper_items else None
        last_equity = paper_items[-1]["equity"] if paper_items else None
        period_return = (
            math.prod(1.0 + float(item["daily_return"]) for item in paper_items) - 1.0
            if paper_items
            else None
        )
        evidence_fingerprints = [
            str(item["source"]["evidence_fingerprint"]) for item in items
        ]
        result.append(
            {
                "week_start": week_start.isoformat(),
                "week_end": week_end.isoformat(),
                "status": status,
                "expected_sessions": len(expected_dates) if calendar else None,
                "included_sessions": len(paper_items),
                "missing_sessions": missing_sessions,
                "unexpected_sessions": unexpected_sessions,
                "start_equity": first_equity,
                "end_equity": last_equity,
                "period_return": period_return,
                "max_drawdown": (
                    min(float(item["drawdown"]) for item in paper_items)
                    if paper_items
                    else None
                ),
                "trades_count": sum(
                    int(item["trades_count"]) for item in paper_items
                ),
                "rejections_count": sum(
                    int(item["rejections_count"]) for item in paper_items
                ),
                "journal_count": sum(
                    int(item["journal"]["entry_count"]) for item in items
                ),
                "latest_positions": (
                    paper_items[-1]["positions"] if paper_items else []
                ),
                "daily_statuses": [
                    {
                        "date": item["as_of_date"],
                        "status": item["status"],
                    }
                    for item in items
                ],
                "source": {
                    "evidence_fingerprints": evidence_fingerprints,
                    "weekly_fingerprint": _fingerprint(evidence_fingerprints),
                    "calendar_available": bool(calendar),
                },
                "authority": dict(_AUTHORITY),
            }
        )
    return sorted(result, key=lambda value: str(value["week_start"]), reverse=True)


def _monthly_records(
    daily: Sequence[Mapping[str, Any]],
    market_calendar: Sequence[date] | None,
) -> list[dict[str, Any]]:
    groups: dict[date, list[Mapping[str, Any]]] = defaultdict(list)
    for item in daily:
        on_date = date.fromisoformat(str(item["as_of_date"]))
        groups[on_date.replace(day=1)].append(item)
    calendar = set(market_calendar or ())
    result: list[dict[str, Any]] = []
    for month_start, items in groups.items():
        items = sorted(items, key=lambda value: str(value["as_of_date"]))
        paper_items = [
            item for item in items if item["source"]["equity_session_id"] is not None
        ]
        current = [item for item in paper_items if item["status"] == "current"]
        month_end = _month_end(month_start)
        expected_dates = sorted(
            value for value in calendar if month_start <= value <= month_end
        )
        included_dates = {
            date.fromisoformat(str(item["as_of_date"])) for item in paper_items
        }
        missing_sessions = [
            value.isoformat() for value in expected_dates if value not in included_dates
        ]
        expected_date_set = set(expected_dates)
        unexpected_sessions = (
            sorted(
                value.isoformat()
                for value in included_dates
                if value not in expected_date_set
            )
            if calendar
            else []
        )
        non_journal_items = [item for item in items if item["status"] != "journal_only"]
        if not paper_items:
            status = "journal_only" if not non_journal_items else "partial"
        elif (
            len(current) != len(paper_items)
            or any(
                item["status"] not in {"current", "journal_only"}
                for item in items
            )
            or missing_sessions
            or unexpected_sessions
        ):
            status = "partial"
        else:
            status = "current"
        evidence_fingerprints = [
            str(item["source"]["evidence_fingerprint"]) for item in items
        ]
        result.append(
            {
                "month_start": month_start.isoformat(),
                "month_end": month_end.isoformat(),
                "status": status,
                "expected_sessions": len(expected_dates) if calendar else None,
                "included_sessions": len(paper_items),
                "missing_sessions": missing_sessions,
                "unexpected_sessions": unexpected_sessions,
                "start_equity": paper_items[0]["equity"] if paper_items else None,
                "end_equity": paper_items[-1]["equity"] if paper_items else None,
                "period_return": (
                    math.prod(1.0 + float(item["daily_return"]) for item in paper_items)
                    - 1.0
                    if paper_items
                    else None
                ),
                "max_drawdown": (
                    min(float(item["drawdown"]) for item in paper_items)
                    if paper_items
                    else None
                ),
                "trades_count": sum(
                    int(item["trades_count"]) for item in paper_items
                ),
                "rejections_count": sum(
                    int(item["rejections_count"]) for item in paper_items
                ),
                "journal_count": sum(
                    int(item["journal"]["entry_count"]) for item in items
                ),
                "latest_positions": paper_items[-1]["positions"] if paper_items else [],
                "daily_statuses": [
                    {"date": item["as_of_date"], "status": item["status"]}
                    for item in items
                ],
                "source": {
                    "evidence_fingerprints": evidence_fingerprints,
                    "monthly_fingerprint": _fingerprint(evidence_fingerprints),
                    "calendar_available": bool(calendar),
                },
                "authority": dict(_AUTHORITY),
            }
        )
    return sorted(result, key=lambda value: str(value["month_start"]), reverse=True)


def _month_end(month_start: date) -> date:
    if month_start.month == 12:
        following = date(month_start.year + 1, 1, 1)
    else:
        following = date(month_start.year, month_start.month + 1, 1)
    return following - timedelta(days=1)


def _journal_summary(entries: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    if len(entries) > MAX_ARCHIVE_JOURNAL_REFS:
        raise RuntimeError("Research archive journal reference limit exceeded")
    categories = Counter(str(item.get("category")) for item in entries)
    decisions = Counter(str(item.get("decision")) for item in entries)
    symbols = Counter(str(item.get("symbol")) for item in entries if item.get("symbol"))
    return {
        "entry_count": len(entries),
        "by_category": dict(sorted(categories.items())),
        "by_decision": dict(sorted(decisions.items())),
        "by_symbol": dict(sorted(symbols.items())),
        "entries": [
            {
                "entry_id": item["entry_id"],
                "entry_fingerprint": item["entry_fingerprint"],
                "category": item["category"],
                "symbol": item["symbol"],
                "title": item["title"],
                "decision": item["decision"],
            }
            for item in entries
        ],
    }


def _selected_dates(
    evidence_dates: Iterable[date], query: ResearchArchiveQuery
) -> set[date]:
    selected = set(evidence_dates)
    if query.on_date is not None:
        return {query.on_date} if query.on_date in selected else set()
    if query.week_start is not None:
        upper = query.week_start + timedelta(days=6)
        return {
            value for value in selected if query.week_start <= value <= upper
        }
    if query.month_start is not None:
        upper = _month_end(query.month_start)
        return {
            value for value in selected if query.month_start <= value <= upper
        }
    maximum_dates = (
        query.limit
        if query.kind == "daily"
        else query.limit * 7
        if query.kind == "weekly"
        else query.limit * 31
    )
    return set(sorted(selected, reverse=True)[:maximum_dates])


def _projection_status(
    daily: Sequence[Mapping[str, Any]],
    weekly: Sequence[Mapping[str, Any]],
    monthly: Sequence[Mapping[str, Any]],
    errors: Sequence[Mapping[str, str]],
) -> str:
    if errors:
        return "partial" if daily or weekly or monthly else "unavailable"
    if not daily and not weekly and not monthly:
        return "empty"
    bad_daily = any(item["status"] != "current" for item in daily)
    bad_weekly = any(item["status"] not in {"current", "journal_only"} for item in weekly)
    bad_monthly = any(
        item["status"] not in {"current", "journal_only"} for item in monthly
    )
    return "partial" if bad_daily or bad_weekly or bad_monthly else "current"


def _validate_query(query: ResearchArchiveQuery) -> None:
    if not isinstance(query, ResearchArchiveQuery):
        raise ValueError("Research archive query is invalid")
    if query.kind not in ARCHIVE_KINDS:
        raise ValueError("Research archive kind must be all, daily, weekly, or monthly")
    if query.on_date is not None and (
        not isinstance(query.on_date, date)
        or isinstance(query.on_date, datetime)
    ):
        raise ValueError("Research archive date is invalid")
    if query.week_start is not None:
        if (
            not isinstance(query.week_start, date)
            or isinstance(query.week_start, datetime)
            or query.week_start.weekday() != 0
        ):
            raise ValueError("Research archive week must be an ISO Monday")
    if query.month_start is not None:
        if (
            not isinstance(query.month_start, date)
            or isinstance(query.month_start, datetime)
            or query.month_start.day != 1
        ):
            raise ValueError("Research archive month must be the first calendar day")
    if sum(
        value is not None
        for value in (query.on_date, query.week_start, query.month_start)
    ) > 1:
        raise ValueError("Research archive date, week, and month cannot be combined")
    if (
        isinstance(query.limit, bool)
        or not isinstance(query.limit, int)
        or not 1 <= query.limit <= ARCHIVE_MAX_LIMIT
    ):
        raise ValueError(
            f"Research archive limit must be between 1 and {ARCHIVE_MAX_LIMIT}"
        )


def _query_payload(query: ResearchArchiveQuery) -> dict[str, Any]:
    return {
        "kind": query.kind,
        "date": query.on_date.isoformat() if query.on_date else None,
        "week_start": query.week_start.isoformat() if query.week_start else None,
        "month_start": query.month_start.isoformat() if query.month_start else None,
        "limit": query.limit,
    }


def _positions(value: object) -> dict[str, int]:
    if not isinstance(value, dict) or len(value) > 5_000:
        raise ValueError("positions must be a bounded object")
    positions: dict[str, int] = {}
    for raw_symbol, quantity in value.items():
        symbol = _pattern_text(raw_symbol, "position symbol", _SYMBOL)
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity <= 0:
            raise ValueError("position quantities must be positive integers")
        positions[symbol] = quantity
    return positions


def _targets(value: object) -> dict[str, float] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or len(value) > 5_000:
        raise ValueError("pending_targets must be a bounded object or null")
    targets: dict[str, float] = {}
    for raw_symbol, raw_weight in value.items():
        symbol = _pattern_text(raw_symbol, "target symbol", _SYMBOL)
        weight = _finite_number(raw_weight, "target weight", minimum=0, maximum=1)
        targets[symbol] = weight
    if math.fsum(targets.values()) > 1 + 1e-8:
        raise ValueError("pending target weights exceed total exposure one")
    return targets


def _bounded_list(value: object, field: str, maximum: int) -> list[Any]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(f"{field} must be a bounded list")
    return value


def _nonnegative_int(value: object, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _finite_number(
    value: object,
    field: str,
    *,
    minimum: float | None = None,
    maximum: float | None = None,
    strict: bool = False,
) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{field} must be finite")
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field} must be finite") from exc
    if not math.isfinite(number):
        raise ValueError(f"{field} must be finite")
    if minimum is not None and (number <= minimum if strict else number < minimum):
        operator = "greater than" if strict else "at least"
        raise ValueError(f"{field} must be {operator} {minimum}")
    if maximum is not None and number > maximum:
        raise ValueError(f"{field} must be at most {maximum}")
    return number


def _bounded_text(value: object, field: str, maximum: int) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value) > maximum
        or any(ord(character) < 32 for character in value)
    ):
        raise ValueError(f"{field} must be a bounded trimmed string")
    return value


def _pattern_text(value: object, field: str, pattern: re.Pattern[str]) -> str:
    text = _bounded_text(value, field, 256)
    if pattern.fullmatch(text) is None:
        raise ValueError(f"{field} is invalid")
    return text


def _iso_date(value: object, field: str) -> date:
    text = _bounded_text(value, field, 10)
    try:
        parsed = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO calendar date") from exc
    if parsed.isoformat() != text:
        raise ValueError(f"{field} must use YYYY-MM-DD format")
    return parsed


def _fingerprint(value: object) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("ascii")
    return sha256(encoded).hexdigest()


def _error(code: str, message: str, recovery_action: str) -> dict[str, str]:
    return {
        "code": code,
        "message": message[:1_000],
        "recovery_action": recovery_action,
    }


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
