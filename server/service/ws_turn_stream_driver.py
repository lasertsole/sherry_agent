"""The WS queue executor's StreamDriver variant.

Extracted from ``turn_runner`` (2026-11-09 audit L9): that module promises no
eager project-internal imports, yet it imported the concrete ``StreamDriver``
base class and subclassed it at module import time. The driver lives here and
is imported lazily at the one call site, so importing ``turn_runner`` no longer
drags in the streaming stack.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

from runtime.session.ws_push import broadcast_to_session
from server.service.stream_driver import StreamDriver


class WsTurnStreamDriver(StreamDriver):
    """Driver for the queue executor's generation turns (WsTurnExecutor._drive).

    The error frame is surfaced here; the row is still delivered so the drain
    never double-sends an error for the same row.
    """

    async def send_frame(self, payload: dict[str, Any]) -> None:
        # Same fan-out contract as the resume path: every live socket of the
        # session receives each frame exactly once, never a replay.
        await broadcast_to_session(self.session_id, payload)

    async def check_interrupt(self) -> dict[str, Any] | None:
        # Lazy seam: turn_runner owns the cycle-safe wrappers.
        from server.service.turn_runner import get_pending_interrupt

        return await get_pending_interrupt(self.session_id)

    def apply_hitl_pending(self) -> None:
        from server.service.turn_runner import set_hitl_pending

        set_hitl_pending(self.session_id, True)

    def log_interrupt(self, interrupt: dict[str, Any]) -> None:
        logger.info(
            f"TurnRunner: HITL interrupt for session {self.session_id}, "
            f"tool={interrupt.get('tool_name')}"
        )

    def log_cancelled(self) -> None:
        logger.info(f"TurnRunner: generation cancelled: session_id={self.session_id}")

    def log_error(self, exc: Exception, elapsed: float) -> None:
        logger.warning(f"TurnRunner: generation failed: session_id={self.session_id}, error={exc}")

    def public_error(self, exc: Exception) -> str:
        from server.service.stream_diag import public_error_text

        return public_error_text(exc)


__all__ = ["WsTurnStreamDriver"]
