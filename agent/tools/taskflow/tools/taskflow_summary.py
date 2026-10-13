"""taskflow_summary: read back the current flow state (openclaw getTaskSummary).

Also the designated re-read step after a revision conflict: the conflict text
instructs the caller to retry with the revision reported here.
"""

import json
import time

from langchain_core.tools import tool

from config.features import TASKFLOW_INFRA
from ..registry import store_sqlite
from ._shared import is_terminal, not_found_error, step_status, steps_summary
from agent.tools.pub_base import SessionId


@tool("taskflow_summary")
async def taskflow_summary(flow_id: str, session_id: SessionId = "") -> str:
    """Read back a task flow owned by the current session: status,
    expected_revision, child_session_key, steps, injected results and wait
    payload. Read-only; also the re-read step to run after a revision conflict
    before retrying a mutation. A flow belonging to another session reads as
    not found."""
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"

    flow = await store_sqlite.get_flow(flow_id, session_id)
    if flow is None:
        return not_found_error(flow_id)

    state = flow["state"] or {}
    steps = state.get("steps") or []
    results = state.get("results") or []

    lines = [
        f"TaskFlow summary: flow_id={flow['flow_id']}",
        f"status={flow['status']}, revision={flow['expected_revision']}",
        f"child_session_key={flow['child_session_key'] or '(none)'}",
        f"description={state.get('description', '')}",
        f"steps: {len(steps)}",
    ]
    for step in steps:
        if isinstance(step, dict):
            depends_on = step.get("depends_on") or []
            deps_text = "[" + ", ".join(str(dep) for dep in depends_on) + "]"
            criteria = str(step.get("validation_criteria") or "").strip()
            criteria_text = f" validation_criteria={criteria}" if criteria else ""
            lines.append(
                f"  - [{step.get('step_id')}] {step_status(step)} {step.get('task')} "
                f"-> {step.get('child_session_key')} depends_on={deps_text}{criteria_text}"
            )
            lines.extend(_expectation_lines(step))
    counts = steps_summary(steps)
    lines.append("step statuses: " + " ".join(f"{status}={n}" for status, n in counts.items()))
    deadline_ts = flow.get("deadline_ts")
    if deadline_ts:
        now = time.time()
        deadline_text = time.strftime("%Y-%m-%d %H:%M", time.localtime(deadline_ts))
        if now > deadline_ts and not is_terminal(flow["status"]):
            lines.append(f"deadline: EXCEEDED (was {deadline_text})")
        else:
            remaining_h = (deadline_ts - now) / 3600
            if remaining_h > 0:
                lines.append(f"deadline: {deadline_text} ({remaining_h:.1f}h remaining)")
            else:
                lines.append(f"deadline: {deadline_text} (passed)")
    lines.append(f"results: {len(results)}")
    for item in results:
        if isinstance(item, dict):
            lines.append(f"  - [{item.get('child_session_key')}] {str(item.get('result'))[:400]}")
            lines.extend(_result_gate_lines(item))
    if flow["wait"] is not None:
        wait_payload = flow["wait"]
        lines.append(f"wait: {json.dumps(wait_payload, ensure_ascii=False)}")
        set_at = wait_payload.get("set_at")
        if set_at:
            now = time.time()
            waiting_secs = now - float(set_at)
            timeout_hours = TASKFLOW_INFRA["waiting_timeout_hours"]
            timeout_secs = timeout_hours * 3600
            if waiting_secs > timeout_secs:
                waiting_h = waiting_secs / 3600
                lines.append(
                    f"wait_status: STALE (waiting {waiting_h:.1f}h, timeout={timeout_hours}h) "
                    f"— child session may have crashed; consider taskflow_resume with a "
                    f"failure result or re-dispatch"
                )
            else:
                remaining_h = (timeout_secs - waiting_secs) / 3600
                lines.append(
                    f"wait_status: active (waiting {waiting_secs / 3600:.1f}h, "
                    f"{remaining_h:.1f}h to timeout)"
                )
    else:
        lines.append("wait: (none)")
    if state.get("summary"):
        lines.append(f"summary: {state['summary']}")
    if state.get("failure_reason"):
        lines.append(f"failure_reason: {state['failure_reason']}")
    if state.get("cancel_reason"):
        lines.append(f"cancel_reason: {state['cancel_reason']}")
    return "\n".join(lines)


def _expectation_lines(step: dict) -> list[str]:
    """Indented lines describing a step's expectation side (nothing when plain).

    Only configured fields are printed, so a pre-closure step adds no lines and
    the summary stays short for simple flows.
    """
    out: list[str] = []
    schema = step.get("response_schema")
    if isinstance(schema, dict):
        properties = sorted(schema.get("properties", {}).keys())
        required = sorted(schema.get("required", []))
        out.append(
            f"      response_schema: properties={properties or '{}'} required={required or '[]'}"
        )
    if step.get("expected_params") is not None:
        out.append(
            "      expected_params: "
            + json.dumps(step["expected_params"], ensure_ascii=False, default=str)
        )
    if step.get("input_bindings"):
        out.append(
            "      input_bindings: "
            + json.dumps(step["input_bindings"], ensure_ascii=False, default=str)
        )
    if step.get("judge_criteria"):
        out.append(f"      judge_criteria: {step['judge_criteria']}")
    if step.get("judge_model"):
        out.append(f"      judge_model: {step['judge_model']}")
    for key, label in (
        ("functional_role", "functional_role"),
        ("step_model", "step_model"),
        ("step_timeout_seconds", "step_timeout_seconds"),
        ("priority", "priority"),
        ("block_reason", "block_reason"),
        ("fail_reason", "fail_reason"),
        ("skip_reason", "skip_reason"),
    ):
        if step.get(key) not in (None, ""):
            out.append(f"      {label}: {step[key]}")
    return out


def _result_gate_lines(record: dict) -> list[str]:
    """Indented lines for a result record's gate verdicts (schema/outcome/judge)."""
    out: list[str] = []
    if record.get("step_id"):
        out.append(f"      step_id: {record['step_id']}")
    schema_validated = record.get("schema_validated")
    if schema_validated is not None:
        out.append(f"      schema_validated: {str(bool(schema_validated)).lower()}")
    if record.get("structured_result") is not None:
        out.append(
            "      structured_result: "
            + json.dumps(record["structured_result"], ensure_ascii=False, default=str)[:400]
        )
    if record.get("step_outcome"):
        out.append(f"      step_outcome: {record['step_outcome']}")
    if record.get("judge_verdict"):
        out.append(f"      judge_verdict: {record['judge_verdict']}")
    if record.get("judge_reason"):
        out.append(f"      judge_reason: {record['judge_reason']}")
    token_usage = record.get("token_usage")
    if isinstance(token_usage, dict) and token_usage:
        out.append("      token_usage: " + json.dumps(token_usage, ensure_ascii=False, default=str))
    return out
