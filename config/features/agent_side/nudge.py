"""Compression-time nudge settings.

The memory review is dispatched on every compression; plan extraction fires
when the todo list is all-complete at a compression.
"""

from typing import TypedDict


class NudgeConfig(TypedDict):
    """Compression-time nudge settings."""

    plan_extraction_enabled: bool


NUDGE: NudgeConfig = {
    "plan_extraction_enabled": True,
}
