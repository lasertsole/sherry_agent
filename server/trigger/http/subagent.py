"""HTTP endpoint for exposing sub-agent run records to the client UI.

The client's "后台任务" (background tasks) tab shows a live list of the
sub-agents spawned by a session, pulled from the sub-agent registry via the
read-only query wrappers.
"""

from loguru import logger

from server.trigger.core import app

from agent.tools.subagent.registry.read import (
    list_descendant_runs_readonly,
    list_runs_for_controller_readonly,
)

# Fields that are safe / useful to surface to the UI. Everything else (paths,
# attachment dirs, internal policy vectors) is omitted from the wire payload.
_PUBLIC_FIELDS = (
    "run_id",
    "child_session_key",
    "requester_session_key",
    "task",
    "task_name",
    "label",
    "spawn_mode",
    "context_mode",
    "agent_id",
    "depth",
    "role",
    "control_scope",
    "generation",
    "swarm_group_id",
    "swarm_run_state",
    "ended_reason",
    "pause_reason",
    "execution",
    "completion",
    "delivery",
)


def _serialize_run(run) -> dict:
    """Convert a SubagentRunRecord into a JSON-serializable dict with only public fields."""
    # model_dump(..., mode="json") recursively converts nested pydantic models
    # (execution / completion / delivery) and enums into plain JSON-safe values.
    return run.model_dump(include=set(_PUBLIC_FIELDS), mode="json")


@app.get("/subagents/runs")
async def get_subagent_runs_handler(request):
    """
    List all sub-agent runs spawned under (or visible to) a session.

    Query parameters:
        session_id (str, required): Session ID whose descendant runs should be returned.
        scope (str, optional): "descendants" (default) returns the full spawned
            tree; "controller" returns runs where the session is requester or child.

    Returns:
        {"runs": [SubagentRunRecord...]}
    """
    query_params = request.query_params

    session_id: str | None = query_params.get("session_id", None)
    scope: str = query_params.get("scope", "descendants")
    logger.debug(f"Reading sub-agent runs: session_id={session_id}, scope={scope}")

    if not session_id:
        raise ValueError("session_id is required")

    if scope == "controller":
        runs = list_runs_for_controller_readonly(session_id)
    else:
        runs = list_descendant_runs_readonly(session_id)

    # Newest-spawned first for the UI list.
    runs = sorted(runs, key=lambda r: r.run_id, reverse=True)
    payload = {"runs": [_serialize_run(run) for run in runs]}
    logger.debug(f"Sub-agent runs: count={len(runs)}")
    return payload
