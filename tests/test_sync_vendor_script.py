from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_sync_vendor_module():
    script_path = (
        Path(__file__).resolve().parent.parent
        / ".github"
        / "scripts"
        / "sync_vendor.py"
    )
    spec = importlib.util.spec_from_file_location("sync_vendor_script", script_path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_build_vendor_snapshot_keeps_runtime_and_testing_contract(tmp_path: Path):
    module = _load_sync_vendor_module()
    repo_root = tmp_path / "repo"
    src_package = repo_root / "src" / "astrbot_sdk"
    cached_dir = src_package / "__pycache__"
    project_note_templates = src_package / "templates" / "project_notes"
    skill_templates = (
        src_package / "templates" / "skills" / "astrbot-plugin-dev" / "references"
    )

    cached_dir.mkdir(parents=True)
    project_note_templates.mkdir(parents=True)
    skill_templates.mkdir(parents=True)
    (src_package / "__init__.py").write_text("__all__ = ['demo']\n", encoding="utf-8")
    (src_package / "testing.py").write_text("TESTING = True\n", encoding="utf-8")
    (src_package / "_testing_support.py").write_text(
        "SUPPORT = True\n", encoding="utf-8"
    )
    (src_package / "AGENTS.md").write_text("internal notes\n", encoding="utf-8")
    (src_package / "_internal").mkdir(parents=True)
    (src_package / "_internal" / "testing_support.py").write_text(
        "SUPPORT = True\n",
        encoding="utf-8",
    )
    (project_note_templates / "AGENTS.md").write_text(
        "AstrBotError\n",
        encoding="utf-8",
    )
    (project_note_templates / "CLAUDE.md").write_text(
        "ErrorCodes\n",
        encoding="utf-8",
    )
    (skill_templates / "api-quick-ref.md").write_text("reference\n", encoding="utf-8")
    (cached_dir / "ignored.pyc").write_bytes(b"cache")
    (repo_root / "LICENSE").write_text("demo license\n", encoding="utf-8")
    (repo_root / "pyproject.toml").write_text(
        """
[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[project]
name = "astrbot-sdk"
readme = "README.md"

# ============================================================
# Package Discovery (src layout) - Hatchling
# ============================================================
[tool.hatch.build.targets.wheel]
packages = ["src/astrbot_sdk"]

[tool.pytest.ini_options]
markers = ["unit"]

[project.optional-dependencies]
dev = ["pytest>=8.0.0", "ruff>=0.4.0"]
""".strip()
        + "\n",
        encoding="utf-8",
    )

    module.build_vendor_snapshot(repo_root)

    vendor_root = repo_root / "vendor"
    assert {path.name for path in vendor_root.iterdir()} == {
        "LICENSE",
        "README.md",
        "VENDORED.md",
        "pyproject.toml",
        "src",
    }
    vendored_pyproject = (vendor_root / "pyproject.toml").read_text(encoding="utf-8")
    assert "# Package Discovery (src layout) - Hatchling" in vendored_pyproject
    assert 'packages = ["src/astrbot_sdk"]' in vendored_pyproject
    assert "[tool.pytest.ini_options]" not in vendored_pyproject
    assert "[project.optional-dependencies]" not in vendored_pyproject
    assert not (vendor_root / "src" / "astrbot_sdk" / "__pycache__").exists()
    assert (vendor_root / "src" / "astrbot_sdk" / "testing.py").exists()
    assert (vendor_root / "src" / "astrbot_sdk" / "_testing_support.py").exists()
    assert not (vendor_root / "src" / "astrbot_sdk" / "AGENTS.md").exists()
    assert (
        vendor_root / "src" / "astrbot_sdk" / "_internal" / "testing_support.py"
    ).exists()
    assert (
        vendor_root
        / "src"
        / "astrbot_sdk"
        / "templates"
        / "project_notes"
        / "AGENTS.md"
    ).exists()
    assert (
        vendor_root
        / "src"
        / "astrbot_sdk"
        / "templates"
        / "project_notes"
        / "CLAUDE.md"
    ).exists()
    assert not (vendor_root / "src" / "astrbot_sdk" / "templates" / "skills").exists()
    assert not (vendor_root / "src" / "astrbot_sdk" / "_command_model.py").exists()
    assert not (vendor_root / "src" / "astrbot_sdk" / "_plugin_logger.py").exists()
    assert not (vendor_root / "src" / "astrbot_sdk" / "_star_runtime.py").exists()
    assert not (vendor_root / "src" / "astrbot_sdk" / "message_components.py").exists()
    assert not (vendor_root / "src" / "astrbot_sdk" / "message_result.py").exists()
    assert not (vendor_root / "src" / "astrbot_sdk" / "message_session.py").exists()


def test_cli_test_template_dependency_remains_in_vendored_contract():
    module = _load_sync_vendor_module()
    cli_source = (
        Path(__file__).resolve().parent.parent / "src" / "astrbot_sdk" / "cli.py"
    ).read_text(encoding="utf-8")

    assert (
        "from astrbot_sdk.testing import MockContext, MockMessageEvent, PluginHarness"
        in cli_source
    )
    assert Path("testing.py") not in module.PACKAGE_EXCLUDE_RELATIVE_PATHS
    assert Path("_testing_support.py") not in module.PACKAGE_EXCLUDE_RELATIVE_PATHS
    assert (
        Path("_internal") / "testing_support.py"
    ) not in module.PACKAGE_EXCLUDE_RELATIVE_PATHS


def test_build_vendor_pyproject_rejects_non_hatchling_backend():
    module = _load_sync_vendor_module()

    pyproject_text = """
[build-system]
requires = ["setuptools>=80", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "astrbot-sdk"
readme = "README.md"
""".strip()

    try:
        module.build_vendor_pyproject(pyproject_text)
    except SystemExit as exc:
        assert exc.code == 1
    else:
        raise AssertionError(
            "build_vendor_pyproject should reject a non-hatchling backend"
        )
