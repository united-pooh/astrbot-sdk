from __future__ import annotations

import asyncio
import sys
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from types import SimpleNamespace
from typing import IO, Any, cast

import pytest

from src.astrbot_sdk.context import CancelToken
from src.astrbot_sdk.errors import AstrBotError, ErrorCodes
from src.astrbot_sdk.protocol.codec import JsonProtocolCodec, MsgpackProtocolCodec
from src.astrbot_sdk.protocol.descriptors import CapabilityDescriptor
from src.astrbot_sdk.runtime._streaming import StreamExecution
from src.astrbot_sdk.runtime import bootstrap as bootstrap_module
from src.astrbot_sdk.runtime.capability_dispatcher import CapabilityDispatcher
from src.astrbot_sdk.runtime.loader import LoadedCapability, LoadedPlugin, PluginSpec
from src.astrbot_sdk.runtime.peer import Peer
from src.astrbot_sdk.runtime import supervisor as supervisor_module
from src.astrbot_sdk.runtime.transport import Transport
from src.astrbot_sdk.runtime import worker as worker_module


class _RecordingRuntime:
    def __init__(self, *, peer_name: str = "runtime-peer") -> None:
        self.peer = SimpleNamespace(name=peer_name)
        self.started = False
        self.stopped = False

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True


class _LinkedMemoryTransport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.peer: _LinkedMemoryTransport | None = None
        self.sent_payloads: list[bytes] = []

    async def start(self) -> None:
        self._closed.clear()

    async def stop(self) -> None:
        if self._closed.is_set():
            return
        self._closed.set()
        remote = self.peer
        if remote is not None and not remote._closed.is_set():
            remote._closed.set()

    async def send(self, payload: bytes) -> None:
        self.sent_payloads.append(payload)
        assert self.peer is not None
        await self.peer._dispatch(payload)


def _linked_transports() -> tuple[_LinkedMemoryTransport, _LinkedMemoryTransport]:
    left = _LinkedMemoryTransport()
    right = _LinkedMemoryTransport()
    left.peer = right
    right.peer = left
    return left, right


def _plugin_spec(name: str) -> PluginSpec:
    plugin_dir = Path(f"/tmp/{name}")
    return PluginSpec(
        name=name,
        plugin_dir=plugin_dir,
        manifest_path=plugin_dir / "plugin.yaml",
        requirements_path=plugin_dir / "requirements.txt",
        python_version="3.12",
        manifest_data={"name": name},
    )


def _build_loaded_capability(
    handler: Callable[..., Any],
    *,
    name: str,
    supports_stream: bool = False,
) -> LoadedCapability:
    return LoadedCapability(
        descriptor=CapabilityDescriptor(
            name=name,
            description="runtime bootstrap test capability",
            input_schema={"type": "object"},
            output_schema={"type": "object"},
            supports_stream=supports_stream,
            cancelable=supports_stream,
        ),
        callable=handler,
        owner=object(),
        plugin_id=name.split(".", 1)[0],
    )


