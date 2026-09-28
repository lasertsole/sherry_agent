"""The three defects retired with the code-quality plan, each pinned behaviourally.

Nothing here asserts source text: every case fails against the code as it was.

1. ``get_pending_interrupt`` stops at the FIRST task of the superstep — a graph
   that resumed several parallel branches carries one task each, so an approval
   sitting on a later task produced no dialog at all.
2. ``process_heartbeat_task`` calls ``agent.invoke`` inside a coroutine, running
   the whole heartbeat turn in the event-loop thread and stalling every other
   session's stream for its duration.
3. ``_run_semantic_search`` bridges sync→async with ``asyncio.run``, creating a
   disposable loop that conflicts with the cached AsyncOpenAI/httpx clients the
   gateway's loop owns (the deadlock the sibling path in the same file documents)
   and raising outright when a loop is already running.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage

from agent.tools.message_search import _run_semantic_search
from server.service import heartbeat as heartbeat_service
from server.service import messages as messages_service

pytestmark = [pytest.mark.unit]


# ---------------------------------------------------------------------------
# 1. The interrupt scan walks every task of the superstep
# ---------------------------------------------------------------------------


class _Interrupt:
    def __init__(self, value: Any) -> None:
        self.value = value


class _Task:
    def __init__(self, *interrupts: _Interrupt) -> None:
        self.interrupts = list(interrupts)


class _Agent:
    """Minimal graph double: only ``aget_state`` is read."""

    def __init__(self, tasks: list[_Task]) -> None:
        self._tasks = tasks

    async def aget_state(self, config: Any = None) -> Any:
        return SimpleNamespace(tasks=self._tasks)


def _approval(tool_name: str) -> dict:
    return {
        "action_requests": [{"name": tool_name, "args": {"path": "/tmp/x"}, "description": "d"}],
        "review_configs": [{"allowed_decisions": ["approve", "reject"]}],
    }


def _patch_state(monkeypatch: pytest.MonkeyPatch, tasks: list[_Task]) -> None:
    async def _built_agent() -> _Agent:
        return _Agent(tasks)

    monkeypatch.setattr(messages_service, "built_agent", _built_agent)


@pytest.mark.asyncio
async def test_interrupt_on_a_later_task_is_found(monkeypatch):
    # Task 1 is a branch that never interrupted; the approval lives on task 2.
    _patch_state(monkeypatch, [_Task(), _Task(_Interrupt(_approval("write_file")))])

    pending = await messages_service.get_pending_interrupt("s1")

    assert pending is not None, "the approval on the second task was dropped"
    assert pending["tool_name"] == "write_file"
    assert pending["allowed_decisions"] == ["approve", "reject"]


@pytest.mark.asyncio
async def test_interrupt_scan_skips_empty_payloads_before_finding_one(monkeypatch):
    _patch_state(
        monkeypatch,
        [
            _Task(_Interrupt(None)),  # a bare resume marker
            _Task(_Interrupt({"action_requests": []})),  # nothing to approve
            _Task(_Interrupt(_approval("terminal"))),
        ],
    )

    pending = await messages_service.get_pending_interrupt("s1")

    assert pending is not None and pending["tool_name"] == "terminal", pending


@pytest.mark.asyncio
async def test_no_interrupt_anywhere_is_still_none(monkeypatch):
    _patch_state(monkeypatch, [_Task(), _Task(_Interrupt({"action_requests": []}))])

    assert await messages_service.get_pending_interrupt("s1") is None


@pytest.mark.asyncio
async def test_the_first_approval_wins_when_several_are_pending(monkeypatch):
    _patch_state(
        monkeypatch,
        [_Task(_Interrupt(_approval("first"))), _Task(_Interrupt(_approval("second")))],
    )

    pending = await messages_service.get_pending_interrupt("s1")

    assert pending is not None and pending["tool_name"] == "first", pending


# ---------------------------------------------------------------------------
# 2. The heartbeat finishes its turn without blocking the loop
# ---------------------------------------------------------------------------


class _BlockingAgent:
    """Fails loudly if the heartbeat reaches for the sync invoke."""

    def __init__(self) -> None:
        self.awaited = False

    def invoke(self, **_kwargs: Any) -> Any:
        raise AssertionError("agent.invoke() runs the turn inside the event loop")

    async def ainvoke(self, **_kwargs: Any) -> Any:
        self.awaited = True
        return {"messages": [AIMessage(content="heartbeat ok")]}


@pytest.mark.asyncio
async def test_heartbeat_task_awaits_the_graph(monkeypatch):
    agent = _BlockingAgent()

    async def _noop(*_args: Any, **_kwargs: Any) -> None:
        return None

    monkeypatch.setattr(heartbeat_service, "_get_heartbeat_agent", lambda: agent)
    monkeypatch.setattr(heartbeat_service, "ensure_workspace_system_files", lambda: None)
    monkeypatch.setattr(heartbeat_service, "build_system_prompt", lambda **_kw: "prompt")
    monkeypatch.setattr(heartbeat_service, "_mark_executed_tasks_completed", _noop)
    monkeypatch.setattr(heartbeat_service, "push_heartbeat_updated", _noop)
    monkeypatch.setattr(heartbeat_service, "push_heartbeat_notification", _noop)

    result = await heartbeat_service.process_heartbeat_task("tidy the desk")

    assert result == "heartbeat ok"
    assert agent.awaited, "the heartbeat never awaited the graph"


# ---------------------------------------------------------------------------
# 3. Semantic search works from inside a running loop
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_semantic_search_bridges_a_running_loop(monkeypatch):
    async def _fake_search(query: str, session_id: str | None, limit: int) -> list[dict]:
        assert (query, session_id, limit) == ("q", "s1", 5)
        return [
            {
                "score": 0.91,
                "session_id": "s1",
                "turn_num": 3,
                "role": "ai",
                "content": "the matching message",
            }
        ]

    # The helper imports it lazily, so patching the module attribute is enough.
    monkeypatch.setattr("context_engine.embeddings.semantic_search", _fake_search)

    # asyncio.run() here raises "cannot be called from a running event loop";
    # run_async hands the coroutine to a disposable thread instead.
    out = _run_semantic_search("q", "s1", 5)

    assert "0.91" in out and "the matching message" in out, out


@pytest.mark.asyncio
async def test_semantic_search_explains_an_empty_index(monkeypatch):
    async def _empty_search(query: str, session_id: str | None, limit: int) -> list[dict]:
        return []

    monkeypatch.setattr("context_engine.embeddings.semantic_search", _empty_search)

    assert "No semantically similar messages" in _run_semantic_search("q", None, 5)
