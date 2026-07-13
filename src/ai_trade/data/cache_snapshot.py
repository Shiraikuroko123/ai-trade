from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
import csv
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import time
from typing import Any
from uuid import uuid4


MARKER_NAME = ".cache-transaction.json"
TRANSACTIONS_DIR_NAME = ".cache-transactions"
TRANSACTION_JOURNAL_NAME = "transaction.json"
TRANSACTION_LOCK_NAME = ".cache-transaction.lock"
REFRESH_LOCK_NAME = ".cache-refresh.lock"
SCHEMA_VERSION = 1

_TRANSACTION_ID = re.compile(r"^[0-9a-f]{32}$")
_STATES = {"backing_up", "prepared", "installing", "committed"}


class CacheSnapshotError(RuntimeError):
    """Base error for a cache snapshot transaction."""


class CacheSnapshotRecoveryError(CacheSnapshotError):
    """The active cache cannot be proven consistent and must not be loaded."""


@contextmanager
def snapshot_refresh_lock(cache_dir: Path) -> Iterator[None]:
    """Serialize complete refresh cycles, including their network fetches."""

    cache_dir.mkdir(parents=True, exist_ok=True)
    with _advisory_lock(cache_dir / REFRESH_LOCK_NAME):
        yield


@contextmanager
def readable_snapshot(cache_dir: Path) -> Iterator[None]:
    """Recover an interrupted commit and hold a stable snapshot for reading."""

    cache_dir.mkdir(parents=True, exist_ok=True)
    with _advisory_lock(cache_dir / TRANSACTION_LOCK_NAME):
        _recover_locked(cache_dir)
        if (cache_dir / MARKER_NAME).exists():
            raise CacheSnapshotRecoveryError(
                "Cache transaction marker remains after recovery; refusing to load"
            )
        yield


def recover_pending_snapshot(cache_dir: Path) -> None:
    """Recover or discard a transaction left by an interrupted process."""

    cache_dir.mkdir(parents=True, exist_ok=True)
    with _advisory_lock(cache_dir / TRANSACTION_LOCK_NAME):
        _recover_locked(cache_dir)


def install_snapshot(
    cache_dir: Path,
    staged_csv_files: Mapping[str, Path],
    manifest: Mapping[str, object],
) -> None:
    """Atomically install a group of CSV files followed by their manifest."""

    cache_dir.mkdir(parents=True, exist_ok=True)
    normalized = _normalize_staged_files(cache_dir, staged_csv_files)
    with _advisory_lock(cache_dir / TRANSACTION_LOCK_NAME):
        _recover_locked(cache_dir)
        _install_locked(cache_dir, normalized, manifest)


