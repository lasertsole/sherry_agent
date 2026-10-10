"""Agent-side producers of trajectory events (the ledger itself is server-side).

``agent/**`` must not import ``server/**``, so the ledger is reached through the
process-level hook registry (``runtime/hooks.py``): the server assembly registers
a recorder, and this module resolves it at call time. Nothing registered (a bare
unit test, an eval process) means the event is dropped — the ledger is
observability, never a dependency.
"""

from __future__ import annotations

from typing import Any

from loguru import logger

__all__ = ["record_trajectory_event"]


def record_trajectory_event(session_id: str, kind: str, payload: dict[str, Any]) -> None:
    """Append one discrete event to the per-turn trajectory ledger (fail-open)."""
    try:
        from runtime import hooks

        recorder = hooks.resolve(hooks.RECORD_TRAJECTORY_EVENT)
        if recorder is None:
            return
        recorder(session_id, kind, payload)
    except Exception:
        logger.debug("trajectory event dropped (fail-open)", exc_info=True)
