from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any, cast

import pytest

from src.astrbot_sdk.errors import AstrBotError, ErrorCodes
from src.astrbot_sdk.protocol.codec import (
    JsonProtocolCodec,
    MsgpackProtocolCodec,
    ProtocolCodec,
)
from src.astrbot_sdk.protocol.messages import (
    ErrorPayload,
    EventMessage,
    InitializeMessage,
    InitializeOutput,
    InvokeMessage,
    PeerInfo,
    ResultMessage,
)
from src.astrbot_sdk.runtime.peer import Peer
from src.astrbot_sdk.runtime.transport import Transport


class _ControlledTransport(Transport):
    def __init__(self) -> None:
        super().__init__()
        self.sent_payloads: list[bytes] = []
        self.on_send: Callable[[bytes], Awaitable[None]] | None = None

    async def start(self) -> None:
        self._closed.clear()

    async def stop(self) -> None:
        self._closed.set()

    async def send(self, payload: bytes) -> None:
        self.sent_payloads.append(payload)
        if self.on_send is not None:
            await self.on_send(payload)

    async def push_message(self, message: Any) -> None:
        payload = message if isinstance(message, bytes) else bytes(message)
        await self._dispatch(cast(bytes, payload))

    def close_unexpected(self) -> None:
        self._closed.set()


class _FailingSendTransport(_ControlledTransport):
    async def send(self, payload: bytes) -> None:
        self.sent_payloads.append(payload)
        raise RuntimeError("send failed")


def _decode_transport_payload(payload: bytes, codec: ProtocolCodec) -> Any:
    return (
        payload if isinstance(codec, MsgpackProtocolCodec) else payload.decode("utf-8")
    )


def _encode_transport_message(message: Any, codec: ProtocolCodec) -> bytes:
    if isinstance(message, bytes):
        return message
    if isinstance(message, str):
        return message.encode("utf-8")
    return codec.encode_message(message)


def _make_peer(
    transport: _ControlledTransport,
    *,
    name: str = "test-plugin",
    wire_codec: ProtocolCodec | None = None,
) -> Peer:
    return Peer(
        transport=transport,
        peer_info=PeerInfo(name=name, role="plugin", version="s5r"),
        wire_codec=wire_codec,
    )


async def _stop_peer(peer: Peer) -> None:
    await peer.stop()
    if peer._transport_watch_task is not None:
        await peer._transport_watch_task


async def _next_event(iterator: Any) -> Any:
    return await anext(iterator)


@pytest.fixture(
    params=[JsonProtocolCodec, MsgpackProtocolCodec], ids=["json", "msgpack"]
)
def wire_codec(request: pytest.FixtureRequest) -> ProtocolCodec:
    codec_cls = request.param
    return codec_cls()


@pytest.mark.asyncio
async def test_initialize_marks_remote_initialized_on_active_side(
    wire_codec: ProtocolCodec,
) -> None:
    transport = _ControlledTransport()
    peer = _make_peer(transport, wire_codec=wire_codec)
    expected_codec = _wire_codec_name(wire_codec)

    async def respond_to_initialize(payload: bytes) -> None:
        message = wire_codec.decode_message(
            _decode_transport_payload(payload, wire_codec)
        )
        assert message.type == "initialize"
        assert message.metadata["wire_codec"] == expected_codec
        await transport.push_message(
            _encode_transport_message(
                ResultMessage(
                    id=message.id,
                    kind="initialize_result",
                    success=True,
                    output={
                        "peer": {
                            "name": "astrbot-core",
                            "role": "core",
                            "version": "s5r",
                        },
                        "protocol_version": "1.0",
                        "capabilities": [],
                        "metadata": {
                            "mode": "test",
                            "wire_codec": expected_codec,
                        },
                    },
                ),
                wire_codec,
            )
        )

    transport.on_send = respond_to_initialize
    await peer.start()
    try:
        waiter = asyncio.create_task(peer.wait_until_remote_initialized(timeout=0.2))
        await asyncio.sleep(0)
        assert not waiter.done()

        output = await peer.initialize([])
        await waiter

        assert output.peer.name == "astrbot-core"
        assert peer.remote_peer is not None
        assert peer.remote_peer.name == "astrbot-core"
        assert peer.remote_metadata["mode"] == "test"
        assert peer.remote_metadata["wire_codec"] == expected_codec
    finally:
        await _stop_peer(peer)


def _wire_codec_name(codec: ProtocolCodec) -> str:
    if isinstance(codec, JsonProtocolCodec):
        return "json"
    if isinstance(codec, MsgpackProtocolCodec):
        return "msgpack"
    raise AssertionError(f"unexpected codec for test: {type(codec)!r}")