def _install_locked(
    cache_dir: Path,
    staged_csv_files: Mapping[str, Path],
    manifest: Mapping[str, object],
) -> None:
    transaction_id = uuid4().hex
    transaction_dir = cache_dir / TRANSACTIONS_DIR_NAME / transaction_id
    staged_dir = transaction_dir / "staged"
    backup_dir = transaction_dir / "backup"
    staged_dir.mkdir(parents=True, exist_ok=False)
    backup_dir.mkdir()

    names = [*sorted(staged_csv_files), "manifest.json"]
    entries: list[dict[str, object]] = []
    marker_written = False
    installing = False
    try:
        for name, source in staged_csv_files.items():
            destination = staged_dir / name
            _copy_file(source, destination)
        _write_json_file(staged_dir / "manifest.json", manifest)
        _validate_snapshot_contents(
            {name: staged_dir / name for name in staged_csv_files}, manifest
        )

        for name in names:
            target = cache_dir / name
            if target.is_symlink():
                raise CacheSnapshotError(
                    f"Cache snapshot target must not be a symbolic link: {target}"
                )
            entries.append(
                {
                    "name": name,
                    "had_original": target.exists(),
                    "backup": f"backup/{name}",
                    "backup_sha256": None,
                    "installed_sha256": _file_sha256(staged_dir / name),
                }
            )

        marker = _new_marker(transaction_id, "backing_up", entries)
        _write_transaction_journal(transaction_dir, marker)
        _write_marker(cache_dir, marker)
        marker_written = True

        for entry in entries:
            if not entry["had_original"]:
                continue
            name = _entry_name(entry)
            source = cache_dir / name
            if not source.is_file() or source.is_symlink():
                raise CacheSnapshotError(
                    f"Existing cache snapshot target is not a regular file: {source}"
                )
            backup = backup_dir / name
            _copy_file(source, backup)
            entry["backup_sha256"] = _file_sha256(backup)

        _update_transaction_state(cache_dir, transaction_dir, marker, "prepared")
        _update_transaction_state(cache_dir, transaction_dir, marker, "installing")
        installing = True

        try:
            for name in names[:-1]:
                _install_file(staged_dir / name, cache_dir / name)
            _install_file(staged_dir / "manifest.json", cache_dir / "manifest.json")
            active_manifest = _read_manifest(cache_dir / "manifest.json")
            _validate_snapshot_contents(
                {name: cache_dir / name for name in staged_csv_files},
                active_manifest,
            )
            _update_transaction_state(cache_dir, transaction_dir, marker, "committed")
        except BaseException as install_error:
            try:
                _rollback_locked(cache_dir, marker)
            except BaseException as rollback_error:
                raise CacheSnapshotRecoveryError(
                    "Cache snapshot commit failed and rollback could not be completed; "
                    "the transaction marker and backups were retained"
                ) from rollback_error
            raise install_error

        _remove_marker(cache_dir)
        shutil.rmtree(transaction_dir, ignore_errors=True)
        _remove_transactions_dir_if_empty(cache_dir)
    except BaseException:
        if marker_written and not installing:
            _discard_uninstalled_transaction(cache_dir, transaction_id)
        elif not marker_written:
            shutil.rmtree(transaction_dir, ignore_errors=True)
            _remove_transactions_dir_if_empty(cache_dir)
        raise


def _recover_locked(cache_dir: Path) -> None:
    marker_path = cache_dir / MARKER_NAME
    if not marker_path.exists():
        _recover_markerless_transaction(cache_dir)
        return

    marker = _read_and_validate_marker(cache_dir)
    state = marker["state"]
    if state in {"backing_up", "prepared"}:
        _discard_uninstalled_transaction(cache_dir, marker["transaction_id"])
        return
    if state == "installing":
        _rollback_locked(cache_dir, marker)
        return
    if state == "committed":
        _recover_committed_locked(cache_dir, marker)
        return
    raise CacheSnapshotRecoveryError(f"Unsupported cache transaction state: {state!r}")


def _recover_markerless_transaction(cache_dir: Path) -> None:
    root = cache_dir / TRANSACTIONS_DIR_NAME
    if not root.exists():
        _cleanup_marker_temporaries(cache_dir)
        return
    if not root.is_dir() or root.is_symlink():
        raise CacheSnapshotRecoveryError(
            "Cache transaction storage is not a regular directory"
        )

    children = list(root.iterdir())
    if not children:
        root.rmdir()
        _cleanup_marker_temporaries(cache_dir)
        return
    if len(children) != 1:
        raise CacheSnapshotRecoveryError(
            "Cache transaction marker is missing and transaction storage is "
            "ambiguous; backups were retained"
        )

    transaction_dir = children[0]
    if (
        not transaction_dir.is_dir()
        or transaction_dir.is_symlink()
        or not _TRANSACTION_ID.fullmatch(transaction_dir.name)
    ):
        raise CacheSnapshotRecoveryError(
            "Cache transaction marker is missing and the remaining transaction "
            "cannot be safely identified; backups were retained"
        )

    marker = _read_and_validate_transaction_journal(cache_dir, transaction_dir)
    state = marker["state"]
    if state in {"backing_up", "prepared"}:
        _discard_uninstalled_transaction(cache_dir, marker["transaction_id"])
    elif state == "installing":
        _rollback_locked(cache_dir, marker)
    elif state == "committed":
        _recover_committed_locked(cache_dir, marker)
    else:
        raise CacheSnapshotRecoveryError(
            f"Unsupported cache transaction state: {state!r}"
        )
    _cleanup_marker_temporaries(cache_dir)


def _recover_committed_locked(cache_dir: Path, marker: Mapping[str, Any]) -> None:
    validated = _validate_marker(cache_dir, marker)
    if validated["state"] != "committed":
        raise CacheSnapshotRecoveryError(
            "Only a committed cache transaction can be finalized"
        )
    try:
        _validate_active_snapshot(cache_dir, validated)
    except CacheSnapshotError:
        _rollback_locked(cache_dir, validated)
        return

    _remove_marker(cache_dir)
    transaction_dir = cache_dir / TRANSACTIONS_DIR_NAME / validated["transaction_id"]
    shutil.rmtree(transaction_dir, ignore_errors=True)
    _remove_transactions_dir_if_empty(cache_dir)


def _validate_active_snapshot(cache_dir: Path, marker: Mapping[str, Any]) -> None:
    entries = marker["entries"]
    manifest_path = cache_dir / "manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise CacheSnapshotError("Installed cache manifest is unavailable")
    csv_files: dict[str, Path] = {}
    for entry in entries:
        name = _entry_name(entry)
        path = cache_dir / name
        if not path.is_file() or path.is_symlink():
            raise CacheSnapshotError(f"Installed cache file is unavailable: {name}")
        try:
            actual_digest = _file_sha256(path)
        except OSError as exc:
            raise CacheSnapshotError(
                f"Installed cache file could not be verified: {name}"
            ) from exc
        if actual_digest != entry["installed_sha256"]:
            raise CacheSnapshotError(
                f"Installed cache file does not match the committed snapshot: {name}"
            )
        if name == "manifest.json":
            continue
        csv_files[name] = path
    manifest = _read_manifest(manifest_path)
    try:
        _validate_snapshot_contents(csv_files, manifest)
    except OSError as exc:
        raise CacheSnapshotError(
            "Installed cache snapshot could not be verified"
        ) from exc


def _rollback_locked(cache_dir: Path, marker: Mapping[str, Any]) -> None:
    validated = _validate_marker(cache_dir, marker)
    if validated["state"] not in {"installing", "committed"}:
        raise CacheSnapshotRecoveryError(
            "Only an installing or committed cache transaction can be rolled back"
        )

    transaction_dir = cache_dir / TRANSACTIONS_DIR_NAME / validated["transaction_id"]
    entries = validated["entries"]
    for entry in entries:
        if not entry["had_original"]:
            continue
        backup = transaction_dir / entry["backup"]
        if not backup.is_file() or backup.is_symlink():
            raise CacheSnapshotRecoveryError(
                f"Cache transaction backup is missing: {entry['name']}"
            )
        if _file_sha256(backup) != entry["backup_sha256"]:
            raise CacheSnapshotRecoveryError(
                f"Cache transaction backup checksum mismatch: {entry['name']}"
            )

    csv_entries = [entry for entry in entries if entry["name"] != "manifest.json"]
    manifest_entries = [entry for entry in entries if entry["name"] == "manifest.json"]
    for entry in [*csv_entries, *manifest_entries]:
        target = cache_dir / entry["name"]
        if entry["had_original"]:
            backup = transaction_dir / entry["backup"]
            _restore_file(backup, target)
        else:
            target.unlink(missing_ok=True)
    _sync_directory(cache_dir)
    _remove_marker(cache_dir)
    shutil.rmtree(transaction_dir, ignore_errors=True)
    _remove_transactions_dir_if_empty(cache_dir)


