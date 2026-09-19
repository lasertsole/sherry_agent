"""Live-network e2e: persistent operator-scoped tool approvals on a REAL graph (P2-2).

LIVE-NETWORK, RUN EXPLICITLY. These tests drive a real ``create_agent`` graph
whose model is the live ``build_main_llm()`` resolved from ``.env``, with the
real ``HumanInTheLoop`` middleware, a real ``ToolApprovalStore`` pointed at a
per-test tmp sandbox, and a real ``MemorySaver`` checkpointer. They do not
touch MesMemory, session dirs, or any repo-tracked file. Run with::

    uv run --no-sync pytest -m llm_e2e tests/full/test_approval_persistence_e2e.py -v

Marker policy: tagged ``llm_e2e``, so a bare ``pytest`` run deselects it via the
``pyproject.toml`` addopts and the hermetic CI gate never collects it;
``tests/run_tests_split.py`` additionally ``--ignore``s ``tests/full/`` outright.
The tag is what keeps the file restricted to explicit invocation.

Harness shape mirrors ``tests/full/test_hitl_real_graph.py`` (real compiled
graph + real ``langgraph.types.interrupt`` propagation), with two differences:
the model is the live main LLM instead of a stub, and a probe tool is
registered under ``interrupted_tools`` so the approval gate is the real
``InterruptOnApprovalHandler`` path.

Coverage:

1. approval decision survives a simulated restart — real interrupt → resume
   ``approve`` → the decision lands in the JSON store; a fresh store +
   middleware + graph (in-memory approval cache cleared) runs the same tool +
   args without re-prompting and the tool actually executes.
2. no operator → auto-deny — ``operator_scope(None)`` around the real graph:
   the approval-requiring tool call is replaced by an error ``ToolMessage``
   carrying ``NO_OPERATOR_MESSAGE`` and the graph never parks on an interrupt.
3. operator isolation — operator ``alice`` approves the exact call; operator
   ``bob`` (same store file, cleared in-memory cache) still gets a real
   pending interrupt for the same tool + args.

Scenario 4 (reducer contract on the real chain — P1-9 in-place same-id tagging
and ``ToolCallNormalize`` full-list replacement) is NOT duplicated here; it is
covered by existing tests, referenced instead:

- P1-9 same-id in-place tag on the real graph:
  ``tests/full/test_context_governance_e2e.py`` (human-message eviction case)
- ``ToolCallNormalize`` sentinel full-list replacement:
  ``tests/agent/core/test_state_messages_reducer.py::TestFullListReplacement``
- standard ``add_messages`` reducer contract (same id / tombstone / reset):
  ``tests/agent/core/test_state_messages_reducer.py::TestStandardAddMessagesContract``

Every test purges the session registers it touched and relies on ``tmp_path``
for the approval sandbox; the checkpointer is process-local ``MemorySaver``.
Each graph invocation is retried once on a transport-level failure (LLM
network hiccup) before failing loudly.
"""

from __future__ import annotations

import contextlib
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command
from loguru import logger

from agent.middlewares.humanInTheLoop import HITLConfig, HumanInTheLoop
from agent.middlewares.humanInTheLoop.approval_scope import NO_OPERATOR_MESSAGE, operator_scope
from agent.middlewares.humanInTheLoop.approval_store import ApprovalVerdict, ToolApprovalStore
from agent.middlewares.humanInTheLoop.types import BLOCKED_MESSAGE
from models import build_main_llm
from runtime import clear_all_register_sessions, state_register_mem

pytestmark = [pytest.mark.llm_e2e, pytest.mark.timeout(900)]

TOOL_NAME = "approval_probe"
PROBE_ARGS = {"probe": "alpha"}

TEST_PROMPT = (
    f"Use the `{TOOL_NAME}` tool exactly once with probe='alpha'. "
    "Do not call any other tool. After the tool result arrives, reply with the single word DONE."
)


@tool(TOOL_NAME)
def approval_probe(probe: str) -> str:
    """Return the probe value (HITL persistent-approval e2e probe)."""
    return f"probe-result:{probe}"


