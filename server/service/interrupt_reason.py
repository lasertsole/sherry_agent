"""The one definition of an interrupted-turn reason (2026-11-09 audit L10).

Both the interrupt marker and the resumable-breakpoint record used to declare
the same ``Literal`` separately; a new reason added to one and missed in the
other would silently fail validation. They import this alias instead.
"""

from __future__ import annotations

from typing import Literal

__all__ = ["BreakpointReason", "InterruptReason"]

InterruptReason = Literal["cancelled", "heartbeat_timeout"]
#: Compatibility name used by the resumable-breakpoint module.
BreakpointReason = InterruptReason
