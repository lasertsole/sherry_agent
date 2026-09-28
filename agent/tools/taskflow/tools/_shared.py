"""Shared plumbing for the taskflow tool family (internal module).

Error-text contract: tools NEVER raise business errors to the LLM; they
return human-readable strings prefixed with ``Error:``. Conflict texts embed
the latest revision so the caller can re-read (taskflow_summary) and retry
with the freshest expected_revision, per skills/builtin/core/taskflow/SKILL.md.
"""

# allow: SIZE_OK — the family's single shared-plumbing module: DAG status/deps
# primitives, optimistic-lock conflict retry, dispatched-step merge, and the
# full-replace step-list validation contract are consumed by multiple tool
# modules; splitting by helper would fragment one shared contract for no
# cohesion gain.

import hashlib
import json
from collections.abc import Callable
from typing import Any

from config.features import TASKFLOW_INFRA
from ..config import TERMINAL_STATUSES, StepStatus
from ..registry import store_sqlite
from ..registry.store_sqlite import FlowConflictError, FlowNotFoundError, UNSET

# Bounded optimistic-lock retries for a mutation whose side effect (a spawned
# child session) already happened: losing the write must never drop the child.
PERSIST_MAX_ATTEMPTS = TASKFLOW_INFRA["persist_max_attempts"]


def requester_session_key(session_id: str) -> str:
    """Build the canonical requester session key from a raw LangGraph session id."""
    return f"agent:main:session:{session_id}"


def result_hash(child_session_key: str, result: str) -> str:
    """Fingerprint a (child_session_key, result) pair for resume idempotency."""
    payload = f"{child_session_key}\x1f{result}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def default_state(
    description: str,
    initial_state: dict | None,
    *,
    creator_session_key: str = "",
) -> dict:
    """Build the initial state_json payload with guaranteed invariants.

    ``steps`` and ``results`` are always lists (run_task / resume append to
    them); ``description`` and any caller keys are preserved.
    ``creator_session_key`` associates the flow with the parent session
    for cross-session auto-resume injection.
    """
    state = dict(initial_state or {})
    state["description"] = description or str(state.get("description") or "")
    state["steps"] = list(state.get("steps") or [])
    state["results"] = list(state.get("results") or [])
    if creator_session_key:
        state["creator_session_key"] = creator_session_key
    return state


def not_found_error(flow_id: str) -> str:
    return f"Error: TaskFlow '{flow_id}' not found"


def terminal_error(flow_id: str, status: str) -> str:
    return (
        f"Error: TaskFlow '{flow_id}' is terminal (status={status}); no further mutations allowed"
    )


def conflict_error(exc: FlowConflictError) -> str:
    return f"Error: {exc}"


def is_terminal(status: str) -> bool:
    return status in TERMINAL_STATUSES


# Expectation / metadata fields a step may carry on top of the DAG core. Every
# one is optional and only written when the caller provided it, so a flow with
# no expectations stays byte-identical to a pre-closure one (backward compat).
_OPTIONAL_STEP_FIELDS: tuple[str, ...] = (
    # Expectation side (the "expected" half of the closure):
    "expected_params",  # A1: structured inputs this step is supposed to work on
    "response_schema",  # A2: JSON Schema the structured result must satisfy
    "judge_criteria",  # B3: semantic acceptance criteria for the LLM judge
    "judge_model",  # B3: model override for that judge
    "validation_criteria",  # legacy text criteria (still judged when present)
    # Execution metadata:
    "functional_role",  # D1: sub-agent specialization
    "step_model",  # D1: child model override
    "step_timeout_seconds",  # D3: per-step child run timeout
    "priority",  # D2: dispatch ordering hint
    "input_bindings",  # C1/C2: upstream structured fields bound as inputs
    "retry_policy",  # existing: failure retry policy
)


