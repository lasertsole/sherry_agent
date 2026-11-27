"""Regression: the orchestration gate buckets by the SESSION the state carries.

Measured on the DeepSWE L1 rerun of 2026-11-27: the product invokes its graph
with ``input={"session_id": …}`` and ``config={"configurable": {"thread_id":
<int hash>}}`` — never ``context=`` — so ``runtime.context`` is None. A gate that
read only the context put EVERY task in the ``""`` bucket: per-task reads
answered zeros (a false "not compliant") and the process-wide unlock the
per-session fix was meant to remove came back.

These tests drive a REAL LangGraph ``SafeToolNode`` so the runtime shape is the
product's: the session must resolve from graph state, and a thread_id (an int
hash, not a session id) must never become a bucket key.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, StateGraph

from agent.safe_tool_node import SafeToolNode
from evals import external_bench
from evals.external_bench import _ORCHESTRATION_UNLOCK

pytestmark = [pytest.mark.regression, pytest.mark.timeout(60)]

_TASK_A = "evals-deepswe-actionlint-aaaaaa"
_TASK_B = "evals-deepswe-bandit-bbbbbb"


@tool
def todowrite(items: str = "") -> str:  # noqa: ARG001 — the NAME is what the gate reads
    """Plan the work."""
    return "planned"


@tool
def taskflow_flow(action: str = "plan") -> str:  # noqa: ARG001
    """Plan the flow."""
    return "flow"


@tool
def terminal(command: str = "") -> str:  # noqa: ARG001
    """Run a command."""
    return "ran"


def _graph() -> Any:
    node = SafeToolNode([todowrite, taskflow_flow, terminal])
    builder = StateGraph(dict)
    builder.add_node("tools", node)
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    return builder.compile()


async def _invoke(
    session_id: str, name: str, *, thread_id: int = 7, args: dict[str, Any] | None = None
) -> Any:
    """One real graph turn: state carries ``session_id``, config a thread_id."""
    message = AIMessage(
        content="",
        tool_calls=[{"name": name, "args": args or {}, "id": f"{session_id}-{name}"}],
    )
    out = await _graph().ainvoke(
        {"messages": [message], "session_id": session_id},
        config={"configurable": {"thread_id": thread_id}},
    )
    return out["messages"][-1]


@pytest.fixture()
def gate(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _fake_arun_one(
        self: Any, call: Any, input_type: Any, tool_runtime: Any
    ) -> ToolMessage:
        return ToolMessage(
            content="executed",
            tool_call_id=str((call or {}).get("id") or ""),
            name=str((call or {}).get("name") or ""),
        )

    monkeypatch.setattr(SafeToolNode, "_arun_one", _fake_arun_one)
    monkeypatch.setattr(external_bench, "_GATE_INSTALLED", False)
    monkeypatch.setattr(external_bench, "_GATE_STATE", {})
    external_bench.install_orchestration_gate()


@pytest.mark.asyncio
async def test_counters_land_in_the_state_session(gate: None) -> None:
    await _invoke(_TASK_A, "todowrite")

    assert external_bench.orchestration_state(_TASK_A)["todo"] == 1
    # The old behaviour (context-only) answered zeros here, which the report
    # read as "the agent never planned".
    assert external_bench.orchestration_state("")["todo"] == 0


@pytest.mark.asyncio
async def test_thread_id_is_never_used_as_a_bucket(gate: None) -> None:
    await _invoke(_TASK_A, "todowrite", thread_id=12345)

    assert "12345" not in external_bench._GATE_STATE
    assert "12345" not in external_bench.orchestration_state()
    assert set(external_bench._GATE_STATE) == {_TASK_A}


@pytest.mark.asyncio
async def test_a_later_task_is_still_gated(gate: None) -> None:
    await _invoke(_TASK_A, "todowrite")

    blocked = await _invoke(_TASK_B, "terminal")

    assert blocked.content == _ORCHESTRATION_UNLOCK
    assert external_bench.orchestration_state(_TASK_B)["blocked"] == 1


@pytest.mark.asyncio
async def test_planning_unlocks_only_its_own_session(gate: None) -> None:
    await _invoke(_TASK_A, "todowrite")
    await _invoke(_TASK_A, "taskflow_flow", args={"action": "plan"})

    unlocked = await _invoke(_TASK_A, "terminal")
    blocked = await _invoke(_TASK_B, "terminal")

    assert unlocked.content == "executed"
    assert blocked.content == _ORCHESTRATION_UNLOCK
    assert external_bench.orchestration_state(_TASK_A)["plan_calls"] == 1