class _RuntimeBootstrapWorkerHarness:
    def __init__(
        self,
        *,
        worker_id: str,
        transport: Transport,
        wire_codec,
        mismatch_metadata_codec: str | None = None,
    ) -> None:
        self.worker_id = worker_id
        self.transport = transport
        self.wire_codec = wire_codec
        self.mismatch_metadata_codec = mismatch_metadata_codec
        self.peer = Peer(
            transport=transport,
            peer_info=supervisor_module.PeerInfo(
                name=worker_id,
                role="plugin",
                version="s5r",
            ),
            wire_codec=wire_codec,
        )
        self.loaded_plugin = LoadedPlugin(
            plugin=_plugin_spec("demo-plugin"),
            handlers=[],
            capabilities=[
                _build_loaded_capability(
                    self._echo_capability,
                    name="demo-plugin.echo",
                ),
                _build_loaded_capability(
                    self._stream_capability,
                    name="demo-plugin.stream",
                    supports_stream=True,
                ),
            ],
            llm_tools=[],
            agents=[],
            instances=[],
        )
        self.dispatcher = CapabilityDispatcher(
            plugin_id="demo-plugin",
            peer=self.peer,
            capabilities=self.loaded_plugin.capabilities,
        )
        self.peer.set_initialize_handler(self._handle_initialize)
        self.peer.set_invoke_handler(self._handle_invoke)
        self.peer.set_cancel_handler(self._handle_cancel)

    async def start(self) -> None:
        await self.peer.start()

    async def stop(self) -> None:
        await self.peer.stop()

    async def _handle_initialize(self, _message) -> Any:
        wire_codec_name = self.mismatch_metadata_codec or self.peer.wire_codec_name
        return supervisor_module.InitializeOutput(
            peer=self.peer.peer_info,
            capabilities=[],
            metadata={
                "worker_id": self.worker_id,
                "plugins": [self.loaded_plugin.plugin.name],
                "loaded_plugins": [self.loaded_plugin.plugin.name],
                "skipped_plugins": {},
                "worker_registry": [
                    {
                        "name": self.loaded_plugin.plugin.name,
                        "display_name": self.loaded_plugin.plugin.name,
                        "description": "",
                        "repo": "",
                        "author": "",
                        "version": "0.0.0",
                        "enabled": True,
                        "config": {},
                    }
                ],
                "capability_sources": {
                    item.descriptor.name: self.loaded_plugin.plugin.name
                    for item in self.loaded_plugin.capabilities
                },
                "issues": [],
                "llm_tools": [],
                "agents": [],
                "wire_codec": wire_codec_name,
            },
        )

    async def _handle_invoke(self, message, cancel_token: CancelToken):
        return await self.dispatcher.invoke(message, cancel_token)

    async def _handle_cancel(self, request_id: str) -> None:
        await self.dispatcher.cancel(request_id)

    async def _echo_capability(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "echo": payload["text"],
            "worker_id": self.worker_id,
            "wire_codec": self.peer.wire_codec_name,
        }

    async def _stream_capability(
        self,
        payload: dict[str, Any],
    ) -> StreamExecution:
        text = str(payload["text"])

        async def iterator() -> AsyncIterator[dict[str, Any]]:
            yield {"chunk": text[:1], "worker_id": self.worker_id}
            yield {"chunk": text[1:], "worker_id": self.worker_id}

        return StreamExecution(
            iterator=iterator(),
            finalize=lambda chunks: {
                "joined": "".join(str(chunk["chunk"]) for chunk in chunks),
                "count": len(chunks),
                "worker_id": self.worker_id,
                "wire_codec": self.peer.wire_codec_name,
            },
        )


async def _run_runtime_bootstrap_roundtrip(
    *,
    wire_codec,
    mismatch_metadata_codec: str | None = None,
) -> dict[str, Any]:
    supervisor_transport, worker_transport = _linked_transports()
    worker_runtime = _RuntimeBootstrapWorkerHarness(
        worker_id="demo-worker",
        transport=worker_transport,
        wire_codec=wire_codec,
        mismatch_metadata_codec=mismatch_metadata_codec,
    )
    capability_router = supervisor_module.CapabilityRouter()
    session = supervisor_module.WorkerSession(
        plugin=_plugin_spec("demo-plugin"),
        repo_root=Path("."),
        env_manager=cast(Any, SimpleNamespace(name="env-manager")),
        capability_router=capability_router,
        wire_codec=wire_codec,
    )

    async def fake_wait_until_initialized() -> None:
        await asyncio.sleep(0)

    session._build_transport = lambda: supervisor_transport  # type: ignore[method-assign]
    session._wait_until_initialized = fake_wait_until_initialized  # type: ignore[method-assign]

    await worker_runtime.start()
    try:
        await session.start()
        assert session.peer is not None
        await session.peer.initialize([])
        session._sync_remote_state()
        session._validate_initialized_state()
        invoke_output = await session.invoke_capability(
            "demo-plugin.echo",
            {"text": "hello"},
            request_id="req-runtime-echo",
        )
        stream_events = [
            event
            async for event in session.invoke_capability_stream(
                "demo-plugin.stream",
                {"text": "hello"},
                request_id="req-runtime-stream",
            )
        ]
        return {
            "session": session,
            "worker_runtime": worker_runtime,
            "invoke_output": invoke_output,
            "stream_events": stream_events,
        }
    except Exception:
        await session.stop()
        await worker_runtime.stop()
        raise


