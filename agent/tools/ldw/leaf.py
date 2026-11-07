"""Build LDW leaf runnables from compiled step metadata."""

from __future__ import annotations

import uuid
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from loguru import logger

from agent.tools.subagent.spawn.inherited_tool_policy import (
    DEFAULT_SUBAGENT_BLOCKED_TOOLS,
    apply_tool_policy,
)
from agent.tools.subagent.ldw_shim import Roster
from agent.tools.subagent.roles import load_role_definition
from agent.tools.subagent.types.functional_role import FunctionalRole
from agent.tools.subagent.types.registry import SubagentRunRecord


def _role(step: dict[str, Any]) -> FunctionalRole:
    try:
        return FunctionalRole(str(step.get("functional_role") or "general"))
    except ValueError:
        return FunctionalRole.GENERAL


def _leaf_tools(step: dict[str, Any]) -> list:
    from agent.tools import build_main_tools

    role = _role(step)
    role_def = load_role_definition(role)
    base = build_main_tools()
    allow = list(role_def.tools) if role_def and role_def.tools is not None else []
    extra = [str(name) for name in (step.get("extra_tools") or []) if str(name)]
    if allow:
        allow.extend(name for name in extra if name not in allow)
    # Empty allow = inherit every non-blocked tool. Extra tool names are already
    # present in ``base`` in this mode; they never bypass the policy filter.
    return list(apply_tool_policy(base, allow, DEFAULT_SUBAGENT_BLOCKED_TOOLS))


def _leaf_model(step: dict[str, Any]):
    from models import build_auxiliary_llm, build_main_llm

    role_def = load_role_definition(_role(step))
    if role_def is not None and role_def.model_tier == "main":
        return build_main_llm()
    return build_auxiliary_llm()


def _leaf_graph(step: dict[str, Any]):
    tools = _leaf_tools(step)
    role_def = load_role_definition(_role(step))
    system_prompt = role_def.prompt_body if role_def is not None else ""
    return create_agent(
        model=_leaf_model(step),
        tools=tools,
        system_prompt=system_prompt,
    ), tools


def _ptc_run_record(step: dict[str, Any], session_id: str) -> SubagentRunRecord:
    return SubagentRunRecord(
        run_id=f"ldw-{uuid.uuid4()}",
        child_session_key=f"agent:ldw:subagent:{uuid.uuid4()}",
        requester_session_key=f"agent:main:session:{session_id}",
        controller_session_key=f"agent:main:session:{session_id}",
        task=str(step.get("task_template") or ""),
        functional_role=_role(step),
        step_id=str(step.get("step_id") or ""),
        spawned_cwd=None,
    )


def make_leaf_runnable(step: dict[str, Any], session_id: str):
    """Build a session-bound leaf; PTC prefill runs before the LLM graph."""
    graph, tools = _leaf_graph(step)
    response_schema = step.get("response_schema")
    if isinstance(response_schema, dict):
        schema = response_schema
    else:
        schema = None

    async def _invoke(input: dict[str, Any], config: dict | None = None):
        from agent.tools.subagent.spawn.core import _try_ptc_prefill
        from agent.tools.pub_base import session_workspace_root

        run = _ptc_run_record(step, session_id)
        run.spawned_cwd = (
            str(session_workspace_root(session_id)) if session_workspace_root(session_id) else None
        )
        try:
            ptc, progress = await _try_ptc_prefill(run, tools, [], schema)
        except Exception as exc:  # noqa: BLE001 - replay stays fail-open
            logger.warning("LDW leaf PTC prefill failed for {}: {}", step.get("step_id"), exc)
            ptc, progress = None, None
        if ptc is not None:
            return {"messages": [AIMessage(content=ptc["output"])]}

        messages = list(input.get("messages") or [])
        if progress and messages:
            text = getattr(messages[-1], "content", "")
            messages[-1] = HumanMessage(content=f"{text}\n\n{progress}")
        cfg = dict(config or {})
        configurable = dict(cfg.get("configurable") or {})
        configurable["session_id"] = session_id
        cfg["configurable"] = configurable
        return await graph.ainvoke({"messages": messages}, config=cfg)

    return RunnableLambda(_invoke)


def register_roster(workflow_meta: dict[str, Any], session_id: str) -> Roster:
    roster = Roster()
    for step in workflow_meta.get("steps") or []:
        agent_type = str(step.get("agent_type") or "")
        if not agent_type:
            continue
        roster.register(
            name=agent_type,
            runnable=make_leaf_runnable(step, session_id),
            description=f"LDW leaf {agent_type}",
        )
    return roster
