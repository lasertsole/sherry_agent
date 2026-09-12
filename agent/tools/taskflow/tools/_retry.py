"""Step-level retry policy helpers (GAP-8).

A step may carry a declarative ``retry_policy`` dict::

    {"max_retries": int, "retry_delay_seconds": float, "retry_on": list[str]}

``retry_count`` (stored on the step, default 0) counts RE-dispatch attempts,
never the original dispatch: 0 while the first child runs, 1 once the first
replacement is spawned. A retry is allowed while
``retry_count < max_retries``; every field lives inside ``state_json`` (no
schema migration).

Failure classification is result-text based (``classify_failure``): the
``retry_on`` filter matches the classified type, while an empty list retries
every classified failure. ``taskflow_wait_all`` detects a dead child without
any result text, so it always consumes the budget while it lasts.

The replacement spawn goes through the shared, monkeypatchable
``_dispatch.dispatch_child`` seam (module-qualified call).
"""

import asyncio
import time

from ..config import StepStatus
from . import _dispatch
from ._shared import requester_session_key, result_hash, update_flow_with_conflict_retry

DEFAULT_RETRY_DELAY_SECONDS = 60.0

# Ordered classifier: timeout/rate-limit are checked before the generic error
# bucket, so a text naming both (e.g. "failed: timed out") gets the specific
# type the ``retry_on`` filter expects.
_FAILURE_PATTERNS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("timeout", ("timeout", "timed out", "time limit")),
    ("rate_limit", ("rate limit", "rate_limit", "429", "too many requests")),
    ("error", ("error", "failed", "failure", "exception", "traceback", "aborted", "crashed")),
)

# Success phrasings that literally contain a failure word ("no errors") but
# report zero failures; stripped before classification so a clean result can
# never trigger a gratuitous retry.
_NEGATED_FAILURE_PHRASES: tuple[str, ...] = (
    "no error",
    "0 errors",
    "zero errors",
    "error-free",
    "error free",
    "without error",
    "no failure",
    "no exception",
    "no problem",
)


def classify_failure(result: str) -> str | None:
    """Classify a result text as a failure type, or None when it looks clean."""
    text = (result or "").lower()
    for phrase in _NEGATED_FAILURE_PHRASES:
        text = text.replace(phrase, " ")
    for error_type, needles in _FAILURE_PATTERNS:
        if any(needle in text for needle in needles):
            return error_type
    return None


def normalize_policy(step: dict) -> dict | None:
    """Return the step's retry policy with defaults filled, or None.

    A missing/malformed policy degrades to None: the step keeps the pre-GAP-8
    no-retry behavior instead of failing the tool call.
    """
    raw = step.get("retry_policy")
    if not isinstance(raw, dict):
        return None
    try:
        max_retries = max(0, int(raw.get("max_retries") or 0))
        delay_raw = raw.get("retry_delay_seconds")
        delay = DEFAULT_RETRY_DELAY_SECONDS if delay_raw is None else max(0.0, float(delay_raw))
    except (TypeError, ValueError):
        return None
    retry_on_raw = raw.get("retry_on")
    retry_on = [str(item) for item in retry_on_raw] if isinstance(retry_on_raw, list) else []
    return {"max_retries": max_retries, "retry_delay_seconds": delay, "retry_on": retry_on}


def step_retry_count(step: dict) -> int:
    """Read a step's retry counter; absent/garbage values degrade to 0."""
    try:
        return max(0, int(step.get("retry_count") or 0))
    except (TypeError, ValueError):
        return 0


def retries_remaining(step: dict) -> int:
    """Retries still available under the step's policy (0 without a policy)."""
    policy = normalize_policy(step)
    if policy is None:
        return 0
    return max(0, policy["max_retries"] - step_retry_count(step))


def should_retry_failure(step: dict, result: str) -> bool:
    """True when a result-text failure consumes a retry under the policy.

    A clean result never retries. With a non-empty ``retry_on`` only matching
    classified types retry; with an empty list every classified failure does.
    """
    policy = normalize_policy(step)
    if policy is None or retries_remaining(step) <= 0:
        return False
    failure_type = classify_failure(result)
    if failure_type is None:
        return False
    if not policy["retry_on"]:
        return True
    return failure_type in policy["retry_on"]


def validate_policy(raw: object) -> str | None:
    """Return an ``Error:`` text for a malformed policy, else None."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        return "Error: retry_policy must be a dict (max_retries, retry_delay_seconds, retry_on)"
    max_retries = raw.get("max_retries")
    if max_retries is not None:
        if isinstance(max_retries, bool) or not isinstance(max_retries, int) or max_retries < 0:
            return "Error: retry_policy.max_retries must be a non-negative integer"
    delay = raw.get("retry_delay_seconds")
    if delay is not None:
        if isinstance(delay, bool) or not isinstance(delay, (int, float)) or delay < 0:
            return "Error: retry_policy.retry_delay_seconds must be a non-negative number"
    retry_on = raw.get("retry_on")
    if retry_on is not None:
        if not isinstance(retry_on, list) or not all(isinstance(item, str) for item in retry_on):
            return "Error: retry_policy.retry_on must be a list of strings"
    return None


def is_redispatch(step: dict) -> bool:
    """True when the step was already dispatched once, so its budget applies."""
    return bool(step.get("child_session_key")) or step_retry_count(step) > 0


def requester_key_for_retry(state: dict, session_id: str) -> str:
    """Requester key for a replacement child: flow creator, else the caller."""
    creator = str(state.get("creator_session_key") or "").strip()
    if creator:
        return creator
    return requester_session_key(session_id) if session_id else ""


async def wait_before_retry(policy: dict) -> None:
    """Sleep the policy's declared backoff (a 0 delay is a no-op)."""
    delay = float(policy.get("retry_delay_seconds") or 0.0)
    if delay > 0:
        await asyncio.sleep(delay)


