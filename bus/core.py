"""Async message queue for decoupled channel-agent communication."""

import asyncio
from loguru import logger
from type.bus import InboundMessage, OutboundMessage


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.
    """

    def __init__(self):
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue()
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue()

    async def publish_inbound(self, msg: InboundMessage) -> None:
        """Publish a message from a channel to the agent."""
        logger.debug(
            f"Publishing inbound message: channel={msg.channel}, "
            f"session_id={getattr(msg, 'session_id', 'N/A')}, "
            f"content_length={len(getattr(msg, 'content', ''))}"
        )
        await self.inbound.put(msg)

    async def consume_inbound(self) -> InboundMessage:
        """Consume the next inbound message (blocks until available)."""
        msg = await self.inbound.get()
        logger.debug(
            f"Consumed inbound message: channel={msg.channel}, "
            f"session_id={getattr(msg, 'session_id', 'N/A')}, "
            f"queue_size={self.inbound.qsize()}"
        )
        return msg

    async def publish_outbound(self, msg: OutboundMessage) -> None:
        """Publish a response from the agent to channels."""
        logger.debug(
            f"Publishing outbound message: channel={msg.channel}, "
            f"content_length={len(getattr(msg, 'content', ''))}"
        )
        await self.outbound.put(msg)

    async def consume_outbound(self) -> OutboundMessage:
        """Consume the next outbound message (blocks until available)."""
        msg = await self.outbound.get()
        logger.debug(
            f"Consumed outbound message: channel={msg.channel}, "
            f"queue_size={self.outbound.qsize()}"
        )
        return msg

    @property
    def inbound_size(self) -> int:
        """Number of pending inbound messages."""
        return self.inbound.qsize()

    @property
    def outbound_size(self) -> int:
        """Number of pending outbound messages."""
        return self.outbound.qsize()