def new_step(
    step_id: str,
    task: str,
    depends_on: list[str] | None = None,
    status: StepStatus | str = StepStatus.READY,
    **optional: Any,
) -> dict:
    """Build a step dict for the flow's ``steps`` list.

    All DAG fields live inside ``state_json`` (no DB migration): a stable
    ``step_id``, the task text, the dependency ids, the current status, and the
    retry counter (0 = no re-dispatch yet; see ``_retry``).

    ``optional`` accepts any of :data:`_OPTIONAL_STEP_FIELDS` (expectations,
    judge criteria, sub-agent metadata, input bindings). A field is written only
    when its value is not ``None`` — an empty/absent expectation must stay
    absent so the resume path can tell "no gate configured" from "gate failed".
    Unknown keys raise, so a typo can never silently drop an expectation.
    """
    step: dict[str, Any] = {
        "step_id": step_id,
        "task": task,
        "depends_on": list(depends_on or []),
        "status": str(status),
        "retry_count": 0,
    }
    for key, value in optional.items():
        if key not in _OPTIONAL_STEP_FIELDS:
            raise ValueError(f"new_step: unknown step field {key!r}")
        if value is not None:
            step[key] = value
    return step


def step_status(step: dict) -> str:
    """Return a step's status, deriving it for pre-DAG legacy steps.

    A legacy step has no ``status`` field; it was dispatched iff it already
    carries a ``child_session_key``, otherwise it is still ready.
    """
    status = step.get("status")
    if status:
        return str(status)
    if step.get("child_session_key"):
        return str(StepStatus.DISPATCHED)
    return str(StepStatus.READY)


def deps_satisfied(step: dict, steps: list[dict]) -> bool:
    """True iff every ``depends_on`` id exists in ``steps`` and is ``done``.

    Missing/empty ``depends_on`` is trivially satisfied. An unknown dep id is
    never satisfied. A self-dependency is never satisfied, which also makes a
    self-referential step safe to leave blocked without an unlock loop.
    """
    depends_on = step.get("depends_on")
    if not depends_on:
        return True
    step_id = step.get("step_id")
    by_id = {s.get("step_id"): s for s in steps}
    for dep_id in depends_on:
        if dep_id == step_id:
            return False
        dep = by_id.get(dep_id)
        if dep is None:
            return False
        if step_status(dep) != StepStatus.DONE:
            return False
    return True


def build_task_with_dep_results(step: dict, steps: list[dict], results: list[dict]) -> str:
    """Append dependency-step results to this step's task when ``aggregate_deps``.

    A dependency's result is looked up by the dependency step's
    ``child_session_key`` against the flow's recorded result entries (shape:
    ``{child_session_key, result, result_hash}``). A dependency whose step is
    unknown or has no recorded result contributes a ``no result recorded``
    placeholder rather than raising. Returns the step's original task text
    unchanged when ``aggregate_deps`` is falsy or the step has no dependencies,
    so the default opt-out path stays byte-identical to the legacy dispatch.
    """
    task = str(step.get("task") or "")
    if not step.get("aggregate_deps"):
        return task
    depends_on = step.get("depends_on")
    if not depends_on:
        return task
    by_id = {s.get("step_id"): s for s in steps if isinstance(s, dict)}
    results_by_key = {
        str(record.get("child_session_key") or ""): record
        for record in results
        if isinstance(record, dict)
    }
    sections: list[str] = []
    for dep_id in depends_on:
        dep = by_id.get(dep_id)
        child_key = str((dep or {}).get("child_session_key") or "")
        record = results_by_key.get(child_key) if child_key else None
        result_text = (
            str(record.get("result") or "") if record is not None else "no result recorded"
        )
        sections.append(f"### {dep_id}\n{result_text}")
    return f"{task}\n\n## Upstream Results\n" + "\n\n".join(sections)


def mark_step_done(steps: list[dict], child_session_key: str) -> str | None:
    """Mark the step matching ``child_session_key`` done; return its id.

    Returns ``None`` when no step carries that child key. Idempotent: a
    repeated call for the same key returns the same id and leaves the step done.
    """
    if not child_session_key:
        return None
    for step in steps:
        if step.get("child_session_key") == child_session_key:
            step["status"] = str(StepStatus.DONE)
            return step.get("step_id")
    return None


