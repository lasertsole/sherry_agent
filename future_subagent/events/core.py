"""Core event bus for future_subagent internal messaging.

Replaces the project-wide MessageBus dependency with a dedicated async queue
owned entirely by the future_subagent system.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from loguru import logger


@dataclass
class InboundMessage:
    """Message delivered into a sub-agent session via the event bus.

    Mirrors the shape of ``type.bus.InboundMessage`` so existing consumer
    code (session checkpointer / agent loop) can process it without
    changes.
    """

    channel: str
    sender_id: str
    chat_id: str
    content: str
    timestamp: datetime = field(default_factory=datetime.now)
    media: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    session_id: str | None = None

    @property
    def unique_id(self) -> str:
        return self.session_id or f"{self.channel}:{self.chat_id}"


class EventBus:
    """Async event queue for subagent-internal message delivery.

    Each ``InboundMessage`` published via ``publish_internal()`` is pushed onto an
    ``asyncio.Queue`` and can be consumed by the sub-agent session loop or
    cleanup routines.
    """

    def __init__(self) -> None:
        self._queue: asyncio.Queue[InboundMessage] = asyncio.Queue()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def publish_internal(self, msg: InboundMessage) -> None:
        """Publish an internal inbound message to the sub-agent event bus."""
        logger.debug(
            "EventBus.publish_internal: channel={}, session_id={}, content_length={}",
            msg.channel,
            msg.session_id,
            len(msg.content),
        )
        await self._queue.put(msg)

    async def consume(self) -> InboundMessage:
        """Consume the next message (blocks until available)."""
        msg = await self._queue.get()
        logger.debug(
            "EventBus.consume: channel={}, session_id={}, queue_size={}",
            msg.channel,
            msg.session_id,
            self._queue.qsize(),
        )
        return msg

    @property
    def size(self) -> int:
        """Number of pending messages in the queue."""
        return self._queue.qsize()


# ------------------------------------------------------------------
# Module-level singleton (per-runtime)
# ------------------------------------------------------------------
_BUS: EventBus | None = None


def get_event_bus() -> EventBus:
    """Return the process-wide singleton ``EventBus`` instance."""
    global _BUS
    if _BUS is None:
        _BUS = EventBus()
    return _BUS
