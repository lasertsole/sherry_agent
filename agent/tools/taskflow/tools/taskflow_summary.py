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


@tool("taskflow_summary")
async def taskflow_summary(flow_id: str) -> str:
    """Read back a task flow: status, expected_revision, child_session_key,
    steps, injected results and wait payload. Read-only; also the re-read
    step to run after a revision conflict before retrying a mutation."""
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"

    flow = await store_sqlite.get_flow(flow_id)
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
