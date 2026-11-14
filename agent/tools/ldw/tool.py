"""Main-agent tool for compiling and replaying verified LDW workflows."""

from __future__ import annotations

import json
from typing import Literal

from langchain_core.tools import BaseTool
from pydantic import BaseModel, Field

from agent.tools.pub_base import SessionId, resolve_tool_session_id
from agent.tools.todolist.knowledge.identity import resolve_session_plan_identity

from .compiler import compile_plan_to_workflow
from .replay import plan_knowledge_from_flow, replay_count, replay_plan

from config.features import LDW
from .scripts import find_ldw_script, write_ldw_script


class WorkflowReplayInput(BaseModel):
    """Arguments for ``workflow_replay``."""

    action: Literal["status", "compile", "replay"] = Field(
        default="status",
        description="status: inspect; compile: build ldw__<plan_key>.py from the current flow; replay: run LDW.",
    )
    variant_input: str = Field(
        default="",
        description="The current task's concrete input; it partitions the journal and feeds every step.",
    )
    flow_id: str = Field(
        default="", description="TaskFlow flow id; empty finds the session's first flow."
    )
    plan_key: str = Field(
        default="", description="Plan identity key; empty resolves it from the session."
    )
    force: bool = Field(
        default=False, description="Compile/replay even below the replay-count threshold."
    )
    session_id: SessionId = ""


class WorkflowReplayTool(BaseTool):
    """Compile or replay a verified plan through the vendored LDW core."""

    name: str = "workflow_replay"
    description: str = (
        "Replay a previously verified TaskFlow plan through langchain-dynamic-workflow "
        "(LDW), or inspect/compile its LDW workflow. Use only when the user asks to "
        "replay a known plan; ordinary new work should use TaskFlow. `status` is "
        "read-only; `compile` writes a skill script; `replay` may run leaves."
    )
    args_schema: type[BaseModel] = WorkflowReplayInput
    metadata: dict = {
        "scope": "main_only",
        "idempotent": False,
        "idempotent_actions": ("status",),
        "side_effect_scope": "session",
    }

    def _run(self, **kwargs) -> str:
        import asyncio

        return asyncio.run(self._arun(**kwargs))

    async def _arun(
        self,
        action: Literal["status", "compile", "replay"] = "status",
        variant_input: str = "",
        flow_id: str = "",
        plan_key: str = "",
        force: bool = False,
        session_id: str = "",
        run_manager=None,
        **_kwargs,
    ) -> str:
        sid = resolve_tool_session_id(session_id, run_manager)
        if not sid:
            return json.dumps({"ok": False, "error": "no session id"}, ensure_ascii=False)
        identity = resolve_session_plan_identity(sid)
        plan_key = plan_key or (identity.key if identity else "")
        if not plan_key:
            return json.dumps({"ok": False, "error": "no plan identity"}, ensure_ascii=False)

        if action == "status":
            from runtime.session.extraction import flow_extraction_enabled

            script = find_ldw_script(plan_key)
            knowledge = plan_knowledge_from_flow(sid, flow_id=flow_id)
            return json.dumps(
                {
                    "ok": True,
                    "enabled": bool(LDW["enabled"]),
                    "flow_extraction_enabled": flow_extraction_enabled(sid),
                    "plan_key": plan_key,
                    "replay_count": replay_count(plan_key),
                    "min_replays": int(LDW["min_replays"]),
                    "script": str(script) if script else None,
                    "has_flow": knowledge is not None,
                    "flow_id": (knowledge or {}).get("flow_id") or flow_id or None,
                },
                ensure_ascii=False,
            )
        if action == "compile":
            # 预设角色 → 高级: flow replay compilation is global-only. Refuse a
            # manual compile while it is off; an existing script still replays.
            from runtime.session.extraction import flow_extraction_enabled

            if not flow_extraction_enabled(sid):
                return json.dumps(
                    {
                        "ok": False,
                        "error": (
                            "flow extraction is disabled in settings; "
                            "enable it in Presets & Role → Advanced"
                        ),
                    },
                    ensure_ascii=False,
                )
            knowledge = plan_knowledge_from_flow(sid, flow_id=flow_id)
            if knowledge is None:
                return json.dumps(
                    {"ok": False, "error": "no TaskFlow DAG for this plan"}, ensure_ascii=False
                )
            meta = compile_plan_to_workflow(knowledge)
            path = write_ldw_script(plan_key, meta)
            return json.dumps(
                {
                    "ok": path is not None,
                    "plan_key": plan_key,
                    "script": str(path) if path else None,
                },
                ensure_ascii=False,
            )
        outcome = await replay_plan(
            sid,
            plan_key=plan_key,
            variant_input=variant_input,
            flow_id=flow_id,
            force=force,
        )
        return json.dumps(
            {
                "ok": outcome.get("status") in ("ok", "taskflow", "degraded"),
                **outcome,
            },
            ensure_ascii=False,
        )


def build_workflow_replay_tool() -> BaseTool:
    """Factory used by the main tool registry."""
    return WorkflowReplayTool()
