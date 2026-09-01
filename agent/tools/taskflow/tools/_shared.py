"""Shared plumbing for the taskflow tool family (internal module).

Error-text contract: tools NEVER raise business errors to the LLM; they
return human-readable strings prefixed with ``Error:``. Conflict texts embed
the latest revision so the caller can re-read (taskflow_summary) and retry
with the freshest expected_revision, per skills/taskflow/SKILL.md.
"""

from ..config import TERMINAL_STATUSES
from ..registry.store_sqlite import FlowConflictError


def requester_session_key(session_id: str) -> str:
    """Build the canonical requester session key from a raw LangGraph session id."""
    return f"agent:main:session:{session_id}"


def default_state(description: str, initial_state: dict | None) -> dict:
    """Build the initial state_json payload with guaranteed invariants.

    ``steps`` and ``results`` are always lists (run_task / resume append to
    them); ``description`` and any caller keys are preserved.
    """
    state = dict(initial_state or {})
    state["description"] = description or str(state.get("description") or "")
    state["steps"] = list(state.get("steps") or [])
    state["results"] = list(state.get("results") or [])
    return state


def not_found_error(flow_id: str) -> str:
    return f"Error: TaskFlow '{flow_id}' not found"


def terminal_error(flow_id: str, status: str) -> str:
    return (
        f"Error: TaskFlow '{flow_id}' is terminal (status={status}); "
        "no further mutations allowed"
    )


def conflict_error(exc: FlowConflictError) -> str:
    return f"Error: {exc}"


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES
