"""Spawn behavior enum for sub-agents: the run mode."""

from enum import StrEnum


class SpawnMode(StrEnum):
    """Sub-agent run mode. RUN = one-shot task deleted after completion; SESSION = persistent session bound to a channel thread."""

    RUN = "run"
    SESSION = "session"
