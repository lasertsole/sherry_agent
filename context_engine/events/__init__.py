"""Append-only event log (SESSION plan P2-1).

Key state changes (compactions, checkpoints) are recorded as durable events.
Replaying a session's events reconstructs the sequence of state changes; the
projector maps events onto read-model updates (progressive migration).
"""

from .store import append_event, get_events, replay_events
from .types import EventType

__all__ = ["EventType", "append_event", "get_events", "replay_events"]
