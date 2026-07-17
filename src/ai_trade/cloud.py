from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
import zipfile
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlsplit

from .config import AppConfig
from .json_utils import load_unique_json, loads_unique_json
from .cloud_usage import (
    CloudPreferencesError,
    CloudUsageStore,
    cloud_preferences_path,
    cloud_state_lock,
    cloud_usage_path,
    directory_usage,
    load_cloud_preferences,
    update_cloud_preferences,
    usage_summary,
)
from .data.eastmoney import load_cached_bars
from .data.market import MarketData


SCHEMA_VERSION = 1
DATASET = "market-cache"
MAX_ARCHIVE_BYTES = 128 * 1024 * 1024
MAX_MEMBER_BYTES = 64 * 1024 * 1024
MAX_MANIFEST_BYTES = 1 * 1024 * 1024
MAX_MEMBERS = 1_000
SNAPSHOT_ID_PATTERN = re.compile(r"\d{8}T\d{6}Z-[0-9a-f]{12}")
INSTALLATION_ID_PATTERN = re.compile(r"[0-9a-f]{32}")


class CloudConfigurationError(RuntimeError):
    pass


class CloudIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class CloudSettings:
    enabled: bool
    endpoint: str = field(repr=False)
    region: str
    bucket: str = field(repr=False)
    access_key_id: str = field(repr=False)
    secret_access_key: str = field(repr=False)
    prefix: str
    installation_id: str

    @property
    def configured(self) -> bool:
        return self.enabled and not self.missing_configuration

    @property
    def credentials_configured(self) -> bool:
        return not self.missing_configuration

    @property
    def missing_configuration(self) -> tuple[str, ...]:
        values = (
            ("R2 endpoint", self.endpoint),
            ("R2 bucket", self.bucket),
            ("R2 Access Key ID", self.access_key_id),
            ("R2 Secret Access Key", self.secret_access_key),
            ("installation ID", self.installation_id),
        )
        return tuple(label for label, value in values if not value)

    @property
    def namespace(self) -> str:
        if not self.installation_id:
            return ""
        return f"{self.prefix}/{self.installation_id}/v1"

    def public_status(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "configured": self.configured,
            "provider": "Cloudflare R2" if self.configured else None,
            "namespace": self.namespace or None,
            "missing_configuration": list(self.missing_configuration),
            "credentials_source": "current-user environment"
            if self.configured
            else None,
        }


@dataclass(frozen=True)
class SnapshotArtifact:
    snapshot_id: str
    dataset_sha256: str
    path: Path
    sha256: str
    size: int
    manifest: dict[str, object]


def load_cloud_settings() -> CloudSettings:
    enabled = _environment_flag("AI_TRADE_CLOUD_ENABLED", False)
    prefix = (
        os.environ.get("AI_TRADE_CLOUD_PREFIX", "ai-trade").strip().strip("/")
        or "ai-trade"
    )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,79}", prefix):
        raise CloudConfigurationError("AI_TRADE_CLOUD_PREFIX is invalid")
    if ".." in PurePosixPath(prefix).parts:
        raise CloudConfigurationError("AI_TRADE_CLOUD_PREFIX must not contain '..'")

    endpoint = os.environ.get("AI_TRADE_R2_ENDPOINT", "").strip().rstrip("/")
    if endpoint:
        _validate_r2_endpoint(endpoint)
    installation_id = (
        os.environ.get("AI_TRADE_CLOUD_INSTALLATION_ID", "").strip().lower()
    )
    if installation_id and not INSTALLATION_ID_PATTERN.fullmatch(installation_id):
        raise CloudConfigurationError(
            "AI_TRADE_CLOUD_INSTALLATION_ID must be a 32-character hexadecimal ID"
        )
    bucket = os.environ.get("AI_TRADE_R2_BUCKET", "").strip()
    if bucket and not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", bucket):
        raise CloudConfigurationError("AI_TRADE_R2_BUCKET is invalid")
    access_key_id = os.environ.get("AI_TRADE_R2_ACCESS_KEY_ID", "").strip()
    secret_access_key = os.environ.get("AI_TRADE_R2_SECRET_ACCESS_KEY", "").strip()
    for name, value in (
        ("AI_TRADE_R2_ACCESS_KEY_ID", access_key_id),
        ("AI_TRADE_R2_SECRET_ACCESS_KEY", secret_access_key),
    ):
        if any(ord(character) < 32 for character in value):
            raise CloudConfigurationError(f"{name} contains control characters")
    settings = CloudSettings(
        enabled=enabled,
        endpoint=endpoint,
        region=os.environ.get("AI_TRADE_R2_REGION", "auto").strip() or "auto",
        bucket=bucket,
        access_key_id=access_key_id,
        secret_access_key=secret_access_key,
        prefix=prefix,
        installation_id=installation_id,
    )
    return settings


def cloud_dependency_available() -> bool:
    try:
        import boto3  # noqa: F401
        import botocore  # noqa: F401
    except ImportError:
        return False
    return True


def cloud_dashboard_status(
    config: AppConfig, *, refresh: bool = False
) -> dict[str, object]:
    configuration_error: str | None = None
    try:
        settings = load_cloud_settings()
    except Exception as exc:
        settings = None
        configuration_error = safe_cloud_error(exc)

    configured = bool(settings and settings.configured)
    credentials_configured = bool(settings and settings.credentials_configured)
    dependency_available = cloud_dependency_available()
    operational = configured and dependency_available
    profile_id = _cloud_profile_id(settings)
    preferences = load_cloud_preferences(
        cloud_preferences_path(config.project_root, profile_id),
        cloud_configured=operational,
    )
    usage_store = CloudUsageStore(cloud_usage_path(config.project_root, profile_id))
    inventory_error: str | None = None
    if refresh:
        if not configured:
            inventory_error = "Cloud backup is not configured for this Windows user"
        elif not dependency_available:
            inventory_error = "Cloud support is not installed"
        else:
            try:
                R2ObjectStore(settings, usage_store=usage_store).refresh_inventory()
            except Exception as exc:
                inventory_error = safe_cloud_error(exc)
    summary = usage_summary(preferences, usage_store)
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "enabled": bool(settings and settings.enabled),
        "credentials_configured": credentials_configured,
        "configured": configured,
        "operational": operational,
        "provider": "Cloudflare R2" if configured else None,
        "dependency_available": dependency_available,
        "configuration_error": configuration_error,
        "missing_configuration": list(settings.missing_configuration)
        if settings is not None
        else [],
        "preferences": preferences.public_status(),
        "effective_storage_mode": "hybrid"
        if operational and preferences.automatic_cloud_backup
        else "local",
        "local": directory_usage(config.cache_dir),
        "usage": summary,
        "inventory_error": inventory_error,
        "official_account_usage": False,
        "usage_notice": (
            "Storage is an R2 inventory of this installation namespace. "
            "Class A/B counts are high-level requests observed by AI Trade since "
            "tracking began; they exclude other clients, pre-upgrade activity, and "
            "SDK-internal retries. Limits are user-configured budgets, not a "
            "Cloudflare billing balance."
        ),
    }


