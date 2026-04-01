from __future__ import annotations

import importlib
import json
import shutil
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import pytest

from astrbot_sdk.runtime.loader import (
    discover_plugins,
    load_plugin_config,
    load_plugin_config_schema,
    load_plugin,
    load_plugin_spec,
    validate_plugin_spec,
)


def _write_plugin(
    plugin_dir: Path,
    *,
    plugin_name: str,
    class_name: str,
    main_source: str,
    extra_files: dict[str, str] | None = None,
    write_requirements: bool = True,
) -> None:
    plugin_dir.mkdir(parents=True, exist_ok=True)
    python_version = f"{sys.version_info.major}.{sys.version_info.minor}"
    (plugin_dir / "plugin.yaml").write_text(
        f"""
_schema_version: 2
name: {plugin_name}
author: tests
repo: astrbot/{plugin_name}
version: 1.0.0
desc: loader regression tests

runtime:
  python: "{python_version}"

components:
  - class: main:{class_name}
""".strip()
        + "\n",
        encoding="utf-8",
    )
    if write_requirements:
        (plugin_dir / "requirements.txt").write_text("", encoding="utf-8")
    (plugin_dir / "main.py").write_text(main_source.lstrip(), encoding="utf-8")

    for relative_path, content in (extra_files or {}).items():
        target = plugin_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _load_first_instance(plugin_dir: Path):
    plugin = load_plugin_spec(plugin_dir)
    validate_plugin_spec(plugin)
    loaded = load_plugin(plugin)
    assert loaded.instances
    return loaded.instances[0]


def _purge_module_roots(*roots: str) -> None:
    for root in {item for item in roots if item}:
        for module_name in list(sys.modules):
            if module_name == root or module_name.startswith(f"{root}."):
                sys.modules.pop(module_name, None)


@contextmanager
def _preserve_import_state(*module_roots: str) -> Iterator[None]:
    original_path = list(sys.path)
    original_modules = {
        name: module
        for name, module in sys.modules.items()
        if any(name == root or name.startswith(f"{root}.") for root in module_roots)
    }
    try:
        yield
    finally:
        sys.path[:] = original_path
        _purge_module_roots(*module_roots)
        sys.modules.update(original_modules)
        importlib.invalidate_caches()


