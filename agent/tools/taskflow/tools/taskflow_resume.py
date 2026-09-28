"""taskflow_resume: inject a child session result into the flow state (openclaw resume).

Idempotency: the (child_session_key, result) pair is fingerprinted; resuming
with the SAME pair again is a no-op that neither re-injects nor bumps the
revision, so announce-pipeline redeliveries cannot corrupt the state.

Failure-aware retry: when the step carries a retry_policy and the
result text classifies as a failure allowed by ``retry_on``, the step is
re-dispatched instead of marked done - the failure result is still recorded.
"""

import json
import time
from typing import Annotated, Any

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import InjectedState

from config.features import STEP_JUDGE
from agent.tools.subagent.swarm.collector import validate_structured_output
from ..config import StepOutcome, StepStatus, TaskFlowStatus
from ..evidence_collector import collect_evidence_summary
from ..registry import store_sqlite
from ..registry.store_sqlite import FlowConflictError, FlowNotFoundError
from ..step_judge import StepVerdict, judge_step_result
from ._retry import (
    apply_redispatch,
    normalize_policy,
    requester_key_for_retry,
    should_retry_failure,
    spawn_replacement,
    step_retry_count,
    wait_before_retry,
    with_judge_feedback,
)
from ._shared import (
    build_task_with_bindings,
    build_task_with_dep_results,
    conflict_error,
    is_terminal,
    not_found_error,
    result_hash,
    steps_summary,
    terminal_error,
    unlock_dependents,
    update_flow_with_conflict_retry,
)

SessionId = Annotated[str, InjectedState("session_id")]

_STATUS_ORDER = tuple(status.value for status in StepStatus)