def _discard_uninstalled_transaction(cache_dir: Path, transaction_id: str) -> None:
    marker_path = cache_dir / MARKER_NAME
    if marker_path.exists():
        marker = _read_and_validate_marker(cache_dir)
        if marker["transaction_id"] != transaction_id:
            raise CacheSnapshotRecoveryError(
                "Cache transaction marker changed while recovery was in progress"
            )
        if marker["state"] in {"installing", "committed"}:
            raise CacheSnapshotRecoveryError(
                "Refusing to discard a cache transaction that may have changed "
                "the active snapshot"
            )
        _remove_marker(cache_dir)
    transaction_dir = cache_dir / TRANSACTIONS_DIR_NAME / transaction_id
    shutil.rmtree(transaction_dir, ignore_errors=True)
    _remove_transactions_dir_if_empty(cache_dir)


def _normalize_staged_files(
    cache_dir: Path, staged_csv_files: Mapping[str, Path]
) -> dict[str, Path]:
    if not staged_csv_files:
        raise ValueError("A cache snapshot must contain at least one CSV file")
    normalized: dict[str, Path] = {}
    cache_root = cache_dir.resolve()
    for name, source in staged_csv_files.items():
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or not name.endswith(".csv")
        ):
            raise ValueError(f"Invalid cache snapshot file name: {name!r}")
        source = Path(source)
        if not source.is_file() or source.is_symlink():
            raise FileNotFoundError(f"Staged cache file is unavailable: {source}")
        try:
            source.resolve().relative_to(cache_root)
        except ValueError as exc:
            raise ValueError(
                f"Staged cache file must be on the cache volume: {source}"
            ) from exc
        normalized[name] = source
    return normalized


def _new_marker(
    transaction_id: str, state: str, entries: list[dict[str, object]]
) -> dict[str, object]:
    return {
        "schema_version": SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "state": state,
        "entries": entries,
    }


def _read_and_validate_marker(cache_dir: Path) -> dict[str, Any]:
    marker_path = cache_dir / MARKER_NAME
    try:
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CacheSnapshotRecoveryError(
            f"Cache transaction marker is unreadable: {marker_path}"
        ) from exc
    return _validate_marker(cache_dir, marker)


