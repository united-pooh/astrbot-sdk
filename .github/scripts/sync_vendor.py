#!/usr/bin/env python3

from __future__ import annotations

import shutil
import subprocess
import sys
import tomllib
from pathlib import Path
from re import MULTILINE, DOTALL, compile as re_compile
from textwrap import dedent
from typing import Any, NoReturn


VENDOR_README = dedent(
    """\
    # AstrBot SDK Vendor Snapshot

    This directory is the minimized subtree payload consumed by the AstrBot main
    repository.

    - `src/astrbot_sdk/` keeps the runtime SDK package plus the minimal testing
      helpers that AstrBot and SDK-generated templates still treat as part of the
      vendored contract
    - agent skill templates and embedded markdown reference files are excluded
    - root project-note templates for `astr init` stay vendored because the CLI
      still generates `AGENTS.md` / `CLAUDE.md` by default
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
    - `vendor/src/astrbot_sdk/` is synchronized from `src/astrbot_sdk/`.
    - Vendored snapshots keep the runtime SDK plus the minimal testing helpers
      (`testing.py`, `_testing_support.py`, `_internal/testing_support.py`) because
      AstrBot and SDK-generated test templates still depend on them.
    - Vendored snapshots exclude agent skill templates and markdown reference
      assets that are not needed by the subtree consumer, but retain the default
      `AGENTS.md` / `CLAUDE.md` project-note templates used by `astr init`.
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
EXPECTED_BUILD_BACKEND = "hatchling.build"
EXPECTED_WHEEL_PACKAGES = ["src/astrbot_sdk"]
PYPROJECT_SECTIONS_TO_DROP = (
    "tool.pytest.ini_options",
    "project.optional-dependencies",
)
PACKAGE_EXCLUDE_RELATIVE_PATHS = (
    Path("AGENTS.md"),
    Path("templates") / "skills",
)
REQUIRED_VENDOR_PACKAGE_RELATIVE_PATHS = (
    Path("testing.py"),
    Path("_testing_support.py"),
    Path("_internal") / "testing_support.py",
    Path("templates") / "project_notes" / "AGENTS.md",
    Path("templates") / "project_notes" / "CLAUDE.md",
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


def parse_toml_document(toml_text: str, *, source: str) -> dict[str, Any]:
    try:
        data = tomllib.loads(toml_text)
    except tomllib.TOMLDecodeError as exc:
        fail(f"{source} is not valid TOML: {exc}")
    if not isinstance(data, dict):
        fail(f"{source} must decode to a TOML table")
    return data


def get_nested_table(
    mapping: dict[str, Any],
    *keys: str,
    source: str,
    required: bool = True,
) -> dict[str, Any] | None:
    current: Any = mapping
    table_name = ".".join(keys)
    for index, key in enumerate(keys):
        if not isinstance(current, dict):
            fail(f"{source} [{'.'.join(keys[:index])}] is not a TOML table")
        if key not in current:
            if required:
                fail(f"{source} is missing [{table_name}]")
            return None
        current = current[key]
    if not isinstance(current, dict):
        fail(f"{source} [{table_name}] is not a TOML table")
    return current


def has_nested_table(mapping: dict[str, Any], *keys: str) -> bool:
    current: Any = mapping
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return False
        current = current[key]
    return isinstance(current, dict)


def validate_hatchling_build_config(
    pyproject_data: dict[str, Any], *, source: str
) -> None:
    build_system = get_nested_table(pyproject_data, "build-system", source=source)
    assert build_system is not None

    requires = build_system.get("requires")
    if not isinstance(requires, list) or not any(
        isinstance(requirement, str) and requirement.startswith("hatchling")
        for requirement in requires
    ):
        fail(f"{source} [build-system].requires must include hatchling")

    build_backend = build_system.get("build-backend")
    if build_backend != EXPECTED_BUILD_BACKEND:
        fail(
            f"{source} [build-system].build-backend must be {EXPECTED_BUILD_BACKEND!r}"
        )

    wheel_target = get_nested_table(
        pyproject_data,
        "tool",
        "hatch",
        "build",
        "targets",
        "wheel",
        source=source,
    )
    assert wheel_target is not None
    packages = wheel_target.get("packages")
    if packages != EXPECTED_WHEEL_PACKAGES:
        fail(
            f"{source} [tool.hatch.build.targets.wheel].packages must be "
            f"{EXPECTED_WHEEL_PACKAGES!r}"
        )


def build_vendor_pyproject(root_pyproject_text: str) -> str:
    root_pyproject_data = parse_toml_document(
        root_pyproject_text,
        source="root pyproject.toml",
    )
    validate_hatchling_build_config(
        root_pyproject_data,
        source="root pyproject.toml",
    )

    # Preserve the hand-edited formatting and comments in vendor/pyproject.toml
    # while still validating the build contract semantically via tomllib.
    vendor_pyproject = root_pyproject_text
    for section_name in PYPROJECT_SECTIONS_TO_DROP:
        vendor_pyproject = drop_toml_section(vendor_pyproject, section_name)

    vendor_pyproject_data = parse_toml_document(
        vendor_pyproject,
        source="vendor/pyproject.toml",
    )
    validate_hatchling_build_config(
        vendor_pyproject_data,
        source="vendor/pyproject.toml",
    )
    for section_name in PYPROJECT_SECTIONS_TO_DROP:
        if has_nested_table(vendor_pyproject_data, *section_name.split(".")):
            fail(
                f"vendor/pyproject.toml still contains non-runtime section [{section_name}]"
            )

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
    vendored_pyproject_data = parse_toml_document(
        vendored_pyproject,
        source="vendor/pyproject.toml",
    )
    validate_hatchling_build_config(
        vendored_pyproject_data,
        source="vendor/pyproject.toml",
    )
    for section_name in PYPROJECT_SECTIONS_TO_DROP:
        if has_nested_table(vendored_pyproject_data, *section_name.split(".")):
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
    for relative_path in REQUIRED_VENDOR_PACKAGE_RELATIVE_PATHS:
        if not (vendor_dir / "src" / "astrbot_sdk" / relative_path).exists():
            fail(f"vendor runtime package is missing required path: {relative_path}")


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