def save_cloud_dashboard_preferences(
    config: AppConfig, payload: dict[str, object]
) -> dict[str, object]:
    settings = load_cloud_settings()
    operational = settings.configured and cloud_dependency_available()
    profile_id = _cloud_profile_id(settings)
    update_cloud_preferences(
        cloud_preferences_path(config.project_root, profile_id),
        payload,
        cloud_configured=operational,
    )
    return cloud_dashboard_status(config)


def automatic_cloud_backup_enabled(config: AppConfig) -> bool:
    settings = load_cloud_settings()
    if not settings.configured or not cloud_dependency_available():
        return False
    profile_id = _cloud_profile_id(settings)
    preferences = load_cloud_preferences(
        cloud_preferences_path(config.project_root, profile_id), cloud_configured=True
    )
    return preferences.automatic_cloud_backup


def tracked_r2_store(config: AppConfig, settings: CloudSettings) -> "R2ObjectStore":
    profile_id = _cloud_profile_id(settings)
    return R2ObjectStore(
        settings,
        usage_store=CloudUsageStore(
            cloud_usage_path(config.project_root, profile_id)
        ),
    )


def _cloud_profile_id(settings: CloudSettings | None) -> str:
    if settings is None or not settings.credentials_configured:
        return "local"
    coordinates = "\0".join(
        (
            settings.endpoint.lower(),
            settings.region.lower(),
            settings.bucket,
            settings.prefix,
            settings.installation_id,
        )
    )
    return hashlib.sha256(coordinates.encode("utf-8")).hexdigest()[:32]


def safe_cloud_error(exc: Exception) -> str:
    if isinstance(
        exc,
        (
            CloudConfigurationError,
            CloudIntegrityError,
            CloudPreferencesError,
            FileExistsError,
            FileNotFoundError,
            ValueError,
        ),
    ):
        return _redact_cloud_error(str(exc))
    code = type(exc).__name__
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        error = response.get("Error", {})
        if isinstance(error, dict) and re.fullmatch(
            r"[A-Za-z0-9_.-]{1,80}", str(error.get("Code", ""))
        ):
            code = str(error["Code"])
    return _redact_cloud_error(
        f"Cloud provider request failed ({code}); credentials and endpoint were redacted"
    )


def _redact_cloud_error(message: str) -> str:
    redacted = re.sub(r"(?i)https://[^\s'\"<>]+", "<redacted endpoint>", message)
    for name, label in (
        ("AI_TRADE_R2_BUCKET", "bucket"),
        ("AI_TRADE_R2_ACCESS_KEY_ID", "access key"),
        ("AI_TRADE_R2_SECRET_ACCESS_KEY", "secret key"),
    ):
        value = os.environ.get(name, "")
        if value:
            redacted = redacted.replace(value, f"<redacted {label}>")
    return re.sub(
        r"(?i)\b(object[ _-]?key|key)\s*[:=]\s*(?:'[^']*'|\"[^\"]*\"|[^\s,;]+)",
        r"\1=<redacted key>",
        redacted,
    )


