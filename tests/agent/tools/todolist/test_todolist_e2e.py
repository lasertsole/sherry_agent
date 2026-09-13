"""Full-stack E2E tests for the todolist planning layer.

This suite drives the REAL todowrite/todoread tools through the REAL
``TodoService`` (E4 transition barrier + E6 default-downgrade), the REAL SQLite
store, the REAL ``build_system_prompt`` dynamic blocks (todo / boulder / facts /
taskflow / continuity), the REAL E7 task-intent middleware, the REAL E3
continuation enforcer, the REAL LT-1 tiered facts layer and the REAL TaskFlow
tools. Fakes exist only at true external boundaries:

* the websocket registry (``service.relation_register`` -> no socket, fail-open),
* the subagent registry liveness seams (``_get_run_by_child_session_key`` /
  ``_is_unended_run``) — the real subagent runtime is never started,
* the TaskFlow child dispatch seam (``_dispatch.dispatch_child``) — the real
  spawn pipeline is never called,
* the memory-flush extraction LLM (a canned response; zero network),
* the E3 fire-and-forget auto-turn sink (a spy).

All persistence is redirected to ``tmp_path`` (todos.db, taskflow_registry.db,
facts/, memory/, session_continuity/) and every module-level ledger (todowrite
reminder, E7 arming, E3 stagnation, tiered store) is reset per test, so the
tests are order-independent and touch no real repo state.
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import AIMessage, HumanMessage

from agent.middlewares.todo_continuation import TodoContinuationEnforcer, _build_status_block
from agent.tools.memory import MemoryStore, memory_tool
from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.registry import store_sqlite as flow_store
from agent.tools.todolist import service as todo_service
from agent.tools.todolist.registry import store_sqlite as todo_store
from agent.tools.todolist.service import TodoStoreError
from agent.tools.todolist.tools import _FANOUT_REMINDER, build_todolist_tools

pytestmark = [pytest.mark.integration]

# The todowrite module's module-level reminder ledger. sys.modules keys are
# exact and immune to the package re-export shadowing (tools/__init__ binds the
# tool object under the name ``todowrite``).
_TODOWRITE_MODULE = sys.modules["agent.tools.todolist.tools.todowrite"]

# The five dynamic prompt block headers this suite verifies end-to-end.
_BLOCK_HEADERS = (
    "## Current Todo List",
    "## Active Work",
    "FACTS (on-demand",
    "## Pending TaskFlows",
    "## Last Session (continuity)",
)

_CONTINUATION_MARKER = "[SYSTEM DIRECTIVE: TODO CONTINUATION]"


# ---------------------------------------------------------------------------
# Fakes (external boundaries only)
# ---------------------------------------------------------------------------


class FakeStateDB:
    """In-memory stand-in for ``state_register_db`` (workspace snapshot cache)."""

    def __init__(self) -> None:
        self._states: dict[str, dict[str, object]] = {}

    def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        return self._states.get(session_id, {}).get(key, default)

    def set_state(self, session_id: str, key: str, value: Any) -> bool:
        self._states.setdefault(session_id, {})[key] = value
        return True


class _FakeRelationRegister:
    """Fail-open websocket registry double: no socket for any session."""

    def get_websocket_by_session_id(self, session_id: str) -> None:
        return None


class _RecordingMiddleware(AgentMiddleware):
    """A no-op chain member that records every ``abefore_model`` it sees."""

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[Any] = []

    async def abefore_model(self, state: Any, runtime: Any = None) -> None:
        self.calls.append(state)
        return None


class _StubFlushResponse:
    """Minimal memory-flush LLM response carrying ``.content`` and ``.text``."""

    def __init__(self, content: str) -> None:
        self.content = content
        self.text = content


class _StubFlushLLM:
    """Canned memory-flush extraction model (no network)."""

    def __init__(self, content: str) -> None:
        self._content = content

    async def ainvoke(self, prompt: Any, config: Any = None) -> _StubFlushResponse:
        return _StubFlushResponse(self._content)


def _flush_factory(content: str):
    """Build an ``llm_factory`` matching ``run_memory_flush``'s signature."""

    def factory(**kwargs: Any) -> _StubFlushLLM:
        return _StubFlushLLM(content)

    return factory


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _tools() -> dict[str, Any]:
    return {t.name: t for t in build_todolist_tools()}


