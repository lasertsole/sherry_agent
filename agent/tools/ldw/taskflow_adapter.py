"""Run a TaskFlow fallback flow from LDW workflow metadata.

This is the bridge that makes degradation automatic: a new TaskFlow flow is
created from the SAME verified DAG (not by re-planning), already-succeeded
steps are seeded as done with their journal results, and only failed/remaining
steps are dispatched. Successful TaskFlow results are written back to the LDW
journal so the next replay is zero-call.
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from loguru import logger

from agent.tools.subagent.ldw_shim import JournalRecord, journal_key, to_pydantic_model
from agent.tools.taskflow.config import StepStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools._shared import deps_satisfied, new_step, result_hash, step_status
from agent.tools.taskflow.tools.taskflow_cancel import taskflow_cancel
from agent.tools.taskflow.tools.taskflow_create import taskflow_create
from agent.tools.taskflow.tools.taskflow_dispatch import taskflow_dispatch
from agent.tools.taskflow.tools.taskflow_fail import taskflow_fail
from agent.tools.taskflow.tools.taskflow_finish import taskflow_finish
from agent.tools.taskflow.tools.taskflow_resume import taskflow_resume
from agent.tools.taskflow.tools.taskflow_wait_all import taskflow_wait_all

from .compiler import build_step_prompt, structured_step_ids
from .repair import merge_knowledge, repair_extraction

_TERMINAL_FLOW_STATUSES = ("done", "failed", "cancelled")
_FAILED_STATUSES = (
    StepStatus.FAILED.value,
    StepStatus.BLOCKED.value,
    StepStatus.SKIPPED.value,
    StepStatus.CANCELLED.value,
)


async def _call(tool: Any, **kwargs: Any) -> str:
    fn = getattr(tool, "coroutine", None)
    if fn is None:
        fn = getattr(tool, "func", None)
    if fn is None:
        raise RuntimeError(f"TaskFlow tool {tool!r} has no callable coroutine")
    return await fn(**kwargs)


def _step_optional_fields(item: dict[str, Any]) -> dict[str, Any]:
    """Fields ``new_step`` accepts and a replay flow must preserve."""
    keys = (
        "functional_role",
        "response_schema",
        "step_model",
        "judge_criteria",
        "validation_criteria",
        "step_timeout_seconds",
        "retry_policy",
        "skills",
        "expected_params",
        "input_bindings",
        "priority",
        "file_deps",
    )
    return {key: item[key] for key in keys if item.get(key) is not None}


def _initial_state(
    meta: dict[str, Any],
    succeeded: dict[str, Any],
    variant_input: str,
) -> tuple[dict[str, Any], str]:
    flow_id = f"ldw-{str(meta.get('plan_key') or 'plan')[:8]}-{uuid.uuid4().hex[:8]}"
    results: list[dict[str, Any]] = []
    steps: list[dict[str, Any]] = []
    for item in meta.get("steps") or []:
        step_id = str(item["step_id"])
        status = StepStatus.DONE.value if step_id in succeeded else StepStatus.READY.value
        step = new_step(
            step_id,
            str(item.get("task_template") or ""),
            depends_on=list(item.get("deps") or []),
            status=status,
            **_step_optional_fields(item),
        )
        if item.get("aggregate_deps"):
            step["aggregate_deps"] = True
        if status == StepStatus.DONE.value:
            key = f"ldw-journal:{flow_id}:{step_id}"
            step["child_session_key"] = key
            result = str(succeeded.get(step_id) or "")
            record: dict[str, Any] = {
                "child_session_key": key,
                "result": result,
                "result_hash": result_hash(key, result),
                "step_id": step_id,
                "schema_validated": False,
            }
            if isinstance(item.get("response_schema"), dict) and result:
                try:
                    record["structured_result"] = json.loads(result)
                    record["schema_validated"] = True
                except (TypeError, ValueError):
                    pass
            results.append(record)
        elif not deps_satisfied(step, steps):
            step["status"] = StepStatus.BLOCKED.value
        steps.append(step)
    state = {
        "description": f"LDW replay of {meta.get('plan_key')} variant={variant_input[:200]}",
        "steps": steps,
        "results": results,
        "ldw_plan_key": str(meta.get("plan_key") or ""),
        "ldw_variant_input": variant_input,
        "ldw_origin_flow": str(meta.get("flow_id") or ""),
    }
    return state, flow_id


def _ready_step_ids(flow: dict[str, Any]) -> list[str]:
    steps = list((flow.get("state") or {}).get("steps") or [])
    ready: list[str] = []
    for step in steps:
        status = step_status(step)
        if status == StepStatus.READY.value:
            ready.append(str(step["step_id"]))
        elif status == StepStatus.BLOCKED.value and deps_satisfied(step, steps):
            ready.append(str(step["step_id"]))
    return ready


def _dispatched_steps(flow: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        step
        for step in ((flow.get("state") or {}).get("steps") or [])
        if step_status(step) == StepStatus.DISPATCHED.value
    ]


def _step_results(flow: dict[str, Any]) -> dict[str, str]:
    steps = list((flow.get("state") or {}).get("steps") or [])
    by_key = {str(step.get("child_session_key") or ""): step for step in steps}
    out: dict[str, str] = {}
    for record in (flow.get("state") or {}).get("results") or []:
        if not isinstance(record, dict):
            continue
        step = by_key.get(str(record.get("child_session_key") or ""))
        if step is not None:
            out[str(step.get("step_id"))] = str(record.get("result") or "")
    return out


def _run_by_key(child_key: str):
    try:
        from agent.tools.subagent.registry.queries import get_run_by_child_session_key

        return get_run_by_child_session_key(child_key)
    except Exception:  # noqa: BLE001 - a missing registry is a fail-open miss
        return None


def _is_live(run: Any) -> bool:
    if run is None:
        return False
    try:
        from agent.tools.subagent.registry.helpers import is_live_unended_run

        return bool(is_live_unended_run(run))
    except Exception:  # noqa: BLE001
        return False


def _run_payload(run: Any) -> tuple[str, str, dict[str, Any] | None]:
    """``(result_text, step_outcome, handoff)`` for one settled child run."""
    if run is None:
        return "", "failure", None
    completion = getattr(run, "completion", None)
    result_text = str(getattr(completion, "result_text", "") or "")
    handoff = getattr(completion, "handoff", None)
    if not isinstance(handoff, dict):
        handoff = None
    execution = getattr(run, "execution", None)
    outcome = getattr(execution, "outcome", None)
    status = str(getattr(outcome, "status", "") or "")
    if status == "ok":
        return result_text, "success", handoff
    if status in ("error", "timeout", "killed"):
        return result_text, "failure", handoff
    # A terminal run with no classified outcome is still usable when it produced
    # text; otherwise it is a failure (never silently "success").
    return result_text, ("success" if result_text else "failure"), handoff


def _collect_handoffs(flow: dict[str, Any]) -> list[dict[str, Any]]:
    handoffs: list[dict[str, Any]] = []
    for step in (flow.get("state") or {}).get("steps") or []:
        key = str(step.get("child_session_key") or "")
        if not key or key.startswith("ldw-journal:"):
            continue
        run = _run_by_key(key)
        handoff = getattr(getattr(run, "completion", None), "handoff", None)
        if isinstance(handoff, dict):
            handoffs.append(handoff)
    return handoffs


async def _write_journal_successes(
    journal: Any,
    meta: dict[str, Any],
    results: dict[str, str],
    variant_input: str,
) -> None:
    structured_ids = structured_step_ids(meta)
    for item in meta.get("steps") or []:
        step_id = str(item["step_id"])
        if step_id not in results:
            continue
        deps = {dep: results[dep] for dep in (item.get("deps") or []) if dep in results}
        prompt = build_step_prompt(item, deps, variant_input, structured_ids)
        schema = item.get("response_schema")
        try:
            schema_model = to_pydantic_model(schema) if isinstance(schema, dict) else None
        except Exception as exc:  # noqa: BLE001 - one bad schema must not block the rest
            logger.warning("LDW fallback: cannot journal schema for {}: {}", step_id, exc)
            continue
        key = journal_key(
            prompt=prompt,
            agent_type=str(item["agent_type"]),
            model=None,
            schema=schema_model,
            isolation="shared",
        )
        await journal.put(key, JournalRecord(result=str(results[step_id]), usage=0))


def _repaired_view(
    meta: dict[str, Any],
    flow: dict[str, Any],
    variant_input: str,
) -> dict[str, Any]:
    steps = list((flow.get("state") or {}).get("steps") or [])
    success_steps: list[dict[str, Any]] = []
    failed_branches: list[dict[str, Any]] = []
    for step in steps:
        status = step_status(step)
        step_id = str(step.get("step_id") or "")
        if status == StepStatus.DONE.value:
            success_steps.append(
                {
                    "step": str(step.get("task") or ""),
                    "step_id": step_id,
                    "outcome": "done",
                }
            )
        elif status in _FAILED_STATUSES:
            failed_branches.append(
                {
                    "branch": str(step.get("task") or step_id),
                    "failure_reason": str(
                        step.get("fail_reason") or step.get("block_reason") or status
                    ),
                    "step_id": step_id,
                }
            )
    return {
        "success_steps": success_steps,
        "failed_branches": failed_branches,
        "hard_constraints": [
            str(item)
            for step in (meta.get("steps") or [])
            for item in (step.get("hard_constraints") or [])
            if str(item).strip()
        ],
        "variant_input": variant_input,
    }


def _original_knowledge(meta: dict[str, Any]) -> dict[str, Any]:
    steps: list[dict[str, Any]] = []
    for item in meta.get("steps") or []:
        step: dict[str, Any] = {
            "step_id": str(item["step_id"]),
            "task": str(item.get("task_template") or ""),
            "depends_on": list(item.get("deps") or []),
            "functional_role": str(item.get("functional_role") or "general"),
            **_step_optional_fields(item),
        }
        # Not accepted by ``new_step`` but part of the compiled LDW contract.
        if item.get("extra_tools"):
            step["extra_tools"] = list(item["extra_tools"])
        if item.get("hard_constraints"):
            step["hard_constraints"] = list(item["hard_constraints"])
        if item.get("aggregate_deps"):
            step["aggregate_deps"] = True
        steps.append(step)
    return {
        "plan_key": str(meta.get("plan_key") or ""),
        "plan_name": str(meta.get("plan_name") or ""),
        "flow_id": str(meta.get("flow_id") or ""),
        "steps": steps,
    }


def _handoff_repair_fields(handoffs: list[dict[str, Any]]) -> dict[str, Any]:
    """Project child handoffs into the knowledge fields repair understands."""
    failed_branches: list[dict[str, Any]] = []
    hard_constraints: list[str] = []
    locked_decisions: list[dict[str, Any]] = []
    success_steps: list[dict[str, Any]] = []
    for handoff in handoffs:
        success_steps.extend(
            item for item in (handoff.get("success_steps") or []) if isinstance(item, dict)
        )
        failed_branches.extend(
            item
            for item in (handoff.get("failed_branches") or handoff.get("failed_paths") or [])
            if isinstance(item, dict)
        )
        hard_constraints.extend(
            str(item) for item in (handoff.get("discovered_hard_constraints") or []) if str(item)
        )
        locked_decisions.extend(
            item for item in (handoff.get("locked_decisions") or []) if isinstance(item, dict)
        )
    return {
        "success_steps": success_steps,
        "failed_branches": failed_branches,
        "hard_constraints": hard_constraints,
        "locked_decisions": locked_decisions,
    }


async def _terminalize(flow_id: str, session_id: str, reason: str, *, cancel: bool = False) -> None:
    flow = store_sqlite.get_flow_sync(flow_id, session_id)
    if flow is None or str(flow.get("status") or "") in _TERMINAL_FLOW_STATUSES:
        return
    tool = taskflow_cancel if cancel else taskflow_fail
    try:
        await _call(tool, flow_id=flow_id, reason=reason, session_id=session_id)
    except Exception:  # noqa: BLE001 - a failed fallback must still return its results
        logger.exception("LDW fallback: could not terminalize {}", flow_id)


async def run_taskflow_fallback(
    session_id: str,
    plan_key: str,
    meta: dict[str, Any],
    succeeded: dict[str, Any],
    variant_input: str,
    *,
    journal: Any,
    repair: bool = True,
    timeout_seconds: float = 900.0,
) -> dict[str, Any]:
    """Create and drive a TaskFlow replay flow, then optionally repair."""
    state, flow_id = _initial_state(meta, succeeded, variant_input)
    original_flow = str(meta.get("flow_id") or "")
    resumed_from = None
    if original_flow:
        try:
            if store_sqlite.get_flow_sync(original_flow, session_id) is not None:
                resumed_from = original_flow
        except Exception:  # noqa: BLE001 - lineage is optional
            resumed_from = None
    created = await _call(
        taskflow_create,
        flow_id=flow_id,
        description=state["description"],
        initial_state=state,
        session_id=session_id,
        resumed_from=resumed_from,
        todo_content=f"LDW replay {plan_key}",
        ack_dup=True,
    )
    if created.startswith("Error:"):
        return {"status": "error", "error": created, "flow_id": flow_id}

    deadline = time.monotonic() + max(30.0, float(timeout_seconds))
    resume_errors: list[str] = []
    dispatch_errors: list[str] = []
    wait_errors: list[str] = []
    while time.monotonic() < deadline:
        flow = store_sqlite.get_flow_sync(flow_id, session_id)
        if flow is None:
            break
        if str(flow.get("status") or "") in _TERMINAL_FLOW_STATUSES:
            break
        dispatched = _dispatched_steps(flow)
        if dispatched:
            waited = await _call(
                taskflow_wait_all,
                flow_id=flow_id,
                timeout_seconds=min(60.0, max(5.0, deadline - time.monotonic())),
                session_id=session_id,
            )
            if waited.startswith("Error:"):
                wait_errors.append(waited)
                break
            # Wait settles children; resume is what records each result and
            # unlocks the downstream wave. Poll again before resuming because
            # wait_all's own retry path may have replaced a child.
            current = store_sqlite.get_flow_sync(flow_id, session_id) or {}
            current_steps = list((current.get("state") or {}).get("steps") or [])
            for step in current_steps:
                if step_status(step) != StepStatus.DISPATCHED.value:
                    continue
                child_key = str(step.get("child_session_key") or "")
                run = _run_by_key(child_key)
                if _is_live(run):
                    continue
                result_text, step_outcome, _handoff = _run_payload(run)
                resumed = await _call(
                    taskflow_resume,
                    flow_id=flow_id,
                    child_session_key=child_key,
                    result=result_text,
                    step_outcome=step_outcome,
                    auto_dispatch=True,
                    session_id=session_id,
                )
                if resumed.startswith("Error:"):
                    resume_errors.append(resumed)
            if resume_errors:
                break
            continue
        ready = _ready_step_ids(flow)
        if ready:
            dispatched_text = await _call(
                taskflow_dispatch,
                flow_id=flow_id,
                step_ids=ready,
                session_id=session_id,
            )
            if dispatched_text.startswith("Error:"):
                dispatch_errors.append(dispatched_text)
                current = store_sqlite.get_flow_sync(flow_id, session_id) or {}
                if _dispatched_steps(current):
                    continue
                break
            continue
        # No dispatched/ready work: either all settled or blocked/failed.
        break

    flow = store_sqlite.get_flow_sync(flow_id, session_id) or {}
    steps = list((flow.get("state") or {}).get("steps") or [])
    results = _step_results(flow)
    failed = [str(step.get("step_id")) for step in steps if step_status(step) in _FAILED_STATUSES]
    unfinished = [
        str(step.get("step_id"))
        for step in steps
        if step_status(step)
        not in (
            StepStatus.DONE.value,
            StepStatus.FAILED.value,
            StepStatus.BLOCKED.value,
            StepStatus.SKIPPED.value,
            StepStatus.CANCELLED.value,
        )
    ]
    all_done = bool(steps) and all(step_status(step) == StepStatus.DONE.value for step in steps)

    await _write_journal_successes(journal, meta, results, variant_input)

    finish_error = ""
    if all_done:
        finished = await _call(
            taskflow_finish,
            flow_id=flow_id,
            summary=f"LDW replay {plan_key}",
            session_id=session_id,
        )
        if finished.startswith("Error:"):
            finish_error = finished
            await _terminalize(flow_id, session_id, f"LDW replay finish rejected: {finished[:300]}")
            latest = store_sqlite.get_flow_sync(flow_id, session_id)
            if latest is not None:
                flow = latest
                steps = list((flow.get("state") or {}).get("steps") or [])
    elif flow:
        # Cancel (not fail) when work is still unfinished: it also marks the
        # non-done steps cancelled, so the repair view sees why they stopped.
        await _terminalize(
            flow_id,
            session_id,
            "LDW replay did not complete"
            + (f": {resume_errors[0][:200]}" if resume_errors else ""),
            cancel=bool(unfinished),
        )
        latest = store_sqlite.get_flow_sync(flow_id, session_id)
        if latest is not None:
            flow = latest
            steps = list((flow.get("state") or {}).get("steps") or [])
            results = _step_results(flow)
            failed = [
                str(step.get("step_id")) for step in steps if step_status(step) in _FAILED_STATUSES
            ]
            unfinished = [
                str(step.get("step_id"))
                for step in steps
                if step_status(step)
                not in (
                    StepStatus.DONE.value,
                    StepStatus.FAILED.value,
                    StepStatus.BLOCKED.value,
                    StepStatus.SKIPPED.value,
                    StepStatus.CANCELLED.value,
                )
            ]

    repaired: dict[str, Any] | None = None
    repair_error = ""
    repaired_failures = [*failed, *unfinished]
    if repair and repaired_failures:
        original = _original_knowledge(meta)
        handoffs = _collect_handoffs(flow)
        repaired_input = merge_knowledge(
            original,
            {
                **_repaired_view(meta, flow, variant_input),
                **_handoff_repair_fields(handoffs),
                "failed_steps": repaired_failures,
                "variant_input": variant_input,
            },
        )
        try:
            repaired = await repair_extraction(
                plan_key,
                original,
                repaired_input,
                variant_input=variant_input,
                plan_name=str(meta.get("plan_name") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - repair is fail-open
            repair_error = f"{type(exc).__name__}: {exc}"
            logger.exception("LDW fallback: repair extraction failed for {}", plan_key)

    status = "ok" if all_done and not finish_error else "partial"
    outcome: dict[str, Any] = {
        "status": status,
        "flow_id": flow_id,
        "results": results,
        "failed_steps": failed,
        "unfinished_steps": unfinished,
        "repaired": repaired is not None,
    }
    if finish_error:
        outcome["finish_error"] = finish_error
    if resume_errors:
        outcome["resume_errors"] = resume_errors
    if dispatch_errors:
        outcome["dispatch_errors"] = dispatch_errors
    if wait_errors:
        outcome["wait_errors"] = wait_errors
    if repair_error:
        outcome["repair_error"] = repair_error
    elif isinstance(repaired, dict):
        if repaired.get("persist_error"):
            outcome["repair_error"] = str(repaired["persist_error"])
        elif repaired.get("recompile_error"):
            outcome["repair_error"] = str(repaired["recompile_error"])
    return outcome
