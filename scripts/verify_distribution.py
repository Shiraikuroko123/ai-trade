from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


WHEEL_REQUIRED = {
    "ai_trade/default_config.json",
    "ai_trade/default_security_master.json",
    "ai_trade/web/assets/__init__.py",
    "ai_trade/web/assets/index.html",
    "ai_trade/web/assets/app.css",
    "ai_trade/web/assets/app.js",
    "ai_trade/web/assets/auth.css",
    "ai_trade/web/assets/auth.js",
    "ai_trade/web/assets/login.html",
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
    "docs/assets/workstation-overview.png",
    "docs/BROKER_ADAPTERS.md",
    "docs/ECOSYSTEM.md",
    "docs/PAPER_TRADING.md",
    "docs/RESEARCH_METHODOLOGY.md",
    "docs/UNIVERSE.md",
    "pyproject.toml",
    "scripts/bootstrap.ps1",
    "scripts/install_paper_task.ps1",
    "scripts/run_daily_paper.ps1",
    "scripts/verify_distribution.py",
    "src/ai_trade/default_config.json",
    "src/ai_trade/default_security_master.json",
    "src/ai_trade/web/assets/index.html",
    "src/ai_trade/web/assets/app.css",
    "src/ai_trade/web/assets/app.js",
    "src/ai_trade/web/assets/auth.css",
    "src/ai_trade/web/assets/auth.js",
    "src/ai_trade/web/assets/login.html",
    "src/ai_trade/web/auth.py",
}

BANNED_PARTS = {
    ".venv",
    "__pycache__",
    ".pytest_cache",
    ".ruff_cache",
    "logs",
    "reports",
    "state",
}
SENSITIVE_NAME_MARKERS = ("beta-users", "beta_users", "内测名单")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify wheel and source distribution release contents"
    )
    parser.add_argument("directory", nargs="?", default="dist")
    args = parser.parse_args(argv)
    directory = Path(args.directory).resolve()
    wheel = _single_artifact(directory, "*.whl", "wheel")
    sdist = _single_artifact(directory, "*.tar.gz", "source distribution")

    with zipfile.ZipFile(wheel) as archive:
        wheel_names = archive.namelist()
    _verify_safe_unique_names(wheel_names, wheel.name)
    wheel_members = set(wheel_names)
    _require_members(wheel_members, WHEEL_REQUIRED, wheel.name)
    _require_suffix(wheel_members, ".dist-info/licenses/LICENSE", wheel.name)
    _require_suffix(wheel_members, ".dist-info/licenses/NOTICE", wheel.name)

    with tarfile.open(sdist, mode="r:gz") as archive:
        sdist_names = archive.getnames()
    _verify_safe_unique_names(sdist_names, sdist.name)
    sdist_members = _strip_sdist_root(sdist_names, sdist.name)
    _require_members(sdist_members, SDIST_REQUIRED, sdist.name)

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


def _verify_safe_unique_names(names: list[str], archive_name: str) -> None:
    if len(names) != len(set(names)):
        raise SystemExit(f"{archive_name} contains duplicate member names")
    for name in names:
        path = PurePosixPath(name)
        if path.is_absolute() or ".." in path.parts:
            raise SystemExit(f"{archive_name} contains an unsafe path: {name}")
        if BANNED_PARTS.intersection(path.parts) or path.suffix in {".pyc", ".pyo"}:
            raise SystemExit(f"{archive_name} contains a generated file: {name}")
        folded_name = path.name.casefold()
        if any(marker in folded_name for marker in SENSITIVE_NAME_MARKERS):
            raise SystemExit(f"{archive_name} contains a beta-user file: {name}")


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
