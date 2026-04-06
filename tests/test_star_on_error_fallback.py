from __future__ import annotations

# pyright: reportMissingImports=false

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from astrbot_sdk.errors import AstrBotError
from astrbot_sdk.runtime.handler_dispatcher import HandlerDispatcher
from astrbot_sdk.star import Star


class _DummyEvent:
    def __init__(self) -> None:
        self.replies: list[str] = []

    async def reply(self, message: str) -> None:
        self.replies.append(message)


@pytest.mark.asyncio
async def test_handle_error_fallback_does_not_instantiate_star(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _fake_default_on_error(error: Exception, event, ctx) -> None:
        del ctx
        await event.reply(str(error))

    def _fail_init(self) -> None:
        raise AssertionError("Star should not be instantiated for fallback on_error")

    monkeypatch.setattr(Star, "default_on_error", staticmethod(_fake_default_on_error))
    monkeypatch.setattr(Star, "__init__", _fail_init)

    dispatcher = HandlerDispatcher(
        plugin_id="plugin", peer=SimpleNamespace(), handlers=[]
    )
    event = _DummyEvent()

    await dispatcher._handle_error(
        object(),
        RuntimeError("boom"),
        event,
        SimpleNamespace(),
    )

    assert event.replies == ["boom"]


@pytest.mark.asyncio
async def test_default_on_error_formats_astrbot_error_reply() -> None:
    event = _DummyEvent()
    error = AstrBotError.invalid_input(
        "bad payload",
        hint="check payload",
        docs_url="https://example.com/docs",
        details={"b": 2, "a": 1},
    )

    await Star.default_on_error(error, event, SimpleNamespace())

    assert event.replies == [
        "check payload\n文档：https://example.com/docs\n详情：{'b': 2, 'a': 1}"
    ]


@pytest.mark.asyncio
async def test_default_on_error_exposes_internal_error_message_when_hint_is_placeholder():
    event = _DummyEvent()
    error = AstrBotError.internal_error("database unavailable", hint="请联系插件作者")

    await Star.default_on_error(error, event, SimpleNamespace())

    assert event.replies == ["请联系插件作者"]


@pytest.mark.asyncio
async def test_default_on_error_keeps_rate_limit_reply_user_friendly() -> None:
    event = _DummyEvent()
    error = AstrBotError.rate_limited(
        hint="操作过于频繁，请 30 秒后再试",
        details={"retry_after": 30},
    )

    await Star.default_on_error(error, event, SimpleNamespace())

    assert event.replies == ["操作过于频繁，请 30 秒后再试\n详情：{'retry_after': 30}"]


@pytest.mark.asyncio
async def test_default_on_error_replies_real_message_for_unknown_errors() -> None:
    event = _DummyEvent()

    await Star.default_on_error(RuntimeError("boom"), event, SimpleNamespace())

    assert event.replies == ["出了点问题，请联系插件作者"]


@pytest.mark.asyncio
async def test_on_error_does_not_dispatch_via_subclass_default_on_error() -> None:
    class PluginWithShadowedDefault(Star):
        async def default_on_error(self, error: Exception, event, ctx) -> None:
            del error, event, ctx
            raise AssertionError(
                "Star.on_error should not virtual-dispatch default_on_error"
            )

    expected_event = _DummyEvent()
    actual_event = _DummyEvent()

    await Star.default_on_error(RuntimeError("boom"), expected_event, SimpleNamespace())
    await PluginWithShadowedDefault().on_error(
        RuntimeError("boom"),
        actual_event,
        SimpleNamespace(),
    )

    assert actual_event.replies == expected_event.replies
