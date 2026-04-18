from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from unittest.mock import AsyncMock

from astrbot_sdk.context import CancelToken, Context
from astrbot_sdk.errors import AstrBotError, ErrorCodes
from astrbot_sdk.llm.agents import AgentSpec
from astrbot_sdk.llm.entities import LLMToolSpec
from astrbot_sdk.protocol.descriptors import CapabilityDescriptor
from astrbot_sdk.protocol.messages import InvokeMessage
from astrbot_sdk.runtime.loader import (
    LoadedAgent,
    LoadedCapability,
    LoadedPlugin,
    PluginDiscoveryIssue,
    PluginSpec,
)
from astrbot_sdk.runtime.worker import (
    GroupPluginRuntimeState,
    GroupWorkerRuntime,
    PluginWorkerRuntime,
)


def _plugin_spec(name: str, tmp_path: Path | None = None) -> PluginSpec:
    import tempfile

    base_dir = tmp_path if tmp_path else Path(tempfile.gettempdir())
    plugin_dir = base_dir / name
    return PluginSpec(
        name=name,
        plugin_dir=plugin_dir,
        manifest_path=plugin_dir / "plugin.yaml",
        requirements_path=plugin_dir / "requirements.txt",
        python_version="3.10",
        manifest_data={"name": name},
    )


@pytest.mark.asyncio
async def test_plugin_worker_handle_invoke_maps_lookup_error_to_astrbot_error() -> None:
    runtime = object.__new__(PluginWorkerRuntime)
    runtime.dispatcher = SimpleNamespace(invoke=AsyncMock())
    runtime.capability_dispatcher = SimpleNamespace(
        invoke=AsyncMock(side_effect=LookupError("missing")),
    )

    with pytest.raises(AstrBotError) as exc_info:
        await PluginWorkerRuntime._handle_invoke(
            runtime,
            InvokeMessage(id="req-cap", capability="missing.capability", input={}),
            CancelToken(),
        )

    assert exc_info.value.code == ErrorCodes.CAPABILITY_NOT_FOUND
    assert "missing.capability" in exc_info.value.message


@pytest.mark.asyncio
async def test_plugin_worker_handle_cancel_fans_out_to_both_dispatchers() -> None:
    runtime = object.__new__(PluginWorkerRuntime)
    runtime.dispatcher = SimpleNamespace(cancel=AsyncMock())
    runtime.capability_dispatcher = SimpleNamespace(cancel=AsyncMock())

    await PluginWorkerRuntime._handle_cancel(runtime, "req-123")

    runtime.dispatcher.cancel.assert_awaited_once_with("req-123")
    runtime.capability_dispatcher.cancel.assert_awaited_once_with("req-123")


@pytest.mark.asyncio
async def test_plugin_worker_start_initializes_metadata_and_handlers(
    tmp_path: Path,
) -> None:
    runtime = object.__new__(PluginWorkerRuntime)
    runtime.plugin = _plugin_spec("alpha", tmp_path)
    runtime.worker_id = "alpha-worker"
    runtime.loaded_plugin = LoadedPlugin(
        plugin=runtime.plugin,
        handlers=[],
        capabilities=[],
        llm_tools=[],
        agents=[],
        instances=[],
    )
    runtime.issues = []
    lifecycle_calls: list[str] = []

    class _Peer:
        def __init__(self) -> None:
            self.started = False
            self.stopped = False
            self.initialize_calls: list[dict[str, object]] = []

        async def start(self) -> None:
            self.started = True

        async def initialize(
            self, handlers, *, provided_capabilities, metadata
        ) -> None:
            self.initialize_calls.append(
                {
                    "handlers": list(handlers),
                    "provided_capabilities": list(provided_capabilities),
                    "metadata": dict(metadata),
                }
            )

        async def stop(self) -> None:
            self.stopped = True

    runtime.peer = _Peer()

    async def fake_run_lifecycle(method_name: str) -> None:
        lifecycle_calls.append(method_name)

    runtime._run_lifecycle = fake_run_lifecycle  # type: ignore[method-assign]

    await PluginWorkerRuntime.start(runtime)

    assert runtime.peer.started is True
    assert lifecycle_calls == ["on_start"]
    assert runtime.peer.initialize_calls[0]["metadata"]["worker_id"] == "alpha-worker"
    assert runtime.peer.initialize_calls[0]["metadata"]["loaded_plugins"] == ["alpha"]
    assert runtime.peer.initialize_calls[0]["metadata"]["worker_registry"] == [
        {
            "name": "alpha",
            "display_name": "alpha",
            "description": "",
            "repo": "",
            "author": "",
            "version": "0.0.0",
            "enabled": True,
            "config": {},
        }
    ]


@pytest.mark.asyncio
async def test_plugin_worker_start_runs_on_stop_when_initialize_fails(
    tmp_path: Path,
) -> None:
    runtime = object.__new__(PluginWorkerRuntime)
    runtime.plugin = _plugin_spec("alpha", tmp_path)
    runtime.worker_id = "alpha-worker"
    runtime.loaded_plugin = LoadedPlugin(
        plugin=runtime.plugin,
        handlers=[],
        capabilities=[],
        llm_tools=[],
        agents=[],
        instances=[],
    )
    runtime.issues = []
    lifecycle_calls: list[str] = []

    class _Peer:
        def __init__(self) -> None:
            self.stopped = False

        async def start(self) -> None:
            return None

        async def initialize(
            self, handlers, *, provided_capabilities, metadata
        ) -> None:
            del handlers, provided_capabilities, metadata
            raise RuntimeError("initialize failed")

        async def stop(self) -> None:
            self.stopped = True

    runtime.peer = _Peer()

    async def fake_run_lifecycle(method_name: str) -> None:
        lifecycle_calls.append(method_name)

    runtime._run_lifecycle = fake_run_lifecycle  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="initialize failed"):
        await PluginWorkerRuntime.start(runtime)

    assert lifecycle_calls == ["on_start", "on_stop"]
    assert runtime.peer.stopped is True


