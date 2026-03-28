"""v4 原生事件对象。

顶层 ``MessageEvent`` 保持精简，只承载 v4 运行时真正需要的基础能力。
迁移期扩展事件能力放在独立模块中，而不是继续塞回顶层事件类型。

MessageEvent 是 handler 接收的主要事件类型，封装了：
    - 消息文本内容
    - 发送者信息（user_id, group_id）
    - 平台标识
    - 回复能力（reply, reply_image, reply_chain）
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, TypeVar

from ._message_types import normalize_message_type
from .message.components import (
    At,
    BaseMessageComponent,
    File,
    Image,
    Plain,
    component_to_payload_sync,
    payloads_to_components,
)
from .message.result import EventResultType, MessageChain, MessageEventResult
from .protocol.descriptors import SessionRef

if TYPE_CHECKING:
    from .context import Context


@dataclass(slots=True)
class PlainTextResult:
    """纯文本结果。

    用于 handler 返回简单的文本结果。
    """

    text: str


ReplyHandler = Callable[[str], Awaitable[None]]
_MessageComponentT = TypeVar("_MessageComponentT", bound=BaseMessageComponent)

_JSON_DROP = object()


def _coerce_str(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return str(value)


def _coerce_optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else str(value)
    return text or None


def _json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple)):
        items = []
        for item in value:
            normalized = _json_safe_value(item)
            if normalized is not _JSON_DROP:
                items.append(normalized)
        return items
    if isinstance(value, dict):
        normalized_dict: dict[str, Any] = {}
        for key, item in value.items():
            normalized = _json_safe_value(item)
            if normalized is not _JSON_DROP:
                normalized_dict[str(key)] = normalized
        return normalized_dict
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        try:
            return _json_safe_value(model_dump())
        except Exception:
            return _JSON_DROP
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return _JSON_DROP
    return value


def _json_safe_mapping(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        safe_item = _json_safe_value(item)
        if safe_item is not _JSON_DROP:
            normalized[str(key)] = safe_item
    return normalized


class MessageEvent:
    """消息事件对象。

    封装收到的消息，提供便捷的回复方法。
    每个 handler 调用都会创建新的 MessageEvent 实例。

    Attributes:
        text: 消息文本内容
        user_id: 发送者用户 ID，缺失时为空字符串
        group_id: 群组 ID（私聊时为 None）
        platform: 平台标识（如 "qq", "wechat"），缺失时为空字符串
        session_id: 会话 ID（通常是 group_id 或 user_id，缺失时为空字符串）
        raw: 原始消息数据

    Example:
        @on_command("echo")
        async def echo(self, event: MessageEvent, ctx: Context):
            await event.reply(f"你说: {event.text}")
    """

    text: str
    user_id: str
    group_id: str | None
    platform: str
    session_id: str
    self_id: str
    platform_id: str
    message_type: str
    sender_name: str
    raw: dict[str, Any]
    _is_admin: bool
    _stopped: bool
    _host_extras: dict[str, Any]
    _host_extras_present: bool
    _sdk_local_extras: dict[str, Any]
    _sdk_local_extras_present: bool
    _sdk_local_extras_dirty: bool
    _messages: list[BaseMessageComponent]
    _messages_present: bool
    _message_outline: str
    _sent_messages: list[BaseMessageComponent]
    _sent_messages_present: bool
    _sent_message_outline: str
    _sent_message_outline_present: bool
    _context: Context | None
    _reply_handler: ReplyHandler | None

    def __init__(
        self,
        *,
        text: str = "",
        user_id: str | None = None,
        group_id: str | None = None,
        platform: str | None = None,
        session_id: str | None = None,
        self_id: str | None = None,
        platform_id: str | None = None,
        message_type: str | None = None,
        sender_name: str | None = None,
        is_admin: bool = False,
        raw: dict[str, Any] | None = None,
        context: Context | None = None,
        reply_handler: ReplyHandler | None = None,
    ) -> None:
        """初始化消息事件。

        Args:
            text: 消息文本
            user_id: 用户 ID
            group_id: 群组 ID
            platform: 平台标识
            session_id: 会话 ID，None 时自动从 group_id/user_id 推断
            raw: 原始消息数据
            context: 运行时上下文
            reply_handler: 自定义回复处理器
        """
        normalized_user_id = _coerce_str(user_id)
        normalized_group_id = _coerce_optional_str(group_id)
        normalized_platform = _coerce_str(platform)
        normalized_session_id = _coerce_str(session_id)

        self.text = text
        self.user_id = normalized_user_id
        self.group_id = normalized_group_id
        self.platform = normalized_platform
        self.session_id = (
            normalized_session_id or normalized_group_id or normalized_user_id or ""
        )
        self.self_id = _coerce_str(self_id)
        self.platform_id = _coerce_str(platform_id) or normalized_platform
        self.message_type = normalize_message_type(
            message_type,
            group_id=normalized_group_id,
            user_id=normalized_user_id,
        )
        self.sender_name = _coerce_str(sender_name)
        self._is_admin = bool(is_admin)
        self.raw = raw or {}
        self._stopped = False
        host_extras = self.raw.get("host_extras")
        raw_extras = self.raw.get("extras")
        self._host_extras = _json_safe_mapping(
            host_extras if isinstance(host_extras, dict) else raw_extras
        )
        self._host_extras_present = "host_extras" in self.raw or "extras" in self.raw
        sdk_local_extras = self.raw.get("sdk_local_extras")
        self._sdk_local_extras = _json_safe_mapping(sdk_local_extras)
        self._sdk_local_extras_present = "sdk_local_extras" in self.raw
        self._sdk_local_extras_dirty = False
        messages_payload = self.raw.get("messages")
        self._messages = (
            payloads_to_components(messages_payload)
            if isinstance(messages_payload, list)
            else []
        )
        self._messages_present = "messages" in self.raw
        self._message_outline = str(self.raw.get("message_outline", self.text))
        sent_messages_payload = self.raw.get("sent_messages")
        self._sent_messages = (
            payloads_to_components(sent_messages_payload)
            if isinstance(sent_messages_payload, list)
            else []
        )
        self._sent_messages_present = "sent_messages" in self.raw
        self._sent_message_outline = str(self.raw.get("sent_message_outline", ""))
        self._sent_message_outline_present = "sent_message_outline" in self.raw
        self._context = context
        self._reply_handler = reply_handler
        if self._reply_handler is None and context is not None:
            self._reply_handler = lambda text: context.platform.send(
                self.session_ref or self.session_id,
                text,
            )

    def _require_runtime_context(self, action: str) -> Context:
        """获取运行时上下文，不存在则抛出异常。"""
        if self._context is None:
            raise RuntimeError(f"MessageEvent 未绑定运行时上下文，无法 {action}")
        return self._context

    def _reply_target(self) -> SessionRef | str:
        """获取回复目标。"""
        return self.session_ref or self.session_id

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, Any],
        *,
        context: Context | None = None,
        reply_handler: ReplyHandler | None = None,
    ) -> MessageEvent:
        """从协议载荷创建事件实例。

        Args:
            payload: 协议层传递的消息数据
            context: 运行时上下文
            reply_handler: 自定义回复处理器

        Returns:
            新的 MessageEvent 实例
        """
        target_payload = payload.get("target")
        session_id = payload.get("session_id")
        platform = payload.get("platform")
        if isinstance(target_payload, dict):
            target = SessionRef.model_validate(target_payload)
            session_id = session_id or target.session
            platform = platform or target.platform
        return cls(
            text=str(payload.get("text", "")),
            user_id=payload.get("user_id"),
            group_id=payload.get("group_id"),
            platform=platform,
            session_id=session_id,
            self_id=payload.get("self_id"),
            platform_id=payload.get("platform_id"),
            message_type=payload.get("message_type"),
            sender_name=payload.get("sender_name"),
            is_admin=bool(payload.get("is_admin", False)),
            raw=payload,
            context=context,
            reply_handler=reply_handler,
        )

    def to_payload(self) -> dict[str, Any]:
        """转换为协议载荷格式。

        Returns:
            可序列化的字典
        """
        payload = dict(self.raw)
        payload.update(
            {
                "text": self.text,
                "user_id": self.user_id,
                "group_id": self.group_id,
                "platform": self.platform,
                "session_id": self.session_id,
                "self_id": self.self_id,
                "platform_id": self.platform_id,
                "message_type": self.message_type,
                "sender_name": self.sender_name,
                "is_admin": self._is_admin,
            }
        )
        if self.session_ref is not None:
            payload["target"] = self.session_ref.to_payload()
        merged_extras = dict(self._host_extras)
        merged_extras.update(self._sdk_local_extras_payload())
        if merged_extras:
            payload["extras"] = merged_extras
        elif self._host_extras_present:
            payload["extras"] = {}
        else:
            payload.pop("extras", None)
        if self._host_extras or self._host_extras_present:
            payload["host_extras"] = dict(self._host_extras)
        else:
            payload.pop("host_extras", None)
        sdk_local_extras = self._sdk_local_extras_payload()
        if sdk_local_extras or self._should_serialize_sdk_local_extras():
            payload["sdk_local_extras"] = sdk_local_extras
        else:
            payload.pop("sdk_local_extras", None)
        if self._messages or self._messages_present:
            payload["messages"] = [
                component_to_payload_sync(component) for component in self._messages
            ]
        else:
            payload.pop("messages", None)
        payload["message_outline"] = self._message_outline
        if self._sent_messages or self._sent_messages_present:
            payload["sent_messages"] = [
                component_to_payload_sync(component)
                for component in self._sent_messages
            ]
        else:
            payload.pop("sent_messages", None)
        if self._sent_message_outline or self._sent_message_outline_present:
            payload["sent_message_outline"] = self._sent_message_outline
        else:
            payload.pop("sent_message_outline", None)
        return payload

    @property
    def session_ref(self) -> SessionRef | None:
        """获取会话引用对象。

        Returns:
            SessionRef 实例，如果没有有效的 session_id 则返回 None
        """
        if not self.session_id:
            return None
        return SessionRef(
            conversation_id=self.session_id,
            platform=self.platform,
            raw=self.raw or None,
        )

    @property
    def target(self) -> SessionRef | None:
        """session_ref 的别名。"""
        return self.session_ref

    @property
    def unified_msg_origin(self) -> str:
        """统一消息来源标识，等价于 session_id。"""
        return self.session_id

    def is_private_chat(self) -> bool:
        """判断是否为私聊事件。优先根据 message_type 判断，否则按 group_id 是否存在推断。"""
        if self.message_type:
            return self.message_type == "private"
        return not bool(self.group_id)

    def is_group_chat(self) -> bool:
        """判断是否为群聊事件。推断逻辑与 is_private_chat 对称。"""
        if self.message_type:
            return self.message_type == "group"
        return bool(self.group_id)

    def get_platform_id(self) -> str:
        """返回平台实例标识（如 "qq_official"）。"""
        return self.platform_id

    def get_message_type(self) -> str:
        """返回归一化后的消息类型（"private" 或 "group"）。"""
        return self.message_type

    def get_session_id(self) -> str:
        """返回当前会话 ID（群聊为 group_id，私聊为 user_id）。"""
        return self.session_id

    def is_admin(self) -> bool:
        """判断发送者是否拥有管理员权限。"""
        return self._is_admin

    def get_messages(self) -> list[BaseMessageComponent]:
        """返回当前事件的所有消息组件（入站消息）。"""
        return list(self._messages)

    def get_sent_messages(self) -> list[BaseMessageComponent]:
        """返回出站消息组件列表，仅对 after-send 事件有意义。"""
        return list(self._sent_messages)

    def has_component(self, type_: type[BaseMessageComponent]) -> bool:
        """判断消息中是否包含指定类型的组件。"""
        return any(isinstance(component, type_) for component in self._messages)

    def get_components(
        self,
        type_: type[_MessageComponentT],
    ) -> list[_MessageComponentT]:
        """筛选并返回消息中所有指定类型的组件。"""
        return [
            component for component in self._messages if isinstance(component, type_)
        ]

    def get_images(self) -> list[Image]:
        """返回消息中所有图片组件。"""
        return self.get_components(Image)

    def get_files(self) -> list[File]:
        """返回消息中所有文件组件。"""
        return self.get_components(File)

    def extract_plain_text(self) -> str:
        """从消息组件中提取所有纯文本内容，用空格拼接。"""
        return " ".join(
            component.text
            for component in self._messages
            if isinstance(component, Plain)
        )

    def get_at_users(self) -> list[str]:
        """返回消息中 @ 的用户 ID 列表，排除 @全体。"""
        return [
            str(component.qq)
            for component in self._messages
            if isinstance(component, At) and str(component.qq).lower() != "all"
        ]

    def get_message_outline(self) -> str:
        """返回消息摘要文本，供日志/展示用。"""
        return self._message_outline

    def get_sent_message_outline(self) -> str:
        """返回出站消息摘要文本，仅对 after-send 事件有意义。"""
        return self._sent_message_outline

    async def get_group(self) -> dict[str, Any] | None:
        """通过宿主桥接获取当前群组的元数据，非群聊或平台不支持时返回 None。"""
        context = self._require_runtime_context("get_group")
        output = await context._proxy.call(  # noqa: SLF001
            "platform.get_group",
            {
                "session": self.session_id,
                "target": (
                    self.session_ref.to_payload()
                    if self.session_ref is not None
                    else None
                ),
            },
        )
        payload = output.get("group")
        if not isinstance(payload, dict):
            return None
        return dict(payload)

    def set_extra(self, key: str, value: Any) -> None:
        """存储 SDK 本地的事件临时数据。

        写入后可通过 ``get_extra()`` 立即读取。若需跨 SDK 桥接传递到
        后续 handler 或生命周期事件，值必须是 JSON 可序列化的。

        推荐用法:
        - 仅使用 ``dict`` / ``list`` / ``str`` / ``int`` / ``float`` /
          ``bool`` / ``None`` 及其嵌套组合。
        - 存储框架对象前先转为 payload（用 ``component_to_payload_sync()``），
          读取后用 ``payload_to_component()`` 还原。

        非序列化值在当前 handler 内可读，但桥接传输时会被丢弃。
        """
        self._sdk_local_extras[key] = value
        self._sdk_local_extras_dirty = True

    def get_extra(self, key: str | None = None, default: Any = None) -> Any:
        """读取事件临时数据，合并宿主侧和 SDK 本地的 extras。

        若 key 为 None 则返回完整的合并字典。
        非序列化值在桥接传输后可能丢失，此时应改用 JSON 安全的 payload。
        """
        extras = dict(self._host_extras)
        extras.update(self._sdk_local_extras)
        if key is None:
            return extras
        return extras.get(key, default)

    def clear_extra(self) -> None:
        """清空 SDK 本地的所有临时数据。"""
        self._sdk_local_extras.clear()
        self._sdk_local_extras_dirty = True

    def _sdk_local_extras_payload(self) -> dict[str, Any]:
        return _json_safe_mapping(self._sdk_local_extras)

    def _should_serialize_sdk_local_extras(self) -> bool:
        return (
            self._sdk_local_extras_present
            or self._sdk_local_extras_dirty
            or bool(self._sdk_local_extras)
        )

    async def request_llm(self) -> bool:
        """请求宿主为当前事件触发默认 LLM 调用链，返回是否成功。"""
        context = self._require_runtime_context("request_llm")
        output = await context._proxy.call(  # noqa: SLF001
            "system.event.llm.request",
            {
                "target": (
                    self.session_ref.to_payload()
                    if self.session_ref is not None
                    else None
                ),
            },
        )
        return bool(output.get("should_call_llm", False))

    async def should_call_llm(self) -> bool:
        """查询宿主当前是否已决定调用默认 LLM。"""
        context = self._require_runtime_context("should_call_llm")
        output = await context._proxy.call(  # noqa: SLF001
            "system.event.llm.get_state",
            {
                "target": (
                    self.session_ref.to_payload()
                    if self.session_ref is not None
                    else None
                ),
            },
        )
        return bool(output.get("should_call_llm", False))

    async def set_result(self, result: MessageEventResult) -> MessageEventResult:
        """将 SDK 结果存入宿主桥接，供后续处理阶段读取。"""
        context = self._require_runtime_context("set_result")
        await context._proxy.call(  # noqa: SLF001
            "system.event.result.set",
            {
                "target": (
                    self.session_ref.to_payload()
                    if self.session_ref is not None
                    else None
                ),
                "result": result.to_payload(),
            },
        )
        return result

    async def get_result(self) -> MessageEventResult | None:
        """从宿主桥接读取当前请求的 SDK 结果，不存在时返回 None。"""
        context = self._require_runtime_context("get_result")
        output = await context._proxy.call(  # noqa: SLF001
            "system.event.result.get",
            {
                "target": (
                    self.session_ref.to_payload()
                    if self.session_ref is not None
                    else None
                ),
            },
        )
        payload = output.get("result")
        if not isinstance(payload, dict):
            return None
        return MessageEventResult.from_payload(payload)

    async def clear_result(self) -> None:
        """清除当前请求的 SDK 结果。"""
        context = self._require_runtime_context("clear_result")
        await context._proxy.call(  # noqa: SLF001
            "system.event.result.clear",
            {
                "target": (
                    self.session_ref.to_payload()
                    if self.session_ref is not None
                    else None
                ),
            },
        )

    def stop_event(self) -> None:
        """标记事件为已停止，阻止后续 handler 处理该事件。"""
        self._stopped = True

    def continue_event(self) -> None:
        """清除停止标记，允许后续 handler 继续处理。"""
        self._stopped = False

    def is_stopped(self) -> bool:
        """查询事件是否已被标记为停止。"""
        return self._stopped

    async def reply(self, text: str) -> None:
        """回复文本消息。

        Args:
            text: 要回复的文本内容

        Raises:
            RuntimeError: 如果未绑定 reply handler
        """
        if self._reply_handler is None:
            raise RuntimeError("MessageEvent 未绑定 reply handler，无法 reply")
        await self._reply_handler(text)

    async def reply_image(self, image_url: str) -> None:
        """回复图片消息。

        Args:
            image_url: 图片 URL

        Raises:
            RuntimeError: 如果未绑定运行时上下文
        """
        context = self._require_runtime_context("reply_image")
        await context.platform.send_image(self._reply_target(), image_url)

    async def reply_chain(
        self,
        chain: MessageChain | list[BaseMessageComponent] | list[dict[str, Any]],
    ) -> None:
        """回复消息链（多类型消息组合）。

        Args:
            chain: 消息链组件列表

        Raises:
            RuntimeError: 如果未绑定运行时上下文
        """
        context = self._require_runtime_context("reply_chain")
        await context.platform.send_chain(self._reply_target(), chain)

    async def react(self, emoji: str) -> bool:
        """发送 emoji 表态，平台不支持时返回 False。"""
        context = self._require_runtime_context("react")
        output = await context._proxy.call(  # noqa: SLF001
            "system.event.react",
            {
                "target": (
                    self.session_ref.to_payload()
                    if self.session_ref is not None
                    else None
                ),
                "emoji": emoji,
            },
        )
        return bool(output.get("supported", False))

    async def send_typing(self) -> bool:
        """发送"正在输入"状态，平台不支持时返回 False。"""
        context = self._require_runtime_context("send_typing")
        output = await context._proxy.call(  # noqa: SLF001
            "system.event.send_typing",
            {
                "target": (
                    self.session_ref.to_payload()
                    if self.session_ref is not None
                    else None
                ),
            },
        )
        return bool(output.get("supported", False))

    async def send_streaming(
        self,
        generator,
        use_fallback: bool = False,
    ) -> bool:
        """流式输出：将 generator 产出的文本/组件逐块推送到宿主流式通道。

        generator 可产出 str、MessageChain、MessageEventResult 或单个组件。
        平台不支持流式输出时返回 False。
        """
        context = self._require_runtime_context("send_streaming")
        output = await context._proxy.call(  # noqa: SLF001
            "system.event.send_streaming",
            {
                "target": (
                    self.session_ref.to_payload()
                    if self.session_ref is not None
                    else None
                ),
                "use_fallback": use_fallback,
            },
        )
        if not bool(output.get("supported", False)):
            return False

        stream_id = str(output.get("stream_id", ""))
        if not stream_id:
            return False

        try:
            async for item in generator:
                if isinstance(item, str):
                    chain = MessageChain([Plain(item, convert=False)])
                else:
                    chain = self._coerce_chain_or_raise(item)
                await context._proxy.call(  # noqa: SLF001
                    "system.event.send_streaming_chunk",
                    {
                        "stream_id": stream_id,
                        "chain": await chain.to_payload_async(),
                    },
                )
        finally:
            output = await context._proxy.call(  # noqa: SLF001
                "system.event.send_streaming_close",
                {"stream_id": stream_id},
            )
        return bool(output.get("supported", False))

    def bind_reply_handler(self, reply_handler: ReplyHandler) -> None:
        """绑定自定义回复处理器。

        Args:
            reply_handler: 回复处理函数
        """
        self._reply_handler = reply_handler

    def plain_result(self, text: str) -> PlainTextResult:
        """创建纯文本结果。

        Args:
            text: 结果文本

        Returns:
            PlainTextResult 实例
        """
        return PlainTextResult(text=text)

    def make_result(self) -> MessageEventResult:
        """创建一个空的 SDK 事件结果包装器。"""
        return MessageEventResult(type=EventResultType.EMPTY)

    def image_result(self, url_or_path: str) -> MessageEventResult:
        """创建包含一张图片的消息链结果。支持 URL、base64 和本地路径。"""
        if url_or_path.startswith(("http://", "https://")):
            image = Image.fromURL(url_or_path)
        elif url_or_path.startswith("base64://"):
            image = Image.fromBase64(url_or_path.removeprefix("base64://"))
        else:
            image = Image.fromFileSystem(url_or_path)
        return MessageEventResult(
            type=EventResultType.CHAIN,
            chain=MessageChain([image]),
        )

    def chain_result(
        self,
        chain: MessageChain | list[BaseMessageComponent],
    ) -> MessageEventResult:
        """Create a chain result from SDK components."""
        normalized = (
            chain if isinstance(chain, MessageChain) else MessageChain(list(chain))
        )
        return MessageEventResult(type=EventResultType.CHAIN, chain=normalized)

    @staticmethod
    def _coerce_chain_or_raise(item: Any) -> MessageChain:
        if isinstance(item, MessageEventResult):
            return item.chain
        if isinstance(item, MessageChain):
            return item
        if isinstance(item, BaseMessageComponent):
            return MessageChain([item])
        if isinstance(item, list) and all(
            isinstance(component, BaseMessageComponent) for component in item
        ):
            return MessageChain(list(item))
        raise TypeError(
            "send_streaming only accepts str, MessageChain, MessageEventResult or SDK message components"
        )
