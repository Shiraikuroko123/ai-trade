from __future__ import annotations

import argparse
from email.parser import Parser
import hashlib
import re
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


WHEEL_REQUIRED = {
    "ai_trade/__init__.py",
    "ai_trade/cloud.py",
    "ai_trade/cloud_usage.py",
    "ai_trade/json_utils.py",
    "ai_trade/assistant/__init__.py",
    "ai_trade/assistant/engine.py",
    "ai_trade/assistant/features.py",
    "ai_trade/assistant/provider.py",
    "ai_trade/assistant/store.py",
    "ai_trade/broker/base.py",
    "ai_trade/broker/ledger.py",
    "ai_trade/broker/lifecycle.py",
    "ai_trade/broker/live.py",
    "ai_trade/broker/live_guard.py",
    "ai_trade/broker/mandate.py",
    "ai_trade/broker/probe.py",
    "ai_trade/broker/reconciliation.py",
    "ai_trade/broker/runtime.py",
    "ai_trade/broker/scope.py",
    "ai_trade/broker/shadow.py",
    "ai_trade/strategy_lab/__init__.py",
    "ai_trade/strategy_lab/engine.py",
    "ai_trade/strategy_lab/lifecycle.py",
    "ai_trade/strategy_lab/schema.py",
    "ai_trade/strategy_lab/store.py",
    "ai_trade/data/cache_snapshot.py",
    "ai_trade/data/tencent.py",
    "ai_trade/default_config.json",
    "ai_trade/default_security_master.json",
    "ai_trade/web/assets/__init__.py",
    "ai_trade/web/assets/index.html",
    "ai_trade/web/assets/app.css",
    "ai_trade/web/assets/app.js",
    "ai_trade/web/assets/auth.css",
    "ai_trade/web/assets/auth.js",
    "ai_trade/web/assets/login.html",
    "ai_trade/web/assets/vendor/klinecharts.min.js",
    "ai_trade/web/assets/vendor/klinecharts.LICENSE.txt",
    "ai_trade/web/assets/vendor/klinecharts.NOTICE.txt",
    "ai_trade/web/assets/vendor/klinecharts.SOURCE.txt",
    "ai_trade/web/assets/vendor/lightweight-charts.LICENSE.txt",
    "ai_trade/web/auth.py",
}

SDIST_REQUIRED = {
    "CHANGELOG.md",
    "DESIGN.md",
    "LICENSE",
    "MANIFEST.in",
    "NOTICE",
    "PRODUCT.md",
    "README.md",
    "SECURITY.md",
    "config/default.json",
    "config/security_master.json",
    "docs/ARCHITECTURE.md",
    "docs/AI_ASSISTANT.md",
    "docs/CLOUD_STORAGE.md",
    "docs/assets/workstation-overview.png",
    "docs/BROKER_ADAPTERS.md",
    "docs/ECOSYSTEM.md",
    "docs/PAPER_TRADING.md",
    "docs/RESEARCH_METHODOLOGY.md",
    "docs/UNIVERSE.md",
    "pyproject.toml",
    "scripts/bootstrap.ps1",
    "scripts/configure_ai.ps1",
    "scripts/configure_cloud.ps1",
    "scripts/install_paper_task.ps1",
    "scripts/run_daily_paper.ps1",
    "scripts/verify_distribution.py",
    "src/ai_trade/default_config.json",
    "src/ai_trade/default_security_master.json",
    "src/ai_trade/__init__.py",
    "src/ai_trade/cloud.py",
    "src/ai_trade/cloud_usage.py",
    "src/ai_trade/json_utils.py",
    "src/ai_trade/assistant/__init__.py",
    "src/ai_trade/assistant/engine.py",
    "src/ai_trade/assistant/features.py",
    "src/ai_trade/assistant/provider.py",
    "src/ai_trade/assistant/store.py",
    "src/ai_trade/broker/base.py",
    "src/ai_trade/broker/ledger.py",
    "src/ai_trade/broker/lifecycle.py",
    "src/ai_trade/broker/live.py",
    "src/ai_trade/broker/live_guard.py",
    "src/ai_trade/broker/mandate.py",
    "src/ai_trade/broker/probe.py",
    "src/ai_trade/broker/reconciliation.py",
    "src/ai_trade/broker/runtime.py",
    "src/ai_trade/broker/scope.py",
    "src/ai_trade/broker/shadow.py",
    "src/ai_trade/strategy_lab/__init__.py",
    "src/ai_trade/strategy_lab/engine.py",
    "src/ai_trade/strategy_lab/lifecycle.py",
    "src/ai_trade/strategy_lab/schema.py",
    "src/ai_trade/strategy_lab/store.py",
    "src/ai_trade/data/cache_snapshot.py",
    "src/ai_trade/data/tencent.py",
    "src/ai_trade/web/assets/index.html",
    "src/ai_trade/web/assets/app.css",
    "src/ai_trade/web/assets/app.js",
    "src/ai_trade/web/assets/auth.css",
    "src/ai_trade/web/assets/auth.js",
    "src/ai_trade/web/assets/login.html",
    "src/ai_trade/web/assets/vendor/klinecharts.min.js",
    "src/ai_trade/web/assets/vendor/klinecharts.LICENSE.txt",
    "src/ai_trade/web/assets/vendor/klinecharts.NOTICE.txt",
    "src/ai_trade/web/assets/vendor/klinecharts.SOURCE.txt",
    "src/ai_trade/web/assets/vendor/lightweight-charts.LICENSE.txt",
    "src/ai_trade/web/auth.py",
}

