"""Async message queue for decoupled channel-agent communication."""

import asyncio
from loguru import logger
from config.num import BUS_QUEUE_MAXSIZE
from type.bus import InboundMessage, OutboundMessage


class MessageBus:
    """
    Async message bus that decouples chat channels from the agent core.

    Channels push messages to the inbound queue, and the agent processes
    them and pushes responses to the outbound queue.

    Both queues are bounded (``BUS_QUEUE_MAXSIZE`` by default): when a queue
    is full, ``publish_inbound`` / ``publish_outbound`` await free space
    instead of letting memory grow without limit (audit #11). The producer
    is backpressured — messages are delayed, never silently dropped — so
    ``asyncio.QueueFull`` cannot surface through this API. Bounded waits are
    preferred over drop-on-full because inbound carries user messages and
    cron deliveries that must not be lost.
    """

    def __init__(self, maxsize: int = BUS_QUEUE_MAXSIZE):
        if maxsize <= 0:
            # maxsize=0 would silently recreate the unbounded-queue problem.
            raise ValueError(f"maxsize must be positive, got {maxsize}")
        self.inbound: asyncio.Queue[InboundMessage] = asyncio.Queue(maxsize=maxsize)
        self.outbound: asyncio.Queue[OutboundMessage] = asyncio.Queue(maxsize=maxsize)

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
            f"Consumed outbound message: channel={msg.channel}, queue_size={self.outbound.qsize()}"
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
