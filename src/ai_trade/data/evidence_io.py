"""Small durable primitives for immutable research-evidence stores."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date
from hashlib import sha256
import json
import os
from pathlib import Path
import re
from threading import Lock, RLock
from typing import Any, Callable, Iterator, Mapping
from uuid import uuid4

from ..json_utils import load_unique_json


_LOCKS_GUARD = Lock()
_LOCKS: dict[str, RLock] = {}
_DATE_DIRECTORY = re.compile(r"\d{4}-\d{2}-\d{2}\Z")
_REVISION_FILE = re.compile(r"revision_(\d{8})\.json\Z")
_FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True)
class DateRevisionSpec:
    dataset: str
    label: str
    id_prefix: str
    schema_version: int = 1
    maximum_periods: int = 5_000
    maximum_revisions: int = 100
    maximum_bytes: int = 16 * 1024 * 1024


class ImmutableDateRevisionStore:
    """Immutable date/revision chain for already validated evidence payloads."""

    def __init__(
        self,
        root: Path,
        spec: DateRevisionSpec,
        validate_payload: Callable[[Mapping[str, Any]], None],
    ):
        self.root = Path(root).resolve()
        self.spec = spec
        self.validate_payload = validate_payload

    def publish(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        with evidence_store_lock(self.root, self.spec.label):
            return self._publish_unlocked(draft)

    def latest(
        self, on_date: date | None = None, *, include_revisions: bool = False
    ) -> dict[str, Any] | None:
        periods = self.periods()
        target = on_date or (periods[-1] if periods else None)
        if target is None or target not in periods:
            return None
        chain = self._load_chain(target)
        result = _json_clone(chain[-1])
        revisions = [_revision_summary(item) for item in chain]
        result["revisions"] = revisions if include_revisions else revisions[-1:]
        return result

    def periods(self) -> list[date]:
        if not self.root.exists():
            return []
        if self.root.is_symlink() or not self.root.is_dir():
            raise RuntimeError(f"{self.spec.label} root is invalid")
        result: list[date] = []
        for path in self.root.iterdir():
            if (
                path.is_symlink()
                or not path.is_dir()
                or _DATE_DIRECTORY.fullmatch(path.name) is None
            ):
                raise RuntimeError(f"{self.spec.label} period directory is invalid")
            result.append(date.fromisoformat(path.name))
            if len(result) > self.spec.maximum_periods:
                raise RuntimeError(f"{self.spec.label} store contains too many periods")
        return sorted(result)

    def _publish_unlocked(self, draft: Mapping[str, Any]) -> dict[str, Any]:
        record = _json_clone(draft)
        if not isinstance(record, dict):
            raise ValueError(f"{self.spec.label} record must be an object")
        self.validate_payload(record)
        on_date = _record_date(record, self.spec)
        chain = self._load_chain(on_date, missing_ok=True)
        evidence = _evidence_fingerprint(record)
        if chain and chain[-1].get("evidence_fingerprint") == evidence:
            result = _json_clone(chain[-1])
            result["reused"] = True
            result["revisions"] = [_revision_summary(item) for item in chain]
            return result
        if len(chain) >= self.spec.maximum_revisions:
            raise RuntimeError(f"{self.spec.label} revision capacity reached")
        previous = chain[-1] if chain else None
        record.update(
            {
                "revision_id": f"{self.spec.id_prefix}_{uuid4().hex}",
                "revision": len(chain) + 1,
                "reused": False,
                "evidence_fingerprint": evidence,
                "supersedes": previous.get("revision_id") if previous else None,
                "supersedes_fingerprint": (
                    previous.get("record_fingerprint") if previous else None
                ),
                "record_fingerprint": None,
            }
        )
        record["record_fingerprint"] = _record_fingerprint(record)
        self._validate_record(record, on_date, len(chain) + 1)
        path = (
            self.root
            / on_date.isoformat()
            / f"revision_{len(chain) + 1:08d}.json"
        )
        atomic_create_json(
            self.root,
            path,
            record,
            label=self.spec.label.lower(),
            maximum_bytes=self.spec.maximum_bytes,
        )
        committed = self._load_chain(on_date)
        result = _json_clone(committed[-1])
        result["revisions"] = [_revision_summary(item) for item in committed]
        return result

    def _load_chain(
        self, on_date: date, *, missing_ok: bool = False
    ) -> list[dict[str, Any]]:
        directory = self.root / on_date.isoformat()
        if not directory.exists():
            if missing_ok:
                return []
            raise RuntimeError(f"{self.spec.label} period is missing")
        if directory.is_symlink() or not directory.is_dir():
            raise RuntimeError(f"{self.spec.label} period is invalid")
        paths: list[tuple[int, Path]] = []
        for path in directory.iterdir():
            if path.is_symlink() or not path.is_file():
                raise RuntimeError(f"{self.spec.label} revision must be a regular file")
            match = _REVISION_FILE.fullmatch(path.name)
            if match is None:
                raise RuntimeError(f"Unexpected {self.spec.label} revision file")
            paths.append((int(match.group(1)), path))
        paths.sort()
        if len(paths) > self.spec.maximum_revisions:
            raise RuntimeError(f"{self.spec.label} period has too many revisions")
        if [number for number, _ in paths] != list(range(1, len(paths) + 1)):
            raise RuntimeError(f"{self.spec.label} revision sequence is not contiguous")
        chain: list[dict[str, Any]] = []
        previous: Mapping[str, Any] | None = None
        for revision, path in paths:
            value = load_unique_json(path, max_bytes=self.spec.maximum_bytes)
            if not isinstance(value, dict):
                raise RuntimeError(f"{self.spec.label} revision must be an object")
            self._validate_record(value, on_date, revision)
            if previous is None:
                if value.get("supersedes") is not None or value.get(
                    "supersedes_fingerprint"
                ) is not None:
                    raise RuntimeError(f"First {self.spec.label} revision has a parent")
            elif (
                value.get("supersedes") != previous.get("revision_id")
                or value.get("supersedes_fingerprint")
                != previous.get("record_fingerprint")
            ):
                raise RuntimeError(f"{self.spec.label} supersedes chain is invalid")
            chain.append(value)
            previous = value
        if not chain and not missing_ok:
            raise RuntimeError(f"{self.spec.label} period has no revisions")
        return chain

    def _validate_record(
        self, value: Mapping[str, Any], on_date: date, revision: int
    ) -> None:
        self.validate_payload(value)
        if _record_date(value, self.spec) != on_date:
            raise RuntimeError(f"{self.spec.label} trade_date does not match directory")
        revision_id = value.get("revision_id")
        if not isinstance(revision_id, str) or re.fullmatch(
            rf"{re.escape(self.spec.id_prefix)}_[0-9a-f]{{32}}", revision_id
        ) is None:
            raise RuntimeError(f"{self.spec.label} revision id is invalid")
        if value.get("revision") != revision:
            raise RuntimeError(f"{self.spec.label} revision number is invalid")
        for field in ("evidence_fingerprint", "record_fingerprint"):
            if _FINGERPRINT.fullmatch(str(value.get(field, ""))) is None:
                raise RuntimeError(f"{self.spec.label} {field} is invalid")
        if value.get("record_fingerprint") != _record_fingerprint(value):
            raise RuntimeError(
                f"{self.spec.label} record fingerprint does not match content"
            )


@contextmanager
def evidence_store_lock(root: Path, label: str) -> Iterator[None]:
    """Serialize one evidence store across threads and local processes."""

    if root.is_symlink():
        raise RuntimeError(f"{label} root must not be symbolic")
    parent = root.parent
    if parent.is_symlink():
        raise RuntimeError(f"{label} parent must not be symbolic")
    parent.mkdir(parents=True, exist_ok=True)
    if not parent.is_dir():
        raise RuntimeError(f"{label} parent must be a directory")
    key = os.path.normcase(str(root.absolute()))
    with _LOCKS_GUARD:
        thread_lock = _LOCKS.setdefault(key, RLock())
    with thread_lock:
        lock_path = parent / f".{root.name}.lock"
        with _file_lock(lock_path, label):
            if root.is_symlink():
                raise RuntimeError(f"{label} root must not be symbolic")
            root.mkdir(exist_ok=True)
            if not root.is_dir():
                raise RuntimeError(f"{label} root must be a directory")
            yield


def atomic_create_json(
    root: Path,
    path: Path,
    value: Mapping[str, Any],
    *,
    label: str,
    maximum_bytes: int,
) -> None:
    """Publish JSON without ever replacing an existing revision."""

    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{label} target must stay below its store root") from exc
    if not relative.parts or relative.name in {"", ".", ".."}:
        raise ValueError(f"{label} target is invalid")

    current = root
    for part in relative.parts[:-1]:
        if part in {"", ".", ".."}:
            raise ValueError(f"{label} target is invalid")
        current = current / part
        if current.is_symlink():
            raise RuntimeError(f"{label} directory must not be symbolic")
        current.mkdir(exist_ok=True)
        if not current.is_dir():
            raise RuntimeError(f"{label} directory must be a directory")
    if path.exists() or path.is_symlink():
        raise FileExistsError(f"Immutable {label} revision already exists: {path.name}")

    staging_root = root.parent / ".evidence-staging"
    if staging_root.is_symlink():
        raise RuntimeError("Evidence staging directory must not be symbolic")
    staging_root.mkdir(exist_ok=True)
    if not staging_root.is_dir():
        raise RuntimeError("Evidence staging path must be a directory")
    stage_directory = staging_root / f"{root.name}-{uuid4().hex}"
    stage_directory.mkdir(mode=0o700)
    temporary = stage_directory / path.name
    published = False
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(
                value,
                handle,
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if temporary.stat().st_size > maximum_bytes:
            raise ValueError(f"{label} revision exceeds {maximum_bytes} bytes")
        if load_unique_json(temporary, max_bytes=maximum_bytes) != value:
            raise RuntimeError(f"{label} staged revision did not round-trip")
        try:
            if os.name == "nt":
                _windows_move_file(temporary, path)
            else:
                os.link(temporary, path)
        except OSError as exc:
            if isinstance(exc, FileExistsError) or getattr(exc, "winerror", None) == 183:
                raise FileExistsError(
                    f"Immutable {label} revision already exists: {path.name}"
                ) from exc
            raise
        published = True
        if load_unique_json(path, max_bytes=maximum_bytes) != value:
            raise RuntimeError(f"{label} published revision did not round-trip")
        _fsync_directory(path.parent)
    except Exception:
        if published:
            path.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
        try:
            stage_directory.rmdir()
        except OSError:
            pass
        try:
            staging_root.rmdir()
        except OSError:
            pass


@contextmanager
def _file_lock(path: Path, label: str) -> Iterator[None]:
    if path.is_symlink():
        raise RuntimeError(f"{label} lock must not be symbolic")
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


def _windows_move_file(source: Path, target: Path) -> None:
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    move_file_ex = kernel32.MoveFileExW
    move_file_ex.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    move_file_ex.restype = wintypes.BOOL
    if not move_file_ex(str(source), str(target), 0x00000008):
        error = ctypes.get_last_error()
        if error in {80, 183}:
            raise FileExistsError(error, ctypes.FormatError(error), str(target))
        raise OSError(error, ctypes.FormatError(error), str(target))


def _fsync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _record_date(value: Mapping[str, Any], spec: DateRevisionSpec) -> date:
    if value.get("schema_version") != spec.schema_version:
        raise RuntimeError(f"{spec.label} schema version is invalid")
    if value.get("dataset") != spec.dataset:
        raise RuntimeError(f"{spec.label} dataset is invalid")
    try:
        return date.fromisoformat(str(value["trade_date"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise RuntimeError(f"{spec.label} trade_date is invalid") from exc


def _record_fingerprint(value: Mapping[str, Any]) -> str:
    body = dict(value)
    body["record_fingerprint"] = None
    body.pop("revisions", None)
    return _json_fingerprint(body)


def _evidence_fingerprint(value: Mapping[str, Any]) -> str:
    """Hash source evidence without retrieval or revision-chain metadata."""

    body = dict(value)
    for field in (
        "retrieved_at",
        "revision_id",
        "revision",
        "reused",
        "evidence_fingerprint",
        "record_fingerprint",
        "supersedes",
        "supersedes_fingerprint",
        "revisions",
    ):
        body.pop(field, None)
    return _json_fingerprint(body)


def _revision_summary(value: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "revision_id": value.get("revision_id"),
        "revision": value.get("revision"),
        "trade_date": value.get("trade_date"),
        "retrieved_at": value.get("retrieved_at"),
        "evidence_fingerprint": value.get("evidence_fingerprint"),
        "record_fingerprint": value.get("record_fingerprint"),
        "supersedes": value.get("supersedes"),
    }


def _json_fingerprint(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(encoded).hexdigest()


def _json_clone(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=True, allow_nan=False))


__all__ = [
    "DateRevisionSpec",
    "ImmutableDateRevisionStore",
    "atomic_create_json",
    "evidence_store_lock",
]
