"""HTTP endpoint for exposing sub-agent run records to the client UI.

The client's "后台任务" (background tasks) tab shows a live list of the
sub-agents spawned by a session, pulled from the sub-agent registry via the
read-only query wrappers. It also supports deleting a run's entire subtree
(root + all descendants), purging both the in-memory registry and SQLite.
"""

import json

from loguru import logger
from robyn import Response

from server.trigger.core import app

from agent.tools.subagent.registry.read import (
    list_descendant_runs_readonly,
    list_runs_for_controller_readonly,
)
from agent.tools.subagent.registry import (
    get_run,
    list_descendant_runs,
    remove_run,
)
from agent.tools.subagent.registry.helpers import safe_remove_attachments_dir

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


# =============================================================================
# Response helpers (mirrors cron.py)
# =============================================================================

def _to_text_response(status_code: int, payload: dict) -> Response:
    """Build a JSON Robyn Response."""
    return Response(
        status_code=status_code,
        headers={"Content-Type": "application/json"},
        description=json.dumps(payload, ensure_ascii=False),
    )


def _ok(payload: dict) -> Response:
    return _to_text_response(200, payload)


def _bad_request(message: str) -> Response:
    return _to_text_response(400, {"success": False, "message": message})


def _not_found(message: str) -> Response:
    return _to_text_response(404, {"success": False, "message": message})


def _read_body(request) -> dict | None:
    """Parse a JSON request body defensively."""
    try:
        body = request.json()
    except Exception:
        return None
    return body if isinstance(body, dict) else None


@app.get("/subagents/runs")
async def get_subagent_runs_handler(request):
    """
    List all sub-agent runs spawned under (or visible to) a session.

    Query parameters:
        session_id (str, optional): Session ID whose descendant runs should be returned.
        run_id (str, optional): If provided, returns only this run plus its
            descendant subtree (scoped refresh of a single task tree).
        scope (str, optional): "descendants" (default) returns the full spawned
            tree; "controller" returns runs where the session is requester or child.

    Returns:
        {"runs": [SubagentRunRecord...]}
    """
    query_params = request.query_params

    session_id: str | None = query_params.get("session_id", None)
    scope: str = query_params.get("scope", "descendants")
    run_id: str | None = query_params.get("run_id", None)
    logger.debug(
        f"Reading sub-agent runs: session_id={session_id}, run_id={run_id}, scope={scope}"
    )

    if run_id:
        root = get_run(run_id)
        if root is None:
            raise ValueError(f"sub-agent run '{run_id}' not found")
        # Root + all of its descendants. list_descendant_runs uses the root's
        # child_session_key as the requester key so it returns only descendants;
        # the root itself is not included, hence we prepend it.
        descendants = list_descendant_runs(root.child_session_key)
        runs = [root] + descendants

    elif not session_id:
        raise ValueError("session_id is required")

    elif scope == "controller":
        runs = list_runs_for_controller_readonly(session_id)

    else:
        runs = list_descendant_runs_readonly(session_id)

    # Newest-spawned first for the UI list.
    runs = sorted(runs, key=lambda r: r.run_id, reverse=True)
    payload = {"runs": [_serialize_run(run) for run in runs]}
    logger.debug(f"Sub-agent runs: count={len(runs)}")
    return payload


@app.delete("/subagents/runs")
async def delete_subagent_run_handler(request):
    """Delete a sub-agent run and its entire descendant subtree.

    Body: {"run_id": str (required)}.

    Deletes the root run plus all descendants (BFS via child_session_key),
    purging each from both the in-memory registry and SQLite, and removes any
    attachments dir. Returns the number of runs removed.
    """
    body = _read_body(request)
    if body is None:
        return _bad_request("Invalid JSON body")

    run_id = body.get("run_id")
    if not run_id or not isinstance(run_id, str) or not run_id.strip():
        return _bad_request("Missing or invalid 'run_id'")

    root = get_run(run_id)
    if root is None:
        return _not_found(f"Sub-agent run '{run_id}' not found")

    # Collect the root + all descendant runs. list_descendant_runs uses the
    # root's child_session_key as the requester key, so it returns all of the
    # root's descendants — the root itself is not included, hence we prepend it.
    descendants = list_descendant_runs(root.child_session_key)
    targets = [root] + descendants

    removed = 0
    for run in targets:
        await remove_run(run.run_id)
        safe_remove_attachments_dir(
            getattr(run, "attachments_dir", None),
            getattr(run, "attachments_root_dir", None),
        )
        removed += 1

    logger.info(f"Sub-agent run subtree removed: run_id={run_id}, removed={removed}")
    return _ok({"success": True, "removed": removed})

