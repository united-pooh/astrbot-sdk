from __future__ import annotations

import asyncio
import io
from types import SimpleNamespace
from typing import Any, cast

import pytest

from src.astrbot_sdk.runtime import transport as transport_module
from src.astrbot_sdk.runtime.transport import (
    StdioTransport,
    WebSocketServerTransport,
    WebSocketClientTransport,
    _frame_stdio_payload,
)


@pytest.mark.unit
def test_frame_stdio_payload_prefixes_utf8_byte_length() -> None:
    payload = b"hello\nworld"

    framed = _frame_stdio_payload(payload)

    assert framed == b"11\nhello\nworld"


@pytest.mark.asyncio
async def test_stdio_read_process_loop_dispatches_messages_and_sets_closed() -> None:
    received: list[bytes] = []

    class _FakeStdout:
        def __init__(self) -> None:
            self._items = [
                b"5\n",
                b"first",
                b"6\n",
                b"second",
                b"",
            ]

        async def readline(self) -> bytes:
            return self._items.pop(0)

        async def readexactly(self, size: int) -> bytes:
            payload = self._items.pop(0)
            assert len(payload) == size
            return payload

    transport = StdioTransport(command=["python", "-V"])
    transport._process = cast(Any, SimpleNamespace(stdout=_FakeStdout()))
    transport.set_message_handler(lambda payload: _capture(received, payload))

    await transport._read_process_loop()

    assert received == [b"first", b"second"]
    assert transport._closed.is_set() is True


@pytest.mark.asyncio
async def test_stdio_read_process_loop_dispatches_opaque_binary_frame_bytes() -> None:
    received: list[bytes] = []

    class _FakeStdout:
        def __init__(self) -> None:
            self._items = [
                b"3\n",
                b"\xff\x00\x7f",
                b"",
            ]

        async def readline(self) -> bytes:
            return self._items.pop(0)

        async def readexactly(self, size: int) -> bytes:
            payload = self._items.pop(0)
            assert len(payload) == size
            return payload

    transport = StdioTransport(command=["python", "-V"])
    transport._process = cast(Any, SimpleNamespace(stdout=_FakeStdout()))
    transport.set_message_handler(
        lambda payload: _capture_bytes(received, cast(bytes, payload))
    )

    await transport._read_process_loop()

    assert received == [b"\xff\x00\x7f"]
    assert transport._closed.is_set() is True


@pytest.mark.asyncio
async def test_stdio_wait_closed_unblocks_after_process_eof() -> None:
    class _FakeStdout:
        async def readline(self) -> bytes:
            return b""

    transport = StdioTransport(command=["python", "-V"])
    transport._process = cast(Any, SimpleNamespace(stdout=_FakeStdout()))

    waiter = asyncio.create_task(transport.wait_closed())
    await transport._read_process_loop()
    await asyncio.wait_for(waiter, timeout=1)

    assert waiter.done() is True


@pytest.mark.asyncio
async def test_stdio_read_file_loop_dispatches_messages_and_sets_closed() -> None:
    received: list[bytes] = []
    payload = _frame_stdio_payload(b"line-1") + _frame_stdio_payload(b"line-2")
    transport = StdioTransport(
        stdin=cast(Any, SimpleNamespace(buffer=io.BytesIO(payload)))
    )
    transport.set_message_handler(lambda payload: _capture(received, payload))

    await transport._read_file_loop()

    assert received == [b"line-1", b"line-2"]
    assert transport._closed.is_set() is True


@pytest.mark.asyncio
async def test_stdio_read_file_loop_dispatches_opaque_binary_frame_bytes() -> None:
    received: list[bytes] = []
    payload = b"3\n\xff\x00\x7f"
    transport = StdioTransport(
        stdin=cast(Any, SimpleNamespace(buffer=io.BytesIO(payload)))
    )
    transport.set_message_handler(
        lambda payload: _capture_bytes(received, cast(bytes, payload))
    )

    await transport._read_file_loop()

    assert received == [b"\xff\x00\x7f"]
    assert transport._closed.is_set() is True


@pytest.mark.asyncio
async def test_stdio_read_process_loop_stops_after_malformed_header() -> None:
    received: list[bytes] = []

    class _FakeStdout:
        def __init__(self) -> None:
            self._items = [b"oops\n", b"6\n", b"second", b""]

        async def readline(self) -> bytes:
            return self._items.pop(0)

        async def readexactly(self, size: int) -> bytes:
            payload = self._items.pop(0)
            assert len(payload) == size
            return payload

    transport = StdioTransport(command=["python", "-V"])
    transport._process = cast(Any, SimpleNamespace(stdout=_FakeStdout()))
    transport.set_message_handler(lambda payload: _capture(received, payload))

    await transport._read_process_loop()

    assert received == []
    assert transport._closed.is_set() is True


@pytest.mark.asyncio
async def test_stdio_read_file_loop_stops_after_malformed_header() -> None:
    received: list[bytes] = []
    payload = b"oops\n" + _frame_stdio_payload(b"second")
    transport = StdioTransport(
        stdin=cast(Any, SimpleNamespace(buffer=io.BytesIO(payload)))
    )
    transport.set_message_handler(lambda payload: _capture(received, payload))

    await transport._read_file_loop()

    assert received == []
    assert transport._closed.is_set() is True


