"""Integration tests: file tools reaching external paths through HITL approval.

Covers the test matrix sections:
D7. search_files over an allowlisted external directory (exact match, no prompt)
G.  file_tools integration (G1-G4): read/patch/write via a real create_agent graph
    with interrupt + Command(resume=...) rounds.
"""

import json
from typing import Any, ClassVar

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from agent.tools.file_tools.patch_file import build_patch_file_tool
from agent.tools.file_tools.read_file import build_read_file_tool
from agent.tools.file_tools.search_files import build_search_files_tool
from agent.tools.file_tools.write_file import build_write_file_tool
from agent.tools.pub_base.path_utils import _add_to_allowlist

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]


class _ScriptedModel(BaseChatModel):
    """Emits one scripted tool call, then idles."""

    calls: ClassVar[int] = 0
    scripted_calls: ClassVar[list[dict[str, Any]]] = []

    @property
    def _llm_type(self) -> str:
        return "stub-external-access"

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


def _build_graph(scripted_calls, tools):
    _ScriptedModel.calls = 0
    _ScriptedModel.scripted_calls = list(scripted_calls)
    return create_agent(
        model=_ScriptedModel(),
        state_schema=_HarnessState,
        checkpointer=MemorySaver(),
        tools=list(tools),
    )


def _invoke_and_resume(graph, thread_id, session_id, resume_decision):
    config = {"configurable": {"thread_id": thread_id}}
    graph.invoke(
        {"messages": [HumanMessage(content="do it")], "session_id": session_id},
        config,
    )
    return graph.invoke(Command(resume={"decisions": [resume_decision]}), config)


def _tool_messages(out) -> list[ToolMessage]:
    return [m for m in out["messages"] if isinstance(m, ToolMessage)]


@pytest.fixture
def clean_state(monkeypatch):
    """Reset state_register_mem to empty and swap state_register_db for a fake."""
    from runtime import state_register_mem
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


# ── D7. search_files over an allowlisted external directory ─────────────


class TestSearchFilesExternalDirectory:
    def test_allowlisted_external_dir_searches_without_prompt(
        self, tmp_path, clean_state, monkeypatch
    ):
        ext_dir = tmp_path / "docs"
        ext_dir.mkdir()
        (ext_dir / "report_a.md").write_text("alpha", encoding="utf-8")
        (ext_dir / "report_b.md").write_text("beta", encoding="utf-8")
        _add_to_allowlist(ext_dir.resolve(), "s1")

        def _no_interrupt(value):
            raise AssertionError("allowlisted directory must not trigger an approval prompt")

        monkeypatch.setattr("langgraph.types.interrupt", _no_interrupt)

        tool = build_search_files_tool()
        out = tool._core("report_*", target="files", path=str(ext_dir), session_id="s1")

        result = json.loads(out)
        assert result["total_count"] == 2
        assert all(str(ext_dir) in f for f in result["files"])


# ── G. file_tools integration (real graph + interrupt/resume) ───────────


class TestFileToolsExternalAccess:
    def test_read_file_external_approve_returns_content(self, tmp_path, clean_state):
        from runtime import state_register_mem

        target = tmp_path / "app.conf"
        target.write_text("key=value\n", encoding="utf-8")

        graph = _build_graph(
            [{"name": "read_file", "args": {"file_path": str(target)}, "id": "c1"}],
            [build_read_file_tool()],
        )
        out = _invoke_and_resume(graph, "t-g1", "s-main", {"type": "approve"})

        result = json.loads(_tool_messages(out)[0].content)
        assert "key=value" in result["content"]
        assert state_register_mem.get_state("s-main", "external_path_allowlist") == [
            str(target.resolve())
        ]

    def test_patch_file_external_approve_patches(self, tmp_path, clean_state):
        target = tmp_path / "result.txt"
        target.write_text("status: old\n", encoding="utf-8")

        graph = _build_graph(
            [
                {
                    "name": "patch_file",
                    "args": {
                        "file_path": str(target),
                        "old_string": "status: old",
                        "new_string": "status: new",
                    },
                    "id": "c1",
                }
            ],
            [build_patch_file_tool()],
        )
        out = _invoke_and_resume(graph, "t-g2", "s-main", {"type": "approve"})

        result = json.loads(_tool_messages(out)[0].content)
        assert result["success"] is True
        assert "status: new" in target.read_text(encoding="utf-8")

    def test_write_file_external_approve_writes(self, tmp_path, clean_state):
        target = tmp_path / "new.txt"

        graph = _build_graph(
            [
                {
                    "name": "write_file",
                    "args": {"file_path": str(target), "text": "hello external"},
                    "id": "c1",
                }
            ],
            [build_write_file_tool()],
        )
        out = _invoke_and_resume(graph, "t-g3", "s-main", {"type": "approve"})

        tool_msg = _tool_messages(out)[0].content
        assert "successfully" in tool_msg
        assert target.read_text(encoding="utf-8") == "hello external"

    def test_write_file_external_without_approval_is_refused(self, tmp_path, clean_state):
        target = tmp_path / "guarded.txt"

        graph = _build_graph(
            [
                {
                    "name": "write_file",
                    "args": {"file_path": str(target), "text": "nope"},
                    "id": "c1",
                }
            ],
            [build_write_file_tool()],
        )
        out = _invoke_and_resume(graph, "t-g4", "s-main", {"type": "reject"})

        result = json.loads(_tool_messages(out)[0].content)
        assert "error" in result
        assert "denied" in result["error"]
        assert not target.exists(), "the built-in root_dir=/ check must not let writes through"
