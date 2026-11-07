"""Phase 0: the vendored LDW core imports and runs without deepagents."""

from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from agent.tools.subagent.ldw_shim import (
    DagNode,
    InMemoryJournalStore,
    Roster,
    run_workflow,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _leaf(calls: list[str]):
    async def _run(input, config=None):
        prompt = input["messages"][-1].content
        calls.append(prompt)
        return {"messages": [AIMessage(content=f"out:{prompt}")]}

    return RunnableLambda(_run)


@pytest.mark.asyncio
async def test_dag_passes_dependency_results_and_journals_second_run() -> None:
    calls: list[str] = []
    roster = Roster().register("worker", runnable=_leaf(calls))

    async def orchestrate(ctx):
        async def root(_deps):
            return await ctx.agent(prompt="root", agent_type="worker")

        async def child(deps):
            return await ctx.agent(
                prompt=f"child:{json.dumps(deps, ensure_ascii=False)}",
                agent_type="worker",
            )

        return await ctx.dag(
            [
                DagNode(id="root", deps=[], run=root),
                DagNode(id="child", deps=["root"], run=child),
            ]
        )

    journal = InMemoryJournalStore()
    first = await run_workflow(orchestrate, roster=roster, journal=journal)
    assert first["root"] == "out:root"
    assert "out:root" in first["child"]
    assert len(calls) == 2

    second = await run_workflow(orchestrate, roster=roster, journal=journal)
    assert second == first
    assert len(calls) == 2, "the journal must serve the repeat run with zero leaves"


@pytest.mark.asyncio
async def test_failed_dag_node_skips_dependents() -> None:
    async def orchestrate(ctx):
        async def boom(_deps):
            raise RuntimeError("boom")

        async def downstream(_deps):
            return await ctx.agent(prompt="should not run", agent_type="worker")

        return await ctx.dag(
            [
                DagNode(id="root", deps=[], run=boom),
                DagNode(id="child", deps=["root"], run=downstream),
            ]
        )

    roster = Roster().register("worker", runnable=_leaf([]))
    result = await run_workflow(orchestrate, roster=roster)
    assert result == {"root": None, "child": None}
