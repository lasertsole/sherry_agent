"""QQ channel implementation using botpy SDK."""

import sys
import json
import time
import shutil
import asyncio
import importlib
import subprocess
from pathlib import Path
from loguru import logger
from pydantic import Field
from bus import MessageBus
from collections import deque
from config.schema import Base
from type.bus import OutboundMessage
from channels.base import BaseChannel
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


# Failure-cooldown state for the runtime dep-install fallback (below).  An
# external restart loop / health check that keeps calling start() would
# otherwise re-run `uv pip install` + the import-retry loop on every call
# while the dependency remains genuinely unavailable.  After N consecutive
# failures we stop attempting for _COOLDOWN_WINDOW seconds and surface the
# manual-install guidance instead.
_COOLDOWN_THRESHOLD = 3      # consecutive failures before suppressing retries
_COOLDOWN_WINDOW = 60        # seconds to stay in cooldown once tripped
_consecutive_install_failures = 0
_cooldown_until = 0.0


def _in_cooldown() -> bool:
    """Return True if repeated install failures have tripped the cooldown."""
    return time.monotonic() < _cooldown_until


def _record_install_failure() -> None:
    """Bump the consecutive-failure counter and arm the cooldown timer."""
    global _consecutive_install_failures, _cooldown_until
    _consecutive_install_failures += 1
    if _consecutive_install_failures >= _COOLDOWN_THRESHOLD:
        _cooldown_until = time.monotonic() + _COOLDOWN_WINDOW
        logger.error(
            "QQ dep auto-install failed {} times in a row; suppressing further attempts for {}s. "
            "Install manually:\n  uv pip install -r plugins/channels/qq/requirements.txt",
            _consecutive_install_failures, _COOLDOWN_WINDOW,
        )


def _reset_cooldown() -> None:
    """Clear failure tracking after a successful install/import."""
    global _consecutive_install_failures, _cooldown_until
    _consecutive_install_failures = 0
    _cooldown_until = 0.0


def _install_deps() -> bool:
    """Install plugin-local requirements.txt (qq-botpy) into the running env.

    Uses ``uv`` when available (consistent with the project toolchain),
    otherwise falls back to ``sys.executable -m pip``.  Installation is
    idempotent -- already-satisfied packages are skipped.  Returns ``True``
    once the dependencies are importable, ``False`` otherwise.

    After ``_COOLDOWN_THRESHOLD`` consecutive failures the install is skipped
    for ``_COOLDOWN_WINDOW`` seconds (see cooldown helpers above), so a
    persistent outage doesn't trigger a hot loop of subprocess installs.
    """
    if _in_cooldown():
        logger.warning("QQ dep auto-install suppressed (in cooldown); install manually")
        return False

    req_file = Path(__file__).resolve().parent / "requirements.txt"
    if not req_file.is_file():
        logger.warning("No requirements.txt for QQ channel: {}", req_file)
        return False

    if shutil.which("uv"):
        cmd = ["uv", "pip", "install", "-q", "-r", str(req_file)]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "-q", "-r", str(req_file)]

    logger.info("Installing QQ dependencies ({}): ...", req_file.name)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired as exc:
        logger.error("Timed out installing QQ dependencies from {}", req_file)
        _record_install_failure()
        return False

    if result.returncode != 0:
        logger.error(
            "Failed to install QQ dependencies ({}): {}", req_file,
            (result.stderr or result.stdout or "").strip(),
        )
        _record_install_failure()
        return False

    # A pip subprocess may not be visible to the freshly-created importers,
    # so clear the module resolution cache before re-importing.
    importlib.invalidate_caches()
    _reset_cooldown()
    logger.info("QQ dependency install succeeded")
    return True


def _try_import_botpy() -> bool:
    """(Re)import the botpy SDK into globals. Returns True when importable.

    Retries a few times: on Windows, antivirus real-time scanning can briefly
    lock freshly-installed files, making a just-installed package unimportable
    for a few hundred milliseconds even though uv reported success.  We catch
    ``Exception`` (not just ``ImportError``) because such a lock surfaces as an
    ``OSError``/``PermissionError``, which otherwise would bubble out of here
    and crash ``start()``.
    """
    global botpy, C2CMessage, GroupMessage
    last_exc: Exception | None = None
    for attempt in range(3):
        importlib.invalidate_caches()
        try:
            import botpy as _botpy
            from botpy.message import C2CMessage as _c2c
            from botpy.message import GroupMessage as _group
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(0.3)
            continue
        botpy = _botpy
        C2CMessage = _c2c
        GroupMessage = _group
        return True
    # Final failure: surface the *real* underlying error once for debugging.
    logger.error("QQ SDK import failed after retries: %r", last_exc)
    botpy = None
    C2CMessage = None
    GroupMessage = None
    return False


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
    receiver: str = ""  # Default chat_id for proactive (heartbeat) delivery.


class QQChannel(BaseChannel):
    """QQ channel using botpy SDK with WebSocket connection."""

    name = "qq"
    display_name = "QQ"

    @classmethod
    def default_config(cls) -> dict[str, Any]:
        return QQConfig().model_dump(by_alias=True)

    def __init__(self, config: Any, bus: MessageBus):
        config = self._merge_credentials(config)
        if isinstance(config, QQConfig):
            self.config = config
        else:
            self.config = QQConfig.model_validate(config)
        super().__init__(self.config, bus)
        self._client: "botpy.Client | None" = None
        self._processed_ids: deque = deque(maxlen=1000)
        self._msg_seq: int = 1  # 消息序列号，避免被 QQ API 去重
        self._chat_type_cache: dict[str, str] = {}

    @staticmethod
    def _merge_credentials(config: Any) -> Any:
        """Merge plugin-local credentials (app_id/receiver) from qq/config.json.

        The generic toggles (enabled/allow_from/.../heartbeat/cron) live in
        plugins/channels/config.json, while QQ credentials live in the
        plugin's own plugins/channels/qq/config.json.
        """
        if not isinstance(config, dict):
            return config
        cred_path = Path(__file__).resolve().parent / "config.json"
        try:
            creds = json.loads(cred_path.read_text(encoding="utf-8"))
        except Exception:
            logger.warning("Failed to load {}", cred_path)
            return config
        if not isinstance(creds, dict):
            return config
        merged = dict(config)
        for key in ("app_id", "receiver"):
            value = creds.get(key)
            if isinstance(value, str):
                merged[key] = value
        return merged

    async def start(self) -> None:
        """Start the QQ bot."""
        if not QQ_AVAILABLE:
            # First import failed at load time -- try to install the SDK on
            # the fly (enable channel -> auto-download deps), then re-import.
            logger.warning("QQ SDK not installed. Attempting to auto-install dependencies...")
            if _install_deps() and _try_import_botpy():
                logger.info("QQ SDK installed and imported successfully")
            else:
                # Installed but still unimportable (or install/suppressed by
                # cooldown) -- count it so a genuinely broken install doesn't
                # hot-loop on every start() call.
                _record_install_failure()
                logger.error(
                    "QQ SDK not available. Install the dependency manually:\n"
                    "  uv pip install -r plugins/channels/qq/requirements.txt\n"
                    "  # or: python -m pip install -r plugins/channels/qq/requirements.txt"
                )
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