@pytest.mark.asyncio
async def test_stdio_stop_kills_process_when_terminate_times_out(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    class _FakeProcess:
        returncode = None
        stdin = None

        def terminate(self) -> None:
            calls.append("terminate")

        def kill(self) -> None:
            calls.append("kill")

        async def wait(self) -> None:
            calls.append("wait")

    async def fake_wait_for(awaitable, timeout: float):
        awaitable.close()
        del timeout
        raise asyncio.TimeoutError

    transport = StdioTransport(command=["python", "-V"])
    transport._process = cast(Any, _FakeProcess())
    monkeypatch.setattr(transport_module.asyncio, "wait_for", fake_wait_for)

    await transport.stop()

    assert calls == ["terminate", "kill", "wait"]
    assert transport._process is None
    assert transport._closed.is_set() is True


@pytest.mark.asyncio
async def test_websocket_client_read_loop_dispatches_text_and_binary_then_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[bytes] = []

    class _FakeWebSocket:
        closed = False

        def __init__(self) -> None:
            self._messages = iter(
                [
                    SimpleNamespace(type="text", data="hello"),
                    SimpleNamespace(type="binary", data=b"world"),
                ]
            )

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._messages)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

        def exception(self):
            return None

    fake_aiohttp = SimpleNamespace(
        WSMsgType=SimpleNamespace(TEXT="text", BINARY="binary", ERROR="error")
    )
    monkeypatch.setattr(transport_module, "_get_aiohttp", lambda: fake_aiohttp)

    transport = WebSocketClientTransport(url="ws://test")
    transport._ws = _FakeWebSocket()
    transport.set_message_handler(lambda payload: _capture(received, payload))

    await transport._read_loop()

    assert received == [b"hello", b"world"]
    assert transport._closed.is_set() is True


@pytest.mark.asyncio
async def test_websocket_client_read_loop_dispatches_text_and_binary_as_opaque_frame_bytes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[bytes] = []

    class _FakeWebSocket:
        closed = False

        def __init__(self) -> None:
            self._messages = iter(
                [
                    SimpleNamespace(type="text", data="hello"),
                    SimpleNamespace(type="binary", data=b"\xff\x00"),
                ]
            )

        def __aiter__(self):
            return self

        async def __anext__(self):
            try:
                return next(self._messages)
            except StopIteration as exc:
                raise StopAsyncIteration from exc

        def exception(self):
            return None

    fake_aiohttp = SimpleNamespace(
        WSMsgType=SimpleNamespace(TEXT="text", BINARY="binary", ERROR="error")
    )
    monkeypatch.setattr(transport_module, "_get_aiohttp", lambda: fake_aiohttp)

    transport = WebSocketClientTransport(url="ws://test")
    transport._ws = _FakeWebSocket()
    transport.set_message_handler(
        lambda payload: _capture_bytes(received, cast(bytes, payload))
    )

    await transport._read_loop()

    assert received == [b"hello", b"\xff\x00"]
    assert transport._closed.is_set() is True


@pytest.mark.asyncio
async def test_websocket_server_send_uses_binary_frames_for_opaque_payloads() -> None:
    calls: list[tuple[str, bytes]] = []

    class _FakeWebSocket:
        closed = False

        async def send_str(self, payload):
            calls.append(("text", payload))

        async def send_bytes(self, payload: bytes):
            calls.append(("binary", payload))

    transport = WebSocketServerTransport()
    transport._connected.set()
    transport._ws = _FakeWebSocket()

    await transport.send(cast(Any, b"\xffpayload"))

    assert calls == [("binary", b"\xffpayload")]


@pytest.mark.asyncio
async def test_websocket_client_send_uses_binary_frames_for_opaque_payloads() -> None:
    calls: list[tuple[str, bytes]] = []

    class _FakeWebSocket:
        closed = False

        async def send_str(self, payload):
            calls.append(("text", payload))

        async def send_bytes(self, payload: bytes):
            calls.append(("binary", payload))

    transport = WebSocketClientTransport(url="ws://test")
    transport._ws = _FakeWebSocket()

    await transport.send(cast(Any, b"\x00payload"))

    assert calls == [("binary", b"\x00payload")]


@pytest.mark.asyncio
async def test_websocket_server_send_raises_when_connection_is_gone_after_wait(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    transport = WebSocketServerTransport()
    transport._connected.set()
    transport._ws = SimpleNamespace(closed=True)

    async def fake_wait_for(awaitable, timeout: float):
        del timeout
        return await awaitable

    monkeypatch.setattr(transport_module.asyncio, "wait_for", fake_wait_for)

    with pytest.raises(RuntimeError, match="尚未连接"):
        await transport.send(b"payload")


async def _capture(received: list[bytes], payload: bytes) -> None:
    received.append(payload)


async def _capture_bytes(received: list[bytes], payload: bytes) -> None:
    received.append(payload)
