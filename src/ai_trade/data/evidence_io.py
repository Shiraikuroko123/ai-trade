"""Small durable primitives for immutable research-evidence stores."""

from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
from threading import Lock, RLock
from typing import Any, Iterator, Mapping
from uuid import uuid4

from ..json_utils import load_unique_json


_LOCKS_GUARD = Lock()
_LOCKS: dict[str, RLock] = {}


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


__all__ = ["atomic_create_json", "evidence_store_lock"]
