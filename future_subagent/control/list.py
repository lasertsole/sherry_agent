"""Build sub-agent list views with deduplication by latest generation.

Includes model, runtime, token, and pending-descendants information.
"""

import time
from ..types.registry import SubagentRunRecord, ExecutionStatus
from ..registry import queries
from .controller import build_latest_by_session_index


def is_subagent_run_visible_to_session(run: SubagentRunRecord, session_key: str) -> bool:
    """Check whether a run should be visible to the given session key."""
    controller = run.controller_session_key or run.requester_session_key
    if session_key == controller:
        return True
    if session_key == run.requester_session_key:
        return True
    return False


def _resolve_runtime_display(run: SubagentRunRecord) -> str | None:
    """Format elapsed runtime as a human-readable string (e.g. '30s', '1.5m', '2.0h').

    Uses current monotonic time as the end if the run has not yet ended.
    """
    if run.execution.started_at is None:
        return None
    end = run.execution.ended_at or time.monotonic()
    elapsed_s = end - run.execution.started_at
    if elapsed_s < 60:
        return f"{elapsed_s:.0f}s"
    if elapsed_s < 3600:  # Less than an hour → show minutes
        return f"{elapsed_s / 60:.1f}m"
    return f"{elapsed_s / 3600:.1f}h"  # Over an hour → show hours


def build_subagent_list(session_key: str) -> dict:
    """Build a structured summary of active and recent sub-agent runs for a session."""
    runs = queries.list_runs_for_requester(session_key)

    visible_runs = [r for r in runs if is_subagent_run_visible_to_session(r, session_key)]

    latest_index = build_latest_by_session_index(visible_runs)
    deduped = list(latest_index.values())

    active = [r for r in deduped if r.execution.status == ExecutionStatus.RUNNING]
    recent = [r for r in deduped if r.execution.status == ExecutionStatus.TERMINAL]

    active_summaries = []
    for r in active[:10]:
        pending_desc = queries.count_pending_descendant_runs(r.child_session_key)
        active_summaries.append({
            "run_id": r.run_id,
            "label": r.label,
            "task": r.task[:80],
            "depth": r.depth,
            "role": r.role,
            "generation": r.generation,
            "model": r.thinking,
            "runtime": _resolve_runtime_display(r),
            "pending_descendants": pending_desc,
        })

    recent_summaries = []
    for r in recent[:5]:
        outcome = r.execution.outcome
        recent_summaries.append({
            "run_id": r.run_id,
            "label": r.label,
            "status": outcome.status if outcome else "unknown",
            "ended_reason": r.ended_reason,
            "task": r.task[:80],
            "generation": r.generation,
            "model": r.thinking,
            "runtime": _resolve_runtime_display(r),
        })

    return {
        "total": len(deduped),
        "active_count": len(active),
        "recent_count": len(recent),
        "active": active_summaries,
        "recent": recent_summaries,
    }
