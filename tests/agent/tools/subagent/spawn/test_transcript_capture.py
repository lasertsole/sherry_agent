"""The child transcript tail is captured before its checkpoint is pruned.

``sessions_messages`` reads the child's checkpoint, which cleanup/pruning (or a
different checkpointer database) can leave empty. This bounded tail is the
fallback that must survive the process, so it is written to the run record and
made durable immediately.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.tools.subagent.spawn import lifecycle
from agent.tools.subagent.types.registry import SubagentRunRecord

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

RUN_ID = "run-capture-1"
CHILD_KEY = "agent:main:subagent:capture"


def _run() -> SubagentRunRecord:
    return SubagentRunRecord(
        run_id=RUN_ID,
        child_session_key=CHILD_KEY,
        requester_session_key="agent:main:session:owner",
        task="probe",
    )


class _ChildAgent:
    def __init__(self, messages: list[Any]) -> None:
        self.messages = messages
        self.calls: list[dict[str, Any]] = []

    async def aget_state(self, config: Any = None) -> SimpleNamespace:
        self.calls.append({"config": config})
        return SimpleNamespace(values={"messages": self.messages})


@pytest.fixture
def captured(monkeypatch: pytest.MonkeyPatch) -> list[SubagentRunRecord]:
    """Patch the registry writes; return what would have been persisted."""
    recorded: list[SubagentRunRecord] = []

    def _update_run(run_id: str, **updates: Any) -> SubagentRunRecord:
        assert run_id == RUN_ID
        run = _run()
        for key, value in updates.items():
            setattr(run, key, value)
        return run

    monkeypatch.setattr(lifecycle, "update_run", _update_run)
    monkeypatch.setattr(lifecycle, "set_run_durable", lambda run: recorded.append(run))
    import pub.func as pub_func

    monkeypatch.setattr(
        pub_func, "build_agent_config", lambda session_key, **kw: {"configurable": {}}
    )
    return recorded


@pytest.mark.asyncio
async def test_the_tail_records_roles_tool_calls_and_tool_results(captured):
    messages = [
        HumanMessage(content="fix the bug"),
        AIMessage(
            content="running tests",
            tool_calls=[{"name": "terminal", "args": {"command": "pytest -q"}, "id": "c1"}],
        ),
        ToolMessage(content="3 passed", tool_call_id="c1"),
        AIMessage(content="done"),
    ]

    await lifecycle._capture_child_transcript(_run(), _ChildAgent(messages))

    assert len(captured) == 1
    tail = captured[0].completion.transcript_tail
    assert [item["type"] for item in tail] == ["human", "ai", "tool", "ai"]
    assert tail[0]["content"] == "fix the bug"
    assert tail[1]["tool_calls"] == [
        {"name": "terminal", "args": {"command": "pytest -q"}, "id": "c1"}
    ]
    assert tail[2]["tool_call_id"] == "c1"
    assert captured[0].run_id == RUN_ID


@pytest.mark.asyncio
async def test_the_tail_keeps_only_the_newest_messages(captured):
    messages = [HumanMessage(content=f"m{i}") for i in range(40)]

    await lifecycle._capture_child_transcript(_run(), _ChildAgent(messages))

    tail = captured[0].completion.transcript_tail
    assert len(tail) == lifecycle._TRANSCRIPT_TAIL_MAX_MESSAGES
    assert tail[0]["content"] == "m10"
    assert tail[-1]["content"] == "m39"


@pytest.mark.asyncio
async def test_an_oversized_message_is_truncated(captured):
    messages = [HumanMessage(content="x" * 20_000)]

    await lifecycle._capture_child_transcript(_run(), _ChildAgent(messages))

    tail = captured[0].completion.transcript_tail
    assert len(tail) == 1
    assert len(tail[0]["content"]) == lifecycle._TRANSCRIPT_MESSAGE_MAX_CHARS


@pytest.mark.asyncio
async def test_a_failed_state_read_is_open(captured):
    class _Broken:
        async def aget_state(self, config: Any = None) -> SimpleNamespace:
            raise RuntimeError("checkpoint gone")

    await lifecycle._capture_child_transcript(_run(), _Broken())

    assert captured == []


@pytest.mark.asyncio
async def test_a_missing_child_agent_writes_nothing(captured):
    await lifecycle._capture_child_transcript(_run(), None)

    assert captured == []


@pytest.mark.asyncio
async def test_an_empty_transcript_writes_nothing(captured):
    await lifecycle._capture_child_transcript(_run(), _ChildAgent([]))

    assert captured == []


@pytest.mark.asyncio
async def test_the_tail_merges_into_the_current_record(monkeypatch):
    """The run object handed to release may predate a steer/finalizer write."""
    import pub.func as pub_func

    from agent.tools.subagent.types.registry import CompletionState

    current = SubagentRunRecord(
        run_id=RUN_ID,
        child_session_key=CHILD_KEY,
        requester_session_key="agent:main:session:owner",
        task="probe",
        completion=CompletionState(result_text="fresh result"),
    )
    completions: list[CompletionState] = []

    def _update_run(run_id: str, **updates: Any) -> SubagentRunRecord:
        completions.append(updates["completion"])
        return current.model_copy(update=updates)

    monkeypatch.setattr(lifecycle, "get_run", lambda run_id: current)
    monkeypatch.setattr(lifecycle, "update_run", _update_run)
    monkeypatch.setattr(lifecycle, "set_run_durable", lambda run: None)
    monkeypatch.setattr(
        pub_func, "build_agent_config", lambda session_key, **kw: {"configurable": {}}
    )

    await lifecycle._capture_child_transcript(_run(), _ChildAgent([HumanMessage(content="hello")]))

    assert completions[0].result_text == "fresh result"
    assert completions[0].transcript_tail[0]["content"] == "hello"