async def _stop_runtime_bootstrap_roundtrip(
    session: supervisor_module.WorkerSession,
    worker_runtime: _RuntimeBootstrapWorkerHarness,
) -> None:
    await session.stop()
    await worker_runtime.stop()


@pytest.mark.asyncio
async def test_run_plugin_worker_requires_exactly_one_target() -> None:
    with pytest.raises(ValueError, match="plugin_dir or group_metadata is required"):
        await bootstrap_module.run_plugin_worker(plugin_dir=None, group_metadata=None)

    with pytest.raises(ValueError, match="mutually exclusive"):
        await bootstrap_module.run_plugin_worker(
            plugin_dir=Path("plugin"),
            group_metadata=Path("group.json"),
        )


@pytest.mark.asyncio
async def test_run_plugin_worker_uses_single_plugin_runtime_and_restores_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[_RecordingRuntime] = []
    original_stdout = sys.stdout
    fake_stdin = cast(IO[str], SimpleNamespace(name="stdin"))
    fake_stdout = cast(IO[str], SimpleNamespace(name="stdout"))

    def fake_prepare_stdio_transport(stdin, stdout):
        assert stdin is fake_stdin
        assert stdout is fake_stdout
        sys.stdout = sys.stderr
        return "transport-stdin", "transport-stdout", original_stdout

    class _FakeTransport:
        def __init__(self, *, stdin, stdout) -> None:
            self.stdin = stdin
            self.stdout = stdout

    def fake_runtime(*, plugin_dir: Path, transport, wire_codec) -> _RecordingRuntime:
        assert plugin_dir == Path("plugin-dir")
        assert transport.stdin == "transport-stdin"
        assert transport.stdout == "transport-stdout"
        assert isinstance(wire_codec, bootstrap_module.MsgpackProtocolCodec)
        runtime = _RecordingRuntime()
        created.append(runtime)
        return runtime

    async def fake_wait_for_shutdown(peer, stop_event) -> None:
        assert peer is created[0].peer
        assert isinstance(stop_event, bootstrap_module.asyncio.Event)

    monkeypatch.setattr(
        bootstrap_module,
        "_prepare_stdio_transport",
        fake_prepare_stdio_transport,
    )
    monkeypatch.setattr(bootstrap_module, "StdioTransport", _FakeTransport)
    monkeypatch.setattr(bootstrap_module, "PluginWorkerRuntime", fake_runtime)
    monkeypatch.setattr(
        bootstrap_module,
        "_install_signal_handlers",
        lambda stop_event: stop_event.set(),
    )
    monkeypatch.setattr(bootstrap_module, "_wait_for_shutdown", fake_wait_for_shutdown)

    await bootstrap_module.run_plugin_worker(
        plugin_dir=Path("plugin-dir"),
        stdin=fake_stdin,
        stdout=fake_stdout,
    )

    assert len(created) == 1
    assert created[0].started is True
    assert created[0].stopped is True
    assert sys.stdout is original_stdout


@pytest.mark.asyncio
async def test_run_plugin_worker_uses_group_runtime_when_group_metadata_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[_RecordingRuntime] = []

    monkeypatch.setattr(
        bootstrap_module,
        "_prepare_stdio_transport",
        lambda stdin, stdout: ("stdin", "stdout", None),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "StdioTransport",
        lambda *, stdin, stdout: SimpleNamespace(stdin=stdin, stdout=stdout),
    )

    def fake_group_runtime(
        *, group_metadata_path: Path, transport, wire_codec
    ) -> _RecordingRuntime:
        assert group_metadata_path == Path("group.json")
        assert transport.stdin == "stdin"
        assert transport.stdout == "stdout"
        assert isinstance(wire_codec, bootstrap_module.MsgpackProtocolCodec)
        runtime = _RecordingRuntime(peer_name="group-peer")
        created.append(runtime)
        return runtime

    monkeypatch.setattr(bootstrap_module, "GroupWorkerRuntime", fake_group_runtime)
    monkeypatch.setattr(
        bootstrap_module,
        "_install_signal_handlers",
        lambda stop_event: stop_event.set(),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "_wait_for_shutdown",
        lambda peer, stop_event: (
            created[0].start() if False else bootstrap_module.asyncio.sleep(0)
        ),
    )

    await bootstrap_module.run_plugin_worker(group_metadata=Path("group.json"))

    assert len(created) == 1
    assert created[0].started is True
    assert created[0].stopped is True


