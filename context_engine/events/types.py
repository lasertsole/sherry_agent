"""Event types for the session event log (progressive coverage)."""

from __future__ import annotations

from enum import StrEnum


class EventType(StrEnum):
    # Session lifecycle
    SESSION_CREATED = "session.created"
    SESSION_ENDED = "session.ended"

    # Messages
    MESSAGE_APPENDED = "message.appended"
    MESSAGE_UPDATED = "message.updated"
    MESSAGE_DELETED = "message.deleted"

    # Compaction
    COMPACTION_STARTED = "compaction.started"
    COMPACTION_ENDED = "compaction.ended"
    COMPACTION_FAILED = "compaction.failed"

    # Checkpoints
    CHECKPOINT_CREATED = "checkpoint.created"
    CHECKPOINT_RESTORED = "checkpoint.restored"

    # State
    STATE_UPDATED = "state.updated"