class _HarnessState(AgentState):
    """Agent state carrying the session id expected by the HITL middleware."""

    session_id: str


# ─────────────────────────────────────────────────────────────────────────────
# Harness helpers
# ─────────────────────────────────────────────────────────────────────────────
def _sid(tag: str) -> str:
    return f"approval-e2e-{tag}-{uuid.uuid4().hex[:8]}"


def _thread_config(tag: str) -> dict[str, Any]:
    return {"configurable": {"thread_id": f"approval-e2e-{tag}-{uuid.uuid4().hex[:8]}"}}


def _middleware(path: Path) -> HumanInTheLoop:
    return HumanInTheLoop(
        HITLConfig(interrupted_tools={TOOL_NAME: True}),
        approval_store=ToolApprovalStore(path),
    )


def _graph(hitl: HumanInTheLoop) -> Any:
    return create_agent(
        model=build_main_llm(),
        state_schema=_HarnessState,
        checkpointer=MemorySaver(),
        tools=[approval_probe],
        middleware=[hitl],
    )


def _invoke(graph: Any, payload: Any, config: dict[str, Any]) -> Any:
    """Invoke the real graph, retrying once on a transport-level LLM failure."""
    try:
        return graph.invoke(payload, config)
    except Exception as exc:
        logger.warning("approval e2e invoke failed ({}); retrying once", exc)
        time.sleep(3)
        return graph.invoke(payload, config)


def _pending_tasks(graph: Any, config: dict[str, Any]) -> list[Any]:
    return list(graph.get_state(config).tasks or [])


def _tool_results(graph: Any, config: dict[str, Any]) -> list[ToolMessage]:
    messages = (graph.get_state(config).values or {}).get("messages", [])
    return [
        message
        for message in messages
        if isinstance(message, ToolMessage) and getattr(message, "name", None) == TOOL_NAME
    ]


def _clear_memory_approval(session_id: str) -> None:
    """Drop the in-memory session approval cache (simulates a process restart)."""
    state_register_mem.delete_state(session_id, f"hitl:tool_approved:{TOOL_NAME}")


def _purge(session_id: str) -> None:
    """Remove every register entry this session created (best effort)."""
    with contextlib.suppress(Exception):
        clear_all_register_sessions(session_id=session_id, clear_persistent_states=True)


def _initial_payload(session_id: str) -> dict[str, Any]:
    return {"messages": [HumanMessage(content=TEST_PROMPT)], "session_id": session_id}


# ─────────────────────────────────────────────────────────────────────────────
# 1) The approval decision survives a store/middleware restart
# ─────────────────────────────────────────────────────────────────────────────
def test_approval_decision_survives_a_store_restart(tmp_path):
    path = tmp_path / "approvals.json"
    session_id = _sid("restart")
    config = _thread_config("restart")
    try:
        # Phase 1 — real interrupt, human approves, decision is persisted.
        graph = _graph(_middleware(path))
        _invoke(graph, _initial_payload(session_id), config)

        tasks = _pending_tasks(graph, config)
        assert len(tasks) == 1, f"expected exactly 1 pending approval, got {len(tasks)}"
        payload = tasks[0].interrupts[0].value
        assert payload["action_requests"][0]["name"] == TOOL_NAME
        assert payload["action_requests"][0]["args"] == PROBE_ARGS

        _invoke(graph, Command(resume={"decisions": [{"type": "approve"}]}), config)
        results = _tool_results(graph, config)
        assert any("probe-result:alpha" in message.content for message in results), (
            "the approved tool must execute after the resume"
        )

        persisted = ToolApprovalStore(path).evaluate(
            TOOL_NAME, PROBE_ARGS, session_id, operator=session_id
        )
        assert persisted.verdict is ApprovalVerdict.ALLOW

        # Phase 2 — simulated restart: fresh store/middleware/graph, memory cache cleared.
        _clear_memory_approval(session_id)
        restarted = _graph(_middleware(path))
        config2 = _thread_config("restart")
        _invoke(restarted, _initial_payload(session_id), config2)

        assert not _pending_tasks(restarted, config2), (
            "a persisted approval must skip the re-prompt after a restart"
        )
        results2 = _tool_results(restarted, config2)
        assert any("probe-result:alpha" in message.content for message in results2), (
            "the persisted approval must let the tool execute without a new decision"
        )
    finally:
        _purge(session_id)


