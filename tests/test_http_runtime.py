from __future__ import annotations

from pathlib import Path

import pytest

from astrbot_sdk._internal.invocation_context import caller_plugin_scope
from astrbot_sdk.errors import AstrBotError
from astrbot_sdk.runtime.capability_router import CapabilityRouter


async def _call(
    router: CapabilityRouter,
    capability: str,
    payload: dict[str, object],
    *,
    plugin_id: str = "test-plugin",
) -> dict[str, object]:
    with caller_plugin_scope(plugin_id):
        result = await router.execute(
            capability,
            payload,
            stream=False,
            cancel_token=object(),
            request_id=f"{plugin_id}:{capability}",
        )
    assert isinstance(result, dict)
    return result


@pytest.mark.asyncio
async def test_http_register_rejects_empty_method_list(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    router = CapabilityRouter()

    with pytest.raises(AstrBotError) as exc_info:
        await _call(
            router,
            "http.register_api",
            {
                "route": "/test-plugin/demo",
                "methods": [],
                "handler_capability": "test-plugin.handler",
                "description": "demo",
            },
        )

    assert exc_info.value.code == "invalid_input"


@pytest.mark.asyncio
async def test_http_register_rejects_methods_empty_after_trimming(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    router = CapabilityRouter()

    with pytest.raises(AstrBotError) as exc_info:
        await _call(
            router,
            "http.register_api",
            {
                "route": "/test-plugin/demo",
                "methods": ["", " ", "\t"],
                "handler_capability": "test-plugin.handler",
                "description": "demo",
            },
        )

    assert exc_info.value.code == "invalid_input"


@pytest.mark.asyncio
async def test_http_register_canonicalizes_methods(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    router = CapabilityRouter()

    await _call(
        router,
        "http.register_api",
        {
            "route": "/test-plugin/demo",
            "methods": ["get", " POST ", "GET"],
            "handler_capability": "test-plugin.handler",
            "description": "demo",
        },
    )

    listed = await _call(router, "http.list_apis", {})

    assert listed == {
        "apis": [
            {
                "route": "/test-plugin/demo",
                "methods": ["GET", "POST"],
                "handler_capability": "test-plugin.handler",
                "description": "demo",
                "plugin_id": "test-plugin",
            }
        ]
    }


@pytest.mark.asyncio
async def test_http_duplicate_route_replaces_overlapping_methods_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    router = CapabilityRouter()

    await _call(
        router,
        "http.register_api",
        {
            "route": "/test-plugin/demo",
            "methods": ["GET", "POST"],
            "handler_capability": "test-plugin.old",
            "description": "old",
        },
    )
    await _call(
        router,
        "http.register_api",
        {
            "route": "/test-plugin/other",
            "methods": ["PATCH"],
            "handler_capability": "test-plugin.other",
            "description": "other route",
        },
    )
    await _call(
        router,
        "http.register_api",
        {
            "route": "/other-plugin/demo",
            "methods": ["POST"],
            "handler_capability": "other-plugin.handler",
            "description": "other plugin",
        },
        plugin_id="other-plugin",
    )
    await _call(
        router,
        "http.register_api",
        {
            "route": "/test-plugin/demo",
            "methods": ["post", "DELETE"],
            "handler_capability": "test-plugin.new",
            "description": "new",
        },
    )

    listed = await _call(router, "http.list_apis", {})
    other_listed = await _call(router, "http.list_apis", {}, plugin_id="other-plugin")

    assert listed == {
        "apis": [
            {
                "route": "/test-plugin/demo",
                "methods": ["GET"],
                "handler_capability": "test-plugin.old",
                "description": "old",
                "plugin_id": "test-plugin",
            },
            {
                "route": "/test-plugin/other",
                "methods": ["PATCH"],
                "handler_capability": "test-plugin.other",
                "description": "other route",
                "plugin_id": "test-plugin",
            },
            {
                "route": "/test-plugin/demo",
                "methods": ["DELETE", "POST"],
                "handler_capability": "test-plugin.new",
                "description": "new",
                "plugin_id": "test-plugin",
            },
        ]
    }
    assert other_listed == {
        "apis": [
            {
                "route": "/other-plugin/demo",
                "methods": ["POST"],
                "handler_capability": "other-plugin.handler",
                "description": "other plugin",
                "plugin_id": "other-plugin",
            }
        ]
    }


@pytest.mark.asyncio
async def test_http_unregister_empty_methods_removes_all_for_route(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    router = CapabilityRouter()

    await _call(
        router,
        "http.register_api",
        {
            "route": "/test-plugin/demo",
            "methods": ["GET", "POST"],
            "handler_capability": "test-plugin.handler",
            "description": "demo",
        },
    )
    await _call(
        router,
        "http.unregister_api",
        {"route": "/test-plugin/demo", "methods": []},
    )

    listed = await _call(router, "http.list_apis", {})

    assert listed == {"apis": []}


@pytest.mark.asyncio
async def test_http_unregister_subset_preserves_other_methods(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    router = CapabilityRouter()

    await _call(
        router,
        "http.register_api",
        {
            "route": "/test-plugin/demo",
            "methods": ["GET", "POST"],
            "handler_capability": "test-plugin.handler",
            "description": "demo",
        },
    )
    await _call(
        router,
        "http.unregister_api",
        {"route": "/test-plugin/demo", "methods": ["POST"]},
    )

    listed = await _call(router, "http.list_apis", {})

    assert listed == {
        "apis": [
            {
                "route": "/test-plugin/demo",
                "methods": ["GET"],
                "handler_capability": "test-plugin.handler",
                "description": "demo",
                "plugin_id": "test-plugin",
            }
        ]
    }


@pytest.mark.asyncio
async def test_http_register_rejects_routes_with_empty_segments(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    router = CapabilityRouter()

    with pytest.raises(AstrBotError) as exc_info:
        await _call(
            router,
            "http.register_api",
            {
                "route": "/test-plugin/foo//bar",
                "methods": ["GET"],
                "handler_capability": "test-plugin.handler",
                "description": "demo",
            },
        )

    assert exc_info.value.code == "invalid_input"


@pytest.mark.asyncio
async def test_http_register_rejects_route_outside_plugin_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    router = CapabilityRouter()

    with pytest.raises(AstrBotError, match="公开命名空间前缀"):
        await _call(
            router,
            "http.register_api",
            {
                "route": "/status",
                "methods": ["GET"],
                "handler_capability": "test-plugin.handler",
                "description": "demo",
            },
        )


@pytest.mark.asyncio
async def test_http_register_rejects_foreign_handler_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    router = CapabilityRouter()

    with pytest.raises(AstrBotError, match="handler_capability 属于当前插件"):
        await _call(
            router,
            "http.register_api",
            {
                "route": "/test-plugin/status",
                "methods": ["GET"],
                "handler_capability": "other-plugin.handler",
                "description": "demo",
            },
        )
