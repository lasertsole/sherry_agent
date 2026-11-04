"""Agent catalogue + per-session agent config (预设 的子代理模型 / 技能 两栏).

GET /agent/catalog
    -> {"success": true,
        "subagent_roles": [{"role": ..., "model_tier": ..., "description": ...}, ...],
        "skills": [{"name": ..., "description": ..., "builtin": bool}, ...]}
       The two lists the preset panel renders. The client NEVER hardcodes role
       or skill names: this endpoint is the single source. ``skills`` carries
       the ACTIVE skills the index can contain (an inactive uploaded skill is
       toggled in 菜单-技能). The built-in tool set and the middleware chain are
       NOT configurable — every tool is always available and every middleware
       always runs — so there is no tools / middlewares list.

GET /sessions/agent_config?session_id=<sid>
    -> {"success": true, "session_id": ..., "config": {...}, "pending": bool}
       ``config`` is the EFFECTIVE next-turn payload (a parked mid-turn
       choice wins); ``{}`` means "role tiers / every skill in the index".

PUT /sessions/agent_config  {"session_id": <sid>, "config": {...}}
    -> {"success": true, "session_id": ..., "config": {...}, "pending": bool}
       400 with the offending name for an unknown role or skill. While a turn
       is in flight the choice is parked (``pending: true``) and lands on the
       next turn — the running turn keeps its config.
"""

from __future__ import annotations

import asyncio

from loguru import logger

from server.service.agent_config_service import (
    AgentConfigError,
    apply_agent_config_choice,
    get_agent_config_state,
)
from server.trigger.core import app
from server.trigger.http.helpers import bad_request, ok, read_body


def _agent_catalog() -> dict:
    """Build the two lists (imported lazily: agent modules are heavy)."""
    from agent.tools.subagent.roles import load_all_role_definitions
    from agent.tools.subagent.types.functional_role import FunctionalRole
    from skills.loader import skills_catalog

    roles: list[dict] = []
    definitions = load_all_role_definitions()  # {FunctionalRole: RoleDefinition}
    for role in FunctionalRole:
        definition = definitions.get(role)
        roles.append(
            {
                "role": role.value,
                # None = the role has no definition file (general): the child
                # LLM follows the depth rule instead of a tier.
                "model_tier": definition.model_tier if definition else None,
                "description": definition.description if definition else "",
            }
        )
    return {
        "subagent_roles": roles,
        "skills": skills_catalog(),
    }


@app.get("/agent/catalog")
async def get_agent_catalog_handler(request):  # noqa: ARG001 - no query params
    """Return the role / skill lists the preset panel renders."""
    catalog = await asyncio.to_thread(_agent_catalog)
    return ok({"success": True, **catalog})


@app.get("/sessions/agent_config")
async def get_agent_config_handler(request):
    """Return the session's effective agent config and its parked state."""
    query = request.query_params or {}
    session_id = query.get("session_id", "") or ""
    if not session_id:
        return bad_request("session_id is required")
    state = await asyncio.to_thread(get_agent_config_state, session_id)
    return ok({"success": True, "session_id": session_id, **state})


@app.put("/sessions/agent_config")
async def put_agent_config_handler(request):
    """Persist the session's agent config (live, or parked while a turn runs)."""
    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    session_id = body.get("session_id") or ""
    if not session_id:
        return bad_request("session_id is required")
    if "config" not in body:
        return bad_request("'config' is required (an object; {} = every default)")
    try:
        config, pending = await apply_agent_config_choice(session_id, body.get("config"))
    except AgentConfigError as exc:
        # The sanitizer's messages name the offending field and carry no internals.
        return bad_request(str(exc) or "invalid agent config")
    except ValueError:
        return bad_request("invalid session_id")
    logger.info(
        "Agent config PUT: session={} roles={} pending={}",
        session_id,
        len(config.get("subagent_models") or {}),
        pending,
    )
    return ok({"success": True, "session_id": session_id, "config": config, "pending": pending})