class R2ObjectStore:
    def __init__(
        self,
        settings: CloudSettings,
        client: Any | None = None,
        usage_store: CloudUsageStore | None = None,
    ):
        if not settings.configured:
            raise CloudConfigurationError(
                "Cloud backup is not configured for this user"
            )
        self.settings = settings
        self._client_value = client
        self.usage_store = usage_store

    def client(self) -> Any:
        if self._client_value is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:
                raise CloudConfigurationError(
                    "Cloud support is not installed; run: python -m pip install 'ai-trade[cloud]'"
                ) from exc
            self._client_value = boto3.client(
                "s3",
                endpoint_url=self.settings.endpoint,
                region_name=self.settings.region,
                aws_access_key_id=self.settings.access_key_id,
                aws_secret_access_key=self.settings.secret_access_key,
                config=Config(
                    signature_version="s3v4",
                    connect_timeout=5,
                    read_timeout=30,
                    retries={"max_attempts": 4, "mode": "standard"},
                ),
            )
        return self._client_value

    def _request(self, operation_class: str, method: str, **request: object) -> Any:
        client = self.client()
        if self.usage_store is not None:
            # Count the high-level request before dispatch so provider-side failures
            # are not silently presented as free operations.
            self.usage_store.record(operation_class)
        return getattr(client, method)(**request)

    def _operation_lock(self):
        if self.usage_store is None:
            return nullcontext()
        return cloud_state_lock(self.usage_store.path.parent)

    def check_connection(self) -> None:
        with self._operation_lock():
            self._request(
                "class_a",
                "list_objects_v2",
                Bucket=self.settings.bucket,
                Prefix=f"{self.settings.namespace}/",
                MaxKeys=1,
            )

    def upload_snapshot(self, artifact: SnapshotArtifact) -> dict[str, object]:
        with self._operation_lock():
            return self._upload_snapshot_locked(artifact)

    def _upload_snapshot_locked(
        self, artifact: SnapshotArtifact
    ) -> dict[str, object]:
        previous = self._latest_pointer()
        if previous and previous.get("dataset_sha256") == artifact.dataset_sha256:
            duplicate = self._verified_duplicate(previous, artifact)
            if duplicate is not None:
                return duplicate | {"skipped_duplicate": True}
        key = self._snapshot_key(artifact.snapshot_id)
        metadata = {
            "sha256": artifact.sha256,
            "dataset-sha256": artifact.dataset_sha256,
            "schema-version": str(SCHEMA_VERSION),
            "dataset": DATASET,
        }
        with artifact.path.open("rb") as handle:
            self._request(
                "class_a",
                "put_object",
                Bucket=self.settings.bucket,
                Key=key,
                Body=handle,
                ContentType="application/zip",
                Metadata=metadata,
            )
        head = self._request(
            "class_b", "head_object", Bucket=self.settings.bucket, Key=key
        )
        if int(head.get("ContentLength", -1)) != artifact.size:
            raise CloudIntegrityError(
                "R2 snapshot size does not match the uploaded archive"
            )
        remote_digest = _metadata(head).get("sha256")
        if remote_digest != artifact.sha256:
            raise CloudIntegrityError(
                "R2 snapshot checksum metadata is missing or incorrect"
            )

        pointer = {
            "schema_version": SCHEMA_VERSION,
            "dataset": DATASET,
            "snapshot_id": artifact.snapshot_id,
            "object_key": key,
            "sha256": artifact.sha256,
            "dataset_sha256": artifact.dataset_sha256,
            "size": artifact.size,
            "created_at": artifact.manifest["created_at"],
            "latest_common_session": artifact.manifest["market"][
                "latest_common_session"
            ],
            "skipped_duplicate": False,
        }
        self._request(
            "class_a",
            "put_object",
            Bucket=self.settings.bucket,
            Key=f"{self.settings.namespace}/indexes/{DATASET}/latest.json",
            Body=json.dumps(pointer, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            ),
            ContentType="application/json",
            Metadata={"schema-version": str(SCHEMA_VERSION)},
        )
        return pointer

    def _verified_duplicate(
        self, pointer: dict[str, object], artifact: SnapshotArtifact
    ) -> dict[str, object] | None:
        snapshot_id = pointer.get("snapshot_id")
        object_key = pointer.get("object_key")
        digest = pointer.get("sha256")
        size = pointer.get("size")
        if (
            not isinstance(snapshot_id, str)
            or not SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id)
            or object_key != self._snapshot_key(snapshot_id)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 1
            or pointer.get("dataset") != DATASET
            or pointer.get("schema_version") != SCHEMA_VERSION
        ):
            return None
        try:
            head = self._request(
                "class_b",
                "head_object",
                Bucket=self.settings.bucket,
                Key=object_key,
            )
        except KeyError:
            return None
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise
        metadata = _metadata(head)
        if (
            int(head.get("ContentLength", -1)) != size
            or metadata.get("sha256") != digest
            or metadata.get("dataset-sha256") != artifact.dataset_sha256
        ):
            return None
        return pointer

    def _latest_pointer(self) -> dict[str, object] | None:
        key = f"{self.settings.namespace}/indexes/{DATASET}/latest.json"
        try:
            head = self._request(
                "class_b", "head_object", Bucket=self.settings.bucket, Key=key
            )
            size = int(head.get("ContentLength", -1))
            if size < 1 or size > 64 * 1024:
                raise CloudIntegrityError("Cloud latest pointer has an invalid size")
            response = self._request(
                "class_b", "get_object", Bucket=self.settings.bucket, Key=key
            )
        except KeyError:
            return None
        except Exception as exc:
            if _is_not_found(exc):
                return None
            raise
        body = response["Body"]
        try:
            content = body.read(64 * 1024 + 1)
        finally:
            close = getattr(body, "close", None)
            if close:
                close()
        if len(content) != size or len(content) > 64 * 1024:
            raise CloudIntegrityError("Cloud latest pointer content is invalid")
        try:
            value = loads_unique_json(content.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CloudIntegrityError("Cloud latest pointer is invalid JSON") from exc
        if not isinstance(value, dict):
            raise CloudIntegrityError("Cloud latest pointer has an invalid structure")
        return value

    def list_snapshots(self, limit: int = 20) -> list[dict[str, object]]:
        with self._operation_lock():
            return self._list_snapshots_locked(limit)

    def _list_snapshots_locked(self, limit: int) -> list[dict[str, object]]:
        if limit < 1 or limit > 1_000:
            raise ValueError("Cloud snapshot limit must be between 1 and 1000")
        prefix = f"{self.settings.namespace}/snapshots/{DATASET}/"
        objects: list[dict[str, object]] = []
        token: str | None = None
        seen_tokens: set[str] = set()
        while True:
            request: dict[str, object] = {
                "Bucket": self.settings.bucket,
                "Prefix": prefix,
                "MaxKeys": 1_000,
            }
            if token:
                request["ContinuationToken"] = token
            response = self._request("class_a", "list_objects_v2", **request)
            for item in response.get("Contents", []):
                key = str(item.get("Key", ""))
                name = PurePosixPath(key).name
                snapshot_id = name.removesuffix(".zip")
                if not name.endswith(".zip") or not SNAPSHOT_ID_PATTERN.fullmatch(
                    snapshot_id
                ):
                    continue
                modified = item.get("LastModified")
                objects.append(
                    {
                        "snapshot_id": snapshot_id,
                        "size": int(item.get("Size", 0) or 0),
                        "last_modified": modified.isoformat()
                        if hasattr(modified, "isoformat")
                        else None,
                        "object_key": key,
                    }
                )
            token = _next_continuation_token(
                response, seen_tokens, "Cloud snapshot listing"
            )
            if token is None:
                break
        objects.sort(key=lambda item: str(item["snapshot_id"]), reverse=True)
        return objects[:limit]

    def refresh_inventory(self) -> dict[str, object]:
        if self.usage_store is None:
            raise CloudConfigurationError(
                "Cloud inventory requires a local usage store"
            )
        with self._operation_lock():
            prefix = f"{self.settings.namespace}/"
            snapshot_prefix = f"{self.settings.namespace}/snapshots/{DATASET}/"
            token: str | None = None
            seen_tokens: set[str] = set()
            object_count = 0
            storage_bytes = 0
            snapshots: list[dict[str, object]] = []
            while True:
                request: dict[str, object] = {
                    "Bucket": self.settings.bucket,
                    "Prefix": prefix,
                    "MaxKeys": 1_000,
                }
                if token:
                    request["ContinuationToken"] = token
                response = self._request("class_a", "list_objects_v2", **request)
                for item in response.get("Contents", []):
                    key = str(item.get("Key", ""))
                    size = int(item.get("Size", 0) or 0)
                    object_count += 1
                    storage_bytes += max(0, size)
                    if not key.startswith(snapshot_prefix):
                        continue
                    name = PurePosixPath(key).name
                    snapshot_id = name.removesuffix(".zip")
                    if not name.endswith(".zip") or not SNAPSHOT_ID_PATTERN.fullmatch(
                        snapshot_id
                    ):
                        continue
                    modified = item.get("LastModified")
                    snapshots.append(
                        {
                            "snapshot_id": snapshot_id,
                            "size": max(0, size),
                            "last_modified": modified.isoformat()
                            if hasattr(modified, "isoformat")
                            else None,
                        }
                    )
                token = _next_continuation_token(
                    response, seen_tokens, "Cloud inventory"
                )
                if token is None:
                    break
            snapshots.sort(
                key=lambda item: str(item["snapshot_id"]), reverse=True
            )
            return self.usage_store.save_inventory(
                object_count=object_count,
                storage_bytes=storage_bytes,
                snapshots=snapshots[:1_000],
            )

    def restore_snapshot(
        self, config: AppConfig, snapshot_id: str, destination: Path
    ) -> Path:
        with self._operation_lock():
            return self._restore_snapshot_locked(config, snapshot_id, destination)

    def _restore_snapshot_locked(
        self, config: AppConfig, snapshot_id: str, destination: Path
    ) -> Path:
        if not SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id):
            raise ValueError("Snapshot ID is invalid")
        key = self._snapshot_key(snapshot_id)
        destination = destination.expanduser().resolve()
        destination.mkdir(parents=True, exist_ok=False)
        try:
            with tempfile.TemporaryDirectory(
                prefix="ai-trade-cloud-download-"
            ) as temporary:
                archive = Path(temporary) / "snapshot.cloudpart"
                self._download_verified(key, archive)
                _extract_verified_snapshot(
                    archive, destination, config, expected_snapshot_id=snapshot_id
                )
            return destination
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise

    def _download_verified(self, key: str, destination: Path) -> None:
        head = self._request(
            "class_b", "head_object", Bucket=self.settings.bucket, Key=key
        )
        size = int(head.get("ContentLength", -1))
        if size < 1 or size > MAX_ARCHIVE_BYTES:
            raise CloudIntegrityError(
                "Cloud snapshot size is outside the permitted range"
            )
        expected = _metadata(head).get("sha256")
        if not expected or not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise CloudIntegrityError("Cloud snapshot has no valid SHA-256 metadata")
        response = self._request(
            "class_b", "get_object", Bucket=self.settings.bucket, Key=key
        )
        body = response["Body"]
        digest = hashlib.sha256()
        total = 0
        try:
            with destination.open("xb") as handle:
                while True:
                    chunk = body.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_ARCHIVE_BYTES:
                        raise CloudIntegrityError(
                            "Cloud snapshot exceeded the download limit"
                        )
                    digest.update(chunk)
                    handle.write(chunk)
        finally:
            close = getattr(body, "close", None)
            if close:
                close()
        if total != size or digest.hexdigest() != expected:
            raise CloudIntegrityError(
                "Cloud snapshot content failed SHA-256 verification"
            )

    def _snapshot_key(self, snapshot_id: str) -> str:
        if not SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id):
            raise ValueError("Snapshot ID is invalid")
        year, month, day = snapshot_id[:4], snapshot_id[4:6], snapshot_id[6:8]
        return (
            f"{self.settings.namespace}/snapshots/{DATASET}/"
            f"{year}/{month}/{day}/{snapshot_id}.zip"
        )