VENDORED_FILES_SHA256 = {
    "vendor/klinecharts.min.js": (
        "00eb7cd35fc003f733a430fd7381283def849fca0aa08b1fccdb17a49c73fd15"
    ),
    "vendor/klinecharts.LICENSE.txt": (
        "ff8311d55ca5766d3888c688b0ecc0f292289a659e45bfee4dfc0870ab74e4ca"
    ),
    "vendor/klinecharts.NOTICE.txt": (
        "e2a48226872b013c897676c3ae1b65dbebcbdd8d3fdcc29fed5073c3d6e5e231"
    ),
    "vendor/klinecharts.SOURCE.txt": (
        "5cc1c693d4c4343721c3ea6826298276444be7ab0e4e6ea98a7b3b84b1d362e1"
    ),
    "vendor/lightweight-charts.LICENSE.txt": (
        "53c3bce42b068bd4c9a7831e18d4d7e7eab1b9cd00b8a3faac0aa96793c99bc5"
    ),
}

BANNED_PARTS = {
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    ".aws",
    "build",
    "dist",
    "local",
    "logs",
    "reports",
    "state",
}
BANNED_PATH_SEQUENCES = {("data", "cache")}
SENSITIVE_NAME_MARKERS = ("beta-users", "beta_users", "内测名单")
SENSITIVE_FILE_NAMES = {
    ".env",
    ".npmrc",
    ".pypirc",
    "credentials",
    "credentials.json",
    "secrets.json",
    "token.json",
}
SENSITIVE_SUFFIXES = {
    ".db",
    ".kdbx",
    ".key",
    ".p12",
    ".pem",
    ".pfx",
    ".sqlite",
    ".sqlite3",
}
TEXT_SUFFIXES = {
    ".bat",
    ".cfg",
    ".conf",
    ".css",
    ".csv",
    ".html",
    ".in",
    ".ini",
    ".js",
    ".json",
    ".md",
    ".properties",
    ".ps1",
    ".py",
    ".rst",
    ".sh",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
MAX_TEXT_MEMBER_BYTES = 16 * 1024 * 1024
SENSITIVE_CONTENT_PATTERNS = (
    (
        "private key",
        re.compile(r"-----BEGIN (?:EC |OPENSSH |RSA )?PRIVATE KEY-----", re.IGNORECASE),
    ),
    (
        "account-scoped Cloudflare R2 endpoint",
        re.compile(
            r"https://[0-9a-f]{32}(?:\.(?:eu|fedramp))?\.r2\.cloudflarestorage\.com",
            re.IGNORECASE,
        ),
    ),
    (
        "Windows user profile path",
        re.compile(
            r"\b[A-Z]:[\\/](?:Users|Documents and Settings)[\\/][^\\/\s\"']+",
            re.IGNORECASE,
        ),
    ),
    (
        "local development path",
        re.compile(r"\b[A-Z]:[\\/](?:touzi|ps)[\\/]", re.IGNORECASE),
    ),
    (
        "literal credential",
        re.compile(
            r"\b(?:AI_TRADE_AI_API_KEY|"
            r"AI_TRADE_R2_(?:ACCESS_KEY_ID|SECRET_ACCESS_KEY)|"
            r"ANTHROPIC_API_KEY|AWS_(?:ACCESS_KEY_ID|SECRET_ACCESS_KEY)|"
            r"DASHSCOPE_API_KEY|DEEPSEEK_API_KEY|GH_TOKEN|GITHUB_TOKEN|"
            r"OPENAI_API_KEY|"
            r"PAPERFIELD_S3_(?:ACCESS_KEY_ID|SECRET_ACCESS_KEY))"
            r"\s*[:=]\s*[\"']?(?![$<{%])[A-Za-z0-9/+_=.-]{16,}",
        ),
    ),
    (
        "provider or GitHub token",
        re.compile(
            r"\b(?:github_pat_[A-Za-z0-9_]{20,}|"
            r"gh[pousr]_[A-Za-z0-9]{20,}|sk-[A-Za-z0-9_-]{20,})"
        ),
    ),
    (
        "bearer credential",
        re.compile(
            r"\bAuthorization\s*:\s*Bearer\s+(?![$<{%])[A-Za-z0-9._~+/-]{16,}",
            re.IGNORECASE,
        ),
    ),
    (
        "credential-bearing proxy URL",
        re.compile(r"https?://[^/\s:@]+:[^/\s@]+@", re.IGNORECASE),
    ),
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify wheel and source distribution release contents"
    )
    parser.add_argument("directory", nargs="?", default="dist")
    args = parser.parse_args(argv)
    directory = Path(args.directory).resolve()
    wheel = _single_artifact(directory, "*.whl", "wheel")
    sdist = _single_artifact(directory, "*.tar.gz", "source distribution")
    source_version = _source_project_version(
        Path(__file__).resolve().parents[1] / "pyproject.toml"
    )

    with zipfile.ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
        _verify_safe_unique_names(wheel_names, wheel.name)
        _scan_zip_text(archive, wheel_names, wheel.name)
        _verify_zip_hashes(
            archive,
            {
                f"ai_trade/web/assets/{name}": digest
                for name, digest in VENDORED_FILES_SHA256.items()
            },
            wheel.name,
        )
        wheel_version = _wheel_version(archive, wheel_names, wheel.name)
    wheel_members = set(wheel_names)
    _require_members(wheel_members, WHEEL_REQUIRED, wheel.name)
    _require_suffix(wheel_members, ".dist-info/licenses/LICENSE", wheel.name)
    _require_suffix(wheel_members, ".dist-info/licenses/NOTICE", wheel.name)

    with tarfile.open(sdist, mode="r:gz") as archive:
        archive_members = archive.getmembers()
        sdist_names = [member.name for member in archive_members]
        _verify_safe_unique_names(sdist_names, sdist.name)
        _scan_tar_text(archive, archive_members, sdist.name)
        _verify_tar_hashes(
            archive,
            archive_members,
            {
                f"src/ai_trade/web/assets/{name}": digest
                for name, digest in VENDORED_FILES_SHA256.items()
            },
            sdist.name,
        )
        sdist_version = _sdist_version(archive, archive_members, sdist.name)
    sdist_members = _strip_sdist_root(sdist_names, sdist.name)
    _require_members(sdist_members, SDIST_REQUIRED, sdist.name)
    _verify_release_versions(
        source_version,
        wheel_version,
        sdist_version,
        wheel.name,
        sdist.name,
    )

    print(f"Verified {wheel.name} and {sdist.name}")
    return 0


def _single_artifact(directory: Path, pattern: str, label: str) -> Path:
    values = sorted(directory.glob(pattern))
    if len(values) != 1:
        raise SystemExit(
            f"Expected exactly one {label} matching {pattern} in {directory}; "
            f"found {len(values)}"
        )
    return values[0]


def _source_project_version(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SystemExit(f"Could not read source project version from {path}") from exc
    project = re.search(r"(?ms)^\[project\]\s*$\n(?P<body>.*?)(?=^\[|\Z)", text)
    if project is None:
        raise SystemExit(f"{path} has no [project] table")
    match = re.search(
        r'(?m)^version\s*=\s*"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"\s*$',
        project.group("body"),
    )
    if match is None:
        raise SystemExit(f"{path} has no supported static project version")
    return match.group("version")


def _wheel_version(
    archive: zipfile.ZipFile, names: list[str], archive_name: str
) -> str:
    metadata_names = [name for name in names if name.endswith(".dist-info/METADATA")]
    if len(metadata_names) != 1:
        raise SystemExit(
            f"{archive_name} must contain exactly one .dist-info/METADATA file"
        )
    try:
        text = archive.read(metadata_names[0]).decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"{archive_name} contains non-UTF-8 METADATA") from exc
    version = Parser().parsestr(text).get("Version")
    if (
        not isinstance(version, str)
        or re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version) is None
    ):
        raise SystemExit(f"{archive_name} contains an unsupported Version field")
    return version


