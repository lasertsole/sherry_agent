"""taskflow_progress: structured progress report for a task flow.

A human-readable companion to taskflow_summary: instead of raw steps/results,
returns a concise progress report with completion percentage, step counts,
next actionable steps, and estimated remaining time based on completed step
durations.
"""

from langchain_core.tools import tool

from ..registry import store_sqlite
from ._shared import not_found_error, step_status, steps_summary

_STEP_ICONS = {
    "done": "✓",
    "dispatched": "→",
    "ready": "○",
    "blocked": "⊘",
}


@tool("taskflow_progress")
async def taskflow_progress(flow_id: str) -> str:
    """Get a structured progress report for a task flow.

    Returns completion percentage, step status breakdown, next actionable
    steps, and estimated remaining time (based on average completed step
    duration). Read-only.
    """
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"

    flow = await store_sqlite.get_flow(flow_id)
    if flow is None:
        return not_found_error(flow_id)

    state = flow.get("state") or {}
    steps = state.get("steps") or []
    counts = steps_summary(steps)
    total = len(steps)
    done = counts.get("done", 0)

    if total == 0:
        return f"Progress: flow_id={flow_id}, status={flow['status']}\nNo steps registered yet."

    pct = int(done / total * 100)

    lines = [
        f"Progress Report: {flow_id}",
        f"  Status: {flow['status']}",
        f"  Description: {state.get('description', '')[:80]}",
        f"  Completion: {done}/{total} steps ({pct}%)",
        "  Breakdown: "
        + " · ".join(f"{s}={counts.get(s, 0)}" for s in ("done", "dispatched", "ready", "blocked")),
    ]

    # Next actionable steps (first 3 non-done)
    actionable = [s for s in steps if step_status(s) != "done"]
    if actionable:
        lines.append("  Next steps:")
        for s in actionable[:3]:
            icon = _STEP_ICONS.get(step_status(s), "?")
            task_text = (s.get("task", "") or "")[:60]
            lines.append(f"    {icon} [{s.get('step_id', '?')}] {task_text}")

    # Estimated remaining time
    done_steps = [s for s in steps if step_status(s) == "done" and s.get("dispatched_at")]
    remaining = total - done
    if len(done_steps) >= 2 and remaining > 0:
        # Average duration between dispatched_at timestamps of done steps
        timestamps = sorted(float(s["dispatched_at"]) for s in done_steps)
        if len(timestamps) >= 2:
            durations = [timestamps[i + 1] - timestamps[i] for i in range(len(timestamps) - 1)]
            avg_duration = sum(durations) / len(durations)
            est_remaining_secs = avg_duration * remaining
            if est_remaining_secs < 3600:
                est_text = f"{est_remaining_secs / 60:.0f} minutes"
            else:
                est_text = f"{est_remaining_secs / 3600:.1f} hours"
            lines.append(
                f"  Est. remaining: ~{est_text} (based on {len(done_steps)} completed steps)"
            )

    if flow.get("wait"):
        lines.append(f"  Waiting on: {flow['wait'].get('reason', 'unknown')}")

    results = state.get("results") or []
    if results:
        lines.append(f"  Results injected: {len(results)}")

    return "\n".join(lines)