def create_market_snapshot(config: AppConfig, destination: Path) -> SnapshotArtifact:
    MarketData(config)
    files = _market_files(config)
    if len(files) + 1 > MAX_MEMBERS:
        raise CloudIntegrityError("Market snapshot contains too many files")
    payloads: dict[str, bytes] = {}
    payload_size = 0
    for path in files:
        remaining = MAX_ARCHIVE_BYTES - payload_size
        payload = _read_stable_file(path, max_bytes=min(MAX_MEMBER_BYTES, remaining))
        payloads[path.name] = payload
        payload_size += len(payload)
    source_manifest = _validate_source_snapshot(config, payloads)
    source_manifest = _cloud_safe_source_manifest(config, source_manifest, payloads)
    payloads["manifest.json"] = (
        json.dumps(source_manifest, ensure_ascii=False, indent=2).encode("utf-8")
        + b"\n"
    )
    source_manifest = _validate_source_snapshot(config, payloads)
    file_entries = [
        {
            "path": f"data/cache/{path.name}",
            "size": len(payloads[path.name]),
            "sha256": hashlib.sha256(payloads[path.name]).hexdigest(),
        }
        for path in files
    ]
    dataset_digest = hashlib.sha256(
        json.dumps(file_entries, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    created_at = datetime.now(timezone.utc).replace(microsecond=0)
    snapshot_id = f"{created_at:%Y%m%dT%H%M%SZ}-{dataset_digest[:12]}"
    manifest: dict[str, object] = {
        "schema_version": SCHEMA_VERSION,
        "dataset": DATASET,
        "snapshot_id": snapshot_id,
        "created_at": created_at.isoformat(),
        "ai_trade_version": _package_version(),
        "dataset_sha256": dataset_digest,
        "config_sha256": _config_fingerprint(config),
        "market": {
            "provider": source_manifest["provider"],
            "adjustment": source_manifest["adjustment"],
            "requested_through": source_manifest["requested_through"],
            "completed_session_cutoff": source_manifest[
                "completed_session_cutoff"
            ],
            "completed_through": source_manifest["completed_through"],
            "latest_common_session": source_manifest["latest_common_session"],
            "universe": config.universe_name,
            "symbols": sorted(item.symbol for item in config.instruments),
            "source_manifest": source_manifest,
        },
        "files": file_entries,
        "excluded": ["state", "logs", "reports", ".env", "broker credentials"],
    }
    manifest_bytes = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    _validate_snapshot_size_limits(
        [len(content) for content in payloads.values()] + [len(manifest_bytes)],
        manifest_size=len(manifest_bytes),
        context="Market snapshot",
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in files:
            archive.writestr(f"data/cache/{path.name}", payloads[path.name])
        archive.writestr(
            "snapshot-manifest.json",
            manifest_bytes,
        )
    size = destination.stat().st_size
    if size > MAX_ARCHIVE_BYTES:
        destination.unlink(missing_ok=True)
        raise CloudIntegrityError("Market snapshot exceeds the permitted archive size")
    return SnapshotArtifact(
        snapshot_id,
        dataset_digest,
        destination,
        _file_sha256(destination),
        size,
        manifest,
    )


def backup_market_cache(config: AppConfig, store: R2ObjectStore) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="ai-trade-cloud-") as temporary:
        path = Path(temporary) / "market-cache.zip"
        artifact = create_market_snapshot(config, path)
        return store.upload_snapshot(artifact)


def _market_files(config: AppConfig) -> list[Path]:
    root = config.cache_dir.resolve()
    names = [f"{item.symbol}.csv" for item in config.instruments] + ["manifest.json"]
    files: list[Path] = []
    for name in sorted(names):
        path = (root / name).resolve()
        if path.parent != root or path.is_symlink() or not path.is_file():
            raise CloudIntegrityError(
                f"Required market cache file is unsafe or missing: {name}"
            )
        files.append(path)
    return files


def _read_stable_file(path: Path, *, max_bytes: int) -> bytes:
    before = path.stat()
    if before.st_size > max_bytes:
        raise CloudIntegrityError(
            f"Market snapshot exceeds the permitted size while reading: {path.name}"
        )
    with path.open("rb") as handle:
        content = handle.read(max_bytes + 1)
    if len(content) > max_bytes:
        raise CloudIntegrityError(
            f"Market snapshot exceeds the permitted size while reading: {path.name}"
        )
    after = path.stat()
    if (
        before.st_size != after.st_size
        or before.st_mtime_ns != after.st_mtime_ns
        or len(content) != after.st_size
    ):
        raise CloudIntegrityError(
            f"Market cache changed while creating a snapshot: {path.name}"
        )
    return content


def _validate_source_snapshot(
    config: AppConfig, payloads: dict[str, bytes]
) -> dict[str, Any]:
    try:
        raw_manifest = payloads["manifest.json"]
        if len(raw_manifest) > MAX_MANIFEST_BYTES:
            raise ValueError("market manifest exceeds the permitted size")
        manifest = loads_unique_json(raw_manifest.decode("utf-8"))
    except (KeyError, TypeError, UnicodeDecodeError, ValueError) as exc:
        raise CloudIntegrityError("Market manifest is incomplete or invalid") from exc
    _validate_market_manifest(
        config,
        manifest,
        lambda symbol: hashlib.sha256(payloads[f"{symbol}.csv"]).hexdigest(),
    )
    return manifest


def _cloud_safe_source_manifest(
    config: AppConfig,
    manifest: dict[str, Any],
    payloads: dict[str, bytes],
) -> dict[str, object]:
    facts = _market_payload_facts(config, payloads)
    latest_common = facts["latest_common_session"]
    if not isinstance(latest_common, str):
        raise CloudIntegrityError("Market snapshot common session is invalid")
    requested = _declared_session(
        manifest,
        ("requested_through", "completed_session_cutoff"),
        fallback=latest_common,
    )
    if date.fromisoformat(requested) < date.fromisoformat(latest_common):
        raise CloudIntegrityError(
            "Market manifest request cutoff precedes the actual common session"
        )
    for field_name in ("completed_through", "latest_common_session"):
        declared = manifest.get(field_name)
        if declared is not None and declared != latest_common:
            raise CloudIntegrityError(
                f"Market manifest {field_name} does not match the cached CSV files"
            )

    raw_files = manifest["files"]
    safe_files: dict[str, dict[str, object]] = {}
    for instrument in config.instruments:
        symbol = instrument.symbol
        raw = raw_files[symbol]
        actual_rows = facts["rows"][symbol]
        actual_latest = facts["latest_sessions"][symbol]
        if raw.get("rows") is not None and raw["rows"] != actual_rows:
            raise CloudIntegrityError(
                f"Market manifest row count mismatch: {symbol}"
            )
        if raw.get("latest_session") is not None and raw["latest_session"] != actual_latest:
            raise CloudIntegrityError(
                f"Market manifest latest session mismatch: {symbol}"
            )
        source = raw.get("source", "unknown")
        if source not in {
            "network",
            "tencent_network_fallback",
            "validated_local_fallback",
            "test-fixture",
        }:
            source = "unknown"
        safe: dict[str, object] = {
            "rows": actual_rows,
            "sha256": raw["sha256"],
            "source": source,
            "latest_session": actual_latest,
            "network_errors": _safe_provider_errors(raw.get("network_errors")),
            "fallback_reason": _safe_fallback_reason(raw.get("fallback_reason")),
        }
        _copy_safe_provider_metadata(raw, safe)
        safe_files[symbol] = safe

    downloaded_at = manifest.get("downloaded_at")
    if not isinstance(downloaded_at, str):
        downloaded_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    else:
        try:
            datetime.fromisoformat(downloaded_at)
        except ValueError as exc:
            raise CloudIntegrityError(
                "Market manifest download timestamp is invalid"
            ) from exc

    requested_from = manifest.get(
        "requested_from", config.raw["data"]["start"]
    )
    try:
        date.fromisoformat(str(requested_from))
    except ValueError as exc:
        raise CloudIntegrityError("Market manifest start date is invalid") from exc

    return {
        "provider": config.raw["data"]["provider"],
        "adjustment": config.raw["data"].get("adjustment", "none"),
        "downloaded_at": downloaded_at,
        "requested_from": str(requested_from),
        "requested_through": requested,
        "completed_session_cutoff": requested,
        "completed_through": latest_common,
        "latest_common_session": latest_common,
        "request_policy": _safe_request_policy(config, manifest.get("request_policy")),
        "files": safe_files,
    }


def _market_payload_facts(
    config: AppConfig, payloads: dict[str, bytes]
) -> dict[str, object]:
    dates: dict[str, list[date]] = {}
    for instrument in config.instruments:
        symbol = instrument.symbol
        name = f"{symbol}.csv"
        try:
            text = payloads[name].decode("utf-8")
        except (KeyError, UnicodeDecodeError) as exc:
            raise CloudIntegrityError(f"Market CSV is invalid: {symbol}") from exc
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or not {
            "date",
            "open",
            "close",
            "high",
            "low",
            "volume",
            "amount",
        }.issubset(reader.fieldnames):
            raise CloudIntegrityError(f"Market CSV schema is invalid: {symbol}")
        parsed: list[date] = []
        for row in reader:
            try:
                value = date.fromisoformat(row["date"])
            except (KeyError, TypeError, ValueError) as exc:
                raise CloudIntegrityError(
                    f"Market CSV date is invalid: {symbol}"
                ) from exc
            if parsed and value <= parsed[-1]:
                raise CloudIntegrityError(
                    f"Market CSV dates are not strictly increasing: {symbol}"
                )
            parsed.append(value)
        if not parsed:
            raise CloudIntegrityError(f"Market CSV has no rows: {symbol}")
        dates[symbol] = parsed

    benchmark = config.strategy.benchmark
    reference = dates[benchmark][-1]
    active = set(config.active_symbols(reference))
    active.add(benchmark)
    common = set(dates[benchmark])
    for symbol in sorted(active - {benchmark}):
        common.intersection_update(dates[symbol])
    if not common:
        raise CloudIntegrityError("Market CSV files have no common session")
    return {
        "latest_common_session": max(common).isoformat(),
        "rows": {symbol: len(values) for symbol, values in dates.items()},
        "latest_sessions": {
            symbol: values[-1].isoformat() for symbol, values in dates.items()
        },
    }


def _declared_session(
    manifest: dict[str, Any], fields: tuple[str, ...], *, fallback: str
) -> str:
    values = [manifest[field] for field in fields if manifest.get(field) is not None]
    if not values:
        return fallback
    if any(not isinstance(value, str) for value in values) or len(set(values)) != 1:
        raise CloudIntegrityError("Market manifest request cutoff is inconsistent")
    try:
        date.fromisoformat(values[0])
    except ValueError as exc:
        raise CloudIntegrityError("Market manifest request cutoff is invalid") from exc
    return values[0]


def _safe_provider_errors(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    for item in value[:100]:
        match = re.search(
            r"\b([A-Za-z][A-Za-z0-9_.]{1,79}(?:Error|Exception|Timeout|Disconnected))\b",
            str(item),
        )
        error_type = match.group(1) if match else "ProviderError"
        result.append(f"provider_error: {error_type}")
    return result


def _safe_fallback_reason(value: object) -> str | None:
    if value is None:
        return None
    errors = _safe_provider_errors([value])
    return f"Provider fallback after {errors[0]}" if errors else "Provider fallback"


def _safe_request_policy(config: AppConfig, value: object) -> dict[str, object]:
    raw = value if isinstance(value, dict) else {}
    circuit_raw = raw.get("eastmoney_circuit_breaker", {})
    circuit_raw = circuit_raw if isinstance(circuit_raw, dict) else {}
    trigger = circuit_raw.get("trigger_symbol")
    symbols = {item.symbol for item in config.instruments}
    if trigger not in symbols:
        trigger = None
    reason = _safe_provider_errors([circuit_raw.get("reason")])
    return {
        "mode": "serial",
        "proxy_mode": raw.get("proxy_mode")
        if raw.get("proxy_mode") in {"system", "direct"}
        else config.raw["data"].get("proxy_mode", "system"),
        "fallback_provider": raw.get("fallback_provider")
        if raw.get("fallback_provider") in {"tencent", "none"}
        else config.raw["data"].get("fallback_provider", "tencent"),
        "eastmoney_circuit_breaker": {
            "opened": bool(circuit_raw.get("opened", False)),
            "trigger_symbol": trigger,
            "reason": reason[0] if reason else None,
        },
        **{
            name: config.raw["data"].get(name, default)
            for name, default in {
                "timeout_seconds": 20,
                "request_interval_seconds": 2.0,
                "request_jitter_seconds": 0.5,
                "failure_cooldown_seconds": 20.0,
                "max_attempts": 4,
                "retry_base_seconds": 1.0,
                "retry_max_seconds": 8.0,
                "retry_jitter_seconds": 0.5,
            }.items()
        },
    }


def _copy_safe_provider_metadata(
    raw: dict[str, Any], destination: dict[str, object]
) -> None:
    allowed_strings = {
        "source_provider": {"tencent_newfqkline"},
        "source_mode": {
            "incremental",
            "full_history",
            "full_rebuild_after_overlap_mismatch",
        },
        "tencent_proxy_mode": {"system", "direct"},
        "amount_quality": {"provider_reported_rounded"},
    }
    for name, allowed in allowed_strings.items():
        if raw.get(name) in allowed:
            destination[name] = raw[name]
    for name in (
        "pages",
        "overlap_rows",
        "retained_cached_rows",
        "cached_seed_rows",
        "amount_resolution_cny",
        "amount_max_rounding_error_cny",
    ):
        value = raw.get(name)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
            destination[name] = value
    cached_seed_source = raw.get("cached_seed_source")
    if cached_seed_source in {
        "network",
        "tencent_network_fallback",
        "validated_local_fallback",
        "unknown",
    }:
        destination["cached_seed_source"] = cached_seed_source
    cached_seed_sha256 = raw.get("cached_seed_sha256")
    if isinstance(cached_seed_sha256, str) and re.fullmatch(
        r"[0-9a-f]{64}", cached_seed_sha256
    ):
        destination["cached_seed_sha256"] = cached_seed_sha256
    if isinstance(raw.get("latest_amount_exact_override"), bool):
        destination["latest_amount_exact_override"] = raw[
            "latest_amount_exact_override"
        ]


def _validate_restored_source_snapshot(
    config: AppConfig, cache: Path
) -> dict[str, Any]:
    try:
        manifest = load_unique_json(
            cache / "manifest.json",
            max_bytes=MAX_MANIFEST_BYTES,
        )
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        raise CloudIntegrityError("Restored market manifest is invalid") from exc
    _validate_market_manifest(
        config, manifest, lambda symbol: _file_sha256(cache / f"{symbol}.csv")
    )
    payloads = {
        f"{item.symbol}.csv": (cache / f"{item.symbol}.csv").read_bytes()
        for item in config.instruments
    }
    payloads["manifest.json"] = (cache / "manifest.json").read_bytes()
    if _cloud_safe_source_manifest(config, manifest, payloads) != manifest:
        raise CloudIntegrityError(
            "Restored market manifest is not in the safe export schema"
        )
    return manifest


def _validate_market_manifest(
    config: AppConfig,
    manifest: dict[str, Any],
    digest_for_symbol,
) -> None:
    try:
        if not isinstance(manifest, dict):
            raise CloudIntegrityError("Market manifest has an invalid structure")
        if manifest["provider"] != config.raw["data"]["provider"]:
            raise CloudIntegrityError(
                "Market manifest provider does not match configuration"
            )
        if manifest["adjustment"] != config.raw["data"].get("adjustment", "none"):
            raise CloudIntegrityError(
                "Market manifest adjustment does not match configuration"
            )
        files = manifest["files"]
        if not isinstance(files, dict):
            raise CloudIntegrityError("Market manifest files have an invalid structure")
        symbols = {item.symbol for item in config.instruments}
        if set(files) != symbols:
            raise CloudIntegrityError(
                "Market manifest symbols do not match configuration"
            )
        for symbol in symbols:
            source = files[symbol]
            if not isinstance(source, dict):
                raise CloudIntegrityError(
                    f"Market manifest file entry is invalid: {symbol}"
                )
            checksum = source["sha256"]
            if (
                not isinstance(checksum, str)
                or not re.fullmatch(r"[0-9a-f]{64}", checksum)
                or checksum != digest_for_symbol(symbol)
            ):
                raise CloudIntegrityError(
                    f"Market manifest checksum mismatch: {symbol}"
                )
            rows = source.get("rows")
            if rows is not None and (
                isinstance(rows, bool) or not isinstance(rows, int) or rows < 0
            ):
                raise CloudIntegrityError(
                    f"Market manifest row count is invalid: {symbol}"
                )
    except (KeyError, TypeError) as exc:
        raise CloudIntegrityError("Market manifest is incomplete or invalid") from exc


def _extract_verified_snapshot(
    archive_path: Path,
    destination: Path,
    config: AppConfig,
    *,
    expected_snapshot_id: str,
) -> None:
    try:
        archive_context = zipfile.ZipFile(archive_path)
    except (OSError, zipfile.BadZipFile) as exc:
        raise CloudIntegrityError("Cloud snapshot is not a valid ZIP archive") from exc
    with archive_context as archive:
        members = _preflight_archive(archive)
        manifest_member = members["snapshot-manifest.json"]
        with archive.open(manifest_member) as source:
            raw_manifest = source.read(MAX_MANIFEST_BYTES + 1)
        if len(raw_manifest) != manifest_member.file_size:
            raise CloudIntegrityError("Cloud snapshot manifest size is invalid")
        try:
            manifest = loads_unique_json(raw_manifest.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise CloudIntegrityError(
                "Cloud snapshot manifest is invalid JSON"
            ) from exc
        _validate_snapshot_manifest(
            manifest, config, members, expected_snapshot_id=expected_snapshot_id
        )
        expected_files = {item["path"]: item for item in manifest["files"]}
        for name, expected in expected_files.items():
            target = _safe_destination(destination, name)
            target.parent.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            total = 0
            with archive.open(name) as source, target.open("xb") as output:
                while True:
                    chunk = source.read(256 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > expected["size"]:
                        raise CloudIntegrityError(
                            f"Snapshot member size mismatch: {name}"
                        )
                    digest.update(chunk)
                    output.write(chunk)
            if total != expected["size"]:
                raise CloudIntegrityError(f"Snapshot member size mismatch: {name}")
            if digest.hexdigest() != expected["sha256"]:
                raise CloudIntegrityError(f"Snapshot member checksum mismatch: {name}")
    cache = destination / "data" / "cache"
    restored_source_manifest = _validate_restored_source_snapshot(config, cache)
    embedded_source_manifest = manifest["market"]["source_manifest"]
    if restored_source_manifest != embedded_source_manifest:
        raise CloudIntegrityError(
            "Restored market manifest does not match the snapshot source manifest"
        )
    for item in config.instruments:
        bars = load_cached_bars(cache / f"{item.symbol}.csv")
        source = restored_source_manifest["files"][item.symbol]
        if source.get("rows") is not None and int(source["rows"]) != len(bars):
            raise CloudIntegrityError(
                f"Restored market row count mismatch: {item.symbol}"
            )


def _validate_snapshot_manifest(
    manifest: dict[str, Any],
    config: AppConfig,
    archive_members: dict[str, zipfile.ZipInfo],
    *,
    expected_snapshot_id: str,
) -> None:
    try:
        if not isinstance(manifest, dict):
            raise CloudIntegrityError(
                "Cloud snapshot manifest has an invalid structure"
            )
        if (
            manifest["schema_version"] != SCHEMA_VERSION
            or manifest["dataset"] != DATASET
        ):
            raise CloudIntegrityError("Cloud snapshot schema or dataset is unsupported")
        if manifest["snapshot_id"] != expected_snapshot_id:
            raise CloudIntegrityError(
                "Cloud snapshot manifest ID does not match the request"
            )
        market = manifest["market"]
        if not isinstance(market, dict):
            raise CloudIntegrityError("Cloud snapshot market metadata is invalid")
        if market["provider"] != config.raw["data"]["provider"]:
            raise CloudIntegrityError(
                "Cloud snapshot provider does not match configuration"
            )
        if market["adjustment"] != config.raw["data"].get("adjustment", "none"):
            raise CloudIntegrityError(
                "Cloud snapshot adjustment does not match configuration"
            )
        expected_symbols = sorted(item.symbol for item in config.instruments)
        symbols = market["symbols"]
        if not isinstance(symbols, list) or sorted(symbols) != expected_symbols:
            raise CloudIntegrityError(
                "Cloud snapshot symbols do not match configuration"
            )
        files = manifest["files"]
        if not isinstance(files, list):
            raise CloudIntegrityError("Cloud snapshot file metadata is invalid")
        entries: dict[str, dict[str, Any]] = {}
        for item in files:
            if not isinstance(item, dict) or not isinstance(item.get("path"), str):
                raise CloudIntegrityError("Cloud snapshot file entry is invalid")
            path = item["path"]
            if path in entries:
                raise CloudIntegrityError(
                    "Cloud snapshot contains duplicate file metadata"
                )
            size = item.get("size")
            if isinstance(size, bool) or not isinstance(size, int):
                raise CloudIntegrityError("Cloud snapshot member has an invalid size")
            if not 0 <= size <= MAX_MEMBER_BYTES:
                raise CloudIntegrityError("Cloud snapshot member has an invalid size")
            checksum = item.get("sha256")
            if not isinstance(checksum, str) or not re.fullmatch(
                r"[0-9a-f]{64}", checksum
            ):
                raise CloudIntegrityError(
                    "Cloud snapshot member has an invalid checksum"
                )
            entries[path] = item
        expected_names = {"snapshot-manifest.json", *entries}
        if set(archive_members) != expected_names:
            raise CloudIntegrityError("Cloud snapshot archive and manifest disagree")
        required = {
            "data/cache/manifest.json",
            *(f"data/cache/{s}.csv" for s in expected_symbols),
        }
        if set(entries) != required:
            raise CloudIntegrityError(
                "Cloud snapshot does not contain the required cache files"
            )
        for path, item in entries.items():
            if archive_members[path].file_size != item["size"]:
                raise CloudIntegrityError(
                    f"Cloud snapshot declared size does not match ZIP metadata: {path}"
                )
        _validate_snapshot_size_limits(
            [member.file_size for member in archive_members.values()],
            manifest_size=archive_members["snapshot-manifest.json"].file_size,
            context="Cloud snapshot",
        )
        dataset_digest = hashlib.sha256(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        if manifest["dataset_sha256"] != dataset_digest:
            raise CloudIntegrityError("Cloud snapshot dataset checksum is invalid")
        if expected_snapshot_id.rsplit("-", 1)[-1] != dataset_digest[:12]:
            raise CloudIntegrityError(
                "Cloud snapshot ID does not match its dataset checksum"
            )
        source_manifest = market["source_manifest"]
        _validate_market_manifest(
            config,
            source_manifest,
            lambda symbol: entries[f"data/cache/{symbol}.csv"]["sha256"],
        )
        for field_name in (
            "requested_through",
            "completed_session_cutoff",
            "completed_through",
            "latest_common_session",
        ):
            if market.get(field_name) != source_manifest.get(field_name):
                raise CloudIntegrityError(
                    "Cloud snapshot market dates do not match its source manifest"
                )
    except (KeyError, TypeError) as exc:
        raise CloudIntegrityError("Cloud snapshot manifest is incomplete") from exc


def _preflight_archive(archive: zipfile.ZipFile) -> dict[str, zipfile.ZipInfo]:
    members = archive.infolist()
    if len(members) > MAX_MEMBERS:
        raise CloudIntegrityError("Cloud snapshot contains too many files")
    names = [member.filename for member in members]
    if len(names) != len(set(names)) or "snapshot-manifest.json" not in names:
        raise CloudIntegrityError("Cloud snapshot member list is invalid")
    for member in members:
        _validate_archive_member(member)
    member_map = dict(zip(names, members, strict=True))
    _validate_snapshot_size_limits(
        [member.file_size for member in members],
        manifest_size=member_map["snapshot-manifest.json"].file_size,
        context="Cloud snapshot",
    )
    return member_map


def _validate_snapshot_size_limits(
    member_sizes: list[int], *, manifest_size: int, context: str
) -> None:
    if len(member_sizes) > MAX_MEMBERS:
        raise CloudIntegrityError(f"{context} contains too many files")
    if manifest_size > MAX_MANIFEST_BYTES:
        raise CloudIntegrityError(f"{context} manifest is too large")
    if any(size < 0 or size > MAX_MEMBER_BYTES for size in member_sizes):
        raise CloudIntegrityError(f"{context} contains an oversized member")
    if sum(member_sizes) > MAX_ARCHIVE_BYTES:
        raise CloudIntegrityError(f"{context} expands beyond the permitted size")


def _validate_archive_member(member: zipfile.ZipInfo) -> None:
    name = member.filename
    path = PurePosixPath(name)
    if (
        not name
        or "\\" in name
        or ":" in name
        or path.is_absolute()
        or ".." in path.parts
        or any(ord(character) < 32 for character in name)
        or member.is_dir()
        or member.file_size > MAX_MEMBER_BYTES
        or member.compress_size > MAX_ARCHIVE_BYTES
        or member.flag_bits & 0x1
    ):
        raise CloudIntegrityError(f"Cloud snapshot contains an unsafe member: {name!r}")
    mode = member.external_attr >> 16
    if mode and (mode & 0o170000) not in {0, 0o100000}:
        raise CloudIntegrityError(
            f"Cloud snapshot contains a non-regular member: {name!r}"
        )


def _safe_destination(root: Path, relative: str) -> Path:
    root = root.resolve()
    target = (root / Path(*PurePosixPath(relative).parts)).resolve()
    if root not in target.parents:
        raise CloudIntegrityError(
            "Cloud snapshot attempted to escape the restore directory"
        )
    return target


def _validate_r2_endpoint(endpoint: str) -> None:
    parsed = urlsplit(endpoint)
    hostname = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or not re.fullmatch(
            r"[0-9a-f]{32}(?:\.(?:eu|fedramp))?\.r2\.cloudflarestorage\.com",
            hostname,
        )
    ):
        raise CloudConfigurationError(
            "AI_TRADE_R2_ENDPOINT must be a Cloudflare R2 HTTPS endpoint"
        )


def _environment_flag(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise CloudConfigurationError(f"{name} must be true or false")


def _next_continuation_token(
    response: dict[str, Any], seen_tokens: set[str], operation: str
) -> str | None:
    if not response.get("IsTruncated"):
        return None
    token = response.get("NextContinuationToken")
    if not isinstance(token, str) or not token or len(token) > 8_192:
        raise CloudIntegrityError(
            f"{operation} pagination did not provide a valid continuation token"
        )
    if token in seen_tokens:
        raise CloudIntegrityError(
            f"{operation} pagination repeated a continuation token"
        )
    seen_tokens.add(token)
    return token


def _metadata(response: dict[str, Any]) -> dict[str, str]:
    return {
        str(key).lower(): str(value).lower()
        for key, value in response.get("Metadata", {}).items()
    }


def _is_not_found(exc: Exception) -> bool:
    response = getattr(exc, "response", None)
    if not isinstance(response, dict):
        return False
    error = response.get("Error", {})
    code = str(error.get("Code", "")) if isinstance(error, dict) else ""
    status = response.get("ResponseMetadata", {})
    http_status = status.get("HTTPStatusCode") if isinstance(status, dict) else None
    return code in {"404", "NoSuchKey", "NotFound"} or http_status == 404


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _config_fingerprint(config: AppConfig) -> str:
    payload = {
        "data": config.raw["data"],
        "security_master_sha256": config.security_master.fingerprint(),
        "universe": config.universe_name,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _package_version() -> str:
    try:
        return version("ai-trade")
    except PackageNotFoundError:
        return "development"