def test_load_plugin_reloads_same_path_after_source_change(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "reload_plugin"
    _write_plugin(
        plugin_dir,
        plugin_name="reload_plugin",
        class_name="ReloadPlugin",
        main_source="""
from astrbot_sdk import Star
from support.value import CURRENT_VALUE


class ReloadPlugin(Star):
    value = CURRENT_VALUE
""",
        extra_files={
            "support/__init__.py": "",
            "support/value.py": 'CURRENT_VALUE = "v1"\n',
        },
    )

    with _preserve_import_state("main", "support"):
        first = _load_first_instance(plugin_dir)
        assert first.value == "v1"

        (plugin_dir / "support" / "value.py").write_text(
            'CURRENT_VALUE = "v2"\n',
            encoding="utf-8",
        )

        second = _load_first_instance(plugin_dir)
        assert second.value == "v2"
        assert second.__class__ is not first.__class__
        second_main_module = sys.modules[second.__class__.__module__]
        second_support_module = sys.modules[
            ".".join(second.__class__.__module__.split(".")[:-1] + ["support.value"])
        ]
        assert (
            Path(second_main_module.__file__).resolve()
            == (plugin_dir / "main.py").resolve()
        )
        assert (
            Path(second_support_module.__file__).resolve()
            == (plugin_dir / "support" / "value.py").resolve()
        )
        assert "main" not in sys.modules
        assert "support.value" not in sys.modules


def test_load_plugin_prefers_target_plugin_dir_for_generic_main_module(
    tmp_path: Path,
) -> None:
    foreign_dir = tmp_path / "foreign_main"
    foreign_dir.mkdir(parents=True, exist_ok=True)
    (foreign_dir / "main.py").write_text(
        """
from astrbot_sdk import Star


class SharedPlugin(Star):
    source = "foreign"
""".lstrip(),
        encoding="utf-8",
    )

    plugin_dir = tmp_path / "generic_main_plugin"
    _write_plugin(
        plugin_dir,
        plugin_name="generic_main_plugin",
        class_name="SharedPlugin",
        main_source="""
from astrbot_sdk import Star


class SharedPlugin(Star):
    source = "plugin"
""",
    )

    with _preserve_import_state("main"):
        sys.path.insert(0, str(foreign_dir.resolve()))
        sys.path.append(str(plugin_dir.resolve()))

        _purge_module_roots("main")
        __import__("main")
        foreign_main = sys.modules["main"]
        assert foreign_main.__file__ is not None
        assert (
            Path(foreign_main.__file__).resolve() == (foreign_dir / "main.py").resolve()
        )

        instance = _load_first_instance(plugin_dir)

        assert instance.source == "plugin"
        loaded_main_module = sys.modules[instance.__class__.__module__]
        assert (
            Path(loaded_main_module.__file__).resolve()
            == (plugin_dir / "main.py").resolve()
        )
        assert (
            Path(sys.modules["main"].__file__).resolve()
            == (foreign_dir / "main.py").resolve()
        )


def test_load_plugin_cleans_stale_bytecode_from_copied_fixture(tmp_path: Path) -> None:
    fixture_source = tmp_path / "fixture_source"
    _write_plugin(
        fixture_source,
        plugin_name="copied_fixture_plugin",
        class_name="FixturePlugin",
        main_source="""
from astrbot_sdk import Star


class FixturePlugin(Star):
    value = "fresh"
""",
    )

    cache_tag = sys.implementation.cache_tag or "cpython"
    stale_main_pyc = fixture_source / "__pycache__" / f"main.{cache_tag}.pyc"
    stale_main_pyc.parent.mkdir(parents=True, exist_ok=True)
    stale_main_pyc.write_bytes(b"stale main bytecode")

    stale_nested_pyc = (
        fixture_source / "nested" / "__pycache__" / f"helper.{cache_tag}.pyc"
    )
    stale_nested_pyc.parent.mkdir(parents=True, exist_ok=True)
    stale_nested_pyc.write_bytes(b"stale nested bytecode")

    stale_orphan_pyc = fixture_source / "orphan.pyc"
    stale_orphan_pyc.write_bytes(b"stale orphan bytecode")

    copied_fixture = tmp_path / "copied_fixture"
    shutil.copytree(fixture_source, copied_fixture)

    with _preserve_import_state("main"):
        instance = _load_first_instance(copied_fixture)

    assert instance.value == "fresh"
    assert not (copied_fixture / "nested" / "__pycache__").exists()
    assert not (copied_fixture / "orphan.pyc").exists()
    if (copied_fixture / "__pycache__" / f"main.{cache_tag}.pyc").exists():
        assert (
            copied_fixture / "__pycache__" / f"main.{cache_tag}.pyc"
        ).read_bytes() != b"stale main bytecode"


def test_discover_plugins_allows_plugins_without_requirements_file(
    tmp_path: Path,
) -> None:
    plugins_dir = tmp_path / "plugins"
    plugin_dir = plugins_dir / "no_requirements"
    _write_plugin(
        plugin_dir,
        plugin_name="no_requirements",
        class_name="NoRequirementsPlugin",
        main_source="""
from astrbot_sdk import Star


class NoRequirementsPlugin(Star):
    value = "no-deps"
""",
        write_requirements=False,
    )

    discovered = discover_plugins(plugins_dir)

    assert [plugin.name for plugin in discovered.plugins] == ["no_requirements"]
    assert discovered.skipped_plugins == {}
    assert discovered.issues == []

    with _preserve_import_state("main"):
        instance = _load_first_instance(plugin_dir)

    assert instance.value == "no-deps"


def test_plugin_config_helpers_treat_non_object_json_as_empty(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "config_plugin"
    _write_plugin(
        plugin_dir,
        plugin_name="config_plugin",
        class_name="ConfigPlugin",
        main_source="""
from astrbot_sdk import Star


class ConfigPlugin(Star):
    pass
""",
    )
    plugin = load_plugin_spec(plugin_dir)
    validate_plugin_spec(plugin)

    (plugin_dir / "_conf_schema.json").write_text(
        json.dumps(["not-an-object"]),
        encoding="utf-8",
    )
    config_path = plugin_dir / "data" / "config" / "config_plugin_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(json.dumps(["still-not-an-object"]), encoding="utf-8")

    assert load_plugin_config_schema(plugin) == {}
    assert load_plugin_config(plugin) == {}


def test_load_plugin_rejects_capability_without_plugin_prefix(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "wrong_capability_namespace"
    _write_plugin(
        plugin_dir,
        plugin_name="alpha_plugin",
        class_name="WrongCapabilityPlugin",
        main_source="""
from astrbot_sdk import Star, provide_capability


class WrongCapabilityPlugin(Star):
    @provide_capability("shared.echo", description="invalid capability namespace")
    async def echo(self, payload: dict) -> dict:
        return payload
""",
    )

    plugin = load_plugin_spec(plugin_dir)
    validate_plugin_spec(plugin)

    with (
        _preserve_import_state("main"),
        pytest.raises(ValueError, match="必须使用当前插件名前缀"),
    ):
        load_plugin(plugin)


def test_load_plugin_rejects_duplicate_capability_names(tmp_path: Path) -> None:
    plugin_dir = tmp_path / "duplicate_capability_names"
    _write_plugin(
        plugin_dir,
        plugin_name="alpha_plugin",
        class_name="DuplicateCapabilityPlugin",
        main_source="""
from astrbot_sdk import Star, provide_capability


class DuplicateCapabilityPlugin(Star):
    @provide_capability("alpha_plugin.echo", description="first capability")
    async def first(self, payload: dict) -> dict:
        return payload

    @provide_capability("alpha_plugin.echo", description="duplicate capability")
    async def second(self, payload: dict) -> dict:
        return payload
""",
    )

    plugin = load_plugin_spec(plugin_dir)
    validate_plugin_spec(plugin)

    with _preserve_import_state("main"), pytest.raises(ValueError, match="重复定义"):
        load_plugin(plugin)
