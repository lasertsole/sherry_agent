"""Session-binding helpers shared by every eval suite entry point.

The two binding paths (``coding_capability._bind_session`` and
``external_bench.bind_external_session``) both grant HITL approvals through
this helper. Without it a high-risk tool call parks the turn behind a human
approval no eval harness can answer, and the task hangs until timeout.

The agent-runtime import stays lazy so this module remains importable without
the server -- the shared-package contract.
"""

from __future__ import annotations


def set_session_yolo(session_id: str) -> None:
    """Auto-grant approvals for an eval session (idempotent)."""
    if not session_id:
        return
    from agent.middlewares.humanInTheLoop.approval import set_session_yolo as _grant

    _grant(str(session_id))
