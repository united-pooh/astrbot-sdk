import argparse
import asyncio
import sys
import uuid
from pathlib import Path

from astrbot_sdk.api.basic.conversation_mgr import BaseConversationManager
from astrbot_sdk.api.event import AstrMessageEvent
from astrbot_sdk.api.event.astrbot_message import AstrBotMessage, MessageMember
from astrbot_sdk.api.event.message_type import MessageType
from astrbot_sdk.api.platform.platform_metadata import PlatformMetadata
from astrbot_sdk.api.star.context import Context
from astrbot_sdk.runtime.galaxy import Galaxy


class ConversationManager(BaseConversationManager):
    async def new_conversation(
        self,
        unified_msg_origin: str,
        platform_id: str | None = None,
        content: list[dict] | None = None,
        title: str | None = None,
        persona_id: str | None = None,
    ) -> str:
        return str(uuid.uuid4())


class TestContext(Context):
    def __init__(self, conversation_manager: ConversationManager):
        super().__init__()
        self.conversation_manager = conversation_manager
        self._register_component(self.conversation_manager)


def build_event(message_str: str = "hello") -> AstrMessageEvent:
    message_obj = AstrBotMessage(
        type=MessageType.FRIEND_MESSAGE,
        self_id="astrbot_123",
        session_id="test_session",
        message_id="msg_001",
        sender=MessageMember(user_id="user_123", nickname="User123"),
        message=[],
        message_str=message_str,
        raw_message={},
    )

    return AstrMessageEvent(
        message_str=message_obj.message_str,
        message_obj=message_obj,
        platform_meta=PlatformMetadata(
            name="fake",
            description="Fake Platform",
            id="fake_1",
        ),
        session_id=message_obj.session_id,
        is_at_or_wake_command=True,
    )


async def amain(plugins_dir: str, python_executable: str) -> None:
    galaxy = Galaxy()
    context = TestContext(ConversationManager())

    star = await galaxy.connect_to_stdio_star(
        context=context,
        star_name="local-stdio",
        config={
            "plugins_dir": plugins_dir,
            "python_executable": python_executable,
        },
    )

    metadata = await star.handshake()
    print(f"Handshake stars: {list(metadata.keys())}")

    if not getattr(star, "_handlers", None):
        print("No handlers discovered.")
        await star.stop()
        return

    event = build_event("hello")
    handler = star._handlers[0]

    async for result in star.call_handler(handler, event):
        print(f"Handler result: {result}")

    await star.stop()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugins-dir", default="plugins")
    parser.add_argument("--python", default=sys.executable)
    args = parser.parse_args()

    plugins_dir = str(Path(args.plugins_dir).resolve())
    asyncio.run(amain(plugins_dir=plugins_dir, python_executable=args.python))


if __name__ == "__main__":
    main()

