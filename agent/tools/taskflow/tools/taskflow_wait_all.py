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

from config.features import TASKFLOW_INFRA
from ..config import StepStatus
from ..registry import store_sqlite
from ._retry import (
    plan_settled_retries,
    persist_retry_actions,
    requester_key_for_retry,
    spawn_replacement,
    wait_before_retry,
)
from ._shared import not_found_error, step_status

SessionId = Annotated[str, InjectedState("session_id")]

# Never busy-spin: the caller's interval is clamped up to this floor.
_MIN_POLL_INTERVAL_SECONDS = TASKFLOW_INFRA["wait_all_min_poll_interval_seconds"]

# Injectable registry seam (bound once by ``_ensure_registry``).
get_run_by_child_session_key: Callable[[str], Any] | None = None
is_live_unended_run: Callable[[Any], bool] | None = None
_REGISTRY_ERROR: str | None = None


def _ensure_registry() -> bool:
    """Bind the registry seam on first use; False when it cannot be imported.

    A failed import is NOT cached: every call re-attempts it, so a transient
    registry-import failure is recoverable on a later invocation.
    """
    global get_run_by_child_session_key, is_live_unended_run, _REGISTRY_ERROR
    if get_run_by_child_session_key is not None and is_live_unended_run is not None:
        return True
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
    _REGISTRY_ERROR = None
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


async def _retry_settled_steps(
    flow_id: str,
    report: list[tuple[str, str, bool]],
    session_id: str,
) -> str:
    """Auto-retry settled steps with a policy; return appended report lines.

    Returns "" when no settled step carries a retry policy, so legacy flows
    keep the byte-identical pre-GAP-8 wait_all output. At most ONE retry
    decision per settled step is executed per call: the replacement child is
    spawned and recorded, and the orchestrator calls wait_all again to wait for
    it. No background retry loop lives in this tool.
    """
    flow = await store_sqlite.get_flow(flow_id)
    if flow is None:
        return ""
    state = flow.get("state") or {}
    steps = list(state.get("steps") or [])
    results = list(state.get("results") or [])
    actions = plan_settled_retries(steps, results, [(sid, key) for sid, key, _ in report])
    if not actions:
        return ""

    requester_key = requester_key_for_retry(state, session_id)
    redispatches: list[tuple[dict, str]] = []
    exhausted: list[dict] = []
    lines: list[str] = []
    for action in actions:
        if action["action"] == "exhausted":
            exhausted.append(action)
            lines.append(
                f"retry: step_id={action['step_id']} exhausted "
                f"(retry_count={action['retry_count']}/{action['max_retries']}); "
                "marked done with a failure note"
            )
            continue
        policy = action["policy"]
        await wait_before_retry(policy)
        try:
            new_key = await spawn_replacement(action["task"], requester_key)
        except Exception as exc:
            # Tool boundary: a failed replacement spawn is reported, never raised.
            lines.append(
                f"retry: re-dispatch FAILED for step_id={action['step_id']} "
                f"({type(exc).__name__}: {exc})"
            )
            continue
        redispatches.append((action, new_key))
        lines.append(
            f"retry: re-dispatched step_id={action['step_id']} -> "
            f"child_session_key={new_key}, "
            f"retry_count={action['retry_count']}/{policy['max_retries']}, "
            f"retry_delay_seconds={policy['retry_delay_seconds']}"
        )

    if redispatches or exhausted:
        updated, error = await persist_retry_actions(
            flow_id, flow, {"redispatches": redispatches, "exhausted": exhausted}
        )
        if updated is None:
            # Spawned replacements are already alive and named by the error.
            return "\n" + "\n".join([*lines, error or ""])
    if redispatches:
        lines.append("Call taskflow_wait_all again to wait for the re-dispatched step(s).")
    if exhausted:
        lines.append(
            "Exhausted step(s) need an explicit decision: resume the failure note or fail the flow."
        )
    return "\n" + "\n".join(lines)


@tool("taskflow_wait_all")
async def taskflow_wait_all(
    flow_id: str,
    timeout_seconds: float = TASKFLOW_INFRA["wait_all_default_timeout_seconds"],
    poll_interval_seconds: float = TASKFLOW_INFRA["wait_all_default_poll_interval_seconds"],
    session_id: SessionId = "",
) -> str:
    """Wait until this flow's dispatched child sessions settle, then report.

    Polls only the child sessions recorded on THIS flow's dispatched steps, so
    unrelated background children can never block the return. Returns as soon
    as every target is settled or the timeout elapses; an unknown run is treated
    as settled. Afterwards call taskflow_resume for each child to inject its
    result.

    A settled (dead) child of a step carrying a retry_policy with budget left
    is re-dispatched once automatically; when the budget is exhausted the step
    is marked done with a failure-note result. Call taskflow_wait_all again to
    wait for the replacement child.

    ``session_id`` is injected by the runtime and used only as the fallback
    requester for a replacement child when the flow has no creator key.
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
        try:
            report = [(step_id, child_key, _settled(child_key)) for step_id, child_key in targets]
        except Exception as exc:
            # The registry is an external seam: a lookup failure is an Error
            # string, never a raised exception at the tool boundary.
            return f"Error: subagent registry lookup failed: {exc}"
        if all(settled for _, _, settled in report):
            base = _format_report(flow_id, report, timed_out=False)
            return base + await _retry_settled_steps(flow_id, report, session_id)
        if time.monotonic() >= deadline:
            return _format_report(flow_id, report, timed_out=True)
        await asyncio.sleep(interval)