def _sdist_version(
    archive: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    archive_name: str,
) -> str:
    files = {
        PurePosixPath(member.name): member for member in members if member.isfile()
    }
    roots = {path.parts[0] for path in files if path.parts}
    if len(roots) != 1:
        raise SystemExit(f"{archive_name} must contain exactly one top-level directory")
    root = next(iter(roots))
    project_member = files.get(PurePosixPath(root, "pyproject.toml"))
    init_member = files.get(PurePosixPath(root, "src", "ai_trade", "__init__.py"))
    if project_member is None or init_member is None:
        raise SystemExit(f"{archive_name} is missing version source files")

    project_path = Path(f"{archive_name}:pyproject.toml")
    project_text = _tar_text(archive, project_member, archive_name)
    project = re.search(r"(?ms)^\[project\]\s*$\n(?P<body>.*?)(?=^\[|\Z)", project_text)
    if project is None:
        raise SystemExit(f"{project_path} has no [project] table")
    project_match = re.search(
        r'(?m)^version\s*=\s*"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"\s*$',
        project.group("body"),
    )
    init_match = re.search(
        r'(?m)^__version__\s*=\s*"(?P<version>[0-9]+\.[0-9]+\.[0-9]+)"\s*$',
        _tar_text(archive, init_member, archive_name),
    )
    if project_match is None or init_match is None:
        raise SystemExit(f"{archive_name} contains unsupported version declarations")
    project_version = project_match.group("version")
    if init_match.group("version") != project_version:
        raise SystemExit(f"{archive_name} has inconsistent packaged versions")
    if root != f"ai_trade-{project_version}":
        raise SystemExit(
            f"{archive_name} top-level directory does not match its version"
        )
    return project_version


