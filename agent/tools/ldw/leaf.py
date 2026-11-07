"""Build LDW leaf runnables from compiled step metadata."""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

from langchain.agents import create_agent
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from loguru import logger

from agent.tools.subagent.ldw_shim import Roster, to_pydantic_model
from agent.tools.subagent.roles import load_role_definition
from agent.tools.subagent.types.capability import SubagentSessionRole
from agent.tools.subagent.types.functional_role import FunctionalRole
from agent.tools.subagent.types.registry import SubagentRunRecord


def _role(step: dict[str, Any]) -> FunctionalRole:
    try:
        return FunctionalRole(str(step.get("functional_role") or "general"))
    except ValueError:
        return FunctionalRole.GENERAL


def _leaf_tools(step: dict[str, Any], session_id: str) -> list:
    """The exact child tool face: role policy + code-intel/PTC/ast-grep injection."""
    from agent.tools.subagent.spawn.core import _assemble_child_tools

    role = _role(step)
    role_def = load_role_definition(role)
    allow = list(role_def.tools) if role_def and role_def.tools is not None else None
    extra = [str(name) for name in (step.get("extra_tools") or []) if str(name)]
    return _assemble_child_tools(None, allow, None, extra, role, session_id)


def _leaf_model(step: dict[str, Any], *, structured: bool):
    from agent.tools.subagent.spawn.core import _select_child_llm

    role_def = load_role_definition(_role(step))
    return _select_child_llm(
        None,
        str(step.get("step_model") or "") or None,
        _role(step),
        role_def.model_tier if role_def is not None else None,
        SubagentSessionRole.LEAF,
        # A forced tool_choice while thinking is a provider-level 400 (DeepSeek).
        # The leaf path has no degradation ladder, so schema leaves always run
        # with thinking off, matching the child agent's last resort rung.
        thinking_off=structured,
    )


def _system_prompt(step: dict[str, Any]) -> str:
    from agent.tools.subagent.swarm.collector import build_structured_output_prompt

    role_def = load_role_definition(_role(step))
    parts = [
        "You are executing ONE step of a previously verified plan. "
        "Complete the task directly; do not spawn subagents or start a new TaskFlow."
    ]
    if role_def is not None and role_def.prompt_body:
        parts.append(role_def.prompt_body)
    schema = step.get("response_schema")
    if isinstance(schema, dict):
        schema_prompt = build_structured_output_prompt(schema)
        if schema_prompt:
            parts.append(schema_prompt)
    return "\n\n".join(parts)


def _leaf_graph(step: dict[str, Any], session_id: str, response_format: Any):
    tools = _leaf_tools(step, session_id)
    graph = create_agent(
        model=_leaf_model(step, structured=response_format is not None),
        tools=tools,
        system_prompt=_system_prompt(step),
        response_format=response_format,
    )
    return graph, tools


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


def make_leaf_runnable(
    step: dict[str, Any],
    session_id: str,
    *,
    response_format: Any = None,
    on_leaf_call: Callable[[str], None] | None = None,
):
    """Build a session-bound leaf; PTC prefill runs before the LLM graph."""
    graph, tools = _leaf_graph(step, session_id, response_format)
    response_schema = step.get("response_schema")
    schema = response_schema if isinstance(response_schema, dict) else None
    schema_model = to_pydantic_model(schema) if schema is not None else None

    def _structured_response(output: str) -> Any:
        if schema_model is None:
            return None
        return schema_model.model_validate(json.loads(output))

    async def _invoke(input: dict[str, Any], config: dict | None = None):
        from agent.tools.pub_base import session_workspace_root
        from agent.tools.subagent.spawn.core import _try_ptc_prefill

        if on_leaf_call is not None:
            on_leaf_call(str(step.get("step_id") or ""))
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
            output = str(ptc.get("output") or "")
            payload: dict[str, Any] = {"messages": [AIMessage(content=output)]}
            if schema_model is not None:
                try:
                    payload["structured_response"] = _structured_response(output)
                except Exception as exc:  # noqa: BLE001 - malformed PTC output -> ReAct
                    logger.warning(
                        "LDW leaf PTC structured output unusable for {}: {}",
                        step.get("step_id"),
                        exc,
                    )
                    ptc = None
                    progress = None
            if ptc is not None:
                return payload

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


def register_roster(
    workflow_meta: dict[str, Any],
    session_id: str,
    *,
    on_leaf_call: Callable[[str], None] | None = None,
) -> Roster:
    """Register one builder per step (the schema binds the returned runnable)."""
    roster = Roster()
    for step in workflow_meta.get("steps") or []:
        agent_type = str(step.get("agent_type") or "")
        if not agent_type:
            continue
        roster.register(
            name=agent_type,
            builder=lambda *, response_format=None, s=step: make_leaf_runnable(
                s,
                session_id,
                response_format=response_format,
                on_leaf_call=on_leaf_call,
            ),
            description=f"LDW leaf {agent_type}",
        )
    return roster
