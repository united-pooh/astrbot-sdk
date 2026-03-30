from __future__ import annotations

import importlib

import msgpack
import pytest

codec_module = importlib.import_module("src.astrbot_sdk.protocol.codec")
messages_module = importlib.import_module("src.astrbot_sdk.protocol.messages")

JsonProtocolCodec = codec_module.JsonProtocolCodec
MsgpackProtocolCodec = codec_module.MsgpackProtocolCodec
ProtocolCodec = codec_module.ProtocolCodec
ErrorPayload = messages_module.ErrorPayload
EventMessage = messages_module.EventMessage
InvokeMessage = messages_module.InvokeMessage
ResultMessage = messages_module.ResultMessage


@pytest.mark.parametrize(
    ("codec", "expected_wire_type"),
    [
        pytest.param(JsonProtocolCodec(), bytes, id="json"),
        pytest.param(MsgpackProtocolCodec(), bytes, id="msgpack"),
    ],
)
def test_protocol_codec_roundtrip_preserves_protocol_message_semantics(
    codec: ProtocolCodec,
    expected_wire_type: type[bytes],
) -> None:
    message = ResultMessage(
        id="msg_0001",
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
            "metadata": {"mode": "test", "attempt": 1},
        },
    )

    payload = codec.encode_message(message)
    decoded = codec.decode_message(payload)

    assert isinstance(payload, expected_wire_type)
    assert decoded == message


@pytest.mark.parametrize(
    "codec",
    [
        pytest.param(JsonProtocolCodec(), id="json"),
        pytest.param(MsgpackProtocolCodec(), id="msgpack"),
    ],
)
def test_protocol_codec_roundtrip_matches_json_top_level_semantics(
    codec: ProtocolCodec,
) -> None:
    message = EventMessage(
        id="stream_0001",
        phase="failed",
        error=ErrorPayload(
            code="internal_error",
            message="boom",
            hint="",
            retryable=False,
            docs_url="",
        ),
    )

    decoded = codec.decode_message(codec.encode_message(message))

    assert isinstance(decoded, EventMessage)
    assert decoded.model_dump(exclude_none=True) == message.model_dump(
        exclude_none=True
    )


@pytest.mark.parametrize(
    "codec",
    [
        pytest.param(JsonProtocolCodec(), id="json"),
        pytest.param(MsgpackProtocolCodec(), id="msgpack"),
    ],
)
def test_protocol_codec_roundtrip_accepts_existing_protocol_message_instances(
    codec: ProtocolCodec,
) -> None:
    message = InvokeMessage(
        id="invoke_0001",
        capability="demo.echo",
        input={"text": "hello"},
        stream=False,
    )

    decoded = codec.decode_message(message)

    assert decoded is message


@pytest.mark.parametrize(
    ("codec", "payload"),
    [
        pytest.param(JsonProtocolCodec(), b"[]", id="json-array"),
        pytest.param(
            MsgpackProtocolCodec(),
            msgpack.packb([], use_bin_type=True),
            id="msgpack-array",
        ),
    ],
)
def test_protocol_codec_malformed_rejects_non_object_top_level_payloads(
    codec: ProtocolCodec,
    payload: bytes,
) -> None:
    with pytest.raises(ValueError, match="协议消息必须是 JSON object"):
        codec.decode_message(payload)


@pytest.mark.parametrize(
    ("codec", "payload"),
    [
        pytest.param(
            JsonProtocolCodec(),
            b'{"type": "mystery", "id": "msg_0001"}',
            id="json-unknown-type",
        ),
        pytest.param(
            MsgpackProtocolCodec(),
            msgpack.packb({"type": "mystery", "id": "msg_0001"}, use_bin_type=True),
            id="msgpack-unknown-type",
        ),
    ],
)
def test_protocol_codec_malformed_rejects_unknown_message_types(
    codec: ProtocolCodec,
    payload: bytes,
) -> None:
    with pytest.raises(ValueError, match="未知消息类型：mystery"):
        codec.decode_message(payload)


@pytest.mark.parametrize(
    ("codec", "payload"),
    [
        pytest.param(JsonProtocolCodec(), b"not-json", id="json-invalid-wire"),
        pytest.param(MsgpackProtocolCodec(), b"\xc1", id="msgpack-invalid-wire"),
    ],
)
def test_protocol_codec_malformed_rejects_invalid_wire_payloads(
    codec: ProtocolCodec,
    payload: bytes,
) -> None:
    with pytest.raises(ValueError):
        codec.decode_message(payload)