def _read_and_validate_transaction_journal(
    cache_dir: Path, transaction_dir: Path
) -> dict[str, Any]:
    journal_path = transaction_dir / TRANSACTION_JOURNAL_NAME
    if not journal_path.is_file() or journal_path.is_symlink():
        raise CacheSnapshotRecoveryError(
            "Cache transaction marker is missing and the transaction journal is "
            "unavailable; backups were retained"
        )
    try:
        marker = json.loads(journal_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CacheSnapshotRecoveryError(
            "Cache transaction marker is missing and the transaction journal is "
            "unreadable; backups were retained"
        ) from exc
    validated = _validate_marker(cache_dir, marker)
    if validated["transaction_id"] != transaction_dir.name:
        raise CacheSnapshotRecoveryError(
            "Cache transaction journal does not match its directory; backups were "
            "retained"
        )
    return validated


def _validate_marker(cache_dir: Path, marker: object) -> dict[str, Any]:
    if not isinstance(marker, dict):
        raise CacheSnapshotRecoveryError("Cache transaction marker must be an object")
    if marker.get("schema_version") != SCHEMA_VERSION:
        raise CacheSnapshotRecoveryError(
            "Cache transaction marker has an unsupported schema version"
        )
    transaction_id = marker.get("transaction_id")
    if not isinstance(transaction_id, str) or not _TRANSACTION_ID.fullmatch(
        transaction_id
    ):
        raise CacheSnapshotRecoveryError(
            "Cache transaction marker has an invalid transaction ID"
        )
    state = marker.get("state")
    if state not in _STATES:
        raise CacheSnapshotRecoveryError(
            "Cache transaction marker has an invalid state"
        )
    entries = marker.get("entries")
    if not isinstance(entries, list) or not entries:
        raise CacheSnapshotRecoveryError("Cache transaction marker has no file entries")

    names: set[str] = set()
    validated_entries: list[dict[str, Any]] = []
    for raw_entry in entries:
        if not isinstance(raw_entry, dict):
            raise CacheSnapshotRecoveryError(
                "Cache transaction marker contains an invalid file entry"
            )
        name = raw_entry.get("name")
        if (
            not isinstance(name, str)
            or Path(name).name != name
            or (name != "manifest.json" and not name.endswith(".csv"))
            or name in names
        ):
            raise CacheSnapshotRecoveryError(
                "Cache transaction marker contains an invalid file name"
            )
        names.add(name)
        had_original = raw_entry.get("had_original")
        if not isinstance(had_original, bool):
            raise CacheSnapshotRecoveryError(
                f"Cache transaction original-file flag is invalid: {name}"
            )
        expected_backup = f"backup/{name}"
        if raw_entry.get("backup") != expected_backup:
            raise CacheSnapshotRecoveryError(
                f"Cache transaction backup path is invalid: {name}"
            )
        digest = raw_entry.get("backup_sha256")
        if had_original and state in {"prepared", "installing", "committed"}:
            if (
                not isinstance(digest, str)
                or len(digest) != 64
                or any(character not in "0123456789abcdef" for character in digest)
            ):
                raise CacheSnapshotRecoveryError(
                    f"Cache transaction backup checksum is invalid: {name}"
                )
        elif digest is not None and not isinstance(digest, str):
            raise CacheSnapshotRecoveryError(
                f"Cache transaction backup checksum is invalid: {name}"
            )
        installed_digest = raw_entry.get("installed_sha256")
        if state == "committed" or installed_digest is not None:
            if (
                not isinstance(installed_digest, str)
                or len(installed_digest) != 64
                or any(
                    character not in "0123456789abcdef"
                    for character in installed_digest
                )
            ):
                raise CacheSnapshotRecoveryError(
                    f"Cache transaction installed checksum is invalid: {name}"
                )
        validated_entries.append(
            {
                "name": name,
                "had_original": had_original,
                "backup": expected_backup,
                "backup_sha256": digest,
                "installed_sha256": installed_digest,
            }
        )
    if "manifest.json" not in names:
        raise CacheSnapshotRecoveryError(
            "Cache transaction marker does not include the manifest"
        )
    transaction_dir = cache_dir / TRANSACTIONS_DIR_NAME / transaction_id
    if (
        state in {"prepared", "installing", "committed"}
        and not transaction_dir.is_dir()
    ):
        raise CacheSnapshotRecoveryError("Cache transaction directory is missing")
    return {
        "schema_version": SCHEMA_VERSION,
        "transaction_id": transaction_id,
        "state": state,
        "entries": validated_entries,
    }


def _write_marker(cache_dir: Path, marker: Mapping[str, object]) -> None:
    _write_json_atomic(cache_dir / MARKER_NAME, marker)


def _write_transaction_journal(
    transaction_dir: Path, marker: Mapping[str, object]
) -> None:
    _write_json_atomic(transaction_dir / TRANSACTION_JOURNAL_NAME, marker)


def _update_transaction_state(
    cache_dir: Path,
    transaction_dir: Path,
    marker: dict[str, object],
    state: str,
) -> None:
    marker["state"] = state
    _write_transaction_journal(transaction_dir, marker)
    _write_marker(cache_dir, marker)


def _read_manifest(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise CacheSnapshotError(
            f"Installed cache manifest is unreadable: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise CacheSnapshotError("Installed cache manifest must be an object")
    return value


def _validate_snapshot_contents(
    csv_files: Mapping[str, Path], manifest: Mapping[str, object]
) -> None:
    raw_files = manifest.get("files")
    if not isinstance(raw_files, Mapping):
        raise CacheSnapshotError("Cache manifest files section is missing or invalid")
    expected_symbols = {Path(name).stem for name in csv_files}
    if set(raw_files) != expected_symbols:
        raise CacheSnapshotError(
            "Cache manifest symbols do not match the staged CSV files"
        )
    for name, path in csv_files.items():
        symbol = Path(name).stem
        raw_entry = raw_files[symbol]
        if not isinstance(raw_entry, Mapping):
            raise CacheSnapshotError(f"Cache manifest file entry is invalid: {symbol}")
        expected_rows = raw_entry.get("rows")
        expected_digest = raw_entry.get("sha256")
        if isinstance(expected_rows, bool) or not isinstance(expected_rows, int):
            raise CacheSnapshotError(f"Cache manifest row count is invalid: {symbol}")
        actual_rows = _csv_data_rows(path)
        if expected_rows != actual_rows:
            raise CacheSnapshotError(f"Cache manifest row count mismatch: {symbol}")
        if not isinstance(expected_digest, str) or expected_digest != _file_sha256(
            path
        ):
            raise CacheSnapshotError(f"Cache manifest checksum mismatch: {symbol}")


def _csv_data_rows(path: Path) -> int:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader)
            if not header:
                raise CacheSnapshotError(f"Cache CSV header is empty: {path.name}")
            rows = sum(1 for row in reader if row)
    except (OSError, UnicodeError, csv.Error, StopIteration) as exc:
        raise CacheSnapshotError(f"Cache CSV is unreadable: {path.name}") from exc
    if rows < 1:
        raise CacheSnapshotError(f"Cache CSV has no data rows: {path.name}")
    return rows


def _write_json_file(path: Path, value: Mapping[str, object]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def _write_json_atomic(path: Path, value: Mapping[str, object]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        _write_json_file(temporary, value)
        _replace_path(temporary, path)
        _sync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _copy_file(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid4().hex}.tmp")
    try:
        with source.open("rb") as source_handle, temporary.open("xb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        _replace_path(temporary, destination)
        _sync_directory(destination.parent)
    finally:
        temporary.unlink(missing_ok=True)


def _restore_file(backup: Path, target: Path) -> None:
    temporary = target.with_name(f".{target.name}.{uuid4().hex}.restore")
    try:
        with backup.open("rb") as source_handle, temporary.open("xb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        _replace_path(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def _install_file(staged: Path, target: Path) -> None:
    _replace_path(staged, target)
    _sync_directory(target.parent)


def _replace_path(source: Path, destination: Path) -> None:
    source.replace(destination)


def _remove_marker(cache_dir: Path) -> None:
    (cache_dir / MARKER_NAME).unlink(missing_ok=True)
    _sync_directory(cache_dir)


def _cleanup_marker_temporaries(cache_dir: Path) -> None:
    for temporary in cache_dir.glob(f".{MARKER_NAME}.*.tmp"):
        temporary.unlink(missing_ok=True)


def _remove_transactions_dir_if_empty(cache_dir: Path) -> None:
    root = cache_dir / TRANSACTIONS_DIR_NAME
    try:
        root.rmdir()
    except FileNotFoundError:
        return
    except OSError:
        return


def _entry_name(entry: Mapping[str, object]) -> str:
    name = entry["name"]
    if not isinstance(name, str):
        raise CacheSnapshotError("Cache transaction entry has an invalid name")
    return name


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _advisory_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        handle.seek(0, os.SEEK_END)
        if handle.tell() == 0:
            handle.write(b"\0")
            handle.flush()
        handle.seek(0)
        _lock_handle(handle)
        try:
            yield
        finally:
            handle.seek(0)
            _unlock_handle(handle)


def _lock_handle(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        while True:
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError:
                time.sleep(0.05)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)


def _unlock_handle(handle: Any) -> None:
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
