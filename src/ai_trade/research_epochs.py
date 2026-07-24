"""Read-only browser for archived paper-account epochs.

Archived epochs are never reactivated or copied into active state. The browser
validates the archived paper identity, projects its own ledgers and reports,
and reads only the matching immutable digest namespace for the current owner.
"""

from __future__ import annotations

from datetime import date, datetime
from hashlib import sha256
from pathlib import Path
import re
from typing import Any, Mapping

from .broker.paper import (
    MAX_PAPER_STATE_BYTES,
    PAPER_STATE_FIELDS,
    PAPER_STATE_VERSION,
    _state_counter,
    _state_date,
    _state_number,
    _state_positions,
    _state_targets,
)
from .json_utils import load_unique_json
from .research_archive import ResearchArchiveProjection, ResearchArchiveQuery
from .research_digest import ResearchDigestQuery, ResearchDigestStore
from .research_journal import ResearchJournalStore


SCHEMA_VERSION = 1
MAX_ARCHIVED_EPOCHS = 200
EPOCH_ID = re.compile(r"\d{8}_\d{6}\Z")
HEX_32 = re.compile(r"[0-9a-f]{32}\Z")
HEX_64 = re.compile(r"[0-9a-f]{64}\Z")

_AUTHORITY = {
    "research_only": True,
    "execution_authorized": False,
    "strategy_changed": False,
    "paper_account_changed": False,
    "broker_permissions_changed": False,
    "archived_epoch_reactivated": False,
}


class ResearchEpochBrowser:
    """Validate and project immutable evidence retained by ``paper-init``."""

    def __init__(
        self,
        archive_root: str | Path,
        journal: ResearchJournalStore,
        digests: ResearchDigestStore,
    ) -> None:
        raw_root = Path(archive_root)
        if raw_root.is_symlink():
            raise RuntimeError("Paper epoch archive root must not be symbolic")
        self.archive_root = raw_root.resolve()
        self.journal = journal
        self.digests = digests

    def list(self, owner: str, *, limit: int = 50) -> dict[str, Any]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
            raise ValueError("Paper epoch limit must be between 1 and 200")
        entries, errors = self._directories()
        visible = entries[:limit]
        epochs: list[dict[str, Any]] = []
        for directory in visible:
            try:
                state = _load_archived_state(directory)
                digest_status = self.digests.list(
                    owner,
                    str(state["account_id"]),
                    ResearchDigestQuery(limit=1),
                )
                epochs.append(
                    _epoch_summary(directory, state, digest_status=digest_status)
                )
            except (OSError, RuntimeError, ValueError) as exc:
                epochs.append(
                    {
                        "epoch_id": directory.name,
                        "available": False,
                        "status": "invalid",
                        "archived_at": _archived_at(directory.name),
                        "account_fingerprint": None,
                        "config_fingerprint": None,
                        "last_run_date": None,
                        "last_equity": None,
                        "has_equity_ledger": (directory / "paper_equity.csv").is_file(),
                        "paper_report_count": 0,
                        "digest_summary": {},
                        "error": str(exc)[:500],
                        "authority": dict(_AUTHORITY),
                    }
                )
        return {
            "schema_version": SCHEMA_VERSION,
            "available": not errors,
            "status": "partial" if errors or any(not item["available"] for item in epochs) else "current",
            "summary": {
                "total": len(entries),
                "returned": len(epochs),
                "truncated": len(entries) > len(visible),
                "invalid": sum(not item["available"] for item in epochs),
            },
            "epochs": epochs,
            "errors": errors,
            "authority": dict(_AUTHORITY),
        }

    def get(
        self,
        owner: str,
        epoch_id: str,
        *,
        query: ResearchArchiveQuery | None = None,
        digest_query: ResearchDigestQuery | None = None,
        market_calendar: list[date] | None = None,
    ) -> dict[str, Any]:
        directory = self._epoch_directory(epoch_id)
        state = _load_archived_state(directory)
        projection = ResearchArchiveProjection(
            directory,
            directory / "paper_equity.csv",
            self.journal,
        ).build(
            owner,
            account_id=str(state["account_id"]),
            config_fingerprint=str(state["config_fingerprint"]),
            query=query,
            market_calendar=market_calendar,
            include_journal=False,
        )
        digests = self.digests.list(
            owner,
            str(state["account_id"]),
            digest_query,
        )
        return {
            "schema_version": SCHEMA_VERSION,
            "available": projection.get("available") is True,
            "status": projection.get("status", "unavailable"),
            "epoch": _epoch_summary(directory, state, digest_status=digests),
            "archive": projection,
            "digests": digests,
            "authority": dict(_AUTHORITY),
            "errors": list(projection.get("errors", [])),
        }

    def _directories(self) -> tuple[list[Path], list[dict[str, str]]]:
        if not self.archive_root.exists():
            return [], []
        if self.archive_root.is_symlink() or not self.archive_root.is_dir():
            raise RuntimeError("Paper epoch archive root is invalid")
        directories: list[Path] = []
        errors: list[dict[str, str]] = []
        for path in self.archive_root.iterdir():
            if path.is_symlink():
                errors.append(
                    {"code": "symbolic_epoch_rejected", "message": path.name}
                )
                continue
            if not path.is_dir() or EPOCH_ID.fullmatch(path.name) is None:
                errors.append(
                    {"code": "unexpected_epoch_member", "message": path.name}
                )
                continue
            directories.append(path)
            if len(directories) > MAX_ARCHIVED_EPOCHS:
                raise RuntimeError(
                    f"Paper epoch archive exceeds {MAX_ARCHIVED_EPOCHS} entries"
                )
        return sorted(directories, key=lambda item: item.name, reverse=True), errors

    def _epoch_directory(self, epoch_id: str) -> Path:
        if not isinstance(epoch_id, str) or EPOCH_ID.fullmatch(epoch_id) is None:
            raise ValueError("Paper epoch id is invalid")
        directory = self.archive_root / epoch_id
        if directory.is_symlink() or not directory.is_dir():
            raise KeyError(epoch_id)
        if directory.resolve().parent != self.archive_root:
            raise RuntimeError("Paper epoch path escapes the archive root")
        return directory


