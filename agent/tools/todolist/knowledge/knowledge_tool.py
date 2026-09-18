"""The ``knowledge`` tool: plan-aware knowledge write / read / list.

Writes happen during plan extraction (nudge agent, on todo completion); reads
are on-demand lookups by the main agent before similar work. Access is isolated
per plan ownership: a session may only read / write / list knowledge for plans
it is associated with (state ``plan_ref``, todos ``plan_ref``, or a boulder work
whose ``session_ids`` include the session) — see ``ownership.py``. Invalid
input and ownership denials are returned as ``"Error: ..."`` text — the repo's
error-as-text contract — never raised. The tool carries ``nudge``/``main_only``
metadata so the nudge allowlist accepts it and the subagent tool policy keeps it
on the main agent.
"""

from typing import Annotated, Literal

from langchain_core.tools import tool
from langgraph.prebuilt.tool_node import InjectedState
from loguru import logger

from .knowledge_store import KnowledgeStore, Layer
from .ownership import associated_plan_names, is_plan_associated


def _deny(action: str, plan_name: str, session_id: str) -> str:
    """Diagnosable denial: names both the plan and the reason."""
    logger.debug(
        "knowledge {} denied for plan '{}': not associated with session '{}'",
        action,
        plan_name,
        session_id,
    )
    return (
        f"Error: knowledge {action} denied for plan '{plan_name}': the plan is not "
        f"associated with this session ('{session_id}'). A plan is accessible only "
        "through the session's plan_ref, a todos plan_ref, or a boulder work whose "
        "session_ids include this session."
    )


def _deny_missing_session(action: str) -> str:
    """Denial when the graph state carried no session_id at all."""
    logger.debug("knowledge {} denied: no session_id available in state", action)
    return (
        f"Error: knowledge {action} denied: no session_id is available, so plan "
        "ownership cannot be verified."
    )


@tool("knowledge")
async def knowledge(
    action: Literal["write", "read", "list"],
    plan_name: str | None = None,
    layer: Layer | None = None,
    data: dict | None = None,
    position: int | None = None,
    wave_index: int | None = None,
    session_id: Annotated[str, InjectedState("session_id")] = "",
) -> str:
    """Plan-aware knowledge store. Write during extraction, read before similar work.

    Ownership isolation: a session can only access plans it is associated with
    (its plan_ref, a todo plan_ref, or a boulder work whose session_ids include
    the session). Multi-session work on the same plan stays fully available to
    every listed session.

    Actions:
      - write: Write knowledge data (nudge agent during plan extraction)
      - read:  Read knowledge for a plan (main agent, on-demand)
      - list:  List this session's plans that have knowledge files

    Read examples:
      knowledge(action="read", plan_name="implement-auth")
        -> Returns plan summary + task index
      knowledge(action="read", plan_name="implement-auth", layer="task", position=0)
        -> Returns full failure_set/success_path/method for task 0
      knowledge(action="read", plan_name="implement-auth", layer="wave", wave_index=0)
        -> Returns wave-level failure/success patterns
      knowledge(action="read", plan_name="implement-auth", layer="plan")
        -> Returns full plan summary

    Write examples:
      knowledge(action="write", plan_name="...", layer="task", position=0, data={...})
      knowledge(action="write", plan_name="...", layer="wave", wave_index=0, data={...})
      knowledge(action="write", plan_name="...", layer="plan", data={...})
    """
    sid = session_id if isinstance(session_id, str) else ""

    match action:
        case "write":
            if not plan_name or not data or not layer:
                return "Error: write requires plan_name, layer, and data"
            if not sid:
                return _deny_missing_session("write")
            if not is_plan_associated(sid, plan_name):
                return _deny("write", plan_name, sid)
            try:
                path = await KnowledgeStore.write(
                    layer=layer,
                    plan_name=plan_name,
                    data=data,
                    position=position,
                    wave_index=wave_index,
                )
            except ValueError as e:
                return f"Error: {e}"
            return f"Knowledge written to {path}"

        case "read":
            if not plan_name:
                return "Error: read requires plan_name"
            if not sid:
                return _deny_missing_session("read")
            if not is_plan_associated(sid, plan_name):
                return _deny("read", plan_name, sid)
            return KnowledgeStore.read_formatted(
                plan_name=plan_name,
                layer=layer,
                position=position,
                wave_index=wave_index,
            )

        case "list":
            if not sid:
                logger.debug("knowledge list: empty session_id; no plans to list")
                return "No knowledge files found."
            associated = associated_plan_names(sid)
            plans = [plan for plan in KnowledgeStore.list_plans() if plan in associated]
            if not plans:
                return "No knowledge files found."
            lines = ["Available plans with knowledge:"]
            for plan in plans:
                summary = KnowledgeStore.read_summary(plan)
                method = summary.get("method", "unknown") if summary else None
                lines.append(f"  - {plan} (method: {method})" if method else f"  - {plan}")
            return "\n".join(lines)

        case _:
            return f"Unknown action: {action}"


# Nudge allowlist + subagent tool-policy tags (mirrors skill_manage).
knowledge.metadata = {**(knowledge.metadata or {}), "nudge": True, "scope": "main_only"}


__all__ = ["Layer", "knowledge"]
