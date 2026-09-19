"""Plan-ownership isolation tests for the knowledge tool.

Covers the three association sources (state ``plan_ref``, todos ``plan_ref``,
boulder ``session_ids``), the fail-open behavior on a missing/corrupt boulder
file, the tool-level read / write / list convergence, multi-session
collaboration on one plan, the empty-session safe denial, and the nudge
plan-extraction path that supplies ``session_id`` through ``InjectedState``.
"""

import json
from pathlib import Path
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.tools import BaseTool

from agent.middlewares.summarization import nudges as nudge_mod
from agent.middlewares.summarization.nudges import StateSchema
from agent.tools.todolist.knowledge import build_knowledge_tools, identity, ownership

pytestmark = [pytest.mark.unit]


class _FakeStateDB:
    """In-memory stand-in for state_register_db (get/set_state only)."""

    def __init__(self) -> None:
        self._states: dict[str, dict[str, object]] = {}

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self._states.get(session_id, {}).get(key, default)

    def set_state(self, session_id: str, key: str, value: object) -> bool:
        self._states.setdefault(session_id, {})[key] = value
        return True


class _Sources:
    """Bundle of the three patched ownership sources."""

    def __init__(self, state_db: _FakeStateDB, todos: dict[str, list[dict]], boulder_path: Path):
        self.state_db = state_db
        self.todos = todos
        self.boulder_path = boulder_path


