from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from typing import Any

from ..data.evidence_io import atomic_create_json, evidence_store_lock
from ..json_utils import load_unique_json
from .schema import PORTFOLIO_PLAN_ID, validate_portfolio_plan


MAX_PORTFOLIO_PLAN_BYTES = 4 * 1024 * 1024
MAX_PLANS_PER_SESSION = 100


class PortfolioPlanStore:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    @property
    def plans_root(self) -> Path:
        return self.root / "plans"

    def publish(self, record: dict[str, Any]) -> dict[str, Any]:
        validate_portfolio_plan(record)
        on_date = date.fromisoformat(str(record["execution_session"]))
        plan_id = str(record["portfolio_plan_id"])
        target = self.plans_root / on_date.isoformat() / f"{plan_id}.json"
        with evidence_store_lock(self.root, "Portfolio plan"):
            paths = self._paths(on_date, missing_ok=True)
            if target.exists() or target.is_symlink():
                existing = self._read(target)
                if existing["plan_fingerprint"] != record["plan_fingerprint"]:
                    raise RuntimeError("Portfolio plan id collision")
                result = _clone(existing)
                result["reused"] = True
                return result
            if len(paths) >= MAX_PLANS_PER_SESSION:
                raise RuntimeError("Portfolio plan session capacity reached")
            atomic_create_json(
                self.root,
                target,
                record,
                label="portfolio plan",
                maximum_bytes=MAX_PORTFOLIO_PLAN_BYTES,
            )
        result = self.get(plan_id, on_date=on_date)
        result["reused"] = False
        return result

    def get(self, plan_id: str, *, on_date: date) -> dict[str, Any]:
        _plan_id(plan_id)
        path = self.plans_root / on_date.isoformat() / f"{plan_id}.json"
        if path.is_symlink() or not path.is_file():
            raise KeyError(plan_id)
        return self._read(path)

    def _paths(self, on_date: date, *, missing_ok: bool) -> list[Path]:
        directory = self.plans_root / on_date.isoformat()
        if not directory.exists():
            if missing_ok:
                return []
            raise RuntimeError("Portfolio plan session is unavailable")
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("Portfolio plan session is invalid")
        paths = []
        for path in directory.iterdir():
            if (
                path.is_symlink()
                or not path.is_file()
                or path.suffix != ".json"
                or PORTFOLIO_PLAN_ID.fullmatch(path.stem) is None
            ):
                raise RuntimeError("Unexpected portfolio plan file")
            paths.append(path)
        if len(paths) > MAX_PLANS_PER_SESSION:
            raise RuntimeError("Portfolio plan session exceeds capacity")
        return sorted(paths, key=lambda item: item.name)

    def _read(self, path: Path) -> dict[str, Any]:
        try:
            value = load_unique_json(path, max_bytes=MAX_PORTFOLIO_PLAN_BYTES)
        except (OSError, UnicodeError, ValueError) as exc:
            raise RuntimeError(f"Invalid portfolio plan {path.name}: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError("Portfolio plan must be an object")
        try:
            validate_portfolio_plan(value)
        except ValueError as exc:
            raise RuntimeError(f"Invalid portfolio plan {path.name}: {exc}") from exc
        if value["portfolio_plan_id"] != path.stem:
            raise RuntimeError("Portfolio plan id does not match its file name")
        if value["execution_session"] != path.parent.name:
            raise RuntimeError("Portfolio plan date does not match its directory")
        return value


def _plan_id(value: object) -> str:
    if not isinstance(value, str) or PORTFOLIO_PLAN_ID.fullmatch(value) is None:
        raise ValueError("Invalid portfolio plan id")
    return value


def _clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


__all__ = ["PortfolioPlanStore"]