@pytest.mark.asyncio
async def test_run_supervisor_passes_env_manager_and_restores_stdout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[_RecordingRuntime] = []
    env_manager = cast(Any, SimpleNamespace(name="env-manager"))
    original_stdout = sys.stdout

    monkeypatch.setattr(
        bootstrap_module,
        "_prepare_stdio_transport",
        lambda stdin, stdout: ("stdin", "stdout", original_stdout),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "StdioTransport",
        lambda *, stdin, stdout: SimpleNamespace(stdin=stdin, stdout=stdout),
    )

    def fake_runtime(
        *,
        transport,
        plugins_dir: Path,
        env_manager,
        workers_manifest: Path | None = None,
        wire_codec,
    ) -> _RecordingRuntime:
        assert plugins_dir == Path("plugins-under-test")
        assert env_manager is not None
        assert workers_manifest is None
        assert transport.stdin == "stdin"
        assert transport.stdout == "stdout"
        assert isinstance(wire_codec, bootstrap_module.MsgpackProtocolCodec)
        runtime = _RecordingRuntime(peer_name="supervisor-peer")
        created.append(runtime)
        return runtime

    monkeypatch.setattr(bootstrap_module, "SupervisorRuntime", fake_runtime)
    monkeypatch.setattr(
        bootstrap_module,
        "_install_signal_handlers",
        lambda stop_event: stop_event.set(),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "_wait_for_shutdown",
        lambda peer, stop_event: bootstrap_module.asyncio.sleep(0),
    )

    await bootstrap_module.run_supervisor(
        plugins_dir=Path("plugins-under-test"),
        env_manager=env_manager,
    )

    assert len(created) == 1
    assert created[0].started is True
    assert created[0].stopped is True
    assert sys.stdout is original_stdout


@pytest.mark.asyncio
async def test_run_supervisor_default_codec_and_msgpack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        bootstrap_module,
        "_prepare_stdio_transport",
        lambda stdin, stdout: ("stdin", "stdout", None),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "StdioTransport",
        lambda *, stdin, stdout: SimpleNamespace(stdin=stdin, stdout=stdout),
    )

    def fake_runtime(
        *,
        transport,
        plugins_dir: Path,
        env_manager,
        workers_manifest: Path | None = None,
        wire_codec,
    ) -> _RecordingRuntime:
        captured["plugins_dir"] = plugins_dir
        captured["transport"] = transport
        captured["env_manager"] = env_manager
        captured["workers_manifest"] = workers_manifest
        captured["wire_codec"] = wire_codec
        return _RecordingRuntime(peer_name="default-codec-peer")

    monkeypatch.setattr(bootstrap_module, "SupervisorRuntime", fake_runtime)
    monkeypatch.setattr(
        bootstrap_module,
        "_install_signal_handlers",
        lambda stop_event: stop_event.set(),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "_wait_for_shutdown",
        lambda peer, stop_event: bootstrap_module.asyncio.sleep(0),
    )

    await bootstrap_module.run_supervisor(plugins_dir=Path("plugins-under-test"))

    assert captured["plugins_dir"] == Path("plugins-under-test")
    assert isinstance(captured["wire_codec"], bootstrap_module.MsgpackProtocolCodec)


