"""The ``knowledge`` tool: plan-aware knowledge write / read / list.

Writes happen during plan extraction (nudge agent, on todo completion); reads
are on-demand lookups by the main agent before similar work. Invalid input is
returned as ``"Error: ..."`` text — the repo's error-as-text contract — never
raised. The tool carries ``nudge``/``main_only`` metadata so the nudge
allowlist accepts it and the subagent tool policy keeps it on the main agent.
"""

from typing import Literal

from langchain_core.tools import tool

from .knowledge_store import KnowledgeStore, Layer


@tool("knowledge")
async def knowledge(
    action: Literal["write", "read", "list"],
    plan_name: str | None = None,
    layer: Layer | None = None,
    data: dict | None = None,
    position: int | None = None,
    wave_index: int | None = None,
) -> str:
    """Plan-aware knowledge store. Write during extraction, read before similar work.

    Actions:
      - write: Write knowledge data (nudge agent during plan extraction)
      - read:  Read knowledge for a plan (main agent, on-demand)
      - list:  List all plans that have knowledge files

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
    match action:
        case "write":
            if not plan_name or not data or not layer:
                return "Error: write requires plan_name, layer, and data"
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
            return KnowledgeStore.read_formatted(
                plan_name=plan_name,
                layer=layer,
                position=position,
                wave_index=wave_index,
            )

        case "list":
            plans = KnowledgeStore.list_plans()
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
