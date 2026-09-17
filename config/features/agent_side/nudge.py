"""Compression-time nudge settings (memory review + plan extraction)."""

from typing import TypedDict


class NudgeConfig(TypedDict):
    """Compression-time nudge settings (memory review + plan extraction)."""

    nudge_memory_threshold: int
    plan_extraction_enabled: bool


NUDGE: NudgeConfig = {
    "nudge_memory_threshold": 10,
    "plan_extraction_enabled": True,
}
