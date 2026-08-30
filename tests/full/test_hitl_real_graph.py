"""Real-graph HITL integration test (live ``interrupt()`` propagation).

PURPOSE
-------
The existing ``tests/unit/test_hitl_integration.py`` verifies the
middleware's *contract* (``check_command`` → ESCALATE, ``get_pending_interrupt``
shape) but never drives a **real LangGraph execution** that calls the real
``langgraph.types.interrupt`` from within ``after_model``.

This harness answers the single most important question: **does HITL's
``interrupt()`` genuinely fire and persist a pending interrupt when the real
graph runs?**

FIX (empirical, in-process real graph, ``uv run pytest``)
---------------------------------------------------------
It now DOES. The ``interrupt()`` call in ``after_model`` (``core.py``) is
wrapped in ``try/except Exception``. ``langgraph.types.interrupt`` raises
``GraphInterrupt`` (MRO: ``['GraphInterrupt', 'GraphBubbleUp', 'Exception',
'BaseException', 'object']``), which **inherits from ``Exception``**, so the
generic ``except Exception`` used to swallow it into a hard-denial artificial
``ToolMessage``:

    content = "Approval interrupt failed. The user has NOT consented ..."

The fix adds a ``except GraphInterrupt: raise`` branch **before** the generic
``except Exception`` (in both interrupt call sites). A genuine HITL interrupt
now propagates out of ``after_model`` so LangGraph persists it to the
checkpointer as a **pending interrupt** (``tasks``), which is what lets the
frontend HITL dialog fire. Genuine, non-interrupt errors still fall through to
the graceful deny ``ToolMessage``.

These tests lock the *corrected* behavior: the dangerous terminal call persists
a pending interrupt, resume with a decide/reject clears it and injects the deny
message, and only a genuine non-interrupt error produces the fallback message.
"""

from __future__ import annotations

from typing import ClassVar

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

import pytest

from agent.middlewares.humanInTheLoop import HITLConfig, HumanInTheLoop


pytestmark = pytest.mark.unit


# ─────────────────────────────────────────────────────────────────────────────
# Deterministic stub model — emits a single ``terminal`` tool call then idles.
# ``create_agent`` re-instantiates the model, so the counter is class-level.
# ─────────────────────────────────────────────────────────────────────────────
class StubTerminalModel(BaseChatModel):
    """Returns an AIMessage requesting ``git reset --hard`` exactly once.

    On the *second* invocation the tool call is dropped so that, if an interrupt
    is swallowed and the agent loops, we can detect the swallow (the run would
    complete with a ``terminal`` error ToolMessage and no pending task).
    """

    calls: ClassVar[int] = 0

    @property
    def _llm_type(self) -> str:
        return "stub-terminal-hitl"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        type(self).calls += 1
        if type(self).calls == 1:
            msg = AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "terminal",
                        "args": {"command": "git reset --hard"},
                        "id": "call_stub_terminal_1",
                        "type": "tool_call",
                    }
                ],
            )
        else:
            msg = AIMessage(content="Done (no further tool calls).")
        return ChatResult(generations=[ChatGeneration(message=msg)])


# ─────────────────────────────────────────────────────────────────────────────
# Shared graph fixture — real create_agent + real HITL middleware
# ─────────────────────────────────────────────────────────────────────────────
def build_graph():
    from langchain.agents import create_agent
    from langchain.agents.middleware import AgentState

    class HarnessState(AgentState):
        session_id: str

    StubTerminalModel.calls = 0  # reset class counter between graphs/tests

    hitl = HumanInTheLoop(HITLConfig())  # default: mode=SMART, smart_approval_llm=None

    graph = create_agent(
        model=StubTerminalModel(),
        state_schema=HarnessState,
        checkpointer=MemorySaver(),
        tools=[],  # after_model intercepts BEFORE the tool node runs
        middleware=[hitl],
    )
    return graph, hitl, HarnessState


TEST_PROMPT = "Please run git reset --hard on this repo."


