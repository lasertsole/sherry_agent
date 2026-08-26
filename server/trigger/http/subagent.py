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
from agent.tools.subagent import delegate_task

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


def _coerce_bool(value) -> bool:
    """Coerce a JSON body value to a bool (for run_in_background)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "yes", "on")
    return bool(value)


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


@app.post("/subagents/runs")
async def post_subagent_run_handler(request):
    """Create a new background sub-agent run (backend-driven dispatch).

    Body (JSON):
        task (str, required): Natural-language task description
            (clear goal + deliverable).
        session_id (str, optional): The requester session key that owns the
            child. Defaults to the message session key. Used verbatim as
            ``requester_session_key`` (mirrors the GET endpoint, which passes
            the browser ``session_id`` query param straight through).
        agent_id (str, optional): Logical sub-agent id. Defaults to
            ``"subagent"`` (must be ``"main"`` or exactly the configured
            ``runtime`` value, else the cross-runtime spawn guard rejects it).
        task_name (str, optional): Short display name; auto-derived from task
            when omitted.
        label (str, optional): User-facing label.
        load_skills (list[str], optional): Skill names to activate for the
            child. **Always evaluated against the task domain before dispatch**
            (see delegate_task contract); pass ``[]`` ONLY when no available
            skill matches.
        run_in_background (bool, optional): ``True`` (default) fire-and-forget;
            ``False`` blocks until terminal.
        run_timeout_seconds (float, optional): Child wall-clock timeout.
        max_spawn_depth (int, optional): Override configured nesting depth.
        max_children_per_agent (int, optional): Override concurrency cap.

    Returns:
        - 200: ``{"run": {SubagentRunRecord...}}`` (PUBLIC_FIELDS only) when
          accepted, or ``{"handle": DelegatedTaskHandle.to_dict()}``.
        - 400: invalid/missing body or required field.
        - 500: delegate_task raised (validation/auth failure).

    Notes:
        - WS broadcast to the front-end is handled automatically by the
          ``register_spawned_hook`` / ``register_ended_hook`` hooks that the
          spawn pipeline fires; this endpoint does NOT need an explicit push.
        - ``_PUBLIC_FIELDS`` is kept in sync with ``server/trigger/ws/subagent_ws.py``.
    """
    body = _read_body(request)
    if body is None:
        return _bad_request("Invalid JSON body")

    task = body.get("task")
    if not task or not isinstance(task, str) or not task.strip():
        return _bad_request("Missing or invalid 'task' (non-empty string required)")

    requester_session_key: str | None = body.get("session_id")
    if not requester_session_key:
        requester_session_key = body.get("requester_session_key")
    if not requester_session_key or not isinstance(requester_session_key, str):
        return _bad_request("Missing 'session_id' (requester_session_key)")

    load_skills = body.get("load_skills")
    if load_skills is not None and not isinstance(load_skills, list):
        return _bad_request("'load_skills' must be a list of strings")

    run_in_background = _coerce_bool(body.get("run_in_background", True))

    # Sanitize numeric overrides (mirror delegate_task's per-call handling).
    run_timeout_seconds = body.get("run_timeout_seconds")
    if run_timeout_seconds is not None:
        run_timeout_seconds = float(run_timeout_seconds)
    max_spawn_depth = body.get("max_spawn_depth")
    if max_spawn_depth is not None:
        max_spawn_depth = int(max_spawn_depth)
    max_children_per_agent = body.get("max_children_per_agent")
    if max_children_per_agent is not None:
        max_children_per_agent = int(max_children_per_agent)

    try:
        handle = delegate_task(
            task=task,
            requester_session_key=requester_session_key,
            agent_id=body.get("agent_id") or "subagent",
            load_skills=load_skills,
            run_in_background=run_in_background,
            task_name=body.get("task_name"),
            label=body.get("label"),
            run_timeout_seconds=run_timeout_seconds,
            max_spawn_depth=max_spawn_depth,
            max_children_per_agent=max_children_per_agent,
        )
    except ValueError as exc:
        logger.warning(f"POST /subagents/runs rejected: {exc}")
        return _bad_request(str(exc))
    except Exception as exc:  # unexpected dispatch failure
        logger.exception(f"POST /subagents/runs dispatch error: {exc}")
        return _to_text_response(500, {"success": False, "message": str(exc)})

    # Return the accepted run record (when available) so the UI can render it
    # immediately, mirroring the wire payload shape of GET.
    run = None
    if handle.accepted and handle.run_id:
        run = get_run(handle.run_id)

    if run is not None:
        return _ok({"run": _serialize_run(run), "handle": handle.to_dict()})

    return _ok({"handle": handle.to_dict()})


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