def unlock_dependents(steps: list[dict]) -> list[str]:
    """Move blocked steps with satisfied deps to ready; return their ids.

    A blocked step with no ``depends_on`` is not waiting on the DAG (for
    example it was blocked by the step judge), so it is never auto-unlocked.
    A single pass, so a self-dependency or a dependency cycle can never loop.

    Dependency results are failure-aware:

    * a dependency that ended ``failed`` (or is still ``blocked``/in-flight)
      never unlocks its dependents — they stay ``blocked`` for a human decision;
    * a dependency that was ``skipped`` or ``cancelled`` cascades ``skipped``
      into its dependents, because waiting on a branch that will never produce a
      result would otherwise park the flow forever.

    A skipped dependent is not followed transitively here: the next call (after
    the caller persists and re-runs the pass) cascades one level further, so a
    long dead branch settles in a bounded number of passes rather than one
    unbounded loop.
    """
    newly_ready: list[str] = []
    for step in steps:
        if step_status(step) != StepStatus.BLOCKED:
            continue
        if not step.get("depends_on"):
            continue
        dep_statuses = _dep_statuses(step, steps)
        if dep_statuses is None:
            continue
        if any(status in _CASCADING_DEP_STATUSES for status in dep_statuses):
            step["status"] = str(StepStatus.SKIPPED)
            step["skip_reason"] = "dependency skipped or cancelled"
            continue
        if all(status == str(StepStatus.DONE) for status in dep_statuses):
            step["status"] = str(StepStatus.READY)
            step_id = step.get("step_id")
            if step_id is not None:
                newly_ready.append(step_id)
    return newly_ready


def _dep_statuses(step: dict, steps: list[dict]) -> list[str] | None:
    """Statuses of ``step``'s dependencies, or ``None`` when one is unusable.

    ``None`` means "cannot judge": an unknown dep id or a self-dependency, which
    must leave the step blocked rather than count as satisfied or skipped.
    """
    step_id = step.get("step_id")
    by_id = {s.get("step_id"): s for s in steps}
    statuses: list[str] = []
    for dep_id in step.get("depends_on") or []:
        if dep_id == step_id:
            return None
        dep = by_id.get(dep_id)
        if dep is None:
            return None
        statuses.append(step_status(dep))
    return statuses


#: A dependency in one of these states kills the branch: its dependents are
#: skipped instead of being unlocked or left blocked forever.
_CASCADING_DEP_STATUSES = frozenset(
    {
        StepStatus.SKIPPED.value,
        StepStatus.CANCELLED.value,
    }
)


def extract_nested(data: Any, path: str) -> Any:
    """Read a dotted path out of a nested dict/list value.

    ``"files.0.name"`` walks ``data["files"][0]["name"]``. A missing key, an
    out-of-range or non-numeric list index, or a path that runs into a scalar
    yields ``None`` (never raises): a binding that cannot be resolved must
    degrade to "no value", not break the dispatch.

    :param data: Parsed structured result (usually a step's
        ``structured_result``).
    :param path: Dotted path into ``data`` ('' returns ``data`` itself).
    :returns: The value at ``path``, or ``None``.
    """
    if not path:
        return data
    current = data
    for part in str(path).split("."):
        if isinstance(current, dict):
            current = current.get(part)
        elif isinstance(current, (list, tuple)) and part.isdigit():
            index = int(part)
            current = current[index] if 0 <= index < len(current) else None
        else:
            return None
    return current


