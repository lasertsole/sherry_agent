"""taskflow_summary: read back the current flow state (openclaw getTaskSummary).

Also the designated re-read step after a revision conflict: the conflict text
instructs the caller to retry with the revision reported here.
"""

import json

from langchain_core.tools import tool

from ..registry import store_sqlite
from ._shared import not_found_error


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
            lines.append(
                f"  - [{step.get('step_id')}] {step.get('task')} -> {step.get('child_session_key')}"
            )
    lines.append(f"results: {len(results)}")
    for item in results:
        if isinstance(item, dict):
            lines.append(
                f"  - [{item.get('child_session_key')}] {str(item.get('result'))[:400]}"
            )
    if flow["wait"] is not None:
        lines.append(f"wait: {json.dumps(flow['wait'], ensure_ascii=False)}")
    else:
        lines.append("wait: (none)")
    if state.get("summary"):
        lines.append(f"summary: {state['summary']}")
    if state.get("failure_reason"):
        lines.append(f"failure_reason: {state['failure_reason']}")
    if state.get("cancel_reason"):
        lines.append(f"cancel_reason: {state['cancel_reason']}")
    return "\n".join(lines)
