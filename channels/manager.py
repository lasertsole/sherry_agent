"""Channel manager for coordinating chat channels."""
import json
import asyncio
from loguru import logger
from .base import BaseChannel
from config import PLUGINS_PATH
from bus.queue import MessageBus
from asyncio import AbstractEventLoop
from typing import Any, Callable, Awaitable
from bus import InboundMessage, OutboundMessage

class ChannelManager:
    """
    Manages chat channels and coordinates message routing.

    Responsibilities:
    - Initialize enabled channels (Telegram, WhatsApp, etc.)
    - Start/stop channels
    - Route outbound messages
    """
    
    _bus: MessageBus = None
    _channels: dict[str, BaseChannel] = {}
    _dispatch_task: asyncio.Task | None = None
    _config: dict[str, str] = None
    _event_loop: AbstractEventLoop | None = None
    _inbound_consumer: Callable[[InboundMessage, BaseChannel], Awaitable[None]] | None = None
    _outbound_consumer: Callable[[OutboundMessage, BaseChannel], Awaitable[None]] | None = None

    async def _inbound_consume_loop(self):
        logger.info("Inbound message consumer loop started")
        while True:
            msg: InboundMessage = await self._bus.consume_inbound()
            logger.debug(
                f"Processing inbound message: channel={msg.channel}, "
                f"chat_id={msg.chat_id}"
            )

            if self._inbound_consumer is not None:
                for channel_name, c in self._config.items():
                    channel = self._channels.get(channel_name)
                    if channel:
                        await self._inbound_consumer(msg, channel)
                    else:
                        logger.warning(f"Channel {channel_name} not found")

    async def _outbound_consume_loop(self):
        logger.info("Outbound message consumer loop started")
        while True:
            msg: OutboundMessage = await self._bus.consume_outbound()
            logger.debug(
                f"Processing outbound message: channel={msg.channel}, "
                f"content_length={len(getattr(msg, 'content', ''))}"
            )

            if self._outbound_consumer is not None:
                for name, func in self._config.items():
                    channel = self._channels.get(name)
                    if channel:
                        await self._outbound_consumer(msg, channel)
                    else:
                        logger.warning("Channel {} not found", name)

    def set_inbound_consumer(self, inbound_consumer: Callable[[InboundMessage, BaseChannel], Awaitable[None]])->None:
        self._inbound_consumer = inbound_consumer

    def set_outbound_consumer(self, outbound_consumer: Callable[[OutboundMessage, BaseChannel], Awaitable[None]])->None:
        self._outbound_consumer = outbound_consumer

    def __init__(self, config: dict[str, str] | None = None,  bus: MessageBus | None = None):
        if config is None:
            channels_json = PLUGINS_PATH / "channels/config.json"
            if not channels_json.exists():
                return

            config = json.loads(channels_json.read_text())

        if bus is None:
            bus = MessageBus()
        self._bus = bus
        self._config = config
        self._init_channels()

        # 如果有运行中的事件循环，则使用它， 否则创建一个新的
        try:
            self._event_loop = asyncio.get_running_loop()
        except RuntimeError:
            self._event_loop = asyncio.new_event_loop()

    def _init_channels(self) -> None:
        """Initialize channels discovered via pkgutil scan + entry_points plugins."""
        from channels.registry import discover_all


        for name, cls in discover_all().items():
            section = self._config.get(name, None)
            if section is None:
                continue
            enabled = (
                section.get("enabled", False)
                if isinstance(section, dict)
                else getattr(section, "enabled", False)
            )
            if not enabled:
                continue
            try:
                channel = cls(section, self._bus)
                self._channels[name] = channel
                logger.info(f"{cls.display_name} channel enabled")
            except Exception as e:
                logger.warning(f"{name} channel not available: {e}")

        self._validate_allow_from()

    def _validate_allow_from(self) -> None:
        for name, ch in self._channels.items():
            if getattr(ch.config, "allow_from", None) == []:
                raise SystemExit(
                    f'Error: "{name}" has empty allowFrom (denies all). '
                    f'Set ["*"] to allow everyone, or add specific user IDs.'
                )

    @staticmethod
    async def _start_channel(name: str, channel: BaseChannel) -> None:
        """Start a channel and log any exceptions."""
        try:
            await channel.start()
        except Exception as e:
            logger.exception(f"Failed to start channel {name}: {e}")

    def start_service(self) -> None:
        """Start all channels and the outbound dispatcher."""
        if not self._event_loop.is_running():
            if not self._channels:
                logger.warning("No channels enabled")
                return

            logger.info(f"Starting channel manager service: channel_count={len(self._channels)}")
            
            # Start outbound dispatcher
            self._dispatch_task = self._event_loop.create_task(self._dispatch_outbound())

            # Start inbound/outbound consumers
            self._event_loop.create_task(self._inbound_consume_loop())
            self._event_loop.create_task(self._outbound_consume_loop())

            # Start channels
            for name, channel in self._channels.items():
                logger.info(f"Starting {name} channel...")
                self._event_loop.create_task(self._start_channel(name, channel))

            # 防止重复运行报错
            try:
                self._event_loop.run_forever()
            except Exception:
                pass

    async def stop_service(self) -> None:
        """Stop all channels and the dispatcher."""
        logger.info("Stopping all channels...")

        # Stop dispatcher
        if self._dispatch_task:
            self._dispatch_task.cancel()
            logger.debug("Dispatcher task cancelled")

        # Stop all channels
        tasks = []
        for name, channel in self._channels.items():
            try:
                tasks.append(asyncio.create_task(channel.stop()))
            except Exception as e:
                logger.error(f"Error stopping {name}: {e}")

        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(f"All channels stopped: channel_count={len(self._channels)}")

        # Stop event loop
        self._event_loop.stop()
        self._event_loop = None
        logger.info("Channel manager service stopped")

    async def _dispatch_outbound(self) -> None:
        """Dispatch outbound messages to the appropriate channel."""
        logger.info("Outbound dispatcher started")

        while True:
            try:
                msg = await asyncio.wait_for(
                    self._bus.consume_outbound(),
                    timeout=1.0
                )

                channel = self._channels.get(msg.channel)
                if channel:
                    try:
                        await channel.send(msg)
                    except Exception as e:
                        logger.error(f"Error sending to {msg.channel}: {e}")
                else:
                    logger.warning(f"Unknown channel: {msg.channel}")

            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    def get_channel(self, name: str) -> BaseChannel | None:
        """Get a channel by name."""
        return self._channels.get(name)

    def get_status(self) -> dict[str, Any]:
        """Get status of all channels."""
        return {
            name: {
                "enabled": True,
                "running": channel.is_running
            }
            for name, channel in self._channels.items()
        }

    def get_bus(self) -> MessageBus:
        """Get the message bus."""
        return self._bus

    def get_event_loop(self) -> asyncio.AbstractEventLoop:
        """Get the event loop."""
        return self._event_loop
    
    @property
    def enabled_channels(self) -> list[str]:
        """Get list of enabled channel names."""
        return list(self._channels.keys())


channel_manager:ChannelManager = ChannelManager()