@pytest.mark.asyncio
async def test_run_supervisor_explicit_json_codec(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        bootstrap_module,
        "_prepare_stdio_transport",
        lambda stdin, stdout: ("stdin", "stdout", None),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "StdioTransport",
        lambda *, stdin, stdout: SimpleNamespace(stdin=stdin, stdout=stdout),
    )

    def fake_runtime(
        *,
        transport,
        plugins_dir: Path,
        env_manager,
        workers_manifest: Path | None = None,
        wire_codec,
    ) -> _RecordingRuntime:
        captured["plugins_dir"] = plugins_dir
        captured["transport"] = transport
        captured["env_manager"] = env_manager
        captured["workers_manifest"] = workers_manifest
        captured["wire_codec"] = wire_codec
        return _RecordingRuntime(peer_name="json-codec-peer")

    monkeypatch.setattr(bootstrap_module, "SupervisorRuntime", fake_runtime)
    monkeypatch.setattr(
        bootstrap_module,
        "_install_signal_handlers",
        lambda stop_event: stop_event.set(),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "_wait_for_shutdown",
        lambda peer, stop_event: bootstrap_module.asyncio.sleep(0),
    )

    await bootstrap_module.run_supervisor(
        plugins_dir=Path("plugins-under-test"),
        wire_codec="json",
    )

    assert captured["plugins_dir"] == Path("plugins-under-test")
    assert isinstance(captured["wire_codec"], bootstrap_module.JsonProtocolCodec)


def test_supervisor_runtime_and_worker_session_default_codec_use_msgpack_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer_codecs: list[object] = []
    env_manager = cast(Any, SimpleNamespace(name="env-manager"))
    capability_router = cast(Any, SimpleNamespace(name="router"))
    plugin = cast(Any, SimpleNamespace(name="demo-plugin"))

    class _FakePeer:
        def __init__(self, *, transport, peer_info, wire_codec, **kwargs) -> None:
            peer_codecs.append(wire_codec)

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        def set_invoke_handler(self, handler) -> None:
            self.invoke_handler = handler

        def set_cancel_handler(self, handler) -> None:
            self.cancel_handler = handler

        def set_initialize_handler(self, handler) -> None:
            self.initialize_handler = handler

    monkeypatch.setattr(supervisor_module, "Peer", _FakePeer)
    monkeypatch.setattr(
        supervisor_module,
        "PluginEnvironmentManager",
        lambda repo_root: SimpleNamespace(repo_root=repo_root),
    )

    runtime = supervisor_module.SupervisorRuntime(
        transport=SimpleNamespace(name="transport"),
        plugins_dir=Path("plugins-under-test"),
        env_manager=env_manager,
    )
    session = supervisor_module.WorkerSession(
        plugin=plugin,
        repo_root=Path("repo-root"),
        env_manager=env_manager,
        capability_router=capability_router,
    )
    monkeypatch.setattr(
        session, "_build_transport", lambda: SimpleNamespace(name="transport")
    )

    async def _fake_wait_until_initialized() -> None:
        return None

    monkeypatch.setattr(
        session, "_wait_until_initialized", _fake_wait_until_initialized
    )
    monkeypatch.setattr(session, "_sync_remote_state", lambda: None)
    monkeypatch.setattr(session, "_validate_initialized_state", lambda: None)

    async def _exercise() -> None:
        await session.start()

    import asyncio

    asyncio.run(_exercise())

    assert isinstance(runtime.wire_codec, supervisor_module.MsgpackProtocolCodec)
    assert isinstance(session.wire_codec, supervisor_module.MsgpackProtocolCodec)
    assert len(peer_codecs) == 2
    assert all(
        isinstance(codec, supervisor_module.MsgpackProtocolCodec)
        for codec in peer_codecs
    )


def test_supervisor_runtime_and_worker_session_explicit_json_codec_use_json_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer_codecs: list[object] = []
    env_manager = cast(Any, SimpleNamespace(name="env-manager"))
    capability_router = cast(Any, SimpleNamespace(name="router"))
    plugin = cast(Any, SimpleNamespace(name="demo-plugin"))
    json_codec = JsonProtocolCodec()

    class _FakePeer:
        def __init__(self, *, transport, peer_info, wire_codec, **kwargs) -> None:
            peer_codecs.append(wire_codec)

        async def start(self) -> None:
            return None

        async def stop(self) -> None:
            return None

        def set_invoke_handler(self, handler) -> None:
            self.invoke_handler = handler

        def set_cancel_handler(self, handler) -> None:
            self.cancel_handler = handler

        def set_initialize_handler(self, handler) -> None:
            self.initialize_handler = handler

    monkeypatch.setattr(supervisor_module, "Peer", _FakePeer)
    monkeypatch.setattr(
        supervisor_module,
        "PluginEnvironmentManager",
        lambda repo_root: SimpleNamespace(repo_root=repo_root),
    )

    runtime = supervisor_module.SupervisorRuntime(
        transport=SimpleNamespace(name="transport"),
        plugins_dir=Path("plugins-under-test"),
        env_manager=env_manager,
        wire_codec=json_codec,
    )
    session = supervisor_module.WorkerSession(
        plugin=plugin,
        repo_root=Path("repo-root"),
        env_manager=env_manager,
        capability_router=capability_router,
        wire_codec=json_codec,
    )
    monkeypatch.setattr(
        session, "_build_transport", lambda: SimpleNamespace(name="transport")
    )

    async def _fake_wait_until_initialized() -> None:
        return None

    monkeypatch.setattr(
        session, "_wait_until_initialized", _fake_wait_until_initialized
    )
    monkeypatch.setattr(session, "_sync_remote_state", lambda: None)
    monkeypatch.setattr(session, "_validate_initialized_state", lambda: None)

    async def _exercise() -> None:
        await session.start()

    import asyncio

    asyncio.run(_exercise())

    assert runtime.wire_codec is json_codec
    assert session.wire_codec is json_codec
    assert peer_codecs == [json_codec, json_codec]


