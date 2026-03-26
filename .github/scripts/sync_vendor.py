#!/usr/bin/env python3

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path
from re import MULTILINE, DOTALL, compile as re_compile
from textwrap import dedent
from typing import NoReturn


VENDOR_README = dedent(
    """\
    # AstrBot SDK Vendor Snapshot

    This directory is the minimized subtree payload consumed by the AstrBot main
    repository.

    - `src/astrbot_sdk/` keeps only the runtime SDK package contents required by AstrBot
    - testing helpers, developer templates, and embedded markdown reference files are excluded
    - `pyproject.toml` keeps the src-layout package discovery but drops dev/test-only metadata
    - `VENDORED.md` describes the vendoring contract
    - tests, docs, CI files, and other source-repo-only content stay outside this directory
    """
)

VENDORED_NOTICE = dedent(
    """\
    # Vendored Snapshot Notes

    This directory is a minimized snapshot for the AstrBot main repository to import
    via `git subtree`.

    - The source of truth is this `astrbot-sdk` repository.
    - `vendor/src/astrbot_sdk/` is synchronized from `src/astrbot_sdk/`, but only for
      the runtime SDK subset consumed by AstrBot.
    - vendored snapshots exclude testing helpers, developer skill templates, and
      markdown reference assets that are not needed at runtime.
    - `vendor/pyproject.toml` keeps src-layout package discovery, but strips
      test/dev-only sections so the subtree stays runtime-focused.
    - Do not edit vendored files directly inside the AstrBot main repository.
    - Tests and documentation remain only in the SDK source repository and are not
      copied into the vendored snapshot.
    - If the vendored copy needs changes, update the SDK source repository first and
      regenerate the `vendor/` snapshot.
    """
)

EXPECTED_TOP_LEVEL = {
    "LICENSE",
    "README.md",
    "VENDORED.md",
    "pyproject.toml",
    "src",
}
FORBIDDEN_PARTS = {"tests", "docs", ".github"}
SRC_LAYOUT_MARKER = "# Package Discovery (src layout)"
SRC_DISCOVERY_LINE = 'where = ["src"]'
PYPROJECT_SECTIONS_TO_DROP = (
    "tool.pytest.ini_options",
    "tool.setuptools.package-data",
    "project.optional-dependencies",
)
PACKAGE_EXCLUDE_RELATIVE_PATHS = (
    Path("AGENTS.md"),
    Path("testing.py"),
    Path("_testing_support.py"),
    Path("_internal") / "testing_support.py",
    Path("templates") / "skills",
)


def fail(message: str) -> NoReturn:
    print(f"error: {message}", file=sys.stderr)
    raise SystemExit(1)


def repo_root() -> Path:
    result = subprocess.run(
        ["git", "rev-parse", "--show-toplevel"],
        check=True,
        capture_output=True,
        text=True,
    )
    return Path(result.stdout.strip()).resolve()


def write_text(path: Path, content: str) -> None:
    path.write_text(content, encoding="utf-8", newline="\n")


def drop_toml_section(toml_text: str, section_name: str) -> str:
    section_pattern = re_compile(
        rf"(?ms)^\[{section_name}\]\n.*?(?=^\[|\Z)",
        MULTILINE | DOTALL,
    )
    return section_pattern.sub("", toml_text)


def build_vendor_pyproject(root_pyproject_text: str) -> str:
    if SRC_LAYOUT_MARKER not in root_pyproject_text:
        fail("root pyproject.toml is missing the expected src layout marker")
    if SRC_DISCOVERY_LINE not in root_pyproject_text:
        fail(
            "root pyproject.toml is missing the expected setuptools src discovery line"
        )

    vendor_pyproject = root_pyproject_text
    for section_name in PYPROJECT_SECTIONS_TO_DROP:
        vendor_pyproject = drop_toml_section(vendor_pyproject, section_name)

    if SRC_DISCOVERY_LINE not in vendor_pyproject:
        fail("vendor/pyproject.toml must retain src-based package discovery")

    return vendor_pyproject.strip() + "\n"


def ensure_runtime_only_vendor_package(vendor_pkg_dir: Path) -> None:
    for relative_path in PACKAGE_EXCLUDE_RELATIVE_PATHS:
        target_path = vendor_pkg_dir / relative_path
        if target_path.is_dir():
            shutil.rmtree(target_path)
        elif target_path.exists():
            target_path.unlink()

    for path in vendor_pkg_dir.rglob("*"):
        if path.is_dir() and path.name == "__pycache__":
            shutil.rmtree(path)
        elif path.is_file() and path.suffix in {".pyc", ".pyo"}:
            path.unlink()


def validate_vendor_layout(vendor_dir: Path, root_license: Path) -> None:
    actual_top_level = {path.name for path in vendor_dir.iterdir()}
    if actual_top_level != EXPECTED_TOP_LEVEL:
        fail(
            "vendor/ top-level contents are invalid; "
            f"expected {sorted(EXPECTED_TOP_LEVEL)}, got {sorted(actual_top_level)}"
        )

    for path in vendor_dir.rglob("*"):
        if any(part in FORBIDDEN_PARTS for part in path.parts):
            fail(f"vendor/ contains forbidden path: {path}")

    if root_license.read_bytes() != (vendor_dir / "LICENSE").read_bytes():
        fail("vendor/LICENSE is out of sync with root LICENSE")

    vendored_pyproject = (vendor_dir / "pyproject.toml").read_text(encoding="utf-8")
    if SRC_DISCOVERY_LINE not in vendored_pyproject:
        fail("vendor/pyproject.toml must retain src-based package discovery")
    for section_name in PYPROJECT_SECTIONS_TO_DROP:
        if f"[{section_name}]" in vendored_pyproject:
            fail(
                f"vendor/pyproject.toml still contains non-runtime section [{section_name}]"
            )
    if not (vendor_dir / "src" / "astrbot_sdk").is_dir():
        fail("vendor/src/astrbot_sdk is missing")
    for relative_path in PACKAGE_EXCLUDE_RELATIVE_PATHS:
        if (vendor_dir / "src" / "astrbot_sdk" / relative_path).exists():
            fail(
                f"vendor runtime package still contains excluded path: {relative_path}"
            )


def build_vendor_snapshot(root: Path) -> None:
    src_dir = root / "src" / "astrbot_sdk"
    vendor_dir = root / "vendor"
    vendor_pkg_dir = vendor_dir / "src" / "astrbot_sdk"
    root_license = root / "LICENSE"
    root_pyproject = root / "pyproject.toml"

    if not src_dir.is_dir():
        fail(f"expected source package at {src_dir}")
    if not root_license.is_file():
        fail(f"expected root LICENSE at {root_license}")
    if not root_pyproject.is_file():
        fail(f"expected root pyproject.toml at {root_pyproject}")

    if vendor_dir.exists():
        shutil.rmtree(vendor_dir)

    shutil.copytree(
        src_dir,
        vendor_pkg_dir,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    ensure_runtime_only_vendor_package(vendor_pkg_dir)

    write_text(vendor_dir / "README.md", VENDOR_README)
    shutil.copy2(root_license, vendor_dir / "LICENSE")
    write_text(
        vendor_dir / "pyproject.toml",
        build_vendor_pyproject(root_pyproject.read_text(encoding="utf-8")),
    )
    write_text(vendor_dir / "VENDORED.md", VENDORED_NOTICE)

    validate_vendor_layout(vendor_dir, root_license)
    print(f"vendor snapshot refreshed from {src_dir}")


def main() -> None:
    build_vendor_snapshot(repo_root())


if __name__ == "__main__":
    main()
