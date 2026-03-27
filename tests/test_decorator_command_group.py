"""测试 on_command / conversation_command 的 group 与 group_help 参数。

覆盖内容：
1. on_command 无 group 时行为不变（基线）
2. on_command group 为字符串时触发器命令和别名正确展开
3. on_command group 为列表时多级路径正确拼接
4. on_command group_help 写入 CommandRouteSpec
5. on_command group 为空字符串 / 空列表时的退化行为
6. conversation_command 的 group/group_help 透传到 on_command
7. 带实际插件运行时的 dispatch 行为
"""
from __future__ import annotations

from pathlib import Path
from textwrap import dedent

import pytest

from astrbot_sdk.decorators import (
    conversation_command,
    on_command,
)
from astrbot_sdk.protocol.descriptors import CommandTrigger


def _get_trigger(func) -> CommandTrigger:
    """从装饰后的方法上取 CommandTrigger，断言类型后返回。"""
    meta = func.__astrbot_handler_meta__
    trigger = meta.trigger
    assert isinstance(trigger, CommandTrigger)
    return trigger


def _write_plugin(
    plugin_dir: Path,
    *,
    name: str,
    class_name: str,
    source: str,
) -> None:
    """写入 plugin.yaml + main.py，使用正确的 manifest v2 格式。"""
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "plugin.yaml").write_text(
        dedent(
            f"""
            _schema_version: 2
            name: {name}
            author: tests
            version: 1.0.0
            desc: command group tests

            runtime:
              python: "3.12"

            components:
              - class: main:{class_name}
            """
        ).strip()
        + "\n",
        encoding="utf-8",
    )
    (plugin_dir / "main.py").write_text(source, encoding="utf-8")


# ── 1. 基线：无 group 时行为不变 ──────────────────────────────────


def test_on_command_without_group_uses_raw_command():
    """没有 group 参数时，trigger.command 和 trigger.aliases 保持原样。"""

    @on_command("hello", aliases=["hi"])
    async def handler(self, event, ctx):
        pass

    trigger = _get_trigger(handler)
    assert trigger.command == "hello"
    assert trigger.aliases == ["hi"]
    meta = handler.__astrbot_handler_meta__
    # 不应设置 command_route
    assert meta.command_route is None


def test_on_command_multi_name_without_group():
    """多命令名时，首项为正式命令，其余合并进 aliases。"""

    @on_command(["hello", "repeat", "say"])
    async def handler(self, event, ctx):
        pass

    trigger = _get_trigger(handler)
    assert trigger.command == "hello"
    assert trigger.aliases == ["repeat", "say"]


# ── 2. group 为字符串时展开 ───────────────────────────────────────


def test_on_command_group_string_expands_command():
    """group="admin" 时 command 变为 "admin <canonical>"，别名也展开。"""

    @on_command("ban", aliases=["block"], group="admin", description="封禁用户")
    async def handler(self, event, ctx):
        pass

    trigger = _get_trigger(handler)
    # trigger 的 command 应为展开后的完整路径
    assert trigger.command == "admin ban"
    assert trigger.aliases == ["admin block"]

    meta = handler.__astrbot_handler_meta__
    assert meta.command_route is not None
    assert meta.command_route.group_path == ["admin"]
    assert meta.command_route.display_command == "admin ban"
    assert meta.command_route.group_help is None


def test_on_command_group_string_with_group_help():
    """group_help 应写入 CommandRouteSpec。"""

    @on_command("ban", group="admin", group_help="管理员命令组")
    async def handler(self, event, ctx):
        pass

    meta = handler.__astrbot_handler_meta__
    assert meta.command_route is not None
    assert meta.command_route.group_help == "管理员命令组"


def test_on_command_group_string_multi_name_aliases():
    """多命令名 + group：非首项命令名也作为别名展开。"""

    @on_command(["ban", "block", "kick"], group="admin")
    async def handler(self, event, ctx):
        pass

    trigger = _get_trigger(handler)
    assert trigger.command == "admin ban"
    # "block" 和 "kick" 来自 commands[1:]，需展开
    assert "admin block" in trigger.aliases
    assert "admin kick" in trigger.aliases


# ── 3. group 为列表时多级路径 ──────────────────────────────────────


def test_on_command_group_list_builds_nested_path():
    """group=["admin", "user"] 时 command 为 "admin user <cmd>"。"""

    @on_command("ban", group=["admin", "user"])
    async def handler(self, event, ctx):
        pass

    trigger = _get_trigger(handler)
    assert trigger.command == "admin user ban"

    meta = handler.__astrbot_handler_meta__
    assert meta.command_route is not None
    assert meta.command_route.group_path == ["admin", "user"]
    assert meta.command_route.display_command == "admin user ban"


def test_on_command_group_list_aliases_expand():
    """多级 group 时别名也要完整展开。"""

    @on_command(
        "ban",
        aliases=["block"],
        group=["admin", "user"],
    )
    async def handler(self, event, ctx):
        pass

    trigger = _get_trigger(handler)
    assert trigger.aliases == ["admin user block"]


# ── 4. group 空值退化 ─────────────────────────────────────────────


def test_on_command_group_empty_string_treated_as_no_group():
    """group="" 等价于不设 group。"""

    @on_command("hello", group="")
    async def handler(self, event, ctx):
        pass

    trigger = _get_trigger(handler)
    assert trigger.command == "hello"
    meta = handler.__astrbot_handler_meta__
    # 空字符串 strip 后为空，group_path 为空列表，不应设置 command_route
    assert meta.command_route is None


