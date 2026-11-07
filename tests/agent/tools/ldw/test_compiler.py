"""LDW workflow metadata compiler and orchestration prompt assembly."""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from agent.tools.ldw.compiler import build_orchestrate, compile_plan_to_workflow
from agent.tools.subagent.ldw_shim import InMemoryJournalStore, Roster, run_workflow

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _knowledge() -> dict:
    return {
        "plan_key": "k1",
        "steps": [
            {"step_id": "a", "task": "do a"},
            {
                "step_id": "b",
                "task": "do b",
                "depends_on": ["a"],
                "functional_role": "executor",
                "extra_tools": ["read_file"],
            },
        ],
    }


def test_compile_keeps_dag_and_step_identity() -> None:
    meta = compile_plan_to_workflow(_knowledge())
    assert meta["plan_key"] == "k1"
    assert [step["step_id"] for step in meta["steps"]] == ["a", "b"]
    assert meta["steps"][1]["deps"] == ["a"]
    assert meta["steps"][1]["agent_type"].startswith("executor+xt:read_file@b")
    assert meta["steps"][0]["task_template"] == "do a"


@pytest.mark.asyncio
async def test_orchestrate_passes_variant_and_upstream_results() -> None:
    meta = compile_plan_to_workflow(_knowledge())
    prompts: list[str] = []

    async def _leaf(input, config=None):
        prompts.append(input["messages"][-1].content)
        return {"messages": [AIMessage(content="ok")]}

    roster = Roster().register("general@a", runnable=RunnableLambda(_leaf))
    roster.register("executor+xt:read_file@b", runnable=RunnableLambda(_leaf))
    result = await run_workflow(
        build_orchestrate(meta, "variant-x"),
        roster=roster,
        journal=InMemoryJournalStore(),
    )
    assert result == {"a": "ok", "b": "ok"}
    assert "variant-x" in prompts[0]
    assert "ok" in prompts[1], "the downstream prompt must carry the predecessor result"
    assert json.dumps({"a": "ok"}, ensure_ascii=False) in prompts[1]
