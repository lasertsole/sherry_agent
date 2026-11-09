"""Skill write-origin provenance — ContextVar for agent-sediment vs user writes.

The curator only consolidates/prunes skills it autonomously created. Skills a
user asks a foreground agent to write belong to the user and must never be
auto-curated.

The background-review fork in
``agent/middlewares/summarization/background_reviews.py`` sets this ContextVar
around its tool loop (``set_current_write_origin(BACKGROUND_REVIEW_ORIGIN)``),
and tool handlers such as ``skill_manage`` read it to mark the created skill as
agent-created.

Usage:
    from agent.tools.pub_base.skill_provenance import (
        set_current_write_origin,
        reset_current_write_origin,
        get_current_write_origin,
    )

    token = set_current_write_origin("background_review")
    try:
        ...  # tool runs here
    finally:
        reset_current_write_origin(token)
"""

import contextvars


_write_origin: contextvars.ContextVar[str] = contextvars.ContextVar(
    "skill_write_origin",
    default="foreground",
)

# The sentinel value the background-review fork uses.
BACKGROUND_REVIEW = "background_review"


def set_current_write_origin(origin: str) -> contextvars.Token[str]:
    """Bind the active write origin to the current context.

    Returns a Token the caller must pass to reset_current_write_origin
    in a finally block.
    """
    return _write_origin.set(origin or "foreground")


def reset_current_write_origin(token: contextvars.Token[str]) -> None:
    """Restore the prior write origin context."""
    _write_origin.reset(token)


def get_current_write_origin() -> str:
    """Return the active write origin.

    Default: "foreground" — any tool call made by a regular (non-review)
    agent, from the CLI, the gateway, cron, or a subagent.

    "background_review" — the self-improvement review fork; only skills
    created under this origin should be marked agent-created for curator
    management.
    """
    return _write_origin.get()


def is_background_review() -> bool:
    """Convenience: True iff the current write origin is the background
    review fork."""
    return get_current_write_origin() == BACKGROUND_REVIEW
