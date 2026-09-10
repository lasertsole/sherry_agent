"""taskflow_wait_all: flow-scoped bounded-poll wait for dispatched steps.

Waits until THIS flow's dispatched child sessions settle (or a deadline) and
then reports each target step. It deliberately does NOT reuse the session-wide
yield event (``register_yield_event`` / ``sessions_yield``): that fires only
when ALL of a requester session's children settle, so it would block behind
unrelated background work and collide with the parent's own yield. Instead this
polls only the child keys recorded on the flow's own dispatched steps.

The subagent registry is imported lazily and held as module-level injectable
references, so the taskflow family stays importable without the spawn pipeline
and tests can substitute the liveness source without touching the real registry.
"""

import asyncio
import time
from collections.abc import Callable
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import InjectedState

from ..config import StepStatus
from ..registry import store_sqlite
from ._shared import not_found_error, step_status

SessionId = Annotated[str, InjectedState("session_id")]

# Never busy-spin: the caller's interval is clamped up to this floor.
_MIN_POLL_INTERVAL_SECONDS = 0.05

# Injectable registry seam (bound once by ``_ensure_registry``).
get_run_by_child_session_key: Callable[[str], Any] | None = None
is_live_unended_run: Callable[[Any], bool] | None = None
_REGISTRY_ERROR: str | None = None


def _ensure_registry() -> bool:
    """Bind the registry seam on first use; False when it cannot be imported."""
    global get_run_by_child_session_key, is_live_unended_run, _REGISTRY_ERROR
    if get_run_by_child_session_key is not None and is_live_unended_run is not None:
        return True
    if _REGISTRY_ERROR is not None:
        return False
    try:
        from agent.tools.subagent.registry.helpers import (
            is_live_unended_run as _real_is_live,
        )
        from agent.tools.subagent.registry.queries import (
            get_run_by_child_session_key as _real_get_run,
        )
    except Exception as exc:
        # A missing/optional registry must degrade to an Error string, never raise.
        _REGISTRY_ERROR = str(exc)
        return False
    get_run_by_child_session_key = _real_get_run
    is_live_unended_run = _real_is_live
    return True


def _settled(child_session_key: str) -> bool:
    """True when the child run is absent (unknown) or no longer live.

    ``None`` is explicitly settled: an unknown/cleaned-up run must never be
    treated as live (which would hang the wait), and its absence is never
    dereferenced.
    """
    if get_run_by_child_session_key is None or is_live_unended_run is None:
        raise RuntimeError("subagent registry unavailable")
    run = get_run_by_child_session_key(child_session_key)
    if run is None:
        return True
    return not is_live_unended_run(run)


def _format_report(
    flow_id: str,
    report: list[tuple[str, str, bool]],
    *,
    timed_out: bool,
) -> str:
    """Render the per-step settled report (full when done, partial on timeout)."""
    settled_count = sum(1 for _, _, settled in report if settled)
    total = len(report)
    lines = [f"TaskFlow wait_all: flow_id={flow_id}"]
    if timed_out:
        lines.append(
            f"TaskFlow wait_all timeout: {settled_count} of {total} dispatched "
            "step(s) settled before the deadline"
        )
    else:
        lines.append(f"TaskFlow wait_all complete: all {total} dispatched step(s) settled")
    for step_id, child_key, settled in report:
        key = child_key or "<none>"
        lines.append(f"  - step_id={step_id}, child_session_key={key}, settled={settled}")
    if timed_out:
        lines.append(
            "Partial report only: call taskflow_resume for the settled children, "
            "then taskflow_wait_all again for the rest."
        )
    else:
        lines.append("Call taskflow_resume for each child to inject its result.")
    return "\n".join(lines)


@tool("taskflow_wait_all")
async def taskflow_wait_all(
    flow_id: str,
    timeout_seconds: float = 300.0,
    poll_interval_seconds: float = 0.5,
    session_id: SessionId = "",
) -> str:
    """Wait until this flow's dispatched child sessions settle, then report.

    Polls only the child sessions recorded on THIS flow's dispatched steps, so
    unrelated background children can never block the return. Returns as soon
    as every target is settled or the timeout elapses; an unknown run is treated
    as settled. Afterwards call taskflow_resume for each child to inject its
    result.
    """
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"

    if not _ensure_registry():
        return f"Error: subagent registry unavailable: {_REGISTRY_ERROR}"

    flow = await store_sqlite.get_flow(flow_id)
    if flow is None:
        return not_found_error(flow_id)

    state = flow.get("state") or {}
    steps = list(state.get("steps") or [])
    targets = [
        (step.get("step_id"), step.get("child_session_key") or "")
        for step in steps
        if step_status(step) == StepStatus.DISPATCHED
    ]

    if not targets:
        return f"TaskFlow wait_all: flow_id={flow_id}, no dispatched steps to wait for."

    interval = max(_MIN_POLL_INTERVAL_SECONDS, float(poll_interval_seconds))
    deadline = time.monotonic() + max(0.0, float(timeout_seconds))

    while True:
        report = [(step_id, child_key, _settled(child_key)) for step_id, child_key in targets]
        if all(settled for _, _, settled in report):
            return _format_report(flow_id, report, timed_out=False)
        if time.monotonic() >= deadline:
            return _format_report(flow_id, report, timed_out=True)
        await asyncio.sleep(interval)
