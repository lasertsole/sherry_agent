"""QQ channel implementation using botpy SDK."""

import asyncio
import time
from loguru import logger
from pydantic import Field
from collections import deque
from config.schema import Base
from bus.queue import MessageBus
from channels.base import BaseChannel
from bus.events import OutboundMessage
from typing import TYPE_CHECKING, Any, Literal


try:
    import botpy
    from botpy.message import C2CMessage, GroupMessage

    QQ_AVAILABLE = True
except ImportError:
    QQ_AVAILABLE = False
    botpy = None
    C2CMessage = None
    GroupMessage = None

if TYPE_CHECKING:
    from botpy.message import C2CMessage, GroupMessage


def _make_bot_class(channel: "QQChannel") -> "type[botpy.Client]":
    """Create a botpy Client subclass bound to the given channel."""
    intents = botpy.Intents(public_messages=True, direct_message=True)

    class _Bot(botpy.Client):
        def __init__(self):
            super().__init__(intents=intents, ext_handlers=False)

        async def on_ready(self):
            logger.info(f"QQ bot ready: {self.robot.name}")

        async def on_c2c_message_create(self, message: "C2CMessage"):
            logger.debug(f"QQ C2C message received: message_id={message.id}")
            await channel._on_message(message, is_group=False)

        async def on_group_at_message_create(self, message: "GroupMessage"):
            logger.debug(f"QQ Group message received: message_id={message.id}")
            await channel._on_message(message, is_group=True)

        async def on_direct_message_create(self, message):
            logger.debug(f"QQ Direct message received: message_id={message.id}")
            await channel._on_message(message, is_group=False)

    return _Bot


class QQConfig(Base):
    """QQ channel configuration using botpy SDK."""

    enabled: bool = False
    app_id: str = ""
    secret: str = ""
    allow_from: list[str] = Field(default_factory=list)
    msg_format: Literal["plain", "markdown"] = "plain"


class QQChannel(BaseChannel):
    """QQ channel using botpy SDK with WebSocket connection."""

    name = "qq"
    display_name = "QQ"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return QQConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        if isinstance(config, dict):
            config = QQConfig.model_validate(config)
        super().__init__(config, bus)
        self.config: QQConfig = config
        self._client: "botpy.Client | None" = None
        self._processed_ids: deque = deque(maxlen=1000)
        self._msg_seq: int = 1  # 消息序列号，避免被 QQ API 去重
        self._chat_type_cache: dict[str, str] = {}

    async def start(self) -> None:
        """Start the QQ bot."""
        if not QQ_AVAILABLE:
            logger.error("QQ SDK not installed. Run: pip install qq-botpy")
            return

        if not self.config.app_id or not self.config.secret:
            logger.error("QQ app_id and secret not configured")
            return

        self._running = True
        BotClass = _make_bot_class(self)
        self._client = BotClass()
        logger.info("QQ bot started (C2C & Group supported)")
        await self._run_bot()

    async def _run_bot(self) -> None:
        """Run the bot connection with auto-reconnect."""
        while self._running:
            try:
                await self._client.start(appid=self.config.app_id, secret=self.config.secret)
            except Exception as e:
                logger.warning(f"QQ bot error: {e}")
            if self._running:
                logger.info("Reconnecting QQ bot in 5 seconds...")
                await asyncio.sleep(5)

    async def stop(self) -> None:
        """Stop the QQ bot."""
        self._running = False
        if self._client:
            try:
                await self._client.close()
            except Exception:
                pass
        logger.info("QQ bot stopped")

    async def send(self, msg: OutboundMessage) -> None:
        """Send a message through QQ."""
        if not self._client:
            logger.warning("QQ client not initialized")
            return

        start_time = time.time()
        logger.debug(
            f"Sending QQ message: chat_id={msg.chat_id}, "
            f"content_length={len(getattr(msg, 'content', ''))}"
        )

        try:
            msg_id = msg.metadata.get("message_id")
            self._msg_seq += 1
            use_markdown = self.config.msg_format == "markdown"
            payload: dict[str, Any] = {
                "msg_type": 2 if use_markdown else 0,
                "msg_id": msg_id,
                "msg_seq": self._msg_seq,
            }
            if use_markdown:
                payload["markdown"] = {"content": msg.content}
            else:
                payload["content"] = msg.content

            chat_type = self._chat_type_cache.get(msg.chat_id, "c2c")
            if chat_type == "group":
                await self._client.api.post_group_message(
                    group_openid=msg.chat_id,
                    **payload,
                )
            else:
                await self._client.api.post_c2c_message(
                    openid=msg.chat_id,
                    **payload,
                )
            
            elapsed = time.time() - start_time
            logger.debug(
                f"QQ message sent successfully: chat_id={msg.chat_id}, "
                f"duration={elapsed:.2f}s"
            )
        except Exception as e:
            elapsed = time.time() - start_time
            logger.error(
                f"Error sending QQ message: chat_id={msg.chat_id}, "
                f"duration={elapsed:.2f}s, error={e}"
            )

    async def _on_message(self, data: "C2CMessage | GroupMessage", is_group: bool = False) -> None:
        """Handle incoming message from QQ."""
        try:
            # Dedup by message ID
            if data.id in self._processed_ids:
                logger.debug(f"Duplicate QQ message ignored: message_id={data.id}")
                return
            self._processed_ids.append(data.id)
    
            content = (data.content or "").strip()
            # 提取图片URL
            media_urls = []
            if hasattr(data, 'attachments') and data.attachments:
                for attachment in data.attachments:
                    if attachment.url:
                        media_urls.append(attachment.url)
                        logger.info(f"Received image: {attachment.filename} from {attachment.url}")

            # 如果既没有文本内容也没有图片，则忽略
            if not content and not media_urls:
                logger.debug(f"QQ message ignored: no content or media, message_id={data.id}")
                return
    
            if is_group:
                chat_id = data.group_openid
                user_id = data.author.member_openid
                self._chat_type_cache[chat_id] = "group"
            else:
                chat_id = str(getattr(data.author, 'id', None) or getattr(data.author, 'user_openid', 'unknown'))
                user_id = chat_id
                self._chat_type_cache[chat_id] = "c2c"
    
            content_preview = content[:50] if content else ""
            logger.info(
                f"QQ message received: chat_id={chat_id}, user_id={user_id}, "
                f"is_group={is_group}, content_preview='{content_preview}', "
                f"media_count={len(media_urls)}"
            )
    
            await self._handle_message(
                sender_id=user_id,
                chat_id=chat_id,
                content=content,
                media=media_urls if media_urls else None,
                metadata={"message_id": data.id},
            )
            logger.debug(f"QQ message processed: message_id={data.id}")
        except Exception as e:
            logger.exception(e)