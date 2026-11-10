"""taskflow_wait_all: flow-scoped event-driven wait for dispatched steps.

Waits until THIS flow's dispatched child sessions settle (or a deadline) and
then reports each target step. It deliberately does NOT reuse the session-wide
yield event (``register_yield_event`` / ``sessions_yield``): that fires only
when ALL of a requester session's children settle, so it would block behind
unrelated background work and collide with the parent's own yield.

Waking is event-driven: the tool subscribes to the keyed terminal-transition
hub (``agent.tools.subagent.registry.completion_events``) for exactly the child
session keys recorded on this flow's dispatched steps, so a child reaching
TERMINAL wakes the waiter immediately instead of waiting for the next poll
tick. The caller's ``poll_interval_seconds`` is kept as the BOUNDED FALLBACK:
a missed notification (or a run that settles through a path that never
publishes) still returns within one interval, and the timeout is unchanged.

The subagent registry is imported lazily and held as module-level injectable
references, so the taskflow family stays importable without the spawn pipeline
and tests can substitute the liveness source without touching the real registry.
"""

import asyncio
import time
from collections.abc import Callable
from typing import Any

from langchain_core.tools import tool

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
from agent.tools.pub_base import SessionId


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


def _subscribe_settle_events(child_keys: list[str]) -> Any | None:
    """Subscribe to the keyed terminal-transition hub; None degrades to polling.

    The import is lazy for the same reason as the registry seam: the taskflow
    family must stay importable without the spawn pipeline. Any failure is
    fail-open — the bounded fallback poll still observes settlement.
    """
    if not child_keys:
        return None
    try:
        from agent.tools.subagent.registry.completion_events import (
            subscribe_run_terminal,
        )

        return subscribe_run_terminal(child_keys)
    except Exception:  # noqa: BLE001 - fallback polling is the safety net
        return None


def _unsubscribe_settle_events(subscription: Any | None) -> None:
    """Drop a settle subscription (fail-open; never breaks the tool result)."""
    if subscription is None:
        return
    try:
        from agent.tools.subagent.registry.completion_events import (
            unsubscribe_run_terminal,
        )

        unsubscribe_run_terminal(subscription)
    except Exception:  # noqa: S110 - a leaked subscription is only a wake hint
        pass


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
            "Partial report only: call taskflow_step(action='resume') for the settled children, "
            "then taskflow_step(action='wait_all') again for the rest."
        )
    else:
        lines.append("Call taskflow_step(action='resume') for each child to inject its result.")
    return "\n".join(lines)


async def _retry_settled_steps(
    flow_id: str,
    report: list[tuple[str, str, bool]],
    session_id: str,
) -> str:
    """Auto-retry settled steps with a policy; return appended report lines.

    Returns "" when no settled step carries a retry policy, so legacy flows
    keep their byte-identical wait_all output. At most ONE retry
    decision per settled step is executed per call: the replacement child is
    spawned and recorded, and the orchestrator calls wait_all again to wait for
    it. No background retry loop lives in this tool.
    """
    flow = await store_sqlite.get_flow(flow_id, session_id)
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
    retryable = [action for action in actions if action["action"] != "exhausted"]

    # The whole wave re-dispatches CONCURRENTLY, bounded by
    # ``max_parallel_dispatch``: every backoff runs at the same time, so a wave
    # of five waits one retry delay instead of five, and one failed spawn never
    # aborts the others. The report keeps the caller's action order.
    semaphore = asyncio.Semaphore(max(1, int(TASKFLOW_INFRA["max_parallel_dispatch"])))

    async def _redispatch_one(action: dict) -> tuple[dict, str | None, Exception | None]:
        async with semaphore:
            try:
                await wait_before_retry(action["policy"])
                return action, await spawn_replacement(action["task"], requester_key), None
            except Exception as exc:  # tool boundary: report, never raise
                return action, None, exc

    outcomes = await asyncio.gather(*(_redispatch_one(action) for action in retryable))
    by_action = {id(action): (new_key, exc) for action, new_key, exc in outcomes}

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
        new_key, exc = by_action.get(id(action), (None, None))
        if exc is not None or new_key is None:
            failure = exc or RuntimeError("replacement spawn returned no child key")
            lines.append(
                f"retry: re-dispatch FAILED for step_id={action['step_id']} "
                f"({type(failure).__name__}: {failure})"
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
            flow_id,
            flow,
            {"redispatches": redispatches, "exhausted": exhausted},
            session_id=session_id,
        )
        if updated is None:
            # Spawned replacements are already alive and named by the error.
            return "\n" + "\n".join([*lines, error or ""])
    if redispatches:
        lines.append(
            "Call taskflow_step(action='wait_all') again to wait for the re-dispatched step(s)."
        )
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

    Wakes on the keyed terminal-transition notification for THIS flow's child
    sessions (unrelated background children can never block the return) and
    re-checks their settled state; ``poll_interval_seconds`` remains the
    bounded fallback so a missed notification cannot hang the call. Returns as
    soon as every target is settled or the timeout elapses; an unknown run is
    treated as settled. Afterwards call taskflow_resume for each child to
    inject its result.

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

    flow = await store_sqlite.get_flow(flow_id, session_id)
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
    # Subscribe BEFORE the first check: a transition that lands between the
    # subscription and the check is observed by the check itself.
    subscription = _subscribe_settle_events([child_key for _, child_key in targets if child_key])

    try:
        while True:
            # Re-arm BEFORE the (synchronous) re-check. A notification that
            # lands after the check but before the wait leaves the event set,
            # so the wait returns immediately — no lost wakeup.
            if subscription is not None:
                subscription.rearm()
            try:
                report = [
                    (step_id, child_key, _settled(child_key)) for step_id, child_key in targets
                ]
            except Exception as exc:
                # The registry is an external seam: a lookup failure is an Error
                # string, never a raised exception at the tool boundary.
                return f"Error: subagent registry lookup failed: {exc}"
            if all(settled for _, _, settled in report):
                base = _format_report(flow_id, report, timed_out=False)
                return base + await _retry_settled_steps(flow_id, report, session_id)
            remaining = max(0.0, deadline - time.monotonic())
            if remaining <= 0:
                return _format_report(flow_id, report, timed_out=True)
            # Event first, fallback poll second: the caller's interval bounds
            # how long a MISSED notification can delay the return.
            wait_seconds = min(interval, remaining)
            if subscription is not None:
                await subscription.wait(wait_seconds)
            else:
                await asyncio.sleep(wait_seconds)
    finally:
        _unsubscribe_settle_events(subscription)
