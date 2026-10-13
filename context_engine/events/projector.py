"""Event projector: maps events onto read-model updates (progressive).

The initial handler set covers checkpoint events — the read-model side of
compaction checkpoints — so a replayed event stream can rebuild compaction state.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from loguru import logger

from context_engine.events.types import EventType


class EventProjector:
    """Dispatch events to registered handlers; unknown types are skipped."""

    def __init__(self) -> None:
        self._handlers: dict[str, Callable[[dict[str, Any]], None]] = {}

    def register(self, event_type: EventType, handler: Callable[[dict[str, Any]], None]) -> None:
        self._handlers[event_type.value] = handler

    def project(self, event: dict[str, Any]) -> None:
        handler = self._handlers.get(event["type"])
        if handler is None:
            return
        handler(event)


def make_default_projector() -> EventProjector:
    """Projector with the checkpoint handlers wired (checkpoint read model)."""
    from context_engine.store.core import (
        create_compaction_checkpoint,
        mark_messages_compacted,
    )

    projector = EventProjector()

    def on_checkpoint_created(event: dict[str, Any]) -> None:
        data = event["data"]
        create_compaction_checkpoint(
            event["session_id"],
            pre_compaction_turn=int(data["pre_compaction_turn"]),
            post_compaction_turn=int(data["post_compaction_turn"]),
            summary_text=str(data.get("summary_text", "")),
        )

    def on_checkpoint_restored(event: dict[str, Any]) -> None:
        data = event["data"]
        mark_messages_compacted(
            event["session_id"],
            from_turn=int(data["from_turn"]),
            checkpoint_id=int(data["checkpoint_id"]),
        )

    projector.register(EventType.CHECKPOINT_CREATED, on_checkpoint_created)
    projector.register(EventType.CHECKPOINT_RESTORED, on_checkpoint_restored)
    logger.debug("event projector ready: {}", sorted(projector._handlers))
    return projector