def test_on_command_group_list_with_empty_items_filters_them():
    """group=["admin", ""] 中空项被过滤，等效于 ["admin"]。"""

    @on_command("ban", group=["admin", ""])
    async def handler(self, event, ctx):
        pass

    trigger = _get_trigger(handler)
    assert trigger.command == "admin ban"

    meta = handler.__astrbot_handler_meta__
    assert meta.command_route is not None
    assert meta.command_route.group_path == ["admin"]


def test_on_command_group_all_empty_items_no_route():
    """group=["", " "] 全部为空时退化为无 group。"""

    @on_command("hello", group=["", " "])
    async def handler(self, event, ctx):
        pass

    meta = handler.__astrbot_handler_meta__
    assert meta.command_route is None
    trigger = _get_trigger(handler)
    assert trigger.command == "hello"


# ── 5. description 保留 ────────────────────────────────────────────


def test_on_command_group_preserves_description():
    """带 group 时 description 应同时设置到 trigger 和 meta 上。"""

    @on_command("ban", group="admin", description="封禁用户")
    async def handler(self, event, ctx):
        pass

    meta = handler.__astrbot_handler_meta__
    assert meta.description == "封禁用户"
    assert meta.trigger.description == "封禁用户"


# ── 6. conversation_command 透传 group ─────────────────────────────


def test_conversation_command_group_forwarded_to_trigger():
    """conversation_command 应将 group/group_help 透传给 on_command。"""

    @conversation_command(
        "chat",
        group="ai",
        group_help="AI 对话组",
        description="AI对话",
        timeout=120,
    )
    async def handler(self, event, ctx):
        pass

    trigger = _get_trigger(handler)
    assert trigger.command == "ai chat"

    meta = handler.__astrbot_handler_meta__
    assert meta.command_route is not None
    assert meta.command_route.group_path == ["ai"]
    assert meta.command_route.group_help == "AI 对话组"
    # conversation 元数据也应存在
    assert meta.conversation is not None
    assert meta.conversation.timeout == 120


def test_conversation_command_without_group_no_route():
    """conversation_command 不设 group 时 command_route 不存在。"""

    @conversation_command("chat", timeout=60)
    async def handler(self, event, ctx):
        pass

    meta = handler.__astrbot_handler_meta__
    assert meta.command_route is None
    trigger = _get_trigger(handler)
    assert trigger.command == "chat"


# ── 7. 通过 PluginHarness 进行 dispatch 集成测试 ──────────────────


@pytest.mark.asyncio
async def test_on_command_group_dispatch_through_harness(tmp_path):
    """带 group 的命令可以通过完整路径触发 dispatch。"""
    from astrbot_sdk.errors import AstrBotError
    from astrbot_sdk.testing import PluginHarness

    plugin_dir = tmp_path / "group_plugin"
    _write_plugin(
        plugin_dir,
        name="group_plugin",
        class_name="GroupPlugin",
        source=dedent("""
            from astrbot_sdk import Context, MessageEvent, Star
            from astrbot_sdk.decorators import on_command


            class GroupPlugin(Star):
                @on_command("ban", aliases=["block"], group="admin", description="封禁用户")
                async def admin_ban(self, event: MessageEvent, ctx: Context) -> None:
                    await event.reply(f"banned:{event.text}")

                @on_command("hello")
                async def hello(self, event: MessageEvent, ctx: Context) -> None:
                    await event.reply("hello")
        """),
    )

    async with PluginHarness.from_plugin_dir(plugin_dir) as harness:
        # 完整路径 "admin ban" 能匹配
        records = await harness.dispatch_text("admin ban user123")
        assert len(records) == 1
        # event.text 包含完整消息文本，含 group 前缀
        assert records[0].text == "banned:admin ban user123"

        # 别名展开 "admin block" 也能匹配
        alias_records = await harness.dispatch_text("admin block user456")
        assert len(alias_records) == 1
        assert alias_records[0].text == "banned:admin block user456"

        # 不带前缀的 "ban" 不应匹配到 group 命令
        with pytest.raises(AstrBotError):
            await harness.dispatch_text("ban user789")

        # 无 group 的命令不受影响
        hello_records = await harness.dispatch_text("hello world")
        assert len(hello_records) == 1
        assert hello_records[0].text == "hello"


@pytest.mark.asyncio
async def test_on_command_multi_level_group_dispatch(tmp_path):
    """多级 group 路径正确分发。"""
    from astrbot_sdk.testing import PluginHarness

    plugin_dir = tmp_path / "multi_group_plugin"
    _write_plugin(
        plugin_dir,
        name="multi_group_plugin",
        class_name="MultiGroupPlugin",
        source=dedent("""
            from astrbot_sdk import Context, MessageEvent, Star
            from astrbot_sdk.decorators import on_command


            class MultiGroupPlugin(Star):
                @on_command("ban", group=["admin", "user"], description="封禁用户")
                async def admin_user_ban(self, event: MessageEvent, ctx: Context) -> None:
                    await event.reply("admin-user-ban")
        """),
    )

    async with PluginHarness.from_plugin_dir(plugin_dir) as harness:
        records = await harness.dispatch_text("admin user ban someone")
        assert len(records) == 1
        assert records[0].text == "admin-user-ban"
