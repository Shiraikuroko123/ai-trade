from __future__ import annotations

import json
import math
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4


PREFERENCES_SCHEMA_VERSION = 1
DEFAULT_STORAGE_LIMIT_BYTES = 10_000_000_000
DEFAULT_CLASS_A_LIMIT = 1_000_000
DEFAULT_CLASS_B_LIMIT = 10_000_000
DEFAULT_BILLING_CYCLE_DAY = 1
STORAGE_MODES = {"local", "hybrid"}
LOCAL_PROFILE_ID = "local"


class CloudPreferencesError(RuntimeError):
    pass


@dataclass(frozen=True)
class CloudPreferences:
    storage_mode: str
    storage_limit_bytes: int
    class_a_limit: int
    class_b_limit: int
    billing_cycle_day: int

    @property
    def automatic_cloud_backup(self) -> bool:
        return self.storage_mode == "hybrid"

    def public_status(self) -> dict[str, object]:
        return {
            "storage_mode": self.storage_mode,
            "storage_limit_bytes": self.storage_limit_bytes,
            "storage_limit_gb": self.storage_limit_bytes / 1_000_000_000,
            "class_a_limit": self.class_a_limit,
            "class_b_limit": self.class_b_limit,
            "billing_cycle_day": self.billing_cycle_day,
            "automatic_cloud_backup": self.automatic_cloud_backup,
        }


def cloud_preferences_path(
    project_root: Path, profile_id: str = LOCAL_PROFILE_ID
) -> Path:
    return _cloud_profile_dir(project_root, profile_id) / "preferences.json"


def cloud_usage_path(project_root: Path, profile_id: str = LOCAL_PROFILE_ID) -> Path:
    return _cloud_profile_dir(project_root, profile_id) / "usage.sqlite3"