def _flow_tools() -> dict[str, Any]:
    return {t.name: t for t in build_taskflow_tools()}


def _parse_todowrite(output: str) -> list[dict]:
    """Parse a todowrite JSON result, tolerating the first-call reminder suffix."""
    return json.loads(output.replace(_FANOUT_REMINDER, ""))


def _block_text(prompt: str, header: str) -> str:
    """Return the prompt slice for one dynamic block (up to the next blank line)."""
    start = prompt.find(header)
    if start < 0:
        return ""
    end = prompt.find("\n\n", start)
    return prompt[start:] if end < 0 else prompt[start:end]


def _build_prompt(session_id: str | None) -> str:
    """Call the real prompt builder lazily (stub-tolerant import)."""
    from workspace.prompt_builder import build_system_prompt

    return build_system_prompt(session_id=session_id)


async def _write_todos(tools: dict[str, Any], session_id: str, todos: list[dict]) -> str:
    return await tools["todowrite"].coroutine(todos=todos, session_id=session_id)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def isolated_stores(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point the todolist AND taskflow stores at tmp dbs; reset init state."""
    for module, db_name in (
        (todo_store, "todos.db"),
        (flow_store, "taskflow_registry.db"),
    ):
        monkeypatch.setattr(module, "_DB_DIR", tmp_path)
        monkeypatch.setattr(module, "_DB_PATH", tmp_path / db_name)
        monkeypatch.setattr(module, "_initialized", False)
        monkeypatch.setattr(module, "_init_loop", None)
        # Fresh lock per test: a contended acquire binds an asyncio.Lock to the
        # acquiring test's event loop, and pytest-asyncio uses a fresh loop.
        monkeypatch.setattr(module, "_init_lock", asyncio.Lock())
        monkeypatch.setattr(module, "_sync_tables_ready", False)
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_module_state(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Reset every process-global ledger + redirect memory/facts to tmp."""
    import agent.middlewares.task_intent as task_intent
    import agent.tools.memory as memory_module
    import agent.tools.memory_tiered as memory_tiered
    from agent.tools.todolist import stagnation_tracker as st

    # todowrite's once-per-session fan-out reminder.
    monkeypatch.setattr(_TODOWRITE_MODULE, "_reminded_sessions", set())

    # E7 arming ledger + a never-existing boulder (the repo has a real one!).
    monkeypatch.setattr(task_intent, "_armed_sessions", set())
    monkeypatch.setattr(task_intent, "_BOULDER_PATH", tmp_path / "no-boulder.json")

    # E3 stagnation state (deterministic, no real sleeps).
    monkeypatch.setattr(st, "_stagnation_count", {})
    monkeypatch.setattr(st, "_last_snapshot", {})
    monkeypatch.setattr(st, "_last_inject_time", {})
    monkeypatch.setattr(st, "_last_failure_time", {})
    monkeypatch.setattr(st, "_recovery_attempts", {})

    # LT-1 tiered facts -> tmp facts dir, fresh cached store.
    monkeypatch.setattr(memory_tiered, "FACTS_DIR", tmp_path / "facts")
    monkeypatch.setattr(memory_tiered, "tiered_store", None)

    # Real MemoryStore, file-backed by a tmp memory dir.
    mem = MemoryStore()
    monkeypatch.setattr(memory_module, "MEMORY_DIR", tmp_path / "memory")
    monkeypatch.setattr(memory_module, "memory_store", mem)
    mem.load_from_disk()


@pytest.fixture()
def prompt_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_stores: Path) -> dict:
    """Isolate persona files, boulder path, state cache, continuity dir, WS."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("AGENTS-PERSONA", encoding="utf-8")
    (workspace / "SOUL.md").write_text("SOUL-PERSONA", encoding="utf-8")

    monkeypatch.setattr("workspace.prompt_builder.WORKSPACE_DIR", workspace)
    monkeypatch.setattr("workspace.prompt_builder.ALL_SYSTEM_FILE_NAMES", ["AGENTS.md", "SOUL.md"])
    monkeypatch.setattr("workspace.prompt_builder.get_skills_text", lambda *a, **k: "SKILLS-BLOCK")
    monkeypatch.setattr("workspace.prompt_builder.ensure_workspace_system_files", lambda: [])

    boulder_path = tmp_path / "boulder.json"
    monkeypatch.setattr("workspace.prompt_builder._BOULDER_PATH", boulder_path)
    monkeypatch.setattr("runtime.state_register_db", FakeStateDB())

    # The WS push is fail-open: no registered socket.
    monkeypatch.setattr(todo_service, "relation_register", _FakeRelationRegister())

    # Continuity end-state files -> tmp dir.
    import context_engine.session_continuity as continuity

    monkeypatch.setattr(continuity, "_CONTINUITY_DIR", tmp_path / "continuity")

    return {"tmp_path": tmp_path, "boulder_path": boulder_path, "workspace": workspace}


@pytest.fixture()
def spy_auto_turn(monkeypatch: pytest.MonkeyPatch) -> list:
    """Capture ``maybe_trigger_auto_turn`` injections (E3 delivery seam)."""
    calls: list[tuple[str, HumanMessage]] = []

    async def _spy(session_key: str, injection: HumanMessage) -> object:
        calls.append((session_key, injection))
        return object()

    import server.service.auto_turn as auto_turn

    monkeypatch.setattr(auto_turn, "maybe_trigger_auto_turn", _spy)
    return calls


# ===========================================================================
# Class 1 — end-to-end flow
# ===========================================================================


class TestTodolistFullPipeline:
    @pytest.mark.asyncio
    async def test_e2e_create_plan_and_prompt(self, prompt_env: dict) -> None:
        """Given a 3-item todowrite plan, When read back and prompted,
        Then the prompt carries ## Current Todo List with all 3 items."""
        tools = _tools()
        sid = "e2e-plan"
        plan = [
            {
                "content": "Draft the design",
                "status": "pending",
                "priority": "high",
                "category": "deep",
            },
            {
                "content": "Implement the module",
                "status": "in_progress",
                "priority": "high",
                "category": "quick",
            },
            {
                "content": "Write the tests",
                "status": "pending",
                "priority": "medium",
                "category": "writing",
            },
        ]

        write_out = await _write_todos(tools, sid, plan)
        assert "Draft the design" in write_out
        read_out = await tools["todoread"].coroutine(session_id=sid)
        read_back = json.loads(read_out)
        assert [t["content"] for t in read_back] == [t["content"] for t in plan]

        prompt = _build_prompt(sid)
        assert "## Current Todo List" in prompt
        for item in plan:
            assert item["content"] in prompt
        assert "[◐]" in prompt and "[○]" in prompt

        print("\n[EVIDENCE test_e2e_create_plan_and_prompt] todowrite output:")
        print(write_out)
        print("[EVIDENCE] todoread rows:")
        print(json.dumps(read_back, ensure_ascii=False, indent=2))
        print("[EVIDENCE] prompt todo block:")
        print(_block_text(prompt, "## Current Todo List"))

    @pytest.mark.asyncio
    async def test_e2e_e4_barrier_blocks_live_subagent(
        self, prompt_env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Given a todo pointed at a live subagent run, When marked completed,
        Then TodoStoreError is raised and the persisted rows are unchanged."""
        tools = _tools()
        sid = "e2e-e4-live"
        await _write_todos(tools, sid, [{"content": "old task", "position": 0}])

        monkeypatch.setattr(todo_service, "_get_run_by_child_session_key", lambda key: object())
        monkeypatch.setattr(todo_service, "_is_live_unended_run", lambda run: True)

        with pytest.raises(TodoStoreError) as excinfo:
            await _write_todos(
                tools,
                sid,
                [
                    {
                        "content": "delegated work",
                        "status": "completed",
                        "subagent_id": "agent:main:subagent:live-1",
                    }
                ],
            )

        rows = await todo_store.get_todos(sid)
        assert [t["content"] for t in rows] == ["old task"]
        assert rows[0]["status"] == "pending"

        print("\n[EVIDENCE test_e2e_e4_barrier_blocks_live_subagent] raised:")
        print(str(excinfo.value))
        print("[EVIDENCE] persisted rows (unchanged):")
        print(json.dumps(rows, ensure_ascii=False, indent=2))

    @pytest.mark.asyncio
    async def test_e2e_e4_barrier_taskflow_step_not_done(self, prompt_env: dict) -> None:
        """Given a todo linked to a TaskFlow step that is dispatched, When marked
        completed, Then it is blocked; once the real resume marks the step done,
        the same completion passes and persists."""
        tools = _tools()
        flow_tools = _flow_tools()
        sid = "e2e-e4-flow"
        flow_id = "e2e-flow-gate"
        child = "agent:main:subagent:gate-1"

        await flow_store.create_flow(
            flow_id,
            {
                "description": "gate flow",
                "steps": [
                    {
                        "step_id": "step-1",
                        "task": "do the gated work",
                        "status": "dispatched",
                        "child_session_key": child,
                        "depends_on": [],
                        "retry_count": 0,
                    }
                ],
                "results": [],
                "creator_session_key": f"agent:main:session:{sid}",
            },
        )

        linked = [
            {
                "content": "linked work",
                "status": "completed",
                "flow_id": flow_id,
                "step_id": "step-1",
            }
        ]
        with pytest.raises(TodoStoreError) as excinfo:
            await _write_todos(tools, sid, linked)
        assert "step-1" in str(excinfo.value)
        assert await todo_store.get_todos(sid) == []

        resumed = await flow_tools["taskflow_resume"].coroutine(
            flow_id=flow_id, child_session_key=child, result="gate passed"
        )
        assert "TaskFlow resumed" in resumed

        result = _parse_todowrite(await _write_todos(tools, sid, linked))
        assert result[0]["status"] == "completed"
        rows = await todo_store.get_todos(sid)
        assert rows[0]["status"] == "completed"

        print("\n[EVIDENCE test_e2e_e4_barrier_taskflow_step_not_done] blocked reason:")
        print(str(excinfo.value))
        print("[EVIDENCE] taskflow resume result:")
        print(resumed)
        print("[EVIDENCE] persisted rows (after step done):")
        print(json.dumps(rows, ensure_ascii=False, indent=2))

    @pytest.mark.asyncio
    async def test_e2e_e7_steering_injection(self, prompt_env: dict) -> None:
        """Given a task-looking user message on a fresh session, When E7 runs,
        Then the full steering prompt is injected first and the short reminder
        on the next qualifying turn."""
        import agent.middlewares.task_intent as task_intent

        middleware = task_intent.TaskIntentMiddleware()
        sid = "e2e-e7"
        task = "Please implement the export pipeline and add tests for it."

        first = await middleware.abefore_model(
            {"messages": [HumanMessage(content=task)], "session_id": sid}
        )
        assert first is not None
        assert task_intent._TASK_STEERING_PROMPT in first["messages"][0].content

        later = await middleware.abefore_model(
            {
                "messages": [
                    HumanMessage(content=task),
                    HumanMessage(content=task_intent._TASK_STEERING_PROMPT),
                    AIMessage(content="working"),
                    HumanMessage(content="now also fix the import bug"),
                ],
                "session_id": sid,
            }
        )
        assert later is not None
        assert later["messages"][0].content == task_intent._TASK_STEERING_REMINDER

        print("\n[EVIDENCE test_e2e_e7_steering_injection] first injection (head):")
        print(first["messages"][0].content[:200])
        print("[EVIDENCE] second injection:")
        print(later["messages"][0].content)

    @pytest.mark.asyncio
    async def test_e2e_e3_continuation_fires(self, prompt_env: dict, spy_auto_turn: list) -> None:
        """Given incomplete todos, When the turn ends, Then the E3 enforcer hands
        a HumanMessage carrying the continuation directive to auto_turn."""
        tools = _tools()
        sid = "e2e-e3"
        await _write_todos(
            tools, sid, [{"content": "finish the report", "status": "pending", "priority": "high"}]
        )

        result = await TodoContinuationEnforcer().aafter_agent({"session_id": sid})

        assert result is None
        assert len(spy_auto_turn) == 1
        session_key, injection = spy_auto_turn[0]
        assert session_key == f"agent:main:session:{sid}"
        assert isinstance(injection, HumanMessage)
        assert _CONTINUATION_MARKER in injection.content
        assert "finish the report" in injection.content

        print("\n[EVIDENCE test_e2e_e3_continuation_fires] injected HumanMessage:")
        print(injection.content)

    @pytest.mark.asyncio
    async def test_e2e_facts_listing_in_prompt(self, prompt_env: dict) -> None:
        """Given a fact written via the memory tool, When the prompt builds,
        Then the LT-1 FACTS listing is present and the fact reads back."""
        sid = "e2e-facts"
        fact = "Sherry stores categorized facts under workspace/memory/facts"

        out = memory_tool("fact_add", target="project", content=fact)
        payload = json.loads(out)
        assert payload["success"] is True
        assert payload["category"] == "project"

        prompt = _build_prompt(sid)
        assert "FACTS (on-demand" in prompt
        assert "project (1 entries)" in prompt
        assert "fact_read/fact_search" in prompt

        # Round-trip: the real facts layer holds the fact text.
        read_out = memory_tool("fact_read", target="project")
        assert fact in read_out

        print("\n[EVIDENCE test_e2e_facts_listing_in_prompt] fact_add result:")
        print(out)
        print("[EVIDENCE] prompt facts block:")
        print(_block_text(prompt, "FACTS (on-demand"))

    @pytest.mark.asyncio
    async def test_e2e_taskflow_block_in_prompt(
        self, prompt_env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Given a real TaskFlow with two steps (one done), When the prompt
        builds, Then ## Pending TaskFlows renders the progress and next step."""
        import agent.tools.taskflow.tools._dispatch as dispatch_mod

        sid = "e2e-taskflow"
        flow_id = "e2e-prompt-flow"
        flow_tools = _flow_tools()

        async def fake_dispatch(task: str, requester_session_key: str, label: str | None = None):
            return f"agent:main:subagent:{task}"

        monkeypatch.setattr(dispatch_mod, "dispatch_child", fake_dispatch)

        await flow_tools["taskflow_create"].coroutine(
            flow_id=flow_id, description="prompt flow", session_id=sid
        )
        await flow_tools["taskflow_run_task"].coroutine(
            flow_id=flow_id, task="first", session_id=sid
        )
        await flow_tools["taskflow_run_task"].coroutine(
            flow_id=flow_id, task="second", session_id=sid
        )
        await flow_tools["taskflow_resume"].coroutine(
            flow_id=flow_id, child_session_key="agent:main:subagent:first", result="first done"
        )

        prompt = _build_prompt(sid)
        assert "## Pending TaskFlows" in prompt
        assert flow_id in prompt
        assert "1/2 steps done" in prompt
        assert 'next: step-2 "second"' in prompt

        print("\n[EVIDENCE test_e2e_taskflow_block_in_prompt] prompt taskflow block:")
        print(_block_text(prompt, "## Pending TaskFlows"))

    @pytest.mark.asyncio
    async def test_e2e_continuity_block_in_prompt(self, prompt_env: dict) -> None:
        """Given a saved previous-session end state for the channel, When a NEW
        session builds its prompt, Then ## Last Session (continuity) appears."""
        from context_engine.session_continuity import save_session_end_state
        from runtime.relation_register import relation_register

        old_sid = "e2e-cont-old"
        new_sid = "e2e-cont-new"
        relation_register.register_channel_chat(new_sid, "channel-9", "chat-9")
        try:
            save_session_end_state(
                session_id=old_sid,
                channel_id="channel-9",
                chat_id="chat-9",
                summary="we shipped the parser and queued the migration",
                taskflow_ids=["flow-x"],
            )
            prompt = _build_prompt(new_sid)
        finally:
            relation_register.unregister_channel_chat_by_session_id(new_sid)

        assert "## Last Session (continuity)" in prompt
        assert "we shipped the parser and queued the migration" in prompt

        print("\n[EVIDENCE test_e2e_continuity_block_in_prompt] prompt continuity block:")
        print(_block_text(prompt, "## Last Session (continuity)"))

    @pytest.mark.asyncio
    async def test_e2e_full_lifecycle(
        self, prompt_env: dict, spy_auto_turn: list, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Given a plan mixing plain, TaskFlow-linked and invalid-category todos,
        When completed one by one (E4/E6 enforced each time), Then the prompt
        shows 100% done, E3 stops firing, the P0-1 memory flush persists a fact
        and the LT-1 FACTS listing updates."""
        import agent.middlewares.memory_flush as memory_flush
        import agent.tools.memory as memory_module
        import agent.tools.taskflow.tools._dispatch as dispatch_mod

        tools = _tools()
        flow_tools = _flow_tools()
        sid = "e2e-lifecycle"
        flow_id = "e2e-lifecycle-flow"
        child = "agent:main:subagent:lifecycle-1"

        async def fake_dispatch(task: str, requester_session_key: str, label: str | None = None):
            return child

        monkeypatch.setattr(dispatch_mod, "dispatch_child", fake_dispatch)

        # A TaskFlow with one dispatched step gates todo B.
        await flow_tools["taskflow_create"].coroutine(
            flow_id=flow_id, description="lifecycle flow", session_id=sid
        )
        await flow_tools["taskflow_run_task"].coroutine(
            flow_id=flow_id, task="lifecycle step", session_id=sid
        )

        plan = [
            {"content": "step A", "status": "pending", "priority": "high", "category": "deep"},
            {
                "content": "step B",
                "status": "pending",
                "priority": "medium",
                "flow_id": flow_id,
                "step_id": "step-1",
            },
            {"content": "step C", "status": "pending", "priority": "low", "category": "bogus"},
        ]
        await _write_todos(tools, sid, plan)
        rows = await todo_store.get_todos(sid)
        # E6: the bogus category was default-downgraded before persistence.
        assert rows[2]["category"] == "quick"

        # Complete A (plain todo).
        plan[0]["status"] = "completed"
        await _write_todos(tools, sid, plan)

        # B is blocked while its TaskFlow step is still dispatched (E4).
        plan[1]["status"] = "completed"
        with pytest.raises(TodoStoreError):
            await _write_todos(tools, sid, plan)
        assert (await todo_store.get_todos(sid))[1]["status"] == "pending"

        # Mark the step done via the real resume tool -> B completes.
        await flow_tools["taskflow_resume"].coroutine(
            flow_id=flow_id, child_session_key=child, result="lifecycle step done"
        )
        await _write_todos(tools, sid, plan)

        # Complete C (plain todo).
        plan[2]["status"] = "completed"
        await _write_todos(tools, sid, plan)

        todos = await todo_store.get_todos(sid)
        done = sum(1 for t in todos if t["status"] in ("completed", "cancelled"))
        assert done == len(todos) == 3
        assert (done / len(todos)) * 100 == 100

        prompt = _build_prompt(sid)
        todo_block = _block_text(prompt, "## Current Todo List")
        assert todo_block.count("[●]") == 3
        assert "[○]" not in todo_block and "[◐]" not in todo_block
        assert (
            _build_status_block(todos) == "[Status: 3/3 completed, 0 remaining]\nRemaining tasks:"
        )

        # All done -> E3 no longer fires.
        await TodoContinuationEnforcer().aafter_agent({"session_id": sid})
        assert spy_auto_turn == []

        # P0-1 memory flush: fake extraction LLM, real MEMORY.md write.
        mem = memory_module.memory_store
        flushed = await memory_flush.run_memory_flush(
            [HumanMessage(content="discarded turn")],
            9_000,
            mem,
            _flush_factory(
                "§ Project: lifecycle uses uv\n§ Decision: extract facts before compaction"
            ),
        )
        assert flushed is True
        mem.load_from_disk()
        prompt_after_flush = _build_prompt(sid)
        assert "lifecycle uses uv" in prompt_after_flush

        # LT-1 facts listing updates after a fact is added.
        memory_tool("fact_add", target="project", content="Lifecycle fact: svc uses uv")
        prompt_after_facts = _build_prompt(sid)
        assert "FACTS (on-demand" in prompt_after_facts
        assert "project (1 entries)" in prompt_after_facts

        print("\n[EVIDENCE test_e2e_full_lifecycle] final DB rows:")
        print(json.dumps(todos, ensure_ascii=False, indent=2))
        print("[EVIDENCE] todo completion = 100% (3/3)")
        print("[EVIDENCE] prompt todo block (all done):")
        print(todo_block)
        print("[EVIDENCE] memory block after P0-1 flush:")
        print(_block_text(prompt_after_flush, "MEMORY (your personal notes)"))
        print("[EVIDENCE] facts block after fact_add:")
        print(_block_text(prompt_after_facts, "FACTS (on-demand"))


# ===========================================================================
# Class 2 — middleware chain integration
# ===========================================================================


class TestTodolistMiddlewareIntegration:
    @pytest.mark.asyncio
    async def test_e7_per_turn_dedup_in_chain(self, prompt_env: dict) -> None:
        """Given E7 in a middleware chain, When the chain runs at turn start and
        again after the model replied, Then steering is injected exactly once."""
        import agent.middlewares.task_intent as task_intent

        chain = [task_intent.TaskIntentMiddleware(), _RecordingMiddleware()]
        recorder = chain[1]
        assert isinstance(recorder, _RecordingMiddleware)
        sid = "e2e-chain"
        task = "implement the cache layer"

        injections: list[Any] = []
        for middleware in chain:
            out = await middleware.abefore_model(
                {"messages": [HumanMessage(content=task)], "session_id": sid}
            )
            if out:
                injections.extend(out["messages"])
        assert len(injections) == 1
        assert task_intent._TASK_STEERING_PROMPT in injections[0].content

        later: list[Any] = []
        for middleware in chain:
            out = await middleware.abefore_model(
                {
                    "messages": [HumanMessage(content=task), AIMessage(content="thinking")],
                    "session_id": sid,
                }
            )
            if out:
                later.extend(out["messages"])
        assert later == []
        assert len(recorder.calls) == 2

        print("\n[EVIDENCE test_e7_per_turn_dedup_in_chain] injections this turn: 1")
        print("[EVIDENCE] later model call injections: 0")

    @pytest.mark.asyncio
    async def test_e3_cooldown_prevents_spam(self, prompt_env: dict, spy_auto_turn: list) -> None:
        """Given incomplete todos, When the enforcer fires twice back-to-back,
        Then the cooldown suppresses the second continuation."""
        tools = _tools()
        sid = "e2e-cooldown"
        await _write_todos(tools, sid, [{"content": "keep working", "status": "pending"}])

        enforcer = TodoContinuationEnforcer()
        await enforcer.aafter_agent({"session_id": sid})
        await enforcer.aafter_agent({"session_id": sid})

        assert len(spy_auto_turn) == 1
        assert _CONTINUATION_MARKER in spy_auto_turn[0][1].content

        print("\n[EVIDENCE test_e3_cooldown_prevents_spam] injection count: 1 (second suppressed)")

    @pytest.mark.asyncio
    async def test_e4_e6_e7_in_one_turn(
        self, prompt_env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Given one turn, When E6 downgrades a bad category, E4 blocks a live
        subagent completion and E7 injects steering, Then all three hold."""
        import agent.middlewares.task_intent as task_intent

        tools = _tools()
        sid = "e2e-combined"

        # E6: invalid category/delegation default-downgrade on write.
        await _write_todos(
            tools,
            sid,
            [{"content": "bad cat", "category": "nonsense", "delegation": "nope"}],
        )
        rows = await todo_store.get_todos(sid)
        assert rows[0]["category"] == "quick"
        assert rows[0]["delegation"] == "self"

        # E4: a completion pointing at a live subagent run is rejected.
        monkeypatch.setattr(todo_service, "_get_run_by_child_session_key", lambda key: object())
        monkeypatch.setattr(todo_service, "_is_live_unended_run", lambda run: True)
        with pytest.raises(TodoStoreError):
            await _write_todos(
                tools,
                sid,
                [
                    {
                        "content": "live delegated",
                        "status": "completed",
                        "subagent_id": "agent:main:subagent:live-combined",
                    }
                ],
            )
        assert [t["content"] for t in await todo_store.get_todos(sid)] == ["bad cat"]

        # E7: steering injected for the turn's task message.
        e7 = task_intent.TaskIntentMiddleware()
        out = await e7.abefore_model(
            {"messages": [HumanMessage(content="implement the dashboard")], "session_id": sid}
        )
        assert out is not None
        assert task_intent._TASK_STEERING_PROMPT in out["messages"][0].content

        print("\n[EVIDENCE test_e4_e6_e7_in_one_turn] E6 row after downgrade:")
        print(json.dumps(rows[0], ensure_ascii=False))
        print("[EVIDENCE] E4 blocked; persisted rows unchanged:")
        print(json.dumps(await todo_store.get_todos(sid), ensure_ascii=False))
        print("[EVIDENCE] E7 injected steering (head):")
        print(out["messages"][0].content[:120])


# ===========================================================================
# Class 3 — prompt blocks present / clean
# ===========================================================================


class TestTodolistPromptBlocks:
    @pytest.mark.asyncio
    async def test_all_blocks_in_prompt(
        self, prompt_env: dict, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Given todos + boulder + facts + taskflow + continuity state, When the
        prompt builds, Then all five dynamic block headers are present."""
        import agent.tools.taskflow.tools._dispatch as dispatch_mod
        from context_engine.session_continuity import save_session_end_state
        from runtime.relation_register import relation_register

        tools = _tools()
        flow_tools = _flow_tools()
        sid = "e2e-all-blocks"

        async def fake_dispatch(task: str, requester_session_key: str, label: str | None = None):
            return f"agent:main:subagent:{task}"

        monkeypatch.setattr(dispatch_mod, "dispatch_child", fake_dispatch)

        # 1. todos
        await _write_todos(
            tools,
            sid,
            [
                {"content": "block task one", "status": "in_progress", "priority": "high"},
                {"content": "block task two", "status": "pending"},
            ],
        )
        # 2. boulder (active work pointer; plan file existence is not required).
        prompt_env["boulder_path"].write_text(
            json.dumps(
                {
                    "schema_version": 2,
                    "active_work_id": "w1",
                    "works": {
                        "w1": {
                            "work_id": "w1",
                            "active_plan": ".omo/plans/e2e.md",
                            "status": "active",
                        }
                    },
                }
            ),
            encoding="utf-8",
        )
        # 3. facts
        memory_tool("fact_add", target="environment", content="All-blocks fact")
        # 4. taskflow
        await flow_tools["taskflow_create"].coroutine(
            flow_id="e2e-all-flow", description="all blocks flow", session_id=sid
        )
        await flow_tools["taskflow_run_task"].coroutine(
            flow_id="e2e-all-flow", task="flow step", session_id=sid
        )
        # 5. continuity (previous session bound to the same channel)
        relation_register.register_channel_chat(sid, "ch-all", "chat-all")
        try:
            save_session_end_state(
                session_id="old-all",
                channel_id="ch-all",
                chat_id="chat-all",
                summary="previous session tail",
                taskflow_ids=[],
            )
            prompt = _build_prompt(sid)
        finally:
            relation_register.unregister_channel_chat_by_session_id(sid)

        for header in _BLOCK_HEADERS:
            assert header in prompt, f"missing block header: {header}"

        print("\n[EVIDENCE test_all_blocks_in_prompt] all 5 block headers present:")
        for header in _BLOCK_HEADERS:
            print(f"  OK {header}")
        print("[EVIDENCE] prompt todo block:")
        print(_block_text(prompt, "## Current Todo List"))
        print("[EVIDENCE] prompt taskflow block:")
        print(_block_text(prompt, "## Pending TaskFlows"))

    @pytest.mark.asyncio
    async def test_prompt_clean_when_no_state(self, prompt_env: dict) -> None:
        """Given a fresh session with no state, When the prompt builds, Then none
        of the five dynamic block headers appear."""
        prompt = _build_prompt("e2e-clean")

        for header in _BLOCK_HEADERS:
            assert header not in prompt, f"unexpected block header: {header}"
        assert "AGENTS-PERSONA" in prompt
        assert "SKILLS-BLOCK" in prompt

        print("\n[EVIDENCE test_prompt_clean_when_no_state] absent block headers:")
        for header in _BLOCK_HEADERS:
            print(f"  ABSENT {header}")
