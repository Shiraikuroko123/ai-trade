from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import re
from typing import Any

from ..config import AppConfig
from ..data.evidence_io import atomic_create_json, evidence_store_lock
from ..json_utils import load_unique_json
from .expression import ExpressionError, compile_expression
from .library import FactorDefinition, factor_definition, FACTORS
from .schema import json_fingerprint


CUSTOM_NAME = re.compile(r"[a-z][a-z0-9_]{2,30}\Z")
MAX_CUSTOM_FACTORS_PER_OWNER = 100
MAX_CUSTOM_RECORD_BYTES = 16 * 1024

_FIELDS = frozenset(
    {
        "schema_version",
        "name",
        "version",
        "label",
        "direction",
        "expression",
        "minimum_history",
        "created_at",
        "safety",
        "fingerprint",
    }
)
_SAFETY = {
    "research_only": True,
    "creates_no_signal": True,
    "expression_allowlist_only": True,
}


class CustomFactorStore:
    """Owner-isolated, create-once registry of expression-defined factors."""

    def __init__(self, config: AppConfig):
        self.root = (config.project_root / "state" / "factor_lab").resolve()

    def owner_id(self, owner: str) -> str:
        normalized = str(owner).strip().casefold()
        if not normalized or "\x00" in normalized or len(normalized) > 256:
            raise ValueError("Custom factor owner is invalid")
        return sha256(normalized.encode("utf-8")).hexdigest()

    def directory(self, owner: str):
        return self.root / "users" / self.owner_id(owner) / "custom"

    def define(
        self,
        owner: str,
        name: str,
        expression: str,
        direction: int,
        label: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(name, str) or CUSTOM_NAME.fullmatch(name) is None:
            raise ValueError(
                "自定义因子名必须是 3-31 位小写字母/数字/下划线并以字母开头"
            )
        if any(item.factor_id == name for item in FACTORS):
            raise ValueError(f"名称与内置因子冲突: {name}")
        if direction not in (-1, 1):
            raise ValueError("direction 必须是 1 或 -1")
        compiled = compile_expression(expression)
        display = (label or name).strip()
        if not display or len(display) > 120:
            raise ValueError("label 长度必须在 1 到 120 个字符之间")
        record = {
            "schema_version": 1,
            "name": name,
            "version": 1,
            "label": display,
            "direction": direction,
            "expression": compiled.source,
            "minimum_history": compiled.minimum_history,
            "created_at": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "safety": dict(_SAFETY),
        }
        record["fingerprint"] = _fingerprint(record)
        target = self.directory(owner) / f"{name}.json"
        with evidence_store_lock(self.root, "Factor lab"):
            if target.exists():
                existing = self.get(owner, name)
                if (
                    existing["expression"] == record["expression"]
                    and existing["direction"] == record["direction"]
                ):
                    existing["reused"] = True
                    return existing
                raise ValueError(
                    f"自定义因子 {name} 已存在且内容不同；定义不可变，请换一个名称"
                )
            records = self.list(owner)
            if len(records) >= MAX_CUSTOM_FACTORS_PER_OWNER:
                raise RuntimeError(
                    "自定义因子数量达到上限 "
                    f"({MAX_CUSTOM_FACTORS_PER_OWNER})"
                )
            atomic_create_json(
                self.root,
                target,
                record,
                label="custom factor definition",
                maximum_bytes=MAX_CUSTOM_RECORD_BYTES,
            )
        stored = self.get(owner, name)
        stored["reused"] = False
        return stored

    def get(self, owner: str, name: str) -> dict[str, Any]:
        if not isinstance(name, str) or CUSTOM_NAME.fullmatch(name) is None:
            raise ValueError("Invalid custom factor name")
        path = self.directory(owner) / f"{name}.json"
        if path.is_symlink() or not path.is_file():
            raise KeyError(name)
        return _read(path, name)

    def list(self, owner: str) -> list[dict[str, Any]]:
        directory = self.directory(owner)
        if not directory.exists():
            return []
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError("Custom factor directory is invalid")
        records: list[dict[str, Any]] = []
        for path in sorted(directory.iterdir()):
            if (
                path.is_symlink()
                or not path.is_file()
                or path.suffix != ".json"
                or CUSTOM_NAME.fullmatch(path.stem) is None
            ):
                raise RuntimeError("Unexpected custom factor store member")
            records.append(_read(path, path.stem))
        return records

    def definition(self, owner: str, name: str) -> FactorDefinition:
        record = self.get(owner, name)
        compiled = compile_expression(str(record["expression"]))
        return FactorDefinition(
            factor_id=str(record["name"]),
            version=int(record["version"]),
            label=str(record["label"]),
            family="custom",
            direction=int(record["direction"]),
            minimum_history=int(record["minimum_history"]),
            formula=str(record["expression"]),
            compute=compiled.compute,
        )


def resolve_factor(
    config: AppConfig, owner: str, factor_id: str
) -> FactorDefinition:
    """Builtin registry first, then the owner's immutable custom registry."""
    try:
        return factor_definition(factor_id)
    except ValueError:
        pass
    try:
        return CustomFactorStore(config).definition(owner, str(factor_id))
    except KeyError as exc:
        raise ValueError(f"Unknown factor: {factor_id!r}") from exc


def _fingerprint(record: dict[str, Any]) -> str:
    body = {key: value for key, value in record.items() if key != "fingerprint"}
    return json_fingerprint(body)


def _read(path, name: str) -> dict[str, Any]:
    try:
        value = load_unique_json(path, max_bytes=MAX_CUSTOM_RECORD_BYTES)
    except (OSError, UnicodeError, ValueError) as exc:
        raise RuntimeError(f"Invalid custom factor: {path}: {exc}") from exc
    if not isinstance(value, dict) or set(value) != _FIELDS:
        raise RuntimeError("Custom factor schema fields are invalid")
    if value.get("name") != name:
        raise RuntimeError("Custom factor name does not match its file name")
    if value.get("safety") != _SAFETY:
        raise RuntimeError("Custom factor safety contract is invalid")
    if value.get("fingerprint") != _fingerprint(value):
        raise RuntimeError("Custom factor fingerprint does not match content")
    try:
        compiled = compile_expression(str(value.get("expression")))
    except ExpressionError as exc:
        raise RuntimeError(f"Custom factor expression is invalid: {exc}") from exc
    if compiled.minimum_history != value.get("minimum_history"):
        raise RuntimeError("Custom factor minimum history does not match")
    return dict(value)


__all__ = [
    "CUSTOM_NAME",
    "CustomFactorStore",
    "MAX_CUSTOM_FACTORS_PER_OWNER",
    "resolve_factor",
]