@pytest.fixture(autouse=True)
def sources(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Sources:
    """Isolate every ownership source; no real register / DB / boulder is read."""
    state_db = _FakeStateDB()
    todos: dict[str, list[dict]] = {}
    boulder_path = tmp_path / "boulder.json"
    monkeypatch.setattr(ownership, "state_register_db", state_db)
    monkeypatch.setattr(
        ownership, "get_todos_sync", lambda session_id: list(todos.get(session_id, []))
    )
    monkeypatch.setattr(ownership, "resolve_boulder_path", lambda: boulder_path)
    return _Sources(state_db, todos, boulder_path)


@pytest.fixture(autouse=True)
def knowledge_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the knowledge store at a per-test root."""
    root = tmp_path / "plans"
    monkeypatch.setattr("agent.tools.todolist.knowledge.knowledge_store._KNOWLEDGE_ROOT", root)
    return root


def _associate_state(sources: _Sources, session_id: str, plan_name: str) -> None:
    sources.state_db.set_state(session_id, "plan_ref", f".omo/plans/{plan_name}.md")


def _associate_todos(sources: _Sources, session_id: str, plan_name: str) -> None:
    sources.todos[session_id] = [
        {"plan_ref": f"workspace/sessions/{session_id}/plans/{plan_name}.md"}
    ]


def _write_boulder(sources: _Sources, works: list[tuple[str, list[str]]]) -> None:
    payload = {
        "schema_version": 2,
        "works": {
            f"work-{index}": {
                "plan_name": plan_name,
                "session_ids": session_ids,
                "active_plan": (
                    f"workspace/sessions/{session_ids[0]}/plans/{plan_name}.md"
                    if session_ids
                    else f"workspace/sessions/unknown/plans/{plan_name}.md"
                ),
            }
            for index, (plan_name, session_ids) in enumerate(works)
        },
    }
    sources.boulder_path.write_text(json.dumps(payload), encoding="utf-8")


def _knowledge_tool() -> BaseTool:
    return {t.name: t for t in build_knowledge_tools()}["knowledge"]


class TestOwnershipSources:
    def test_state_plan_ref_grants_ownership(self, sources: _Sources):
        _associate_state(sources, "sess-a", "alpha")

        assert ownership.is_plan_associated("sess-a", "alpha") is True
        assert ownership.is_plan_associated("sess-a", "beta") is False

    def test_state_plan_ref_matches_by_stem_across_paths(self, sources: _Sources):
        sources.state_db.set_state(
            "sess-a", "plan_ref", "workspace/sessions/sess-a/plans/deep-auth.md"
        )

        assert ownership.is_plan_associated("sess-a", "deep-auth") is True

    def test_todos_plan_ref_grants_ownership(self, sources: _Sources):
        _associate_todos(sources, "sess-a", "beta")

        assert ownership.is_plan_associated("sess-a", "beta") is True
        assert ownership.is_plan_associated("sess-b", "beta") is False

    def test_boulder_session_ids_grant_ownership(self, sources: _Sources):
        _write_boulder(sources, [("gamma", ["sess-a"])])

        assert ownership.is_plan_associated("sess-a", "gamma") is True
        assert ownership.is_plan_associated("sess-b", "gamma") is False

    def test_unknown_plan_with_no_source_is_not_associated(self, sources: _Sources):
        _associate_state(sources, "sess-a", "alpha")

        assert ownership.is_plan_associated("sess-a", "other-plan") is False

    def test_missing_boulder_file_denies(self, sources: _Sources):
        assert sources.boulder_path.is_file() is False
        assert ownership.is_plan_associated("sess-a", "alpha") is False

    def test_corrupt_boulder_file_denies(self, sources: _Sources):
        sources.boulder_path.write_text("{ not json", encoding="utf-8")

        assert ownership.is_plan_associated("sess-a", "alpha") is False

    def test_boulder_work_listed_for_another_session_denies(self, sources: _Sources):
        _write_boulder(sources, [("gamma", ["sess-b"])])

        assert ownership.is_plan_associated("sess-a", "gamma") is False

    def test_multi_session_boulder_grants_every_listed_session(self, sources: _Sources):
        _write_boulder(sources, [("shared-plan", ["sess-a", "sess-b", "sess-c"])])

        assert ownership.is_plan_associated("sess-a", "shared-plan") is True
        assert ownership.is_plan_associated("sess-b", "shared-plan") is True
        assert ownership.is_plan_associated("sess-c", "shared-plan") is True
        assert ownership.is_plan_associated("sess-d", "shared-plan") is False

    def test_empty_session_id_denies(self, sources: _Sources):
        _write_boulder(sources, [("gamma", ["sess-a"])])

        assert ownership.is_plan_associated("", "gamma") is False
        assert ownership.associated_plan_names("") == set()

    def test_blank_plan_name_denies(self, sources: _Sources):
        _associate_state(sources, "sess-a", "alpha")

        assert ownership.is_plan_associated("sess-a", "  ") is False

    def test_session_derived_fallback_name_is_owned(self, sources: _Sources):
        """The nudge fallback name (session-<sha1(id)[:8]>) belongs only to its session."""
        own_name = identity.fallback_plan_name("sess-nudge")
        other_name = identity.fallback_plan_name("sess-other")

        assert ownership.is_plan_associated("sess-nudge", own_name) is True
        assert ownership.is_plan_associated("sess-nudge", other_name) is False


class TestToolPlanIsolation:
    @pytest.mark.asyncio
    async def test_own_plan_can_write_and_read(self, sources: _Sources, knowledge_root: Path):
        _associate_state(sources, "sess-a", "alpha")
        tool = _knowledge_tool()

        written = await tool.coroutine(
            action="write",
            plan_name="alpha",
            layer="plan",
            data={"method": "own-method"},
            session_id="sess-a",
        )
        read_back = await tool.coroutine(
            action="read", plan_name="alpha", layer="plan", session_id="sess-a"
        )

        assert written.startswith("Knowledge written to ")
        assert "own-method" in read_back

    @pytest.mark.asyncio
    async def test_foreign_plan_write_is_denied(self, sources: _Sources, knowledge_root: Path):
        _associate_state(sources, "sess-b", "beta")
        tool = _knowledge_tool()

        output = await tool.coroutine(
            action="write",
            plan_name="beta",
            layer="plan",
            data={"method": "stolen"},
            session_id="sess-a",
        )

        assert output.startswith("Error: knowledge write denied for plan 'beta'")
        assert "not associated with this session ('sess-a')" in output
        assert not knowledge_root.exists() or list(knowledge_root.iterdir()) == []

    @pytest.mark.asyncio
    async def test_foreign_plan_read_is_denied(self, sources: _Sources, knowledge_root: Path):
        _associate_state(sources, "sess-b", "beta")
        tool = _knowledge_tool()
        await tool.coroutine(
            action="write",
            plan_name="beta",
            layer="plan",
            data={"method": "beta-method"},
            session_id="sess-b",
        )

        output = await tool.coroutine(action="read", plan_name="beta", session_id="sess-a")

        assert output.startswith("Error: knowledge read denied for plan 'beta'")
        assert "beta-method" not in output

    @pytest.mark.asyncio
    async def test_list_returns_only_associated_plans(
        self, sources: _Sources, knowledge_root: Path
    ):
        _associate_state(sources, "sess-a", "plan-a")
        _associate_state(sources, "sess-b", "plan-b")
        tool = _knowledge_tool()
        for session, plan in (("sess-a", "plan-a"), ("sess-b", "plan-b")):
            await tool.coroutine(
                action="write",
                plan_name=plan,
                layer="plan",
                data={"method": f"{plan}-method"},
                session_id=session,
            )

        output_a = await tool.coroutine(action="list", session_id="sess-a")
        output_b = await tool.coroutine(action="list", session_id="sess-b")

        assert "plan-a" in output_a and "plan-a-method" in output_a
        assert "plan-b" not in output_a
        assert "plan-b" in output_b and "plan-b-method" in output_b
        assert "plan-a" not in output_b

    @pytest.mark.asyncio
    async def test_multi_session_collaboration_stays_usable(
        self, sources: _Sources, knowledge_root: Path
    ):
        _write_boulder(sources, [("shared-plan", ["sess-a", "sess-b"])])
        tool = _knowledge_tool()

        write_a = await tool.coroutine(
            action="write",
            plan_name="shared-plan",
            layer="plan",
            data={"method": "from-a"},
            session_id="sess-a",
        )
        read_b = await tool.coroutine(
            action="read", plan_name="shared-plan", layer="plan", session_id="sess-b"
        )
        write_b = await tool.coroutine(
            action="write",
            plan_name="shared-plan",
            layer="task",
            position=0,
            data={"method": "from-b"},
            session_id="sess-b",
        )
        read_a = await tool.coroutine(
            action="read", plan_name="shared-plan", layer="task", position=0, session_id="sess-a"
        )

        assert write_a.startswith("Knowledge written to ")
        assert "from-a" in read_b
        assert write_b.startswith("Knowledge written to ")
        assert "# Task 0 Knowledge: shared-plan" in read_a
        assert "from-b" in read_a
        shared_dirs = [entry for entry in knowledge_root.iterdir() if entry.is_dir()]
        assert len(shared_dirs) == 1

    @pytest.mark.asyncio
    async def test_empty_session_id_denies_write_read_and_list(
        self, sources: _Sources, knowledge_root: Path
    ):
        _associate_state(sources, "sess-a", "alpha")
        tool = _knowledge_tool()

        write_out = await tool.coroutine(
            action="write", plan_name="alpha", layer="plan", data={"m": 1}, session_id=""
        )
        read_out = await tool.coroutine(action="read", plan_name="alpha", session_id="")
        list_out = await tool.coroutine(action="list", session_id="")

        assert write_out.startswith("Error: knowledge write denied: no session_id")
        assert read_out.startswith("Error: knowledge read denied: no session_id")
        assert list_out == "No knowledge files found."

    @pytest.mark.asyncio
    async def test_none_session_id_is_a_safe_denial(self, sources: _Sources):
        tool = _knowledge_tool()

        output = await tool.coroutine(action="read", plan_name="alpha", session_id=None)

        assert output.startswith("Error: knowledge read denied: no session_id")


class TestNudgeSessionIdSupply:
    @pytest.mark.asyncio
    async def test_plan_extraction_nudge_seeds_session_id_into_agent_input(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        captured: list[dict[str, Any]] = []

        class _RecordingAgent:
            async def ainvoke(self, input: Any = None, config: Any = None) -> Any:
                captured.append(dict(input))
                return {"messages": []}

        async def _create(system_prompt: str, allowed_metadata_key: Any = None, tools: Any = None):
            return _RecordingAgent()

        monkeypatch.setattr(nudge_mod, "_build_plan_context", lambda session_id: {"plan_name": "p"})
        monkeypatch.setattr(nudge_mod, "_create_nudge_agent", _create)

        await nudge_mod._nudge_plan_extraction("sess-nudge", "sys", [])

        assert len(captured) == 1
        assert captured[0]["session_id"] == "sess-nudge"

    def test_nudge_state_schema_declares_session_id(self):
        assert "session_id" in StateSchema.__annotations__

    @pytest.mark.asyncio
    async def test_nudge_style_agent_write_succeeds_with_injected_session(
        self, sources: _Sources, knowledge_root: Path
    ):
        """Same construction as ``_create_nudge_agent`` (StateSchema + tools)."""
        _associate_state(sources, "sess-nudge", "extract-plan")

        class _ToolCallingFakeModel(GenericFakeChatModel):
            def bind_tools(self, tools: Any, **kwargs: Any) -> Any:
                return self

        tool_call = AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "knowledge",
                    "args": {
                        "action": "write",
                        "plan_name": "extract-plan",
                        "layer": "plan",
                        "data": {"method": "nudge-extract"},
                    },
                    "id": "call-1",
                    "type": "tool_call",
                }
            ],
        )
        model = _ToolCallingFakeModel(messages=iter([tool_call, AIMessage(content="done")]))
        agent = create_agent(
            model=model,
            state_schema=StateSchema,
            tools=[_knowledge_tool()],
        )

        out = await agent.ainvoke(
            {"messages": [HumanMessage(content="extract")], "session_id": "sess-nudge"}
        )

        tool_contents = [m.content for m in out["messages"] if m.type == "tool"]
        assert any("Knowledge written to" in str(content) for content in tool_contents)
        written_dirs = [entry for entry in knowledge_root.iterdir() if entry.is_dir()]
        assert len(written_dirs) == 1
        assert (written_dirs[0] / "plan-summary.json").is_file()