@tool("taskflow_resume")
async def taskflow_resume(
    flow_id: str,
    child_session_key: str = "",
    result: str = "",
    expected_revision: int | None = None,
    token_usage: dict | None = None,
    validation_criteria: str | None = None,
    structured_result: dict | None = None,
    step_outcome: str | None = None,
    session_id: SessionId = "",
) -> str:
    """Inject a completed child session result into the flow state (idempotent).

    Appends {child_session_key, result} to the flow's results and returns the
    flow to running when it was waiting. Resuming with the SAME
    child_session_key and result again is a no-op (already resumed), so
    duplicate deliveries never inject twice. Pass expected_revision to fail
    fast on concurrent writers. Pass
    token_usage={"input_tokens": N, "output_tokens": M, "model_name": "..."} to
    accumulate the child's token spend and estimated cost onto the flow.

    When the step carries validation_criteria (set by taskflow_run_task or
    passed here), the step result is judged by an auxiliary-LLM step judge:
    PASS marks the step done, RETRY re-dispatches it (reusing the step's own
    retry budget) and records the judge's guidance on ``judge_feedback``, and
    BLOCK marks the step blocked. The judge is fail-open: when it is disabled
    or unavailable the step is marked done as before.

    When the result text classifies as a failure and the step's retry_policy
    allows it (``retry_on`` filter, budget left), a replacement child is
    spawned and the step stays dispatched on the new child; otherwise the step
    is marked done. ``session_id`` is injected by the runtime and used as the
    fallback requester for a replacement child.

    Two-tier quality gate (both tiers are opt-in per step, and they chain):

    * **Tier 1 — structure (free).** When the step declares ``response_schema``,
      the result is parsed (``structured_result`` when the caller passes it,
      else the result text) and validated against that schema. A result that
      fails structure is recorded (``schema_validated=false``) and treated as a
      failure: it retries under the step's policy (``schema_error`` is the retry
      type) and never spends a judge call. A pass stores
      ``structured_result`` + ``schema_validated=true`` and continues to Tier 2
      — structure alone never proves the content is right.
    * **Tier 2 — semantics (LLM).** When the step declares ``judge_criteria``
      (or the legacy ``validation_criteria``), the step judge evaluates the
      result — the validated ``structured_result`` when there is one, else the
      result text.

    ``step_outcome`` lets the caller state how the run ended
    (``success``/``failure``/``partial``/``skipped``), which outranks the
    gates: ``failure`` marks the step ``failed`` and skips the judge,
    ``skipped`` marks it ``skipped`` (its dependents cascade ``skipped``), and
    ``success``/``partial`` are recorded on the result while the gates still
    run. A ``failed`` step never unlocks its dependents.
    """
    flow_id = (flow_id or "").strip()
    child_session_key = (child_session_key or "").strip()
    result = result or ""
    declared_outcome = (step_outcome or "").strip().lower() or None
    if declared_outcome is not None and declared_outcome not in {
        member.value for member in StepOutcome
    }:
        allowed = ", ".join(member.value for member in StepOutcome)
        return f"Error: step_outcome must be one of: {allowed}"
    if not flow_id:
        return "Error: flow_id is required"
    if not child_session_key and not result:
        return "Error: taskflow_resume requires child_session_key or result"

    flow = await store_sqlite.get_flow(flow_id, session_id)
    if flow is None:
        return not_found_error(flow_id)
    if is_terminal(flow["status"]):
        return terminal_error(flow_id, flow["status"])

    revision = (
        int(expected_revision) if expected_revision is not None else flow["expected_revision"]
    )

    result_hash_value = result_hash(child_session_key, result)
    state = dict(flow["state"])
    results = list(state.get("results") or [])
    if any(isinstance(r, dict) and r.get("result_hash") == result_hash_value for r in results):
        return (
            f"TaskFlow resume skipped (already resumed): flow_id={flow_id}, "
            f"result_hash={result_hash_value}, results={len(results)}"
        )

    result_record = {
        "child_session_key": child_session_key,
        "result": result,
        "result_hash": result_hash_value,
        "injected_at": time.time(),
    }
    results.append(result_record)
    state["results"] = results

    # DAG bookkeeping: a retried failure keeps the step dispatched on its
    # replacement child; any other result marks the step done and may unlock
    # blocked dependents. Resume never spawns the newly-ready steps.
    steps = list(state.get("steps") or [])
    step = next((s for s in steps if s.get("child_session_key") == child_session_key), None)
    step_id = step.get("step_id") if step is not None else None

    # ── Tier 1: structural gate (expectation -> actual pairing) ──────────────
    # Nothing here calls a model: a schema-less step skips it entirely, keeping
    # pre-closure flows byte-identical.
    schema = step.get("response_schema") if step is not None else None
    parsed_result: Any = structured_result
    schema_validated: bool | None = None
    schema_error: str | None = None
    if schema is not None:
        if parsed_result is None:
            try:
                parsed_result = json.loads(result)
            except (json.JSONDecodeError, TypeError) as exc:
                parsed_result = None
                schema_error = f"result is not JSON: {exc}"
        if parsed_result is not None:
            schema_ok, schema_error = validate_structured_output(
                json.dumps(parsed_result, ensure_ascii=False, default=str), schema
            )
            schema_validated = bool(schema_ok)
        else:
            schema_validated = False
            schema_error = schema_error or "no structured result to validate"
        if parsed_result is not None:
            result_record["structured_result"] = parsed_result
    result_record["step_id"] = step_id
    result_record["schema_validated"] = schema_validated
    if declared_outcome is not None:
        result_record["step_outcome"] = declared_outcome

    retry_text = ""
    redispatched_key: str | None = None
    retry_count_after: int | None = None
    if step is not None:
        policy = normalize_policy(step)
        if policy is not None and should_retry_failure(
            step, result, schema_validated=schema_validated
        ):
            requester_key = requester_key_for_retry(state, session_id)
            await wait_before_retry(policy)
            try:
                redispatched_key = await spawn_replacement(
                    with_judge_feedback(
                        build_task_with_bindings(
                            build_task_with_dep_results(step, steps, results), step, steps, results
                        ),
                        str(step.get("judge_feedback") or ""),
                    ),
                    requester_key,
                )
            except Exception as exc:
                retry_text = (
                    f"\n  retry: re-dispatch failed for step_id={step_id} "
                    f"({type(exc).__name__}: {exc}); step left done with the failure result"
                )
            else:
                retry_count_after = step_retry_count(step) + 1
                apply_redispatch(step, redispatched_key, retry_count_after)
                retry_text = (
                    f"\n  retry: re-dispatched step_id={step_id}, "
                    f"child_session_key={redispatched_key}, "
                    f"retry_count={retry_count_after}/{policy['max_retries']}, "
                    f"retry_delay_seconds={policy['retry_delay_seconds']}"
                )

    validation_text = ""
    judge_text = ""
    outcome_text = ""
    if step is not None and redispatched_key is None:
        criteria = (validation_criteria or "").strip()
        if criteria:
            step["validation_criteria"] = criteria
        stored_criteria = str(step.get("validation_criteria") or "").strip()
        # Tier 2 criteria: judge_criteria wins over the legacy field.
        judge_criteria_text = str(step.get("judge_criteria") or "").strip()
        if judge_criteria_text:
            validation_text = f"\n  judge_criteria: {judge_criteria_text}"
        elif stored_criteria:
            validation_text = f"\n  validation_criteria: {stored_criteria}"

        if schema_validated is False:
            # Tier 1 failed with no retry left: the expectation was not met.
            # No judge call — the structure is already known to be wrong.
            step["status"] = str(StepStatus.FAILED)
            step["fail_reason"] = f"response_schema not satisfied: {schema_error}"
            outcome_text = f"\n  outcome: failed (schema, no retry left: {schema_error})"
        elif declared_outcome == StepOutcome.FAILURE.value:
            # The caller reported a definitive failure: it outranks the gates.
            step["status"] = str(StepStatus.FAILED)
            step["fail_reason"] = f"step_outcome=failure: {(result or '')[:200]}"
            outcome_text = "\n  outcome: failed (declared by the caller)"
        elif declared_outcome == StepOutcome.SKIPPED.value:
            step["status"] = str(StepStatus.SKIPPED)
            step["skip_reason"] = "step_outcome=skipped"
            outcome_text = "\n  outcome: skipped (declared by the caller)"
        elif judge_criteria_text or stored_criteria:
            judge_result = await judge_step_result(
                step_task=str(step.get("task") or ""),
                criteria=judge_criteria_text or stored_criteria,
                result_text=result,
                evidence_summary=collect_evidence_summary(
                    session_key=str(step.get("child_session_key") or "")
                ),
                structured_result=parsed_result if schema_validated else None,
            )
            if judge_result.verdict == StepVerdict.RETRY:
                if step_retry_count(step) < STEP_JUDGE["max_retries"]:
                    requester_key = requester_key_for_retry(state, session_id)
                    try:
                        redispatched_key = await spawn_replacement(
                            with_judge_feedback(
                                build_task_with_bindings(
                                    build_task_with_dep_results(step, steps, results),
                                    step,
                                    steps,
                                    results,
                                ),
                                judge_result.feedback,
                            ),
                            requester_key,
                        )
                    except Exception as exc:  # tool boundary: block instead of raising
                        step["status"] = str(StepStatus.BLOCKED)
                        step["block_reason"] = f"StepJudge RETRY re-dispatch failed: {exc}"
                        judge_text = (
                            f"\n  judge: RETRY re-dispatch failed ({type(exc).__name__}: {exc})"
                        )
                    else:
                        retry_count_after = step_retry_count(step) + 1
                        apply_redispatch(step, redispatched_key, retry_count_after)
                        step["judge_feedback"] = judge_result.feedback
                        judge_text = (
                            f"\n  judge: RETRY ({retry_count_after}/"
                            f"{STEP_JUDGE['max_retries']}), "
                            f"child_session_key={redispatched_key}: {judge_result.reason}"
                        )
                else:
                    step["status"] = str(StepStatus.BLOCKED)
                    step["block_reason"] = (
                        f"StepJudge RETRY budget exhausted (retry_count="
                        f"{step_retry_count(step)}, max_retries={STEP_JUDGE['max_retries']}): "
                        f"{judge_result.reason}"
                    )
                    judge_text = f"\n  judge: BLOCKED retry budget exhausted: {judge_result.reason}"
            elif judge_result.verdict == StepVerdict.BLOCK:
                step["status"] = str(StepStatus.BLOCKED)
                step["block_reason"] = f"StepJudge blocked: {judge_result.reason}"
                judge_text = f"\n  judge: BLOCKED {judge_result.reason}"
            else:
                step["status"] = str(StepStatus.DONE)
                judge_text = f"\n  judge: PASS {judge_result.reason}"
        else:
            step["status"] = str(StepStatus.DONE)
    newly_ready = unlock_dependents(steps)
    state["steps"] = steps
    counts = steps_summary(steps)

    # Resuming a waiting flow returns it to running; a running flow stays running.
    new_status = (
        TaskFlowStatus.RUNNING.value if flow["status"] == TaskFlowStatus.WAITING.value else None
    )

    # Aggregate the child's token spend when the caller reports it. The
    # idempotency guard above already returned for a duplicate delivery, so a
    # redelivered result never double-counts.
    total_tokens: int | None = None
    total_cost: float | None = None
    if token_usage:
        from config.features import MODEL_PRICING

        input_tokens = int(token_usage.get("input_tokens") or 0)
        output_tokens = int(token_usage.get("output_tokens") or 0)
        pricing_table = MODEL_PRICING["model_pricing_per_m_tokens"]
        model_name = str(token_usage.get("model_name") or "")
        pricing = pricing_table.get(model_name, pricing_table["_default"])

        total_tokens = int(flow.get("total_tokens") or 0) + input_tokens + output_tokens
        cost_delta = (
            input_tokens * pricing["input"] / 1_000_000
            + output_tokens * pricing["output"] / 1_000_000
        )
        total_cost = round(float(flow.get("total_cost") or 0.0) + cost_delta, 6)

    if redispatched_key is not None:
        replacement_key = redispatched_key
        replacement_count = retry_count_after or 0

        def build_state(fresh_flow: dict, _attempt: int) -> dict:
            fresh_state = dict(fresh_flow["state"])
            fresh_results = list(fresh_state.get("results") or [])
            if not any(
                isinstance(r, dict) and r.get("result_hash") == result_hash_value
                for r in fresh_results
            ):
                fresh_results.append(result_record)
            fresh_steps = list(fresh_state.get("steps") or [])
            target = next((s for s in fresh_steps if s.get("step_id") == step_id), None)
            if target is not None:
                apply_redispatch(target, replacement_key, replacement_count)
                if step is not None and step.get("judge_feedback"):
                    target["judge_feedback"] = step["judge_feedback"]
            fresh_state["results"] = fresh_results
            fresh_state["steps"] = fresh_steps
            return fresh_state

        updated, error = await update_flow_with_conflict_retry(
            flow_id,
            revision,
            flow,
            build_state,
            child_keys=[replacement_key],
            session_id=session_id,
            update_kwargs={
                "wait": None,
                "status": new_status,
                "total_tokens": total_tokens,
                "total_cost": total_cost,
            },
        )
        if updated is None:
            return error
    else:
        try:
            updated = await store_sqlite.update_flow(
                flow_id,
                revision,
                session_id=session_id,
                state=state,
                wait=None,
                status=new_status,
                total_tokens=total_tokens,
                total_cost=total_cost,
            )
        except FlowConflictError as exc:
            return conflict_error(exc)
        except FlowNotFoundError:
            return not_found_error(flow_id)

    counts_text = ",".join(f"{status}={counts[status]}" for status in _STATUS_ORDER)
    unlocked_text = ",".join(newly_ready)
    return (
        f"TaskFlow resumed: flow_id={flow_id}, revision={updated['expected_revision']}, "
        f"results={len(results)}, status={updated['status']}, step_id={step_id}, "
        f"unlocked=[{unlocked_text}], step_statuses={counts_text}"
        f"{validation_text}"
        f"{retry_text}"
        f"{judge_text}"
        f"{outcome_text}"
        f"{_schema_text(schema_validated, schema_error)}"
    )


def _schema_text(schema_validated: bool | None, schema_error: str | None) -> str:
    """One response line for the Tier 1 verdict (nothing when no schema ran)."""
    if schema_validated is None:
        return ""
    if schema_validated:
        return "\n  schema: validated"
    return f"\n  schema: invalid ({schema_error})"