def test_plugin_and_group_worker_runtime_default_codec_use_msgpack_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer_codecs: list[object] = []

    class _FakePeer:
        def __init__(self, *, transport, peer_info, wire_codec, **kwargs) -> None:
            peer_codecs.append(wire_codec)

        def set_invoke_handler(self, handler) -> None:
            self.invoke_handler = handler

        def set_cancel_handler(self, handler) -> None:
            self.cancel_handler = handler

    monkeypatch.setattr(worker_module, "Peer", _FakePeer)
    monkeypatch.setattr(
        worker_module,
        "load_plugin_spec",
        lambda plugin_dir: SimpleNamespace(
            name=plugin_dir.name,
            manifest_data={},
            plugin_dir=plugin_dir,
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "load_plugin",
        lambda plugin: SimpleNamespace(
            handlers=[],
            capabilities=[],
            llm_tools=[],
            agents=[],
            instances=[],
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "_load_plugin_specs",
        lambda plugin_dirs: [
            SimpleNamespace(name=path.name, manifest_data={}, plugin_dir=path)
            for path in plugin_dirs
        ],
    )

    plugin_runtime = worker_module.PluginWorkerRuntime(
        plugin_dir=Path("plugin-alpha"),
        transport=SimpleNamespace(name="transport"),
    )
    group_runtime = worker_module.GroupWorkerRuntime(
        plugin_dirs=[Path("plugin-alpha"), Path("plugin-beta")],
        transport=SimpleNamespace(name="transport"),
    )

    assert isinstance(plugin_runtime.wire_codec, worker_module.MsgpackProtocolCodec)
    assert isinstance(group_runtime.wire_codec, worker_module.MsgpackProtocolCodec)
    assert len(peer_codecs) == 2
    assert all(
        isinstance(codec, worker_module.MsgpackProtocolCodec) for codec in peer_codecs
    )


def test_plugin_and_group_worker_runtime_explicit_json_codec_use_json_peer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    peer_codecs: list[object] = []
    json_codec = JsonProtocolCodec()

    class _FakePeer:
        def __init__(self, *, transport, peer_info, wire_codec, **kwargs) -> None:
            peer_codecs.append(wire_codec)

        def set_invoke_handler(self, handler) -> None:
            self.invoke_handler = handler

        def set_cancel_handler(self, handler) -> None:
            self.cancel_handler = handler

    monkeypatch.setattr(worker_module, "Peer", _FakePeer)
    monkeypatch.setattr(
        worker_module,
        "load_plugin_spec",
        lambda plugin_dir: SimpleNamespace(
            name=plugin_dir.name,
            manifest_data={},
            plugin_dir=plugin_dir,
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "load_plugin",
        lambda plugin: SimpleNamespace(
            handlers=[],
            capabilities=[],
            llm_tools=[],
            agents=[],
            instances=[],
        ),
    )
    monkeypatch.setattr(
        worker_module,
        "_load_plugin_specs",
        lambda plugin_dirs: [
            SimpleNamespace(name=path.name, manifest_data={}, plugin_dir=path)
            for path in plugin_dirs
        ],
    )

    plugin_runtime = worker_module.PluginWorkerRuntime(
        plugin_dir=Path("plugin-alpha"),
        transport=SimpleNamespace(name="transport"),
        wire_codec=json_codec,
    )
    group_runtime = worker_module.GroupWorkerRuntime(
        plugin_dirs=[Path("plugin-alpha"), Path("plugin-beta")],
        transport=SimpleNamespace(name="transport"),
        wire_codec=json_codec,
    )

    assert plugin_runtime.wire_codec is json_codec
    assert group_runtime.wire_codec is json_codec
    assert peer_codecs == [json_codec, json_codec]


@pytest.mark.asyncio
async def test_run_websocket_server_uses_websocket_transport_and_default_cwd(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[_RecordingRuntime] = []
    websocket_transports: list[SimpleNamespace] = []
    ssl_contexts: list[tuple[Path, Path, Path]] = []

    monkeypatch.setattr(bootstrap_module.Path, "cwd", lambda: Path("cwd-plugin"))
    monkeypatch.setattr(
        bootstrap_module,
        "_load_plugin_specs",
        lambda plugin_dirs: [SimpleNamespace(name="cwd-plugin")],
    )

    def fake_ssl_context(*, ca_file: Path, cert_file: Path, key_file: Path):
        ssl_contexts.append((ca_file, cert_file, key_file))
        return "ssl-context"

    def fake_transport(*, host: str, port: int, path: str, ssl_context):
        transport = SimpleNamespace(
            host=host,
            port=port,
            path=path,
            ssl_context=ssl_context,
        )
        websocket_transports.append(transport)
        return transport

    def fake_runtime(
        *, plugin_dir: Path, worker_id: str | None, transport, wire_codec
    ) -> _RecordingRuntime:
        assert plugin_dir == Path("cwd-plugin").resolve()
        assert worker_id == "cwd-plugin"
        assert transport is websocket_transports[0]
        assert isinstance(wire_codec, bootstrap_module.MsgpackProtocolCodec)
        runtime = _RecordingRuntime(peer_name="ws-peer")
        created.append(runtime)
        return runtime

    monkeypatch.setattr(
        bootstrap_module,
        "build_websocket_server_ssl_context",
        fake_ssl_context,
    )
    monkeypatch.setattr(bootstrap_module, "WebSocketServerTransport", fake_transport)
    monkeypatch.setattr(bootstrap_module, "PluginWorkerRuntime", fake_runtime)
    monkeypatch.setattr(
        bootstrap_module,
        "_install_signal_handlers",
        lambda stop_event: stop_event.set(),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "_wait_for_shutdown",
        lambda peer, stop_event: bootstrap_module.asyncio.sleep(0),
    )

    await bootstrap_module.run_websocket_server(
        host="0.0.0.0",
        port=9000,
        path="/ws",
        plugin_dirs=None,
        tls_ca_file=Path("ca.pem"),
        tls_cert_file=Path("cert.pem"),
        tls_key_file=Path("key.pem"),
    )

    assert websocket_transports == [
        SimpleNamespace(
            host="0.0.0.0",
            port=9000,
            path="/ws",
            ssl_context="ssl-context",
        )
    ]
    assert ssl_contexts == [(Path("ca.pem"), Path("cert.pem"), Path("key.pem"))]
    assert created[0].started is True
    assert created[0].stopped is True


@pytest.mark.asyncio
async def test_run_websocket_server_uses_group_runtime_for_multiple_plugins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[_RecordingRuntime] = []

    monkeypatch.setattr(
        bootstrap_module,
        "build_websocket_server_ssl_context",
        lambda *, ca_file, cert_file, key_file: (
            ca_file,
            cert_file,
            key_file,
        ),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "WebSocketServerTransport",
        lambda **kwargs: SimpleNamespace(**kwargs),
    )

    def fake_runtime(
        *,
        plugin_dirs: list[Path],
        worker_id: str,
        transport,
        wire_codec,
    ) -> _RecordingRuntime:
        assert plugin_dirs == [Path("alpha").resolve(), Path("beta").resolve()]
        assert worker_id == "remote-bundle"
        assert transport.path == "/ws"
        assert isinstance(wire_codec, bootstrap_module.MsgpackProtocolCodec)
        runtime = _RecordingRuntime(peer_name="group-ws-peer")
        created.append(runtime)
        return runtime

    monkeypatch.setattr(bootstrap_module, "GroupWorkerRuntime", fake_runtime)
    monkeypatch.setattr(
        bootstrap_module,
        "_install_signal_handlers",
        lambda stop_event: stop_event.set(),
    )
    monkeypatch.setattr(
        bootstrap_module,
        "_wait_for_shutdown",
        lambda peer, stop_event: bootstrap_module.asyncio.sleep(0),
    )

    await bootstrap_module.run_websocket_server(
        worker_id="remote-bundle",
        plugin_dirs=[Path("alpha"), Path("beta")],
        path="/ws",
        tls_ca_file=Path("ca.pem"),
        tls_cert_file=Path("cert.pem"),
        tls_key_file=Path("key.pem"),
    )

    assert created[0].started is True
    assert created[0].stopped is True


async def _assert_runtime_bootstrap_codec_roundtrip(wire_codec) -> None:
    outcome = await _run_runtime_bootstrap_roundtrip(wire_codec=wire_codec)
    session = cast(supervisor_module.WorkerSession, outcome["session"])
    worker_runtime = cast(_RuntimeBootstrapWorkerHarness, outcome["worker_runtime"])
    try:
        invoke_output = cast(dict[str, Any], outcome["invoke_output"])
        stream_events = cast(list[Any], outcome["stream_events"])

        assert session.peer is not None
        assert (
            session.peer.remote_metadata["wire_codec"]
            == worker_runtime.peer.wire_codec_name
        )
        assert session.loaded_plugins == ["demo-plugin"]
        assert session.capability_sources == {
            "demo-plugin.echo": "demo-plugin",
            "demo-plugin.stream": "demo-plugin",
        }

        assert invoke_output == {
            "echo": "hello",
            "worker_id": "demo-worker",
            "wire_codec": worker_runtime.peer.wire_codec_name,
        }
        assert [(event.phase, event.data, event.output) for event in stream_events] == [
            ("delta", {"chunk": "h", "worker_id": "demo-worker"}, {}),
            ("delta", {"chunk": "ello", "worker_id": "demo-worker"}, {}),
            (
                "completed",
                {},
                {
                    "joined": "hello",
                    "count": 2,
                    "worker_id": "demo-worker",
                    "wire_codec": worker_runtime.peer.wire_codec_name,
                },
            ),
        ]
    finally:
        await _stop_runtime_bootstrap_roundtrip(session, worker_runtime)


@pytest.mark.asyncio
async def test_worker_session_initialize_response_includes_wire_codec_metadata() -> (
    None
):
    capability_router = supervisor_module.CapabilityRouter()
    session = supervisor_module.WorkerSession(
        plugin=_plugin_spec("demo-plugin"),
        repo_root=Path("."),
        env_manager=cast(Any, SimpleNamespace(name="env-manager")),
        capability_router=capability_router,
        wire_codec=JsonProtocolCodec(),
    )
    session.peer = cast(Any, SimpleNamespace(wire_codec_name="json"))

    output = await session._handle_initialize(object())
    peer = session.peer

    assert peer is not None
    assert output.metadata["wire_codec"] == peer.wire_codec_name


@pytest.mark.asyncio
async def test_runtime_default_msgpack_startup_initializes_invokes_and_streams() -> (
    None
):
    await _assert_runtime_bootstrap_codec_roundtrip(MsgpackProtocolCodec())


@pytest.mark.asyncio
async def test_runtime_json_debug_startup_initializes_invokes_and_streams() -> None:
    await _assert_runtime_bootstrap_codec_roundtrip(JsonProtocolCodec())


@pytest.mark.asyncio
async def test_runtime_initialize_rejects_wire_codec_mismatch_at_startup_boundary() -> (
    None
):
    with pytest.raises(AstrBotError, match="wire_codec mismatch") as exc_info:
        await _run_runtime_bootstrap_roundtrip(
            wire_codec=MsgpackProtocolCodec(),
            mismatch_metadata_codec="json",
        )

    assert exc_info.value.code == ErrorCodes.PROTOCOL_ERROR