def _tar_text(
    archive: tarfile.TarFile, member: tarfile.TarInfo, archive_name: str
) -> str:
    handle = archive.extractfile(member)
    if handle is None:
        raise SystemExit(f"{archive_name} could not read {member.name}")
    try:
        return handle.read().decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit(f"{archive_name} contains non-UTF-8 {member.name}") from exc


def _verify_release_versions(
    expected: str,
    wheel_version: str,
    sdist_version: str,
    wheel_name: str,
    sdist_name: str,
) -> None:
    if wheel_version != expected or sdist_version != expected:
        raise SystemExit(
            "Artifact versions do not match the source project: "
            f"source={expected}, wheel={wheel_version}, sdist={sdist_version}"
        )
    if not wheel_name.startswith(f"ai_trade-{expected}-"):
        raise SystemExit(f"{wheel_name} does not match source version {expected}")
    if sdist_name != f"ai_trade-{expected}.tar.gz":
        raise SystemExit(f"{sdist_name} does not match source version {expected}")


def _verify_safe_unique_names(names: list[str], archive_name: str) -> None:
    if len(names) != len(set(names)):
        raise SystemExit(f"{archive_name} contains duplicate member names")
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"{archive_name} contains an unsafe path: {name}")
        if (
            BANNED_PARTS.intersection(path.parts)
            or _contains_banned_sequence(path.parts)
            or path.suffix.casefold() in {".pyc", ".pyo"}
        ):
            raise SystemExit(f"{archive_name} contains a generated file: {name}")
        folded_name = path.name.casefold()
        if any(marker in folded_name for marker in SENSITIVE_NAME_MARKERS):
            raise SystemExit(f"{archive_name} contains a beta-user file: {name}")
        if (
            folded_name in SENSITIVE_FILE_NAMES
            or folded_name.startswith(".env.")
            or path.suffix.casefold() in SENSITIVE_SUFFIXES
        ):
            raise SystemExit(f"{archive_name} contains a local credential file: {name}")


