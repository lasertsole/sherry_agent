"""Child-mode context eviction: in-memory tool-result clipping + graph e2e.

The child pipeline registers the same ``ContextEvictionMiddleware`` with
``human_eviction=False, evict_to_file=False``. It must bound oversized tool
results (the 20k generic net) without handing the child a file pointer it
cannot open, and the main-chain fail-open path must fall back to the same
in-memory clip instead of letting the raw payload through.
"""

from __future__ import annotations

from typing import ClassVar

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool
from langgraph.checkpoint.memory import MemorySaver

from agent.middlewares.context_eviction import ContextEvictionMiddleware
from agent.middlewares.context_eviction import core as eviction_core
from pub.func.message.eviction import CLIPPED_PREFIX, hard_clip_tool_result

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _huge_message(name: str = "big_output") -> ToolMessage:
    return ToolMessage(
        content="line\n" * 10_000,
        name=name,
        tool_call_id="call-1",
    )


class _ScriptedModel(BaseChatModel):
    """One scripted tool call, then a normal answer."""

    calls: ClassVar[int] = 0

    @property
    def _llm_type(self) -> str:
        return "stub-child-eviction-e2e"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        type(self).calls += 1
        if type(self).calls == 1:
            msg = AIMessage(
                content="",
                tool_calls=[{"name": "big_output", "args": {}, "id": "call-1"}],
            )
        else:
            msg = AIMessage(content="Done.")
        return ChatResult(generations=[ChatGeneration(message=msg)])


class _HarnessState(AgentState):
    session_id: str


@tool
def big_output() -> str:
    """Return a payload well over the eviction threshold."""
    return "line\n" * 10_000


def test_disk_free_mode_hard_clips_without_a_file_pointer(monkeypatch, tmp_path):
    monkeypatch.setattr("pub.func.message.eviction.SESSIONS_DIR", tmp_path)
    middleware = ContextEvictionMiddleware(human_eviction=False, evict_to_file=False)

    result = middleware._maybe_evict(_huge_message(), "child-1")

    assert result.content.startswith(CLIPPED_PREFIX)
    assert "evicted to:" not in result.content
    assert "re-run with a narrower range" in result.content
    assert len(result.content) < len(_huge_message().content)
    assert not (tmp_path / "child-1" / "evicted").exists()


def test_child_mode_evicts_to_its_own_session_directory(monkeypatch, tmp_path):
    from pub.func.message.eviction import load_evicted

    monkeypatch.setattr("pub.func.message.eviction.SESSIONS_DIR", tmp_path)
    middleware = ContextEvictionMiddleware(human_eviction=False)

    original = _huge_message()
    result = middleware._maybe_evict(original, "child-1")

    assert result.content.startswith("[evicted to: ")
    path = result.content.split("[evicted to: ", 1)[1].split("]", 1)[0]
    assert load_evicted(path) == original.content
    assert (tmp_path / "child-1" / "evicted").is_dir()


def test_main_offload_failure_hard_clips_instead_of_returning_raw(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(eviction_core, "evict_tool_result", _boom)
    middleware = ContextEvictionMiddleware()

    result = middleware._maybe_evict(_huge_message(), "main-1")

    assert result.content.startswith(CLIPPED_PREFIX)
    assert len(result.content) < len(_huge_message().content)


def test_hard_clip_respects_threshold_excluded_and_multimodal():
    small = ToolMessage(content="ok", name="terminal", tool_call_id="call-small")
    assert hard_clip_tool_result(small) is None
    assert hard_clip_tool_result(_huge_message("read_file")) is None

    multimodal = ToolMessage(
        content=[{"type": "text", "text": "x" * 30_000}, {"type": "image_url", "image_url": {}}],
        name="screenshot",
        tool_call_id="call-2",
    )
    assert hard_clip_tool_result(multimodal) is None


def test_child_mode_keeps_the_read_file_slice():
    middleware = ContextEvictionMiddleware(human_eviction=False, evict_to_file=False)
    payload = ToolMessage(
        content="x" * 30_000,
        name="read_file",
        tool_call_id="call-3",
    )

    result = middleware._maybe_evict(payload, "child-1")

    assert len(result.content) < len(payload.content)
    assert "read_file with offset and limit" in result.content


def test_child_mode_does_not_evict_human_messages():
    middleware = ContextEvictionMiddleware(human_eviction=False, evict_to_file=False)
    message = HumanMessage(content="x" * 250_000, metadata={"origin": "user"})

    update = middleware._tag_last_human({"messages": [message], "session_id": "child-1"})

    assert update is None


@pytest.mark.asyncio
async def test_a_real_child_graph_clips_the_tool_message(monkeypatch, tmp_path):
    monkeypatch.setattr("pub.func.message.eviction.SESSIONS_DIR", tmp_path)
    _ScriptedModel.calls = 0
    graph = create_agent(
        model=_ScriptedModel(),
        state_schema=_HarnessState,
        checkpointer=MemorySaver(),
        tools=[big_output],
        middleware=[ContextEvictionMiddleware(human_eviction=False)],
    )

    out = await graph.ainvoke(
        {"messages": [HumanMessage(content="run it")], "session_id": "child-e2e"},
        config={"configurable": {"thread_id": "child-e2e-thread"}},
    )

    tool_messages = [m for m in out["messages"] if isinstance(m, ToolMessage)]
    assert len(tool_messages) == 1
    content = tool_messages[0].content
    assert isinstance(content, str)
    assert content.startswith("[evicted to: ")
    assert CLIPPED_PREFIX not in content
    assert len(content) < 100_000
    assert (tmp_path / "child-e2e" / "evicted").is_dir()