# ─────────────────────────────────────────────────────────────────────────────
# 2) No operator in scope → the approval gate auto-denies (never interrupts)
# ─────────────────────────────────────────────────────────────────────────────
def test_no_operator_auto_denies_without_interrupt(tmp_path):
    path = tmp_path / "approvals.json"
    session_id = _sid("headless")
    config = _thread_config("headless")
    try:
        graph = _graph(_middleware(path))
        with operator_scope(None):
            _invoke(graph, _initial_payload(session_id), config)

        assert not _pending_tasks(graph, config), (
            "with no operator the gate must auto-deny instead of parking the graph"
        )
        denials = _tool_results(graph, config)
        assert denials, "expected an artificial denial ToolMessage for the probe call"
        assert NO_OPERATOR_MESSAGE in denials[0].content
        assert BLOCKED_MESSAGE in denials[0].content

        record = ToolApprovalStore(path).evaluate(
            TOOL_NAME, PROBE_ARGS, session_id, operator=session_id
        )
        assert record.verdict is ApprovalVerdict.UNKNOWN, (
            "an auto-denied call must not persist any approval"
        )
    finally:
        _purge(session_id)


# ─────────────────────────────────────────────────────────────────────────────
# 3) Operator isolation on the real graph: alice's approval never authorizes bob
# ─────────────────────────────────────────────────────────────────────────────
def test_operator_decisions_are_isolated_on_the_real_graph(tmp_path):
    path = tmp_path / "approvals.json"
    session_id = _sid("isolation")
    try:
        # Alice approves the exact tool + args call.
        alice_graph = _graph(_middleware(path))
        alice_config = _thread_config("isolation")
        with operator_scope("alice"):
            _invoke(alice_graph, _initial_payload(session_id), alice_config)
        assert len(_pending_tasks(alice_graph, alice_config)) == 1
        with operator_scope("alice"):
            _invoke(
                alice_graph,
                Command(resume={"decisions": [{"type": "approve"}]}),
                alice_config,
            )
        alice_record = ToolApprovalStore(path).evaluate(
            TOOL_NAME, PROBE_ARGS, session_id, operator="alice"
        )
        assert alice_record.verdict is ApprovalVerdict.ALLOW

        # Bob — same store file and call, but a different operator — must still be asked.
        _clear_memory_approval(session_id)
        bob_graph = _graph(_middleware(path))
        bob_config = _thread_config("isolation")
        with operator_scope("bob"):
            _invoke(bob_graph, _initial_payload(session_id), bob_config)
        bob_tasks = _pending_tasks(bob_graph, bob_config)
        assert len(bob_tasks) == 1, (
            "operator bob must not inherit operator alice's persisted approval"
        )
        assert bob_tasks[0].interrupts[0].value["action_requests"][0]["name"] == TOOL_NAME

        # Resolve bob's interrupt (reject) so the scenario leaves no pending work.
        with operator_scope("bob"):
            _invoke(
                bob_graph,
                Command(resume={"decisions": [{"type": "reject", "message": "bob rejects"}]}),
                bob_config,
            )
        assert not _pending_tasks(bob_graph, bob_config)
        bob_record = ToolApprovalStore(path).evaluate(
            TOOL_NAME, PROBE_ARGS, session_id, operator="bob"
        )
        assert bob_record.verdict is ApprovalVerdict.DENY
        still_alice = ToolApprovalStore(path).evaluate(
            TOOL_NAME, PROBE_ARGS, session_id, operator="alice"
        )
        assert still_alice.verdict is ApprovalVerdict.ALLOW
    finally:
        _purge(session_id)
