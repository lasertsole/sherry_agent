"""Statistical summaries and formatting for sub-agent run results."""

from ..types.registry import SubagentRunRecord, RunOutcomeStatus


def build_child_completion_findings(run: SubagentRunRecord) -> str:
    """Build a human-readable summary string of a sub-agent run's outcome and result."""
    parts = []
    label = run.label or run.task[:50]
    parts.append(f"Subagent [{label}]")

    outcome = run.execution.outcome
    if outcome:
        parts.append(f"Status: {outcome.status}")
        if outcome.error:
            parts.append(f"Error: {outcome.error}")

    if run.accumulated_runtime_ms > 0:
        parts.append(f"Runtime: {run.accumulated_runtime_ms / 1000:.1f}s")

    if run.completion.result_text:
        result = run.completion.result_text
        if len(result) > 2000:  # Truncate long results in summary
            result = result[:2000] + "\n... [truncated]"
        parts.append(f"Result:\n{result}")

    return "\n".join(parts)


def build_compact_announce_stats_line(runs: list[SubagentRunRecord]) -> str:
    """Build a compact one-line stats summary across multiple sub-agent runs."""
    total = len(runs)
    ok = sum(1 for r in runs if r.execution.outcome and r.execution.outcome.status == RunOutcomeStatus.OK)
    errors = sum(1 for r in runs if r.execution.outcome and r.execution.outcome.status == RunOutcomeStatus.ERROR)
    timeouts = sum(1 for r in runs if r.execution.outcome and r.execution.outcome.status == RunOutcomeStatus.TIMEOUT)
    killed = sum(1 for r in runs if r.execution.outcome and r.execution.outcome.status == RunOutcomeStatus.KILLED)
    total_runtime_ms = sum(r.accumulated_runtime_ms for r in runs)

    parts = [f"total={total} ok={ok} errors={errors} timeouts={timeouts} killed={killed}"]
    if total_runtime_ms > 0:
        parts.append(f"runtime={total_runtime_ms / 1000:.1f}s")

    return " ".join(parts)


def dedupe_latest_child_completion_rows(runs: list[SubagentRunRecord]) -> list[SubagentRunRecord]:
    """Deduplicate runs by child_session_key, keeping only the latest generation per key."""
    seen: dict[str, SubagentRunRecord] = {}
    for run in runs:
        key = run.child_session_key
        if key not in seen or run.generation > seen[key].generation:
            seen[key] = run
    return list(seen.values())


def filter_current_direct_child_completion_rows(
    runs: list[SubagentRunRecord],
    requester_session_key: str,
) -> list[SubagentRunRecord]:
    """Filter runs to only those directly requested by the given session key."""
    return [run for run in runs if run.requester_session_key == requester_session_key]
