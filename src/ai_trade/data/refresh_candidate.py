"""Durable, unpublished checkpoints for long market-data refreshes."""

from __future__ import annotations

from collections.abc import Mapping
import copy
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Any
from uuid import uuid4

from ..json_utils import load_unique_json


CANDIDATES_DIR_NAME = ".refresh-candidates"
STATE_NAME = "candidate.json"
SCHEMA_VERSION = 1
MAX_STATE_BYTES = 2 * 1024 * 1024
_SAFE_SYMBOL = re.compile(r"^[A-Za-z0-9_-]+$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RefreshCandidateError(RuntimeError):
    """A durable refresh candidate cannot be trusted or updated."""


@dataclass
class RefreshCandidate:
    """Store verified CSVs without exposing them as the active snapshot."""

    directory: Path
    fingerprint: str
    identity: dict[str, Any]
    _state: dict[str, Any]

    @classmethod
    def for_refresh(
        cls, cache_dir: Path, identity: Mapping[str, object]
    ) -> "RefreshCandidate":
        normalized_identity = copy.deepcopy(dict(identity))
        fingerprint = _fingerprint(normalized_identity)
        root = cache_dir / CANDIDATES_DIR_NAME
        directory = root / fingerprint
        _ensure_plain_directory(cache_dir)
        _ensure_plain_directory(root, create=True)
        _ensure_plain_directory(directory, create=True)
        state_path = directory / STATE_NAME
        if state_path.exists():
            if state_path.is_symlink() or not state_path.is_file():
                raise RefreshCandidateError(
                    f"Refresh candidate state is not a regular file: {state_path}"
                )
            try:
                state = load_unique_json(state_path, max_bytes=MAX_STATE_BYTES)
            except (OSError, UnicodeError, ValueError) as exc:
                raise RefreshCandidateError(
                    f"Refresh candidate state is unreadable: {state_path}"
                ) from exc
            _validate_state(state, fingerprint, normalized_identity)
        else:
            state = {
                "schema_version": SCHEMA_VERSION,
                "fingerprint": fingerprint,
                "identity": normalized_identity,
                "files": {},
            }
            _write_state(state_path, state)
        candidate = cls(
            directory=directory,
            fingerprint=fingerprint,
            identity=normalized_identity,
            _state=state,
        )
        candidate._adopt_compatible(root)
        return candidate

    def path_for(self, symbol: str) -> Path:
        _validate_symbol(symbol)
        return self.directory / f"{symbol}.csv"

    def restore(self, symbol: str) -> tuple[Path, dict[str, Any]] | None:
        """Return one verified checkpoint entry, if it was fully recorded."""

        _validate_symbol(symbol)
        raw_files = self._state["files"]
        if not isinstance(raw_files, dict):
            raise RefreshCandidateError("Refresh candidate files section is invalid")
        raw_entry = raw_files.get(symbol)
        if raw_entry is None:
            return None
        if not isinstance(raw_entry, dict):
            raise RefreshCandidateError(
                f"Refresh candidate entry is invalid for {symbol}"
            )
        path = self.path_for(symbol)
        if path.is_symlink() or not path.is_file():
            raise RefreshCandidateError(
                f"Refresh candidate CSV is unavailable for {symbol}"
            )
        expected_rows = raw_entry.get("rows")
        expected_digest = raw_entry.get("sha256")
        if (
            isinstance(expected_rows, bool)
            or not isinstance(expected_rows, int)
            or expected_rows < 1
        ):
            raise RefreshCandidateError(
                f"Refresh candidate row count is invalid for {symbol}"
            )
        if not isinstance(expected_digest, str) or not _SHA256.fullmatch(
            expected_digest
        ):
            raise RefreshCandidateError(
                f"Refresh candidate checksum is invalid for {symbol}"
            )
        if _csv_data_rows(path) != expected_rows:
            raise RefreshCandidateError(
                f"Refresh candidate row count mismatch for {symbol}"
            )
        if _file_sha256(path) != expected_digest:
            raise RefreshCandidateError(
                f"Refresh candidate checksum mismatch for {symbol}"
            )
        return path, copy.deepcopy(raw_entry)

    def record(
        self,
        symbol: str,
        path: Path,
        metadata: Mapping[str, object],
    ) -> dict[str, Any]:
        """Persist provenance only after the staged CSV has been verified."""

        expected_path = self.path_for(symbol)
        if path != expected_path:
            raise RefreshCandidateError(
                f"Refresh candidate path does not match {symbol}: {path}"
            )
        if path.is_symlink() or not path.is_file():
            raise RefreshCandidateError(
                f"Refresh candidate CSV is unavailable for {symbol}"
            )
        entry = copy.deepcopy(dict(metadata))
        entry.update(
            {
                "rows": _csv_data_rows(path),
                "sha256": _file_sha256(path),
                "captured_at": datetime.now(timezone.utc).isoformat(),
            }
        )
        raw_files = self._state["files"]
        if not isinstance(raw_files, dict):
            raise RefreshCandidateError("Refresh candidate files section is invalid")
        raw_files[symbol] = entry
        _write_state(self.directory / STATE_NAME, self._state)
        return copy.deepcopy(entry)

    def discard(self) -> None:
        """Remove this generated checkpoint after an atomic publish succeeds."""

        root = self.directory.parent
        if root.name != CANDIDATES_DIR_NAME or self.directory.name != self.fingerprint:
            raise RefreshCandidateError(
                f"Refusing to remove unexpected candidate path: {self.directory}"
            )
        if self.directory.is_symlink() or root.is_symlink():
            raise RefreshCandidateError(
                f"Refusing to remove symbolic-link candidate path: {self.directory}"
            )
        shutil.rmtree(self.directory)
        try:
            root.rmdir()
        except OSError:
            pass

    def _adopt_compatible(self, root: Path) -> None:
        current_files = self._state["files"]
        if not isinstance(current_files, dict):
            raise RefreshCandidateError("Refresh candidate files section is invalid")
        for sibling in sorted(root.iterdir(), key=lambda value: value.name):
            if sibling == self.directory or sibling.is_symlink() or not sibling.is_dir():
                continue
            state_path = sibling / STATE_NAME
            if state_path.is_symlink() or not state_path.is_file():
                continue
            try:
                state = load_unique_json(state_path, max_bytes=MAX_STATE_BYTES)
                if not isinstance(state, dict):
                    continue
                old_identity = state.get("identity")
                old_fingerprint = state.get("fingerprint")
                if not isinstance(old_identity, dict) or not isinstance(
                    old_fingerprint, str
                ):
                    continue
                if (
                    not _SHA256.fullmatch(old_fingerprint)
                    or sibling.name != old_fingerprint
                    or _fingerprint(old_identity) != old_fingerprint
                ):
                    continue
                _validate_state(state, old_fingerprint, old_identity)
                previous = RefreshCandidate(
                    directory=sibling,
                    fingerprint=old_fingerprint,
                    identity=old_identity,
                    _state=state,
                )
                old_files = state["files"]
                if not isinstance(old_files, dict):
                    continue
                for symbol in sorted(old_files):
                    if symbol in current_files or not _identities_match_for_symbol(
                        self.identity, old_identity, symbol
                    ):
                        continue
                    restored = previous.restore(symbol)
                    if restored is None:
                        continue
                    source, entry = restored
                    destination = self.path_for(symbol)
                    _copy_file(source, destination)
                    metadata = {
                        key: copy.deepcopy(value)
                        for key, value in entry.items()
                        if key not in {"rows", "sha256", "captured_at"}
                    }
                    metadata["adopted_from_fingerprint"] = old_fingerprint
                    if isinstance(entry.get("captured_at"), str):
                        metadata["original_captured_at"] = entry["captured_at"]
                    self.record(symbol, destination, metadata)
            except (OSError, UnicodeError, ValueError, RefreshCandidateError):
                continue


def _fingerprint(identity: Mapping[str, object]) -> str:
    try:
        payload = json.dumps(
            identity,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RefreshCandidateError("Refresh candidate identity is not JSON-safe") from exc
    return hashlib.sha256(payload).hexdigest()


def _identities_match_for_symbol(
    current: Mapping[str, object],
    previous: Mapping[str, object],
    symbol: str,
) -> bool:
    current_common = {key: value for key, value in current.items() if key != "instruments"}
    previous_common = {
        key: value for key, value in previous.items() if key != "instruments"
    }
    if current_common != previous_common:
        return False
    current_instrument = _instrument_identity(current.get("instruments"), symbol)
    previous_instrument = _instrument_identity(previous.get("instruments"), symbol)
    return current_instrument is not None and current_instrument == previous_instrument


def _instrument_identity(value: object, symbol: str) -> dict[str, object] | None:
    if not isinstance(value, list):
        return None
    matches = [
        item
        for item in value
        if isinstance(item, dict) and item.get("symbol") == symbol
    ]
    if len(matches) != 1:
        return None
    return matches[0]


def _validate_state(
    value: object,
    fingerprint: str,
    identity: Mapping[str, object],
) -> None:
    if not isinstance(value, dict):
        raise RefreshCandidateError("Refresh candidate state must be an object")
    if value.get("schema_version") != SCHEMA_VERSION:
        raise RefreshCandidateError("Refresh candidate schema version is unsupported")
    if value.get("fingerprint") != fingerprint:
        raise RefreshCandidateError("Refresh candidate fingerprint does not match")
    if value.get("identity") != identity:
        raise RefreshCandidateError("Refresh candidate identity does not match")
    files = value.get("files")
    if not isinstance(files, dict):
        raise RefreshCandidateError("Refresh candidate files section is invalid")
    for symbol in files:
        if not isinstance(symbol, str):
            raise RefreshCandidateError("Refresh candidate symbol is invalid")
        _validate_symbol(symbol)


def _validate_symbol(symbol: str) -> None:
    if not isinstance(symbol, str) or not _SAFE_SYMBOL.fullmatch(symbol):
        raise RefreshCandidateError(f"Unsafe refresh candidate symbol: {symbol!r}")


def _ensure_plain_directory(path: Path, *, create: bool = False) -> None:
    if create:
        path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise RefreshCandidateError(
            f"Refresh candidate directory is unavailable or unsafe: {path}"
        )


def _csv_data_rows(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader)
            if not header:
                raise RefreshCandidateError(
                    f"Refresh candidate CSV header is empty: {path.name}"
                )
            rows = sum(1 for row in reader if row)
    except (OSError, UnicodeError, csv.Error, StopIteration) as exc:
        raise RefreshCandidateError(
            f"Refresh candidate CSV is unreadable: {path.name}"
        ) from exc
    if rows < 1:
        raise RefreshCandidateError(
            f"Refresh candidate CSV has no data rows: {path.name}"
        )
    return rows


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_state(path: Path, value: Mapping[str, object]) -> None:
    if path.is_symlink():
        raise RefreshCandidateError(
            f"Refresh candidate state must not be a symbolic link: {path}"
        )
    try:
        payload = (
            json.dumps(
                value,
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise RefreshCandidateError("Refresh candidate state is not JSON-safe") from exc
    if len(payload) > MAX_STATE_BYTES:
        raise RefreshCandidateError(
            f"Refresh candidate state exceeds {MAX_STATE_BYTES} bytes"
        )
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("xb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_file(source: Path, destination: Path) -> None:
    if source.is_symlink() or not source.is_file() or destination.is_symlink():
        raise RefreshCandidateError(
            f"Refresh candidate copy path is unsafe: {source} -> {destination}"
        )
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with source.open("rb") as source_handle, temporary.open("xb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        temporary.replace(destination)
        _sync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "CANDIDATES_DIR_NAME",
    "RefreshCandidate",
    "RefreshCandidateError",
]
