"""taskflow_template tools: save/load reusable TaskFlow plan structures.

Thin tool wrappers around :mod:`agent.tools.taskflow.template_store`. They plug
into the merged ``taskflow_flow`` tool as ``save_as_template`` /
``load_from_template`` actions (WorkMode stage 4).
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import tool

from agent.tools.pub_base import SessionId
from agent.tools.taskflow import template_store

__all__ = [
    "taskflow_load_template",
    "taskflow_save_template",
]


@tool("taskflow_save_template")
async def taskflow_save_template(
    flow_id: str,
    name: str,
    description: str = "",
    session_id: SessionId = "",
) -> str:
    """Save a flow's step structure (ids/tasks/deps/roles/criteria) as a reusable template.

    Runtime state (child sessions, retry counts, results, statuses) is NOT saved —
    a loaded template is an empty step skeleton to run afresh. ``name`` is a
    1-64 char filename stem (letters, digits, dot, dash, underscore).
    """
    flow_id = (flow_id or "").strip()
    if not flow_id:
        return "Error: flow_id is required"
    from agent.tools.taskflow.registry import store_sqlite

    flow = await store_sqlite.get_flow(flow_id, session_id)
    if flow is None:
        return f"Error: TaskFlow '{flow_id}' not found"
    try:
        result = await template_store.save_template(
            name, flow.get("state") or {}, description=description
        )
    except template_store.TemplateStoreError as exc:
        return f"Error: {exc}"
    if not result.get("success"):
        return f"Error: {result.get('message')}"
    return (
        f"Template saved: name={result['name']}, steps={result['steps']}"
        f", description={result.get('description') or '-'}"
    )


@tool("taskflow_load_template")
async def taskflow_load_template(
    name: str,
    flow_id: str,
    description: str = "",
    session_id: SessionId = "",
    ack_dup: bool = False,
) -> str:
    """Create a NEW flow whose steps come from a saved template (empty skeleton, re-run).

    The new flow's steps are READY with no results; dependencies and acceptance
    criteria are preserved from the template. This reuses the PLAN structure —
    for replaying an exact EXECUTION sequence use PTC/LDW instead.
    """
    template = template_store.load_template(name)
    if template is None:
        return f"Error: template '{name}' not found or unreadable"
    from agent.tools.taskflow.tools.taskflow_create import taskflow_create

    steps: list[dict[str, Any]] = []
    for raw in template["steps"]:
        step = dict(raw)
        # A template carries no runtime status; a fresh flow starts all READY
        # and the DAG's dependency check gates dispatch as usual.
        step["status"] = "ready"
        step.setdefault("depends_on", [])
        steps.append(step)
    initial_state = {"steps": steps, "results": []}
    label = description or f"from template:{template['name']}"
    return await taskflow_create.coroutine(
        flow_id=flow_id,
        description=label,
        initial_state=initial_state,
        session_id=session_id,
        ack_dup=ack_dup,
    )
