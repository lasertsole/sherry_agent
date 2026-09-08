"""Channel manager for coordinating chat channels."""

import json
import asyncio
from loguru import logger
from bus import MessageBus
from .base import BaseChannel
from config import PLUGINS_PATH
from asyncio import AbstractEventLoop
from typing import Any
from collections.abc import Callable, Awaitable
from type.bus import InboundMessage, OutboundMessage


class ChannelManager:
    """
    Manages chat channels and coordinates message routing.

    Responsibilities:
    - Initialize enabled channels (Telegram, WhatsApp, etc.)
    - Start/stop channels
    - Route outbound messages
    """

    _bus: MessageBus | None
    _channels: dict[str, BaseChannel]
    _dispatch_task: asyncio.Task | None
    _config: dict[str, str] | None
    _event_loop: AbstractEventLoop | None
    _inbound_consumer: Callable[[InboundMessage, BaseChannel], Awaitable[None]] | None
    _outbound_consumer: Callable[[OutboundMessage, BaseChannel], Awaitable[None]] | None
    _started: bool

    async def _consume_loop(self, direction: str) -> None:
        """Shared consumer loop (audit 3.1.1): drain one bus queue forever,
        invoking the direction's registered consumer once per configured
        channel. ``direction`` is ``"inbound"`` or ``"outbound"``.
        """
        consume = (
            self._bus.consume_inbound if direction == "inbound" else self._bus.consume_outbound
        )
        logger.info(f"{direction.capitalize()} message consumer loop started")
        while True:
            msg = await consume()
            if direction == "inbound":
                logger.debug(
                    f"Processing inbound message: channel={msg.channel}, chat_id={msg.chat_id}"
                )
            else:
                logger.debug(
                    f"Processing outbound message: channel={msg.channel}, "
                    f"content_length={len(getattr(msg, 'content', ''))}"
                )

            consumer = self._inbound_consumer if direction == "inbound" else self._outbound_consumer
            if consumer is not None:
                for channel_name, c in self._config.items():
                    channel = self._channels.get(channel_name)
                    if channel:
                        await consumer(msg, channel)
                    else:
                        logger.warning(f"Channel {channel_name} not found")

    async def _inbound_consume_loop(self):
        await self._consume_loop("inbound")

    async def _outbound_consume_loop(self):
        await self._consume_loop("outbound")

    def set_inbound_consumer(
        self, inbound_consumer: Callable[[InboundMessage, BaseChannel], Awaitable[None]]
    ) -> None:
        """Bind the callback invoked for every inbound message."""
        self._inbound_consumer = inbound_consumer

    def set_outbound_consumer(
        self, outbound_consumer: Callable[[OutboundMessage, BaseChannel], Awaitable[None]]
    ) -> None:
        """Bind the callback invoked for every outbound message."""
        self._outbound_consumer = outbound_consumer

    def __init__(self, config: dict[str, str] | None = None, bus: MessageBus | None = None):
        # Set BEFORE the early return below: config-less managers stay usable.
        self._bus = None
        self._channels = {}
        self._dispatch_task = None
        self._config = None
        self._event_loop = None
        self._inbound_consumer = None
        self._outbound_consumer = None
        self._started = False

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
        """Initialize channels discovered via plugins directory scan + entry_points plugins."""
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
        """Schedule all channels and the dispatcher on the event loop, then return.

        Non-blocking (audit #16): the calling thread is never parked here —
        the caller owns running ``self._event_loop`` (``run_forever()``).
        """
        if self._started:
            return

        if not self._channels:
            logger.warning("No channels enabled")
            return

        if self._event_loop is None:
            logger.warning("Channel manager has no event loop; cannot start service")
            return

        self._started = True
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
        if self._event_loop is not None:
            self._event_loop.stop()
            self._event_loop = None
        self._started = False
        logger.debug("Channel manager service stopped")

    async def _dispatch_outbound(self) -> None:
        """Dispatch outbound messages to the appropriate channel."""
        logger.debug("Outbound dispatcher started")

        while True:
            try:
                msg = await asyncio.wait_for(self._bus.consume_outbound(), timeout=1.0)

                channel = self._channels.get(msg.channel)
                if channel:
                    try:
                        await channel.send(msg)
                    except Exception as e:
                        logger.error(f"Error sending to {msg.channel}: {e}")
                else:
                    logger.warning(f"Unknown channel: {msg.channel}")

            except TimeoutError:
                continue
            except asyncio.CancelledError:
                break

    def get_channel(self, name: str) -> BaseChannel | None:
        """Get a channel by name."""
        return self._channels.get(name)

    def get_status(self) -> dict[str, Any]:
        """Get status of all channels."""
        return {
            name: {"enabled": True, "running": channel.is_running}
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


_channel_manager: ChannelManager | None = None


def get_channel_manager() -> ChannelManager:
    """Lazy singleton accessor (audit #16: no instantiation at import time)."""
    global _channel_manager
    if _channel_manager is None:
        _channel_manager = ChannelManager()
    return _channel_manager


def __getattr__(name: str) -> Any:
    if name == "channel_manager":
        return get_channel_manager()
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