# ─────────────────────────────────────────────────────────────────────────────
# 1) CORE EMPIRICAL FINDING (AFTER FIX): the interrupt PROPAGATES and is
#    persisted to the checkpointer as a pending task. The run returns normally
#    but ``graph.get_state().tasks`` now reports exactly one pending interrupt
#    carrying the full HITLRequest payload — this is what triggers the frontend
#    HITL dialog.
# ─────────────────────────────────────────────────────────────────────────────
def test_terminal_interrupt_propagates_and_persists_pending_task():
    """The real interrupt must propagate and persist; tasks == 1."""
    graph, _hitl, _state_schema = build_graph()

    config = {"configurable": {"thread_id": "hitl-fix-1"}}
    out = graph.invoke(
        {"messages": [HumanMessage(content=TEST_PROMPT)], "session_id": "hitl-fix-1"},
        config,
    )

    # The interrupt is NOT swallowed into a deny ToolMessage anymore:
    msgs = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    swallowed = [
        m
        for m in msgs
        if getattr(m, "name", None) == "terminal"
        and "Approval interrupt failed" in (m.content or "")
    ]
    assert not swallowed, (
        "The interrupt must propagate to the checkpointer, not be swallowed "
        "into an 'Approval interrupt failed' ToolMessage."
    )

    # A genuine pending interrupt must now be persisted:
    state = graph.get_state(config)
    tasks = list(state.tasks or [])
    assert len(tasks) == 1, (
        f"Expected exactly 1 pending interrupt task, got {len(tasks)}. "
        "The GraphInterrupt is being swallowed somewhere upstream."
    )
    task = tasks[0]
    assert task.name == "HumanInTheLoop.after_model"
    interrupt_payload = task.interrupts[0].value if task.interrupts else None
    assert interrupt_payload, "Pending task should carry a HITLRequest interrupt value"
    assert interrupt_payload.get("action_requests"), (
        "Interrupt payload must include action_requests for the frontend dialog"
    )
    first_action = interrupt_payload["action_requests"][0]
    assert first_action["name"] == "terminal", (
        "The pending interrupt must reference the dangerous terminal command"
    )
    assert "git reset --hard" in first_action["args"]["command"]


# ─────────────────────────────────────────────────────────────────────────────
# 2) The terminal tool_call is NOT stripped/zeroed merely by the swallow — it
#    remains in the pending interrupt so a decided resume can act on it.
# ─────────────────────────────────────────────────────────────────────────────
def test_terminal_tool_call_is_not_erased_by_swallow():
    graph, _hitl, _state_schema = build_graph()

    config = {"configurable": {"thread_id": "hitl-fix-2"}}
    graph.invoke(
        {"messages": [HumanMessage(content=TEST_PROMPT)], "session_id": "hitl-fix-2"},
        config,
    )

    state = graph.get_state(config)
    tasks = list(state.tasks or [])
    assert len(tasks) == 1
    payload = tasks[0].interrupts[0].value
    actions = payload.get("action_requests", [])
    assert any(a["name"] == "terminal" for a in actions), (
        "The intercepted terminal call should live in the pending interrupt, "
        "not be silently erased."
    )


# ─────────────────────────────────────────────────────────────────────────────
# 3) A resume with a reject decision must resolve the pending interrupt and
#    inject the deny ToolMessage. tasks returns to empty afterwards.
# ─────────────────────────────────────────────────────────────────────────────
def test_command_resume_reject_resolves_interrupt_and_denies():
    graph, _hitl, _state_schema = build_graph()

    config = {"configurable": {"thread_id": "hitl-fix-3"}}
    graph.invoke(
        {"messages": [HumanMessage(content=TEST_PROMPT)], "session_id": "hitl-fix-3"},
        config,
    )

    state = graph.get_state(config)
    assert len(list(state.tasks or [])) == 1, "Expected a pending interrupt before resume"

    # Resume exactly as the transport would after a user clicks "Reject":
    resume = Command(resume={"decisions": [{"type": "reject", "message": "No"}]})
    graph.invoke(resume, config)

    state2 = graph.get_state(config)
    assert not list(state2.tasks or []), (
        "After a decided resume the pending interrupt should be resolved."
    )

    terminal_err = [
        m
        for m in (state2.values or {}).get("messages", [])
        if getattr(m, "name", None) == "terminal" and getattr(m, "status", None) == "error"
    ]
    assert terminal_err, "Expected a deny ToolMessage after the reject decision"
    assert "No" in terminal_err[0].content, "The deny message should reflect the user's rejection."