async def spawn_replacement(task: str, requester_key: str) -> str:
    """Spawn the step's replacement child through the shared dispatch seam."""
    return await _dispatch.dispatch_child(
        task=task,
        requester_session_key=requester_key,
        label=None,
    )


def apply_redispatch(step: dict, new_child_key: str, retry_count: int) -> None:
    """Mark the step re-dispatched in place (the caller persists the state)."""
    step["retry_count"] = retry_count
    step["child_session_key"] = new_child_key
    step["dispatched_at"] = time.time()
    step["status"] = str(StepStatus.DISPATCHED)


def exhausted_note(step: dict, *, retry_count: int, max_retries: int) -> str:
    """Failure-note text recorded when a retry budget runs out."""
    child_key = str(step.get("child_session_key") or "")
    return (
        f"retry budget exhausted: step {step.get('step_id')} child "
        f"{child_key or '<none>'} settled without a result after {retry_count} "
        f"retry/retries (max_retries={max_retries}); marked done by taskflow_wait_all"
    )


def failure_result_record(child_session_key: str, note: str, now: float) -> dict:
    """Result entry for an exhausted step (same idempotency shape as resume)."""
    return {
        "child_session_key": child_session_key,
        "result": note,
        "result_hash": result_hash(child_session_key, note),
        "injected_at": now,
        "retry_exhausted": True,
    }


def plan_settled_retries(
    steps: list[dict],
    results: list[dict],
    settled_pairs: list[tuple[str, str]],
) -> list[dict]:
    """Decide the retry action for each settled dispatched step.

    ``settled_pairs`` is the ``(step_id, child_session_key)`` snapshot the poll
    waited on. Each action is either ``"redispatch"`` (budget remains: spawn a
    replacement) or ``"exhausted"`` (mark done with a failure-note result).

    Steps WITHOUT a retry policy produce no action (legacy wait_all behavior),
    and a child whose result was already injected is left alone: the caller
    owns that result.
    """
    injected = {
        str(record.get("child_session_key") or "") for record in results if isinstance(record, dict)
    }
    by_id = {step.get("step_id"): step for step in steps}
    now = time.time()
    actions: list[dict] = []
    for step_id, child_key in settled_pairs:
        step = by_id.get(step_id)
        if step is None:
            continue
        policy = normalize_policy(step)
        if policy is None or child_key in injected:
            continue
        count = step_retry_count(step)
        if count < policy["max_retries"]:
            actions.append(
                {
                    "action": "redispatch",
                    "step_id": step_id,
                    "child_session_key": child_key,
                    "retry_count": count + 1,
                    "policy": policy,
                    "task": str(step.get("task") or ""),
                }
            )
        else:
            note = exhausted_note(step, retry_count=count, max_retries=policy["max_retries"])
            actions.append(
                {
                    "action": "exhausted",
                    "step_id": step_id,
                    "child_session_key": child_key,
                    "retry_count": count,
                    "max_retries": policy["max_retries"],
                    "record": failure_result_record(child_key, note, now),
                }
            )
    return actions


async def persist_retry_actions(
    flow_id: str,
    flow: dict,
    executed: dict,
) -> tuple[dict | None, str | None]:
    """Persist the executed wait_all retry plan with optimistic-lock retries.

    ``executed`` is ``{"redispatches": [(action, new_child_key), ...],
    "exhausted": [action, ...]}``. Replacement children are already alive, so
    the write goes through the bounded conflict-retry helper that re-applies
    the plan onto a freshly-read step list instead of dropping them.
    """
    redispatches: list[tuple[dict, str]] = list(executed.get("redispatches") or [])
    exhausted: list[dict] = list(executed.get("exhausted") or [])
    if not redispatches and not exhausted:
        return None, None
    new_keys = [new_key for _, new_key in redispatches]

    def build_state(fresh_flow: dict, _attempt: int) -> dict:
        fresh_state = dict(fresh_flow["state"])
        fresh_steps = list(fresh_state.get("steps") or [])
        by_id = {s.get("step_id"): s for s in fresh_steps if s.get("step_id") is not None}
        for action, new_key in redispatches:
            target = by_id.get(action["step_id"])
            if target is not None:
                apply_redispatch(target, new_key, action["retry_count"])
        fresh_results = list(fresh_state.get("results") or [])
        hashes = {r.get("result_hash") for r in fresh_results if isinstance(r, dict)}
        for action in exhausted:
            record = action["record"]
            if record.get("result_hash") not in hashes:
                fresh_results.append(dict(record))
                hashes.add(record.get("result_hash"))
            target = by_id.get(action["step_id"])
            if target is not None:
                target["status"] = str(StepStatus.DONE)
        fresh_state["steps"] = fresh_steps
        fresh_state["results"] = fresh_results
        return fresh_state

    return await update_flow_with_conflict_retry(
        flow_id,
        int(flow["expected_revision"]),
        flow,
        build_state,
        child_keys=new_keys,
    )