def build_task_with_bindings(
    task_text: str, step: dict, steps: list[dict], results: list[dict]
) -> str:
    """Append the step's ``input_bindings`` values to its dispatched task text.

    ``input_bindings`` maps a parameter name to a dotted path into an upstream
    step's structured result: ``{"target_files": "step-A.structured_result.files"}``
    reads ``files`` out of step-A's recorded ``structured_result``. The resolved
    values are appended as an ``## Input parameters`` JSON block, so the child
    receives the upstream data as data (not as prose it has to re-parse).

    Degradation is deliberate and silent: an unresolvable binding (no such step,
    no structured result, missing path) contributes a ``null`` value instead of
    failing the dispatch — a step must still run when an upstream field it hoped
    for is absent, and the null is visible in the child's task text.

    :param task_text: The task text built so far (dep-result aggregation applied).
    :param step: The step carrying ``input_bindings``.
    :param steps: The flow's steps (binding sources are looked up here).
    :param results: The flow's recorded result entries.
    :returns: ``task_text`` unchanged when no bindings, else text + param block.
    """
    bindings = step.get("input_bindings")
    if not isinstance(bindings, dict) or not bindings:
        return task_text
    by_id = {s.get("step_id"): s for s in steps}
    by_child = {str(r.get("child_session_key") or ""): r for r in results if isinstance(r, dict)}
    resolved: dict[str, Any] = {}
    for param_name, binding in bindings.items():
        value: Any = None
        if isinstance(binding, str) and binding:
            parts = binding.split(".", 2)
            if len(parts) == 3 and parts[1] == "structured_result":
                source_step = by_id.get(parts[0])
                if source_step is not None:
                    record = by_child.get(str(source_step.get("child_session_key") or ""))
                    if record is not None:
                        value = extract_nested(record.get("structured_result"), parts[2])
        resolved[str(param_name)] = value
    block = json.dumps(resolved, ensure_ascii=False, default=str, indent=2)
    return f"{task_text}\n\n## Input parameters\n{block}"


def record_unpersisted_children_error(flow_id: str, child_keys: list[str], attempts: int) -> str:
    """Error text naming spawned children that could not be persisted.

    A spawned child is already running: the caller must recover it by key and
    must NOT re-dispatch it. This text is the only trace of an unrecorded child,
    so it always names every key.
    """
    keys = ", ".join(key for key in child_keys if key)
    return (
        f"Error: TaskFlow '{flow_id}': spawned child session(s) could not be recorded "
        f"after {attempts} optimistic-lock attempt(s): child_session_key(s)=[{keys}]. "
        "The child session(s) are already running - do NOT dispatch them again; "
        "recover using the key(s) above."
    )


def apply_dispatched_steps(fresh_steps: list[dict], records: list[dict]) -> list[dict]:
    """Re-apply recorded dispatched-step payloads onto a freshly-read step list.

    Matching is by ``step_id``. A record whose step is absent from the fresh
    state is appended, so a spawned child is never lost to a concurrent writer
    that rewrote the step list.
    """
    merged = list(fresh_steps)
    by_id = {s.get("step_id"): s for s in merged if s.get("step_id") is not None}
    for record in records:
        step_id = record.get("step_id")
        target = by_id.get(step_id) if step_id is not None else None
        if target is None:
            merged.append(dict(record))
            continue
        target["status"] = record["status"]
        target["child_session_key"] = record["child_session_key"]
        target["dispatched_at"] = record["dispatched_at"]
        target.pop("judge_feedback", None)
    return merged


