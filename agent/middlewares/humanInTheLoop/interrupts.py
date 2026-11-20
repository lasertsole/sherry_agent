"""Heartbeat freeze shared by every HITL interrupt path.

The heartbeat-staleness watchdog kills a session whose ``(iteration, tool)``
pair stops changing. A graph suspended on ``interrupt()`` does not advance
either, so without a freeze a slow operator would get the agent killed for
"stalling". The ``question`` tool already declares ``skip_heartbeat`` in its
metadata; the approval strategies call :func:`freeze_heartbeat` right before
``interrupt`` and :func:`unfreeze_heartbeat` after the resumed decision comes
back (LangGraph re-executes the node from its start on resume, so the pair
runs again on the resume pass). A turn that never suspends leaves no flag.
"""

from __future__ import annotations

from loguru import logger

__all__ = ["freeze_heartbeat", "unfreeze_heartbeat"]


def freeze_heartbeat(session_id: str) -> None:
    """Set the session's heartbeat-skip flag (best-effort)."""
    from runtime import StateKey, state_register_mem

    try:
        state_register_mem.set_state(session_id, StateKey.HEARTBEAT_SKIP, True)
    except Exception:  # noqa: BLE001 - best-effort: the card must still appear
        logger.debug("HITL: could not set heartbeat_skip for {}", session_id)


def unfreeze_heartbeat(session_id: str) -> None:
    """Clear the session's heartbeat-skip flag (best-effort)."""
    from runtime import StateKey, state_register_mem

    try:
        state_register_mem.delete_state(session_id, StateKey.HEARTBEAT_SKIP)
    except Exception:  # noqa: BLE001
        logger.debug("HITL: could not clear heartbeat_skip for {}", session_id)
