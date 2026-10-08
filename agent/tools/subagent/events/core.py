"""Core event bus for the subagent subsystem internal messaging.

Replaces the project-wide MessageBus dependency with a dedicated async queue
owned entirely by the subagent system.
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

    Uses a thread-safe collections.deque as the backing store so that
    publish and consume can happen on different event loops / threads
    without "bound to a different event loop" errors.
    """

    def __init__(self) -> None:
        import collections

        self._buffer: collections.deque[InboundMessage] = collections.deque()
        self._lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def publish_internal(self, msg: InboundMessage) -> None:
        """Publish an internal inbound message to the sub-agent event bus."""
        self._buffer.append(msg)
        logger.debug(
            "EventBus.publish_internal: channel={}, session_id={}, content_length={}",
            msg.channel,
            msg.session_id,
            len(msg.content),
        )

    def publish_internal_sync(self, msg: InboundMessage) -> None:
        """Thread-safe publish: works from any thread/event loop."""
        self._buffer.append(msg)
        logger.debug(
            "EventBus.publish_internal_sync: channel={}, session_id={}, content_length={}",
            msg.channel,
            msg.session_id,
            len(msg.content),
        )

    async def consume(self) -> InboundMessage:
        """Consume the next message (blocks until available).

        Polls the buffer with short sleeps when empty. This avoids
        asyncio.Event / asyncio.Queue cross-loop binding issues.
        """
        while True:
            if self._buffer:
                msg = self._buffer.popleft()
                logger.debug(
                    "EventBus.consume: channel={}, session_id={}, remaining={}",
                    msg.channel,
                    msg.session_id,
                    len(self._buffer),
                )
                return msg
            await asyncio.sleep(0.05)

    @property
    def size(self) -> int:
        """Number of pending messages in the buffer."""
        return len(self._buffer)


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