@pytest.mark.asyncio
async def test_initialize_and_codec_and_match_succeeds(
    wire_codec: ProtocolCodec,
) -> None:
    transport = _ControlledTransport()
    peer = _make_peer(transport, wire_codec=wire_codec)
    expected_codec = _wire_codec_name(wire_codec)

    async def respond_to_initialize(payload: bytes) -> None:
        message = wire_codec.decode_message(
            _decode_transport_payload(payload, wire_codec)
        )
        assert isinstance(message, InitializeMessage)
        assert message.metadata["wire_codec"] == expected_codec
        await transport.push_message(
            _encode_transport_message(
                ResultMessage(
                    id=message.id,
                    kind="initialize_result",
                    success=True,
                    output=InitializeOutput(
                        peer=PeerInfo(
                            name="astrbot-core",
                            role="core",
                            version="s5r",
                        ),
                        protocol_version="1.0",
                        capabilities=[],
                        metadata={
                            "mode": "test",
                            "wire_codec": expected_codec,
                        },
                    ).model_dump(),
                ),
                wire_codec,
            )
        )

    transport.on_send = respond_to_initialize
    await peer.start()
    try:
        output = await peer.initialize([])

        assert output.metadata["wire_codec"] == expected_codec
        assert peer.remote_metadata["wire_codec"] == expected_codec
    finally:
        await _stop_peer(peer)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("remote_metadata", "expected_message"),
    [
        ({}, "wire_codec metadata missing"),
        ({"wire_codec": "mismatch"}, "wire_codec mismatch"),
    ],
    ids=["missing", "mismatch"],
)
async def test_initialize_and_codec_and_mismatch_fails_fast(
    wire_codec: ProtocolCodec,
    remote_metadata: dict[str, str],
    expected_message: str,
) -> None:
    local_transport = _ControlledTransport()
    remote_transport = _ControlledTransport()
    local_peer = _make_peer(local_transport, name="local", wire_codec=wire_codec)
    remote_peer = _make_peer(remote_transport, name="remote", wire_codec=wire_codec)

    async def remote_initialize_handler(
        message: InitializeMessage,
    ) -> InitializeOutput:
        return InitializeOutput(
            peer=remote_peer.peer_info,
            protocol_version=message.protocol_version,
            capabilities=[],
            metadata=remote_metadata,
        )

    remote_peer.set_initialize_handler(remote_initialize_handler)

    async def pump_to_remote(payload: bytes) -> None:
        await remote_transport.push_message(payload)

    async def pump_to_local(payload: bytes) -> None:
        await local_transport.push_message(payload)

    local_transport.on_send = pump_to_remote
    remote_transport.on_send = pump_to_local

    await local_peer.start()
    await remote_peer.start()
    try:
        with pytest.raises(AstrBotError, match=expected_message) as exc_info:
            await local_peer.initialize([])

        assert exc_info.value.code == ErrorCodes.PROTOCOL_ERROR
        assert local_peer._unusable is True
        assert remote_peer._unusable is True
    finally:
        await _stop_peer(local_peer)
        await _stop_peer(remote_peer)


@pytest.mark.asyncio
async def test_wait_until_remote_initialized_fails_when_transport_closes_pre_init(
    wire_codec: ProtocolCodec,
) -> None:
    transport = _ControlledTransport()
    peer = _make_peer(transport, wire_codec=wire_codec)
    await peer.start()
    try:
        waiter = asyncio.create_task(peer.wait_until_remote_initialized(timeout=None))
        await asyncio.sleep(0)

        transport.close_unexpected()

        with pytest.raises(AstrBotError, match="连接在初始化完成前关闭") as exc_info:
            await asyncio.wait_for(waiter, timeout=0.2)

        assert exc_info.value.code == ErrorCodes.PROTOCOL_ERROR
    finally:
        await _stop_peer(peer)


@pytest.mark.asyncio
async def test_invoke_fails_pending_call_on_unexpected_transport_close(
    wire_codec: ProtocolCodec,
) -> None:
    transport = _ControlledTransport()
    peer = _make_peer(transport, wire_codec=wire_codec)
    await peer.start()
    try:
        invoke_task = asyncio.create_task(peer.invoke("llm.chat", {"prompt": "hello"}))
        await asyncio.sleep(0)

        assert len(transport.sent_payloads) == 1
        transport.close_unexpected()

        with pytest.raises(AstrBotError, match="连接已关闭") as exc_info:
            await asyncio.wait_for(invoke_task, timeout=0.2)

        assert exc_info.value.code == ErrorCodes.NETWORK_ERROR
    finally:
        await _stop_peer(peer)


@pytest.mark.asyncio
async def test_invoke_stream_fails_pending_iterator_on_unexpected_transport_close(
    wire_codec: ProtocolCodec,
) -> None:
    transport = _ControlledTransport()
    peer = _make_peer(transport, wire_codec=wire_codec)
    await peer.start()
    try:
        iterator = await peer.invoke_stream("llm.stream", {"prompt": "hello"})
        consume_task = asyncio.create_task(_next_event(iterator))
        await asyncio.sleep(0)

        assert len(transport.sent_payloads) == 1
        transport.close_unexpected()

        with pytest.raises(AstrBotError, match="连接已关闭") as exc_info:
            await asyncio.wait_for(consume_task, timeout=0.2)

        assert exc_info.value.code == ErrorCodes.NETWORK_ERROR
    finally:
        await _stop_peer(peer)