def _load_archived_state(directory: Path) -> dict[str, Any]:
    state_path = directory / "paper_state.json"
    if state_path.is_symlink() or not state_path.is_file():
        raise RuntimeError("Archived paper state is unavailable")
    value = load_unique_json(state_path, max_bytes=MAX_PAPER_STATE_BYTES)
    if not isinstance(value, dict) or set(value) != PAPER_STATE_FIELDS:
        raise RuntimeError("Archived paper state schema is invalid")
    if type(value.get("version")) is not int or value["version"] != PAPER_STATE_VERSION:
        raise RuntimeError("Archived paper state version is unsupported")
    account_id = value.get("account_id")
    config_fingerprint = value.get("config_fingerprint")
    if not isinstance(account_id, str) or HEX_32.fullmatch(account_id) is None:
        raise RuntimeError("Archived paper account identity is invalid")
    if not isinstance(config_fingerprint, str) or HEX_64.fullmatch(config_fingerprint) is None:
        raise RuntimeError("Archived paper configuration identity is invalid")
    cash = _state_number(value["cash"], "cash", positive=False)
    high_water_mark = _state_number(
        value["high_water_mark"], "high_water_mark", positive=True
    )
    last_equity = _state_number(
        value["last_equity"], "last_equity", positive=False
    )
    if last_equity > high_water_mark + 1e-8:
        raise RuntimeError("Archived paper last_equity exceeds its high_water_mark")
    positions = _state_positions(value["positions"])
    pending_targets = _state_targets(value["pending_targets"])
    last_run_date = _state_date(value["last_run_date"], "last_run_date")
    pending_signal_date = _state_date(
        value["pending_signal_date"], "pending_signal_date"
    )
    if (pending_targets is None) != (pending_signal_date is None):
        raise RuntimeError(
            "Archived paper pending_targets and pending_signal_date must be present together"
        )
    if last_run_date is None and (positions or pending_targets is not None):
        raise RuntimeError(
            "Archived unprocessed paper state cannot contain positions or targets"
        )
    if pending_signal_date is not None and (
        last_run_date is None or pending_signal_date > last_run_date
    ):
        raise RuntimeError("Archived paper pending signal date is after its last run date")
    _state_counter(value["cooldown_remaining"], "cooldown_remaining")
    _state_counter(
        value["sessions_since_rebalance"], "sessions_since_rebalance"
    )
    if cash > high_water_mark + 1e-8 and not positions:
        raise RuntimeError("Archived paper cash exceeds its high_water_mark")
    return value


def _epoch_summary(
    directory: Path,
    state: Mapping[str, Any],
    *,
    digest_status: Mapping[str, Any],
) -> dict[str, Any]:
    account_id = str(state["account_id"])
    report_count = sum(
        1
        for path in directory.glob("paper_????????.json")
        if path.is_file() and not path.is_symlink()
    )
    return {
        "epoch_id": directory.name,
        "available": True,
        "status": "archived",
        "archived_at": _archived_at(directory.name),
        "account_fingerprint": sha256(account_id.encode("utf-8")).hexdigest(),
        "config_fingerprint": state["config_fingerprint"],
        "last_run_date": state.get("last_run_date"),
        "last_equity": float(state["last_equity"]),
        "has_equity_ledger": (directory / "paper_equity.csv").is_file(),
        "paper_report_count": report_count,
        "digest_summary": dict(digest_status.get("summary", {})),
        "error": None,
        "authority": dict(_AUTHORITY),
    }


def _archived_at(epoch_id: str) -> str:
    parsed = datetime.strptime(epoch_id, "%Y%m%d_%H%M%S")
    return parsed.isoformat()