@pytest.mark.asyncio
async def test_group_worker_start_raises_when_all_plugins_become_inactive(
    tmp_path: Path,
) -> None:
    runtime = object.__new__(GroupWorkerRuntime)
    alpha = _plugin_spec("alpha", tmp_path)
    runtime.worker_id = "worker-group"
    runtime.plugins = [alpha]
    runtime._plugin_states = [
        GroupPluginRuntimeState(
            plugin=alpha,
            loaded_plugin=LoadedPlugin(plugin=alpha, handlers=[], instances=[]),
            lifecycle_context=Context(peer=SimpleNamespace(), plugin_id="alpha"),
        )
    ]
    runtime._active_plugin_states = list(runtime._plugin_states)
    runtime.skipped_plugins = {}
    runtime.issues = []
    refresh_snapshots: list[list[str]] = []

    class _Peer:
        def __init__(self) -> None:
            self.started = False
            self.stopped = False

        async def start(self) -> None:
            self.started = True

        async def initialize(
            self, handlers, *, provided_capabilities, metadata
        ) -> None:
            del handlers, provided_capabilities, metadata
            raise AssertionError("initialize should not run without active plugins")

        async def stop(self) -> None:
            self.stopped = True

    runtime.peer = _Peer()

    def fake_refresh_dispatchers() -> None:
        refresh_snapshots.append(
            [state.plugin.name for state in runtime._active_plugin_states]
        )

    async def fake_run_lifecycle(state, method_name: str) -> None:
        del state, method_name
        raise RuntimeError("on_start failed")

    runtime._refresh_dispatchers = fake_refresh_dispatchers  # type: ignore[method-assign]
    runtime._run_lifecycle = fake_run_lifecycle  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="has no active plugins"):
        await GroupWorkerRuntime.start(runtime)

    assert runtime.peer.started is True
    assert runtime.peer.stopped is True
    assert runtime.skipped_plugins == {"alpha": "on_start failed"}
    assert runtime.issues[0].phase == "lifecycle"
    assert refresh_snapshots[-1] == []


def test_group_worker_initialize_metadata_aggregates_runtime_state(
    tmp_path: Path,
) -> None:
    alpha = _plugin_spec("alpha", tmp_path)
    beta = _plugin_spec("beta", tmp_path)
    alpha_capability = LoadedCapability(
        descriptor=CapabilityDescriptor(
            name="alpha.echo",
            description="echo",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
        ),
        callable=lambda: None,
        owner=object(),
        plugin_id="alpha",
    )
    alpha_tool = LoadedAgent(
        spec=AgentSpec(
            name="alpha-agent",
            description="agent",
            runner_class="alpha.runner:Runner",
        ),
        runner_class=type("Runner", (), {}),
        plugin_id="alpha",
    )
    alpha_llm_tool = LoadedPlugin(
        plugin=alpha,
        handlers=[],
        capabilities=[alpha_capability],
        llm_tools=[
            SimpleNamespace(
                spec=LLMToolSpec.create(name="alpha-tool", description="tool")
            )
        ],
        agents=[alpha_tool],
        instances=[object()],
    )
    beta_plugin = LoadedPlugin(
        plugin=beta,
        handlers=[],
        capabilities=[],
        llm_tools=[],
        agents=[],
        instances=[object()],
    )
    runtime = object.__new__(GroupWorkerRuntime)
    runtime.worker_id = "worker-group"
    runtime.plugins = [alpha, beta]
    runtime.skipped_plugins = {"beta": "start failed"}
    runtime.issues = [
        PluginDiscoveryIssue(
            severity="error",
            phase="load",
            plugin_id="beta",
            message="插件加载失败",
            details="start failed",
        )
    ]
    runtime._active_plugin_states = [
        GroupPluginRuntimeState(
            plugin=alpha,
            loaded_plugin=alpha_llm_tool,
            lifecycle_context=Context(peer=SimpleNamespace(), plugin_id="alpha"),
        ),
        GroupPluginRuntimeState(
            plugin=beta,
            loaded_plugin=beta_plugin,
            lifecycle_context=Context(peer=SimpleNamespace(), plugin_id="beta"),
        ),
    ]

    metadata = GroupWorkerRuntime._initialize_metadata(runtime)

    assert metadata["worker_id"] == "worker-group"
    assert metadata["plugins"] == ["alpha", "beta"]
    assert metadata["loaded_plugins"] == ["alpha", "beta"]
    assert metadata["skipped_plugins"] == {"beta": "start failed"}
    assert metadata["worker_registry"] == [
        {
            "name": "alpha",
            "display_name": "alpha",
            "description": "",
            "repo": "",
            "author": "",
            "version": "0.0.0",
            "enabled": True,
            "config": {},
        },
        {
            "name": "beta",
            "display_name": "beta",
            "description": "",
            "repo": "",
            "author": "",
            "version": "0.0.0",
            "enabled": True,
            "config": {},
        },
    ]
    assert metadata["capability_sources"] == {"alpha.echo": "alpha"}
    assert metadata["llm_tools"] == [
        {
            "name": "alpha-tool",
            "description": "tool",
            "parameters_schema": {"type": "object", "properties": {}},
            "active": True,
            "plugin_id": "alpha",
        }
    ]
    assert metadata["agents"] == [
        {
            "name": "alpha-agent",
            "description": "agent",
            "tool_names": [],
            "runner_class": "alpha.runner:Runner",
            "plugin_id": "alpha",
        }
    ]
