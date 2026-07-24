"""Verified Cloudflare R2 snapshots for immutable research digests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any, Mapping
import zipfile
import zlib

from .cloud import (
    CloudIntegrityError,
    R2ObjectStore,
    SNAPSHOT_ID_PATTERN,
    _is_not_found,
)
from .json_utils import loads_unique_json
from .research_digest import ResearchDigestStore, _read_record


SCHEMA_VERSION = 1
DATASET = "research-digests"
MAX_FILES = 10_000
MAX_FILE_BYTES = 1 * 1024 * 1024
MAX_ARCHIVE_BYTES = 64 * 1024 * 1024
MAX_UNCOMPRESSED_BYTES = 128 * 1024 * 1024
FINGERPRINT = re.compile(r"[0-9a-f]{64}\Z")
REVISION_PATH = re.compile(
    r"(?:daily|weekly)/\d{4}-\d{2}-\d{2}/revision_\d{8}\.json\Z"
)
ARCHIVE_REVISION_PATH = re.compile(
    r"digests/(?P<kind>daily|weekly)/(?P<period>\d{4}-\d{2}-\d{2})/"
    r"revision_(?P<revision>\d{8})\.json\Z"
)
MANIFEST_FIELDS = frozenset(
    {
        "schema_version",
        "dataset",
        "snapshot_id",
        "created_at",
        "owner_fingerprint",
        "account_fingerprint",
        "config_fingerprint",
        "dataset_sha256",
        "files",
        "authority",
    }
)
MANIFEST_AUTHORITY = {
    "research_only": True,
    "active_state_included": False,
    "restore_overwrites_active_state": False,
}


@dataclass(frozen=True)
class ResearchDigestSnapshot:
    snapshot_id: str
    dataset_sha256: str
    path: Path
    sha256: str
    size: int
    manifest: dict[str, Any]


def create_research_digest_snapshot(
    store: ResearchDigestStore,
    owner: str,
    account_id: str,
    destination: str | Path,
) -> ResearchDigestSnapshot:
    """Create one verified snapshot of an owner/account digest namespace."""

    store.verify(owner, account_id)
    source_root = store.owner_directory(owner, account_id) / "digests"
    files = _digest_files(source_root)
    if not files:
        raise CloudIntegrityError("Research digest snapshot has no revisions")
    payloads = _stable_payloads(source_root, files)
    store.verify(owner, account_id)
    if _stable_payloads(source_root, _digest_files(source_root)) != payloads:
        raise CloudIntegrityError("Research digests changed while creating a snapshot")

    owner_fingerprint = store.owner_id(owner)
    account_fingerprint = store.account_id(account_id)
    entries: dict[str, dict[str, Any]] = {}
    config_fingerprints: set[str] = set()
    for relative, body in sorted(payloads.items()):
        record = _record(body, relative)
        if record.get("owner") != owner_fingerprint:
            raise CloudIntegrityError("Research digest owner binding is invalid")
        if record.get("account_fingerprint") != account_fingerprint:
            raise CloudIntegrityError("Research digest account binding is invalid")
        config = record.get("config_fingerprint")
        if not isinstance(config, str) or FINGERPRINT.fullmatch(config) is None:
            raise CloudIntegrityError("Research digest configuration binding is invalid")
        config_fingerprints.add(config)
        archive_name = f"digests/{relative}"
        entries[archive_name] = {
            "sha256": sha256(body).hexdigest(),
            "size": len(body),
        }
    if len(config_fingerprints) != 1:
        raise CloudIntegrityError("Research digest snapshot mixes configurations")
    dataset_sha256 = _dataset_fingerprint(entries)
    created = datetime.now(timezone.utc).replace(microsecond=0)
    created_at = created.isoformat()
    snapshot_id = (
        created.strftime("%Y%m%dT%H%M%SZ")
        + "-"
        + dataset_sha256[:12]
    )
    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "dataset": DATASET,
        "snapshot_id": snapshot_id,
        "created_at": created_at,
        "owner_fingerprint": owner_fingerprint,
        "account_fingerprint": account_fingerprint,
        "config_fingerprint": next(iter(config_fingerprints)),
        "dataset_sha256": dataset_sha256,
        "files": entries,
        "authority": dict(MANIFEST_AUTHORITY),
    }
    output = Path(destination)
    if output.exists():
        raise FileExistsError(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "research-digest-manifest.json",
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
        )
        prefix = "digests/"
        for relative, body in sorted(payloads.items()):
            archive.writestr(prefix + relative, body)
    size = output.stat().st_size
    if size < 1 or size > MAX_ARCHIVE_BYTES:
        output.unlink(missing_ok=True)
        raise CloudIntegrityError("Research digest snapshot archive is too large")
    archive_sha256 = _file_sha256(output)
    return ResearchDigestSnapshot(
        snapshot_id=snapshot_id,
        dataset_sha256=dataset_sha256,
        path=output,
        sha256=archive_sha256,
        size=size,
        manifest=manifest,
    )


def backup_research_digests(
    store: ResearchDigestStore,
    owner: str,
    account_id: str,
    r2: R2ObjectStore,
) -> dict[str, Any]:
    with tempfile.TemporaryDirectory(prefix="ai-trade-digest-cloud-") as temporary:
        artifact = create_research_digest_snapshot(
            store,
            owner,
            account_id,
            Path(temporary) / "research-digests.zip",
        )
        return upload_research_digest_snapshot(r2, artifact)


def upload_research_digest_snapshot(
    r2: R2ObjectStore,
    artifact: ResearchDigestSnapshot,
) -> dict[str, Any]:
    with r2._operation_lock():
        pointer = _latest_pointer(r2)
        if pointer and pointer.get("dataset_sha256") == artifact.dataset_sha256:
            key = pointer.get("object_key")
            if isinstance(key, str) and _remote_matches(r2, key, artifact):
                return {**pointer, "skipped_duplicate": True}
        key = _object_key(r2, artifact.snapshot_id)
        with artifact.path.open("rb") as handle:
            r2._request(
                "class_a",
                "put_object",
                Bucket=r2.settings.bucket,
                Key=key,
                Body=handle,
                ContentType="application/zip",
                Metadata={
                    "sha256": artifact.sha256,
                    "dataset-sha256": artifact.dataset_sha256,
                    "schema-version": str(SCHEMA_VERSION),
                    "dataset": DATASET,
                },
            )
        if not _remote_matches(r2, key, artifact):
            raise CloudIntegrityError("R2 research digest snapshot verification failed")
        result = {
            "schema_version": SCHEMA_VERSION,
            "dataset": DATASET,
            "snapshot_id": artifact.snapshot_id,
            "object_key": key,
            "sha256": artifact.sha256,
            "dataset_sha256": artifact.dataset_sha256,
            "size": artifact.size,
            "created_at": artifact.manifest["created_at"],
            "account_fingerprint": artifact.manifest["account_fingerprint"],
            "skipped_duplicate": False,
        }
        r2._request(
            "class_a",
            "put_object",
            Bucket=r2.settings.bucket,
            Key=_latest_key(r2),
            Body=json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8"),
            ContentType="application/json",
            Metadata={"schema-version": str(SCHEMA_VERSION), "dataset": DATASET},
        )
        return result


def list_research_digest_snapshots(
    r2: R2ObjectStore, *, limit: int = 20
) -> list[dict[str, Any]]:
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1_000:
        raise ValueError("Research digest cloud limit must be between 1 and 1000")
    with r2._operation_lock():
        prefix = f"{r2.settings.namespace}/snapshots/{DATASET}/"
        values: list[dict[str, Any]] = []
        token: str | None = None
        seen: set[str] = set()
        while True:
            request: dict[str, Any] = {
                "Bucket": r2.settings.bucket,
                "Prefix": prefix,
                "MaxKeys": 1_000,
            }
            if token:
                request["ContinuationToken"] = token
            response = r2._request("class_a", "list_objects_v2", **request)
            for item in response.get("Contents", []):
                key = str(item.get("Key", ""))
                name = PurePosixPath(key).name
                snapshot_id = name.removesuffix(".zip")
                if not name.endswith(".zip") or SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id) is None:
                    continue
                modified = item.get("LastModified")
                values.append(
                    {
                        "snapshot_id": snapshot_id,
                        "size": max(0, int(item.get("Size", 0) or 0)),
                        "last_modified": modified.isoformat() if hasattr(modified, "isoformat") else None,
                        "object_key": key,
                    }
                )
            if not response.get("IsTruncated"):
                break
            raw_token = response.get("NextContinuationToken")
            if not isinstance(raw_token, str) or not raw_token or raw_token in seen:
                raise CloudIntegrityError("Research digest cloud listing pagination is invalid")
            seen.add(raw_token)
            token = raw_token
        values.sort(key=lambda item: item["snapshot_id"], reverse=True)
        return values[:limit]


def restore_research_digest_snapshot(
    r2: R2ObjectStore,
    snapshot_id: str,
    destination: str | Path,
) -> Path:
    if not isinstance(snapshot_id, str) or SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id) is None:
        raise ValueError("Research digest snapshot ID is invalid")
    target = Path(destination).expanduser().resolve()
    if target.exists():
        raise FileExistsError(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    with r2._operation_lock(), tempfile.TemporaryDirectory(
        prefix="ai-trade-digest-restore-", dir=target.parent
    ) as temporary:
        temporary_root = Path(temporary)
        archive = temporary_root / "snapshot.zip"
        r2._download_verified(_object_key(r2, snapshot_id), archive)
        staging = temporary_root / "verified"
        staging.mkdir()
        _extract_verified(archive, staging, expected_snapshot_id=snapshot_id)
        os.rename(staging, target)
    return target


def _digest_files(root: Path) -> list[Path]:
    if root.is_symlink() or not root.is_dir():
        return []
    files: list[Path] = []
    for path in root.rglob("*"):
        if path.is_symlink():
            raise CloudIntegrityError("Research digest snapshot rejects symbolic members")
        if path.is_dir():
            continue
        relative = path.relative_to(root).as_posix()
        if not path.is_file() or REVISION_PATH.fullmatch(relative) is None:
            raise CloudIntegrityError("Research digest snapshot contains an unexpected member")
        files.append(path)
        if len(files) > MAX_FILES:
            raise CloudIntegrityError("Research digest snapshot has too many files")
    return sorted(files)


def _stable_payloads(root: Path, files: list[Path]) -> dict[str, bytes]:
    payloads: dict[str, bytes] = {}
    total = 0
    for path in files:
        size = path.stat().st_size
        if size < 1 or size > MAX_FILE_BYTES:
            raise CloudIntegrityError("Research digest revision size is invalid")
        body = path.read_bytes()
        if len(body) != size or path.stat().st_size != size:
            raise CloudIntegrityError("Research digest revision changed while reading")
        total += len(body)
        if total > MAX_UNCOMPRESSED_BYTES:
            raise CloudIntegrityError("Research digest snapshot is too large")
        payloads[path.relative_to(root).as_posix()] = body
    return payloads


def _record(body: bytes, relative: str) -> dict[str, Any]:
    try:
        value = loads_unique_json(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CloudIntegrityError(f"Research digest revision is invalid: {relative}") from exc
    if not isinstance(value, dict):
        raise CloudIntegrityError("Research digest revision must be an object")
    return value


def _dataset_fingerprint(entries: Mapping[str, Mapping[str, Any]]) -> str:
    value = [
        [name, item["sha256"], item["size"]]
        for name, item in sorted(entries.items())
    ]
    return sha256(json.dumps(value, separators=(",", ":")).encode("utf-8")).hexdigest()


def _object_key(r2: R2ObjectStore, snapshot_id: str) -> str:
    if SNAPSHOT_ID_PATTERN.fullmatch(snapshot_id) is None:
        raise ValueError("Research digest snapshot ID is invalid")
    return (
        f"{r2.settings.namespace}/snapshots/{DATASET}/"
        f"{snapshot_id[:4]}/{snapshot_id[4:6]}/{snapshot_id[6:8]}/{snapshot_id}.zip"
    )


def _latest_key(r2: R2ObjectStore) -> str:
    return f"{r2.settings.namespace}/indexes/{DATASET}/latest.json"


def _latest_pointer(r2: R2ObjectStore) -> dict[str, Any] | None:
    key = _latest_key(r2)
    try:
        head = r2._request("class_b", "head_object", Bucket=r2.settings.bucket, Key=key)
        size = int(head.get("ContentLength", -1))
        if not 1 <= size <= 64 * 1024:
            raise CloudIntegrityError("Research digest latest pointer size is invalid")
        response = r2._request("class_b", "get_object", Bucket=r2.settings.bucket, Key=key)
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
    if len(content) != size:
        raise CloudIntegrityError("Research digest latest pointer content is invalid")
    try:
        value = loads_unique_json(content.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise CloudIntegrityError("Research digest latest pointer is invalid") from exc
    if not isinstance(value, dict) or value.get("dataset") != DATASET:
        raise CloudIntegrityError("Research digest latest pointer schema is invalid")
    return value


def _remote_matches(
    r2: R2ObjectStore, key: str, artifact: ResearchDigestSnapshot
) -> bool:
    try:
        head = r2._request("class_b", "head_object", Bucket=r2.settings.bucket, Key=key)
    except KeyError:
        return False
    except Exception as exc:
        if _is_not_found(exc):
            return False
        raise
    metadata = {str(k).lower(): str(v) for k, v in dict(head.get("Metadata", {})).items()}
    return (
        int(head.get("ContentLength", -1)) == artifact.size
        and metadata.get("sha256") == artifact.sha256
        and metadata.get("dataset-sha256") == artifact.dataset_sha256
        and metadata.get("dataset") == DATASET
    )


def _extract_verified(archive_path: Path, destination: Path, *, expected_snapshot_id: str) -> None:
    try:
        archive = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as exc:
        raise CloudIntegrityError("Research digest cloud snapshot is not a ZIP archive") from exc
    with archive:
        members = archive.infolist()
        names = [item.filename for item in members]
        if len(names) != len(set(names)) or "research-digest-manifest.json" not in names:
            raise CloudIntegrityError("Research digest cloud member list is invalid")
        if len(names) > MAX_FILES + 1:
            raise CloudIntegrityError("Research digest cloud snapshot has too many members")
        total = 0
        for member in members:
            path = PurePosixPath(member.filename)
            if member.is_dir() or path.is_absolute() or ".." in path.parts or "\\" in member.filename:
                raise CloudIntegrityError("Research digest cloud snapshot has an unsafe member")
            total += member.file_size
            member_limit = 8 * 1024 * 1024 if member.filename == "research-digest-manifest.json" else MAX_FILE_BYTES
            if member.file_size < 0 or member.file_size > member_limit or total > MAX_UNCOMPRESSED_BYTES:
                raise CloudIntegrityError("Research digest cloud snapshot member size is invalid")
        try:
            manifest = loads_unique_json(_read_member(archive, "research-digest-manifest.json").decode("utf-8"))
        except (KeyError, UnicodeDecodeError, ValueError) as exc:
            raise CloudIntegrityError("Research digest cloud manifest is invalid") from exc
        if (
            not isinstance(manifest, dict)
            or set(manifest) != MANIFEST_FIELDS
            or manifest.get("schema_version") != SCHEMA_VERSION
            or manifest.get("dataset") != DATASET
            or manifest.get("snapshot_id") != expected_snapshot_id
            or manifest.get("authority") != MANIFEST_AUTHORITY
        ):
            raise CloudIntegrityError("Research digest cloud manifest schema is invalid")
        for field in (
            "owner_fingerprint",
            "account_fingerprint",
            "config_fingerprint",
            "dataset_sha256",
        ):
            if FINGERPRINT.fullmatch(str(manifest.get(field, ""))) is None:
                raise CloudIntegrityError(
                    f"Research digest cloud manifest {field} is invalid"
                )
        try:
            created_at = datetime.fromisoformat(str(manifest.get("created_at")))
        except ValueError as exc:
            raise CloudIntegrityError(
                "Research digest cloud manifest timestamp is invalid"
            ) from exc
        if created_at.tzinfo is None:
            raise CloudIntegrityError(
                "Research digest cloud manifest timestamp must include a timezone"
            )
        entries = manifest.get("files")
        if not isinstance(entries, dict) or set(names) != {"research-digest-manifest.json", *entries}:
            raise CloudIntegrityError("Research digest cloud manifest and archive disagree")
        normalized: dict[str, dict[str, Any]] = {}
        for name, metadata in entries.items():
            if (
                not isinstance(name, str)
                or ARCHIVE_REVISION_PATH.fullmatch(name) is None
                or not isinstance(metadata, dict)
                or set(metadata) != {"sha256", "size"}
            ):
                raise CloudIntegrityError("Research digest cloud file metadata is invalid")
            digest = metadata.get("sha256")
            size = metadata.get("size")
            if not isinstance(digest, str) or FINGERPRINT.fullmatch(digest) is None or isinstance(size, bool) or not isinstance(size, int):
                raise CloudIntegrityError("Research digest cloud file metadata is invalid")
            body = _read_member(archive, name)
            if len(body) != size or sha256(body).hexdigest() != digest:
                raise CloudIntegrityError("Research digest cloud file checksum is invalid")
            normalized[name] = {"sha256": digest, "size": size}
        if manifest.get("dataset_sha256") != _dataset_fingerprint(normalized):
            raise CloudIntegrityError("Research digest cloud dataset checksum is invalid")
        if expected_snapshot_id.rsplit("-", 1)[-1] != str(manifest["dataset_sha256"])[:12]:
            raise CloudIntegrityError("Research digest cloud snapshot identity is invalid")
        for name in sorted(entries):
            output = destination.joinpath(*PurePosixPath(name).parts)
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(_read_member(archive, name))
        _verify_restored_records(destination, manifest, sorted(entries))
        (destination / "research-digest-manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def _verify_restored_records(
    destination: Path,
    manifest: Mapping[str, Any],
    names: list[str],
) -> None:
    owner = str(manifest["owner_fingerprint"])
    account = str(manifest["account_fingerprint"])
    config = str(manifest["config_fingerprint"])
    chains: dict[tuple[str, date], list[tuple[int, Path]]] = {}
    for name in names:
        match = ARCHIVE_REVISION_PATH.fullmatch(name)
        if match is None:
            raise CloudIntegrityError("Research digest cloud revision path is invalid")
        try:
            period = date.fromisoformat(match.group("period"))
        except ValueError as exc:
            raise CloudIntegrityError(
                "Research digest cloud revision period is invalid"
            ) from exc
        revision = int(match.group("revision"))
        chains.setdefault((match.group("kind"), period), []).append(
            (revision, destination.joinpath(*PurePosixPath(name).parts))
        )

    seen_ids: set[str] = set()
    seen_fingerprints: set[str] = set()
    try:
        for (kind, period), members in chains.items():
            members.sort(key=lambda item: item[0])
            if [item[0] for item in members] != list(range(1, len(members) + 1)):
                raise CloudIntegrityError(
                    "Research digest cloud revision chain has a gap"
                )
            previous: Mapping[str, Any] | None = None
            for revision, path in members:
                record = _read_record(
                    path,
                    expected_owner=owner,
                    expected_account=account,
                    expected_kind=kind,
                    expected_period=period,
                    expected_revision=revision,
                )
                if record["config_fingerprint"] != config:
                    raise CloudIntegrityError(
                        "Research digest cloud configuration binding is invalid"
                    )
                if previous is None:
                    if record["supersedes"] is not None:
                        raise CloudIntegrityError(
                            "Research digest cloud first revision has a parent"
                        )
                elif (
                    record["supersedes"] != previous["digest_id"]
                    or record["supersedes_fingerprint"]
                    != previous["digest_fingerprint"]
                ):
                    raise CloudIntegrityError(
                        "Research digest cloud supersedes chain is invalid"
                    )
                if record["digest_id"] in seen_ids:
                    raise CloudIntegrityError(
                        "Research digest cloud contains a duplicate digest ID"
                    )
                if record["digest_fingerprint"] in seen_fingerprints:
                    raise CloudIntegrityError(
                        "Research digest cloud contains a duplicate digest fingerprint"
                    )
                seen_ids.add(record["digest_id"])
                seen_fingerprints.add(record["digest_fingerprint"])
                previous = record
    except CloudIntegrityError:
        raise
    except (KeyError, OSError, RuntimeError, TypeError, ValueError) as exc:
        raise CloudIntegrityError(
            "Research digest cloud revision evidence is invalid"
        ) from exc


def _file_sha256(path: Path) -> str:
    digest = sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(256 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    try:
        return archive.read(name)
    except (KeyError, OSError, RuntimeError, zipfile.BadZipFile, zlib.error) as exc:
        raise CloudIntegrityError("Research digest cloud member cannot be verified") from exc