def _contains_banned_sequence(parts: tuple[str, ...]) -> bool:
    folded = tuple(part.casefold() for part in parts)
    return any(
        folded[index : index + len(sequence)] == sequence
        for sequence in BANNED_PATH_SEQUENCES
        for index in range(len(folded) - len(sequence) + 1)
    )


def _scan_zip_text(
    archive: zipfile.ZipFile, names: list[str], archive_name: str
) -> None:
    for name in names:
        if name.endswith("/") or not _is_text_member(name):
            continue
        info = archive.getinfo(name)
        if info.file_size > MAX_TEXT_MEMBER_BYTES:
            raise SystemExit(f"{archive_name} contains an oversized text file: {name}")
        _verify_text_content(name, archive.read(name), archive_name)


def _scan_tar_text(
    archive: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    archive_name: str,
) -> None:
    for member in members:
        if not member.isfile() or not _is_text_member(member.name):
            continue
        if member.size > MAX_TEXT_MEMBER_BYTES:
            raise SystemExit(
                f"{archive_name} contains an oversized text file: {member.name}"
            )
        handle = archive.extractfile(member)
        if handle is None:
            raise SystemExit(f"{archive_name} could not read text file: {member.name}")
        _verify_text_content(member.name, handle.read(), archive_name)


def _verify_zip_hashes(
    archive: zipfile.ZipFile,
    expected: dict[str, str],
    archive_name: str,
) -> None:
    for name, digest in expected.items():
        try:
            content = archive.read(name)
        except KeyError as exc:
            raise SystemExit(f"{archive_name} is missing hashed file: {name}") from exc
        _verify_sha256(name, content, digest, archive_name)


def _verify_tar_hashes(
    archive: tarfile.TarFile,
    members: list[tarfile.TarInfo],
    expected: dict[str, str],
    archive_name: str,
) -> None:
    files: dict[str, tarfile.TarInfo] = {}
    for member in members:
        path = PurePosixPath(member.name)
        if member.isfile() and len(path.parts) > 1:
            files[PurePosixPath(*path.parts[1:]).as_posix()] = member
    for name, digest in expected.items():
        member = files.get(name)
        if member is None:
            raise SystemExit(f"{archive_name} is missing hashed file: {name}")
        handle = archive.extractfile(member)
        if handle is None:
            raise SystemExit(f"{archive_name} could not read hashed file: {name}")
        _verify_sha256(name, handle.read(), digest, archive_name)


def _verify_sha256(
    name: str,
    content: bytes,
    expected: str,
    archive_name: str,
) -> None:
    actual = hashlib.sha256(content).hexdigest()
    if actual != expected:
        raise SystemExit(
            f"{archive_name} has an unexpected SHA-256 for {name}: {actual}"
        )


def _is_text_member(name: str) -> bool:
    path = PurePosixPath(name)
    return path.suffix.casefold() in TEXT_SUFFIXES or path.name == "MANIFEST.in"


def _verify_text_content(name: str, content: bytes, archive_name: str) -> None:
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SystemExit(
            f"{archive_name} contains a non-UTF-8 text file: {name}"
        ) from exc
    for label, pattern in SENSITIVE_CONTENT_PATTERNS:
        if pattern.search(text):
            raise SystemExit(
                f"{archive_name} contains {label} content in text file: {name}"
            )


def _strip_sdist_root(names: list[str], archive_name: str) -> set[str]:
    populated = [PurePosixPath(name) for name in names if PurePosixPath(name).parts]
    roots = {path.parts[0] for path in populated}
    if len(roots) != 1:
        raise SystemExit(f"{archive_name} must contain exactly one top-level directory")
    return {
        PurePosixPath(*path.parts[1:]).as_posix()
        for path in populated
        if len(path.parts) > 1
    }


def _require_members(actual: set[str], required: set[str], archive_name: str) -> None:
    missing = sorted(required - actual)
    if missing:
        raise SystemExit(f"{archive_name} is missing required files: {missing}")


def _require_suffix(actual: set[str], suffix: str, archive_name: str) -> None:
    if not any(name.endswith(suffix) for name in actual):
        raise SystemExit(f"{archive_name} is missing a required {suffix} file")


if __name__ == "__main__":
    raise SystemExit(main())