def load_cloud_preferences(
    path: Path, *, cloud_configured: bool = False
) -> CloudPreferences:
    if path.is_symlink():
        raise CloudPreferencesError("Cloud preferences storage is not a regular file")
    if not path.exists():
        return _default_preferences(cloud_configured)
    if not path.is_file():
        raise CloudPreferencesError("Cloud preferences storage is not a regular file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CloudPreferencesError("Cloud preferences are unreadable or invalid") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != PREFERENCES_SCHEMA_VERSION:
        raise CloudPreferencesError("Cloud preferences use an unsupported schema")
    return _validate_preferences(payload)


def update_cloud_preferences(
    path: Path,
    payload: Mapping[str, object],
    *,
    cloud_configured: bool,
) -> CloudPreferences:
    with cloud_state_lock(path.parent):
        allowed = {
            "storage_mode",
            "storage_limit_gb",
            "class_a_limit",
            "class_b_limit",
            "billing_cycle_day",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise ValueError(
                f"Unknown cloud preference fields: {', '.join(sorted(unknown))}"
            )
        current = load_cloud_preferences(path, cloud_configured=cloud_configured)
        mode = str(payload.get("storage_mode", current.storage_mode)).strip().lower()
        if mode not in STORAGE_MODES:
            raise ValueError("Storage mode must be local or hybrid")
        if mode == "hybrid" and not cloud_configured:
            raise ValueError(
                "Cloud backup must be configured before selecting hybrid storage"
            )

        storage_limit_gb = _finite_number(
            payload.get(
                "storage_limit_gb", current.storage_limit_bytes / 1_000_000_000
            ),
            "Cloud storage limit",
        )
        if storage_limit_gb < 0.1 or storage_limit_gb > 1_000_000:
            raise ValueError(
                "Cloud storage limit must be between 0.1 GB and 1,000,000 GB"
            )
        storage_limit_bytes = round(storage_limit_gb * 1_000_000_000)
        class_a_limit = _bounded_integer(
            payload.get("class_a_limit", current.class_a_limit),
            "Class A operation limit",
            1,
            1_000_000_000_000,
        )
        class_b_limit = _bounded_integer(
            payload.get("class_b_limit", current.class_b_limit),
            "Class B operation limit",
            1,
            1_000_000_000_000,
        )
        billing_cycle_day = _bounded_integer(
            payload.get("billing_cycle_day", current.billing_cycle_day),
            "Billing cycle day",
            1,
            28,
        )
        preferences = CloudPreferences(
            storage_mode=mode,
            storage_limit_bytes=storage_limit_bytes,
            class_a_limit=class_a_limit,
            class_b_limit=class_b_limit,
            billing_cycle_day=billing_cycle_day,
        )
        _write_preferences(path, preferences)
        return preferences


@contextmanager
def cloud_state_lock(state_dir: Path) -> Iterator[None]:
    state_dir.mkdir(parents=True, exist_ok=True)
    path = state_dir / ".cloud-state.lock"
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


class CloudUsageStore:
    """Durable, process-safe counters for AI Trade's own high-level R2 requests."""

    def __init__(self, path: Path):
        self.path = path

    def record(self, operation_class: str, count: int = 1) -> None:
        if operation_class not in {"class_a", "class_b"}:
            raise ValueError("Cloud operation class must be class_a or class_b")
        if isinstance(count, bool) or not isinstance(count, int) or count < 1:
            raise ValueError("Cloud operation count must be a positive integer")
        today = datetime.now(timezone.utc).date().isoformat()
        class_a = count if operation_class == "class_a" else 0
        class_b = count if operation_class == "class_b" else 0
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO daily_usage(day, class_a, class_b)
                VALUES (?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    class_a = class_a + excluded.class_a,
                    class_b = class_b + excluded.class_b
                """,
                (today, class_a, class_b),
            )

    def usage(self, start: date, end: date) -> dict[str, object]:
        if end <= start:
            raise ValueError("Cloud usage period end must be after its start")
        with self._connection() as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(class_a), 0), COALESCE(SUM(class_b), 0)
                FROM daily_usage WHERE day >= ? AND day < ?
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchone()
            started = connection.execute(
                "SELECT value FROM metadata WHERE key = 'tracking_started_at'"
            ).fetchone()
        return {
            "period_start": start.isoformat(),
            "period_end": end.isoformat(),
            "class_a": int(row[0]),
            "class_b": int(row[1]),
            "tracking_started_at": started[0] if started else None,
        }

    def save_inventory(
        self,
        *,
        object_count: int,
        storage_bytes: int,
        snapshots: list[dict[str, object]],
    ) -> dict[str, object]:
        if object_count < 0 or storage_bytes < 0:
            raise ValueError("Cloud inventory values must not be negative")
        safe_snapshots = _validate_snapshot_summaries(snapshots)
        scanned_at = datetime.now(timezone.utc).isoformat()
        with self._connection() as connection:
            connection.execute(
                """
                INSERT INTO inventory(id, scanned_at, object_count, storage_bytes, snapshots_json)
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    scanned_at = excluded.scanned_at,
                    object_count = excluded.object_count,
                    storage_bytes = excluded.storage_bytes,
                    snapshots_json = excluded.snapshots_json
                """,
                (
                    scanned_at,
                    object_count,
                    storage_bytes,
                    json.dumps(safe_snapshots, sort_keys=True, separators=(",", ":")),
                ),
            )
        return {
            "scanned_at": scanned_at,
            "object_count": object_count,
            "storage_bytes": storage_bytes,
            "snapshots": safe_snapshots,
        }

    def inventory(self) -> dict[str, object]:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT scanned_at, object_count, storage_bytes, snapshots_json "
                "FROM inventory WHERE id = 1"
            ).fetchone()
        if row is None:
            return {
                "scanned_at": None,
                "object_count": 0,
                "storage_bytes": 0,
                "snapshots": [],
            }
        try:
            snapshots = json.loads(row[3])
        except json.JSONDecodeError as exc:
            raise CloudPreferencesError("Cloud inventory cache is invalid") from exc
        return {
            "scanned_at": row[0],
            "object_count": int(row[1]),
            "storage_bytes": int(row[2]),
            "snapshots": _validate_snapshot_summaries(snapshots),
        }

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if self.path.is_symlink() or (
            self.path.exists() and not self.path.is_file()
        ):
            raise CloudPreferencesError("Cloud usage storage is not a regular file")
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            connection.execute("PRAGMA busy_timeout = 10000")
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS daily_usage (
                    day TEXT PRIMARY KEY,
                    class_a INTEGER NOT NULL DEFAULT 0 CHECK(class_a >= 0),
                    class_b INTEGER NOT NULL DEFAULT 0 CHECK(class_b >= 0)
                );
                CREATE TABLE IF NOT EXISTS inventory (
                    id INTEGER PRIMARY KEY CHECK(id = 1),
                    scanned_at TEXT NOT NULL,
                    object_count INTEGER NOT NULL CHECK(object_count >= 0),
                    storage_bytes INTEGER NOT NULL CHECK(storage_bytes >= 0),
                    snapshots_json TEXT NOT NULL
                );
                """
            )
            connection.execute(
                "INSERT OR IGNORE INTO metadata(key, value) "
                "VALUES ('tracking_started_at', ?)",
                (datetime.now(timezone.utc).isoformat(),),
            )
            connection.commit()
            return connection
        except Exception:
            connection.close()
            raise


def usage_summary(
    preferences: CloudPreferences,
    store: CloudUsageStore,
    *,
    now: datetime | None = None,
) -> dict[str, object]:
    current = now or datetime.now(timezone.utc)
    start, end = billing_period(current.date(), preferences.billing_cycle_day)
    operations = store.usage(start, end)
    inventory = store.inventory()
    storage_bytes = int(inventory["storage_bytes"])
    class_a = int(operations["class_a"])
    class_b = int(operations["class_b"])
    return {
        **operations,
        **inventory,
        "storage_limit_bytes": preferences.storage_limit_bytes,
        "storage_remaining_bytes": max(
            0, preferences.storage_limit_bytes - storage_bytes
        ),
        "storage_overage_bytes": max(
            0, storage_bytes - preferences.storage_limit_bytes
        ),
        "storage_percent": _ratio(storage_bytes, preferences.storage_limit_bytes),
        "class_a_limit": preferences.class_a_limit,
        "class_a_remaining": max(0, preferences.class_a_limit - class_a),
        "class_a_overage": max(0, class_a - preferences.class_a_limit),
        "class_a_percent": _ratio(class_a, preferences.class_a_limit),
        "class_b_limit": preferences.class_b_limit,
        "class_b_remaining": max(0, preferences.class_b_limit - class_b),
        "class_b_overage": max(0, class_b - preferences.class_b_limit),
        "class_b_percent": _ratio(class_b, preferences.class_b_limit),
        "measurement_scope": "this_installation_namespace_and_observed_requests",
    }


def billing_period(today: date, cycle_day: int) -> tuple[date, date]:
    if cycle_day < 1 or cycle_day > 28:
        raise ValueError("Billing cycle day must be between 1 and 28")
    if today.day >= cycle_day:
        start = date(today.year, today.month, cycle_day)
    else:
        previous_month = 12 if today.month == 1 else today.month - 1
        previous_year = today.year - 1 if today.month == 1 else today.year
        start = date(previous_year, previous_month, cycle_day)
    next_month = 1 if start.month == 12 else start.month + 1
    next_year = start.year + 1 if start.month == 12 else start.year
    return start, date(next_year, next_month, cycle_day)


def directory_usage(path: Path) -> dict[str, int]:
    total = 0
    count = 0
    if not path.exists():
        return {"bytes": 0, "files": 0}
    for candidate in path.rglob("*"):
        try:
            if candidate.is_file() and not candidate.is_symlink():
                total += candidate.stat().st_size
                count += 1
        except OSError:
            continue
    return {"bytes": total, "files": count}


def _cloud_profile_dir(project_root: Path, profile_id: str) -> Path:
    if profile_id != LOCAL_PROFILE_ID and (
        len(profile_id) != 32
        or any(character not in "0123456789abcdef" for character in profile_id)
    ):
        raise ValueError("Cloud profile ID is invalid")
    return project_root / "state" / "cloud_profiles" / profile_id


def _default_preferences(cloud_configured: bool) -> CloudPreferences:
    return CloudPreferences(
        storage_mode="hybrid" if cloud_configured else "local",
        storage_limit_bytes=DEFAULT_STORAGE_LIMIT_BYTES,
        class_a_limit=DEFAULT_CLASS_A_LIMIT,
        class_b_limit=DEFAULT_CLASS_B_LIMIT,
        billing_cycle_day=DEFAULT_BILLING_CYCLE_DAY,
    )


def _validate_preferences(payload: Mapping[str, object]) -> CloudPreferences:
    mode = str(payload.get("storage_mode", "")).strip().lower()
    if mode not in STORAGE_MODES:
        raise CloudPreferencesError("Cloud preferences contain an invalid storage mode")
    try:
        storage_limit_bytes = _stored_integer(payload.get("storage_limit_bytes"))
        class_a_limit = _stored_integer(payload.get("class_a_limit"))
        class_b_limit = _stored_integer(payload.get("class_b_limit"))
        billing_cycle_day = _stored_integer(payload.get("billing_cycle_day"))
    except ValueError as exc:
        raise CloudPreferencesError("Cloud preferences contain invalid limits") from exc
    if storage_limit_bytes < 100_000_000 or storage_limit_bytes > 1_000_000_000_000_000:
        raise CloudPreferencesError("Cloud preferences contain an invalid storage limit")
    if not 1 <= class_a_limit <= 1_000_000_000_000:
        raise CloudPreferencesError("Cloud preferences contain an invalid Class A limit")
    if not 1 <= class_b_limit <= 1_000_000_000_000:
        raise CloudPreferencesError("Cloud preferences contain an invalid Class B limit")
    if not 1 <= billing_cycle_day <= 28:
        raise CloudPreferencesError("Cloud preferences contain an invalid billing cycle day")
    return CloudPreferences(
        storage_mode=mode,
        storage_limit_bytes=storage_limit_bytes,
        class_a_limit=class_a_limit,
        class_b_limit=class_b_limit,
        billing_cycle_day=billing_cycle_day,
    )


def _write_preferences(path: Path, preferences: CloudPreferences) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema_version": PREFERENCES_SCHEMA_VERSION,
        "storage_mode": preferences.storage_mode,
        "storage_limit_bytes": preferences.storage_limit_bytes,
        "class_a_limit": preferences.class_a_limit,
        "class_b_limit": preferences.class_b_limit,
        "billing_cycle_day": preferences.billing_cycle_day,
    }
    temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _validate_snapshot_summaries(value: object) -> list[dict[str, object]]:
    if not isinstance(value, list) or len(value) > 1_000:
        raise CloudPreferencesError("Cloud inventory snapshot list is invalid")
    snapshots: list[dict[str, object]] = []
    for item in value:
        if not isinstance(item, dict):
            raise CloudPreferencesError("Cloud inventory snapshot entry is invalid")
        snapshot_id = item.get("snapshot_id")
        size = item.get("size")
        modified = item.get("last_modified")
        if (
            not isinstance(snapshot_id, str)
            or len(snapshot_id) > 80
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
            or (modified is not None and not isinstance(modified, str))
        ):
            raise CloudPreferencesError("Cloud inventory snapshot entry is invalid")
        snapshots.append(
            {
                "snapshot_id": snapshot_id,
                "size": size,
                "last_modified": modified,
            }
        )
    return snapshots


def _finite_number(value: object, label: str) -> float:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be a number")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a number") from exc
    if not math.isfinite(parsed):
        raise ValueError(f"{label} must be finite")
    return parsed


def _bounded_integer(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool):
        raise ValueError(f"{label} must be an integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be an integer") from exc
    if str(value).strip() not in {str(parsed), f"{parsed}.0"}:
        raise ValueError(f"{label} must be an integer")
    if parsed < minimum or parsed > maximum:
        raise ValueError(f"{label} must be between {minimum} and {maximum}")
    return parsed


def _stored_integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("Stored value is not an integer")
    return value


def _ratio(value: int, limit: int) -> float:
    return round(value / limit * 100, 6) if limit else 0.0


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
