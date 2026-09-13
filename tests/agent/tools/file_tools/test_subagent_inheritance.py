"""Integration tests: subagent authorization inheritance for external file access.

Covers the test matrix section H:
H1. child inherits the parent's approved exact path (no interrupt)
H2. child without authorization is refused (fail-closed JSON error)
H3. spawn writes requester_session_key + caller_scope into the child's state
"""

import asyncio
import json
from typing import Any, ClassVar

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import MemorySaver

from agent.tools.file_tools.read_file import build_read_file_tool
from agent.tools.pub_base.path_utils import _add_to_allowlist
from runtime import state_register_mem

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]


class _ScriptedModel(BaseChatModel):
    """Emits one scripted tool call, then idles."""

    calls: ClassVar[int] = 0
    scripted_calls: ClassVar[list[dict[str, Any]]] = []

    @property
    def _llm_type(self) -> str:
        return "stub-subagent-inheritance"

    def bind_tools(self, tools, **kwargs):
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        type(self).calls += 1
        if type(self).calls == 1 and type(self).scripted_calls:
            msg = AIMessage(content="", tool_calls=list(type(self).scripted_calls))
        else:
            msg = AIMessage(content="Done.")
        return ChatResult(generations=[ChatGeneration(message=msg)])


class _HarnessState(AgentState):
    session_id: str


def _build_child_graph(scripted_calls, tools, session_id: str, thread_id: str):
    _ScriptedModel.calls = 0
    _ScriptedModel.scripted_calls = list(scripted_calls)
    graph = create_agent(
        model=_ScriptedModel(),
        state_schema=_HarnessState,
        checkpointer=MemorySaver(),
        tools=list(tools),
    )
    return graph, {"configurable": {"thread_id": thread_id}}


def _tool_messages(out) -> list[ToolMessage]:
    return [m for m in out["messages"] if isinstance(m, ToolMessage)]


@pytest.fixture
def clean_state(monkeypatch):
    """Reset state_register_mem to empty and swap state_register_db for a fake."""
    import runtime

    monkeypatch.setattr(state_register_mem, "_states", {})

    class _FakeDB:
        def __init__(self):
            self.store: dict[tuple[str, str], object] = {}

        def get_state(self, session_id: str, key: str, default=None):
            return self.store.get((session_id, key), default)

        def set_state(self, session_id: str, key: str, value) -> bool:
            self.store[(session_id, key)] = value
            return True

    fake_db = _FakeDB()
    monkeypatch.setattr(runtime, "state_register_db", fake_db)
    yield fake_db


PARENT_SESSION = "s-parent"
CHILD_SESSION = "agent:main:subagent:uuid-h1"


def _seed_spawn_state(parent_allowlist: list[str] | None):
    """Replicate what spawn/core.py writes for the child session."""
    state_register_mem.set_state(CHILD_SESSION, "requester_session_key", PARENT_SESSION)
    state_register_mem.set_state(CHILD_SESSION, "caller_scope", "subagent")
    if parent_allowlist is not None:
        state_register_mem.set_state(PARENT_SESSION, "external_path_allowlist", parent_allowlist)


# ── H1 / H2 ─────────────────────────────────────────────────────────────


class TestSubagentExternalAccess:
    def test_child_inherits_parent_approved_path(self, tmp_path, clean_state, monkeypatch):
        target = tmp_path / "app.conf"
        target.write_text("inherited=value\n", encoding="utf-8")
        _add_to_allowlist(target.resolve(), PARENT_SESSION)
        _seed_spawn_state([str(target.resolve())])

        def _no_interrupt(value):
            raise AssertionError("child must reuse the parent's approval, not re-prompt")

        monkeypatch.setattr("langgraph.types.interrupt", _no_interrupt)

        graph, config = _build_child_graph(
            [{"name": "read_file", "args": {"file_path": str(target)}, "id": "c1"}],
            [build_read_file_tool()],
            CHILD_SESSION,
            "t-h1",
        )
        out = graph.invoke(
            {"messages": [HumanMessage(content="read it")], "session_id": CHILD_SESSION}, config
        )

        result = json.loads(_tool_messages(out)[0].content)
        assert "inherited=value" in result["content"]

    def test_child_without_authorization_gets_json_error(self, tmp_path, clean_state, monkeypatch):
        target = tmp_path / "secret.conf"
        target.write_text("topsecret\n", encoding="utf-8")
        _seed_spawn_state([])

        def _no_interrupt(value):
            raise AssertionError("subagents must never self-approve via interrupt")

        monkeypatch.setattr("langgraph.types.interrupt", _no_interrupt)

        graph, config = _build_child_graph(
            [{"name": "read_file", "args": {"file_path": str(target)}, "id": "c1"}],
            [build_read_file_tool()],
            CHILD_SESSION,
            "t-h2",
        )
        out = graph.invoke(
            {"messages": [HumanMessage(content="read it")], "session_id": CHILD_SESSION}, config
        )

        result = json.loads(_tool_messages(out)[0].content)
        assert "error" in result
        assert "not authorized for subagent" in result["error"]
        assert "main session" in result["error"]


# ── H3. spawn writes the inheritance keys ───────────────────────────────


class TestSpawnWritesInheritanceKeys:
    @pytest.fixture(autouse=True)
    def _cleanup_registry(self):
        from agent.tools.subagent.registry import clear as clear_registry

        clear_registry()
        yield
        clear_registry()

    @pytest.mark.asyncio
    async def test_spawn_writes_requester_session_key_and_caller_scope(
        self, clean_state, monkeypatch
    ):
        from unittest.mock import AsyncMock

        from agent.tools.subagent.spawn.core import spawn_subagent_direct
        from agent.tools.subagent.registry import get_task

        class _FakeChildAgent:
            async def ainvoke(self, input=None, config=None, **kwargs):
                return {"messages": [AIMessage(content="child done")]}

        async def _fake_build_child_agent(**kwargs):
            return _FakeChildAgent()

        monkeypatch.setattr(
            "agent.tools.subagent.spawn.core._build_child_agent", _fake_build_child_agent
        )
        monkeypatch.setattr("agent.tools.build_main_tools", lambda: [])
        monkeypatch.setattr(
            "agent.tools.subagent.registry.lifecycle.complete_subagent_run", AsyncMock()
        )

        result = await spawn_subagent_direct(
            task="inherit keys",
            requester_session_key="agent:main:session:s-parent",
        )
        assert result.status == "accepted"

        bg_task = get_task(result.run_id)
        assert bg_task is not None
        await asyncio.wait_for(bg_task, timeout=30)

        child = result.child_session_key
        assert state_register_mem.get_state(child, "requester_session_key") == "s-parent"
        assert state_register_mem.get_state(child, "caller_scope") == "subagent"