async def update_flow_with_conflict_retry(
    flow_id: str,
    expected_revision: int,
    origin_flow: dict,
    build_state: Callable[[dict, int], dict],
    *,
    child_keys: list[str],
    session_id: str,
    flow_child_session_key: object = UNSET,
    max_attempts: int = PERSIST_MAX_ATTEMPTS,
    update_kwargs: dict[str, Any] | None = None,
) -> tuple[dict | None, str | None]:
    """Persist a mutation, re-reading and rebuilding state on optimistic-lock conflict.

    A child session that has ALREADY spawned must never be silently dropped: the
    caller invokes this only after a successful spawn, and when the write loses
    the race ``build_state(fresh_flow, attempt)`` is invoked again against the
    freshly-read flow and retried up to ``max_attempts``. A vanished or terminal
    flow is terminal for the retry (the child keys are still named).
    ``session_id`` scopes both the write and the re-read, so a flow owned by
    another session counts as vanished.

    ``update_kwargs`` forwards extra ``store_sqlite.update_flow`` keyword
    arguments (wait/status/token aggregation) unchanged on every attempt.

    Returns ``(updated_flow, error_text)`` with exactly one non-None: on success
    the updated flow; on exhaustion an error naming every ``child_keys`` entry.
    """
    revision = int(expected_revision)
    extra_kwargs = dict(update_kwargs or {})
    current_flow = origin_flow
    for attempt in range(1, max_attempts + 1):
        state = build_state(current_flow, attempt)
        try:
            updated = await store_sqlite.update_flow(
                flow_id,
                revision,
                session_id=session_id,
                state=state,
                child_session_key=flow_child_session_key,
                **extra_kwargs,
            )
            return updated, None
        except FlowConflictError:
            if attempt == max_attempts:
                break
            fresh = await store_sqlite.get_flow(flow_id, session_id)
            if fresh is None or is_terminal(fresh["status"]):
                return None, record_unpersisted_children_error(flow_id, child_keys, attempt)
            current_flow = fresh
            revision = int(fresh["expected_revision"])
        except FlowNotFoundError:
            return None, record_unpersisted_children_error(flow_id, child_keys, attempt)
    return None, record_unpersisted_children_error(flow_id, child_keys, max_attempts)


def steps_summary(steps: list[dict]) -> dict[str, int]:
    """Count steps per status, always including all four statuses (zero-filled)."""
    counts = {status.value: 0 for status in StepStatus}
    for step in steps:
        status = step_status(step)
        counts[status] = counts.get(status, 0) + 1
    return counts


def validate_steps_list(steps: list) -> str | None:
    """Validate the structural contract of a full steps-list replacement.

    Returns an ``Error:``-prefixed string when the list is malformed, else
    ``None``. The contract (shared by ``taskflow_update_steps`` and its tests):
    ``step_id`` is non-empty and unique across the list; ``task`` is non-empty;
    ``status`` is one of the four ``StepStatus`` values; ``depends_on`` is a
    list of non-empty ids that all exist **in this same list** and never name
    the step itself. Cross-step safety rules (dispatched/done immutability,
    child-key retention) are enforced by ``taskflow_update_steps``, which owns
    the previous step list and therefore cannot live here.
    """
    if not isinstance(steps, list):
        return "Error: steps must be a list of step objects"
    seen: set[str] = set()
    valid_statuses = {member.value for member in StepStatus}
    for index, step in enumerate(steps):
        if not isinstance(step, dict):
            return f"Error: steps[{index}] must be an object"
        step_id = step.get("step_id")
        if not isinstance(step_id, str) or not step_id.strip():
            return f"Error: steps[{index}].step_id is required"
        step_id = step_id.strip()
        if step_id in seen:
            return f"Error: duplicate step_id '{step_id}'"
        seen.add(step_id)
        task = step.get("task")
        if not isinstance(task, str) or not task.strip():
            return f"Error: step '{step_id}' task is required"
        status = step.get("status")
        if status not in valid_statuses:
            valid_text = ", ".join(member.value for member in StepStatus)
            return (
                f"Error: step '{step_id}' has invalid status {status!r} "
                f"(expected one of: {valid_text})"
            )
        depends_on = step.get("depends_on")
        if not isinstance(depends_on, list):
            return f"Error: step '{step_id}' depends_on must be a list"
        for dep in depends_on:
            if not isinstance(dep, str) or not dep.strip():
                return f"Error: step '{step_id}' has an invalid depends_on entry"
            if dep.strip() == step_id:
                return f"Error: step '{step_id}' cannot depend on itself"
    for step in steps:
        step_id = str(step["step_id"]).strip()
        for dep in step.get("depends_on") or []:
            dep_id = str(dep).strip()
            if dep_id not in seen:
                return f"Error: step '{step_id}' depends_on references unknown step_id '{dep_id}'"
    return None
