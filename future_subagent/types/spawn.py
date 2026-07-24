"""Spawn behavior enums for sub-agents: run mode and context inheritance mode."""

from enum import Enum


class SpawnMode(str, Enum):
    """Sub-agent run mode. RUN = one-shot task deleted after completion; SESSION = persistent session bound to a channel thread."""
    RUN = "run"
    SESSION = "session"


class ContextMode(str, Enum):
    """Context inheritance mode. ISOLATED = clean context with no parent transcript; FORK = copy parent transcript as starting context."""
    ISOLATED = "isolated"
    FORK = "fork"