@pytest.mark.asyncio
async def test_invoke_stream_hides_completed_event_by_default(
    wire_codec: ProtocolCodec,
) -> None:
    transport = _ControlledTransport()
    peer = _make_peer(transport, wire_codec=wire_codec)

    async def emit_stream(payload: bytes) -> None:
        message = wire_codec.decode_message(
            _decode_transport_payload(payload, wire_codec)
        )
        assert message.type == "invoke"
        await transport.push_message(
            _encode_transport_message(
                EventMessage(id=message.id, phase="started"), wire_codec
            )
        )
        await transport.push_message(
            _encode_transport_message(
                EventMessage(id=message.id, phase="delta", data={"text": "hello"}),
                wire_codec,
            )
        )
        await transport.push_message(
            _encode_transport_message(
                EventMessage(
                    id=message.id, phase="completed", output={"text": "hello"}
                ),
                wire_codec,
            )
        )

    transport.on_send = emit_stream
    await peer.start()
    try:
        iterator = await peer.invoke_stream("llm.stream", {"prompt": "hello"})
        events = [event async for event in iterator]

        assert [(event.phase, event.data, event.output) for event in events] == [
            ("delta", {"text": "hello"}, {})
        ]
    finally:
        await _stop_peer(peer)


@pytest.mark.asyncio
async def test_invoke_stream_can_include_completed_event(
    wire_codec: ProtocolCodec,
) -> None:
    transport = _ControlledTransport()
    peer = _make_peer(transport, wire_codec=wire_codec)

    async def emit_stream(payload: bytes) -> None:
        message = wire_codec.decode_message(
            _decode_transport_payload(payload, wire_codec)
        )
        assert message.type == "invoke"
        await transport.push_message(
            _encode_transport_message(
                EventMessage(id=message.id, phase="started"), wire_codec
            )
        )
        await transport.push_message(
            _encode_transport_message(
                EventMessage(id=message.id, phase="delta", data={"text": "hello"}),
                wire_codec,
            )
        )
        await transport.push_message(
            _encode_transport_message(
                EventMessage(
                    id=message.id, phase="completed", output={"text": "hello"}
                ),
                wire_codec,
            )
        )

    transport.on_send = emit_stream
    await peer.start()
    try:
        iterator = await peer.invoke_stream(
            "llm.stream",
            {"prompt": "hello"},
            include_completed=True,
        )
        events = [event async for event in iterator]

        assert [(event.phase, event.data, event.output) for event in events] == [
            ("delta", {"text": "hello"}, {}),
            ("completed", {}, {"text": "hello"}),
        ]
    finally:
        await _stop_peer(peer)


@pytest.mark.asyncio
async def test_invoke_stream_failed_event_becomes_exception(
    wire_codec: ProtocolCodec,
) -> None:
    transport = _ControlledTransport()
    peer = _make_peer(transport, wire_codec=wire_codec)

    async def emit_failed_event(payload: bytes) -> None:
        message = wire_codec.decode_message(
            _decode_transport_payload(payload, wire_codec)
        )
        assert message.type == "invoke"
        await transport.push_message(
            _encode_transport_message(
                EventMessage(id=message.id, phase="started"), wire_codec
            )
        )
        await transport.push_message(
            _encode_transport_message(
                EventMessage(
                    id=message.id,
                    phase="failed",
                    error=ErrorPayload(
                        code=ErrorCodes.INTERNAL_ERROR,
                        message="boom",
                        hint="",
                        retryable=False,
                        docs_url="",
                    ),
                ),
                wire_codec,
            )
        )

    transport.on_send = emit_failed_event
    await peer.start()
    try:
        iterator = await peer.invoke_stream("llm.stream", {"prompt": "hello"})

        with pytest.raises(AstrBotError, match="boom") as exc_info:
            async for _event in iterator:
                pass

        assert exc_info.value.code == ErrorCodes.INTERNAL_ERROR
    finally:
        await _stop_peer(peer)


@pytest.mark.asyncio
async def test_inbound_invoke_send_failure_marks_peer_unusable(
    wire_codec: ProtocolCodec,
) -> None:
    transport = _FailingSendTransport()
    peer = _make_peer(transport, wire_codec=wire_codec)

    async def handle_invoke(_message: Any, _token: Any) -> dict[str, Any]:
        return {"ok": True}

    peer.set_invoke_handler(handle_invoke)
    await peer.start()
    try:
        await transport.push_message(
            _encode_transport_message(
                InvokeMessage(
                    id="msg_0001",
                    capability="demo.echo",
                    input={},
                    stream=False,
                ),
                wire_codec,
            )
        )

        await asyncio.wait_for(peer.wait_closed(), timeout=0.2)

        assert peer._unusable is True
        assert len(transport.sent_payloads) == 2
    finally:
        await _stop_peer(peer)
