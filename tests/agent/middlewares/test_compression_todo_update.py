"""Unit tests for the post-compression todo reconciliation nudge.

Covers:
- trigger gating (feature switch / empty slice / empty todo list / lock),
- fire-and-forget scheduling on the async compression path (``create_task`` +
  module task set; the scheduler returns without awaiting the runner; the sync
  path skips with no running loop and releases the lock),
- per-session reentrancy lock (a second compression while the runner is in
  flight does not double-fire),
- fail-open behavior (runner errors never reach the compression result and
  always release the lock),
- prompt content (discarded slice + current todos + replacement rules),
- ``_NudgeLimitTool(allowed_metadata_key=...)`` metadata gate (and the legacy
  ``nudge`` metadata rule non-regression),
- full-fork isolation (derived session key, main-session-bound ``todowrite``
  shim, no checkpointer / message leakage).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import Any

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.prebuilt.tool_node import InjectedState
from pydantic import BaseModel

import agent.middlewares.summarization.nudges as nudge_mod
from agent.middlewares.summarization.nudges import (
    _COMPRESSION_TODO_LOCK_KEY,
    _COMPRESSION_TODO_METADATA_KEY,
    _COMPRESSION_TODO_SESSION_SUFFIX,
    _COMPRESSION_TODO_TASKS,
    _NudgeLimitTool,
    schedule_compression_todo_update,
    update_todos_from_compaction,
)
from agent.middlewares.summarization import Summarization
from agent.tools.todolist import service as todo_service
from agent.tools.todolist.tools import build_todolist_tools
from config.features import SUMMARIZATION
from runtime import state_register_mem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


# ---------------------------------------------------------------------------
# Fakes / helpers
# ---------------------------------------------------------------------------


class _FakeStateRegister:
    """Minimal in-memory stand-in for ``state_register_mem`` (no DB)."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], object] = {}

    def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        return self.data.get((session_id, key), default)

    def set_state(self, session_id: str, key: str, value: Any) -> None:
        self.data[(session_id, key)] = value

    def clear_session(self, session_id: str) -> bool:
        keys = [key for key in self.data if key[0] == session_id]
        for key in keys:
            del self.data[key]
        return bool(keys)


class _StubSummaryModel:
    """Fake auxiliary summary model (sync + async)."""

    _llm_type = "fake"

    def invoke(self, prompt: Any, config: Any = None) -> Any:  # noqa: ARG002
        return self._response()

    async def ainvoke(self, prompt: Any, config: Any = None) -> Any:  # noqa: ARG002
        return self._response()

    @staticmethod
    def _response() -> Any:
        return type(
            "R",
            (),
            {
                "content": (
                    "A sufficiently long deterministic summary of the conversation "
                    "history covering decisions, files and next steps."
                )
            },
        )()


def _large_history(turns: int = 150, out_chars: int = 500) -> list:
    messages: list = []
    for i in range(turns):
        messages.append(HumanMessage(content=f"question {i}"))
        messages.append(
            AIMessage(
                content=f"working {i}",
                tool_calls=[
                    {
                        "name": "memory",
                        "args": {"action": "read"},
                        "id": f"c{i}",
                        "type": "tool_call",
                    }
                ],
            )
        )
        messages.append(ToolMessage(content="x" * out_chars, tool_call_id=f"c{i}"))
    return messages


def _request(messages: list, session_id: str) -> ModelRequest:
    return ModelRequest(
        model=_StubSummaryModel(),
        messages=list(messages),
        state={"session_id": session_id, "messages": list(messages)},
    )


def _make_summarization() -> Summarization:
    return Summarization(
        model=_StubSummaryModel(),
        trigger=[("tokens", 5_000)],
        main_llm_context_window=2_000,
    )


def _patch_todos(monkeypatch: pytest.MonkeyPatch, todos: list[dict]) -> None:
    monkeypatch.setattr(
        "agent.tools.todolist.registry.store_sqlite.get_todos_sync",
        lambda session_id: todos,
    )


def _spy_create_task(monkeypatch: pytest.MonkeyPatch) -> list[asyncio.Task[None]]:
    created: list[asyncio.Task[None]] = []
    real_create_task = asyncio.create_task

    def _spy(coro: Coroutine[Any, Any, None], **kwargs: Any) -> asyncio.Task[None]:
        task = real_create_task(coro, **kwargs)
        created.append(task)
        return task

    monkeypatch.setattr(nudge_mod.asyncio, "create_task", _spy)
    return created


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_nudge_state(monkeypatch: pytest.MonkeyPatch) -> _FakeStateRegister:
    fake = _FakeStateRegister()
    monkeypatch.setattr(nudge_mod, "state_register_mem", fake)
    return fake


@pytest.fixture
def sid(request: pytest.FixtureRequest) -> str:
    session = "cmp-todo-" + request.node.name[:40] + "-" + uuid.uuid4().hex[:6]
    yield session
    state_register_mem.clear_session(session)
    state_register_mem.clear_session(f"{session}{_COMPRESSION_TODO_SESSION_SUFFIX}")


@pytest.fixture(autouse=True)
def _clean_tasks() -> Any:
    yield
    for task in list(_COMPRESSION_TODO_TASKS):
        task.cancel()
    _COMPRESSION_TODO_TASKS.clear()


# ---------------------------------------------------------------------------
# Trigger gates (scheduler level)
# ---------------------------------------------------------------------------


class TestTriggerGates:
    def test_disabled_switch_never_schedules(
        self, monkeypatch: pytest.MonkeyPatch, fake_nudge_state: _FakeStateRegister
    ) -> None:
        monkeypatch.setattr(
            nudge_mod,
            "SUMMARIZATION",
            {**SUMMARIZATION, "compression_todo_update_enabled": False},
        )
        _patch_todos(monkeypatch, [{"content": "x", "status": "pending"}])
        assert schedule_compression_todo_update("sess-off", [HumanMessage("hi")]) is False
        assert _COMPRESSION_TODO_TASKS == set()

    def test_empty_slice_never_schedules(
        self, monkeypatch: pytest.MonkeyPatch, fake_nudge_state: _FakeStateRegister
    ) -> None:
        _patch_todos(monkeypatch, [{"content": "x", "status": "pending"}])
        created = _spy_create_task(monkeypatch)
        assert schedule_compression_todo_update("sess-noslice", []) is False
        assert created == []
        assert _COMPRESSION_TODO_TASKS == set()

    def test_empty_todo_list_never_schedules(
        self, monkeypatch: pytest.MonkeyPatch, fake_nudge_state: _FakeStateRegister
    ) -> None:
        _patch_todos(monkeypatch, [])
        created = _spy_create_task(monkeypatch)
        assert schedule_compression_todo_update("sess-notodos", [HumanMessage("hi")]) is False
        assert created == []
        assert _COMPRESSION_TODO_TASKS == set()

    def test_lock_held_never_schedules(
        self, monkeypatch: pytest.MonkeyPatch, fake_nudge_state: _FakeStateRegister
    ) -> None:
        _patch_todos(monkeypatch, [{"content": "x", "status": "pending"}])
        fake_nudge_state.set_state("sess-locked", _COMPRESSION_TODO_LOCK_KEY, True)
        created = _spy_create_task(monkeypatch)
        assert schedule_compression_todo_update("sess-locked", [HumanMessage("hi")]) is False
        assert created == []
        assert _COMPRESSION_TODO_TASKS == set()

    def test_failed_gates_leave_lock_unset(
        self, monkeypatch: pytest.MonkeyPatch, fake_nudge_state: _FakeStateRegister
    ) -> None:
        _patch_todos(monkeypatch, [])
        schedule_compression_todo_update("sess-untouched", [HumanMessage("hi")])
        assert (
            fake_nudge_state.get_state("sess-untouched", _COMPRESSION_TODO_LOCK_KEY, False) is False
        )


# ---------------------------------------------------------------------------
# Fire-and-forget scheduling
# ---------------------------------------------------------------------------


class TestFireAndForget:
    @pytest.mark.asyncio
    async def test_schedules_task_set_and_returns_without_awaiting_runner(
        self, monkeypatch: pytest.MonkeyPatch, fake_nudge_state: _FakeStateRegister
    ) -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        calls: list[tuple[str, list]] = []

        async def _slow_runner(session_id: str, messages: list) -> None:
            calls.append((session_id, messages))
            started.set()
            await release.wait()

        monkeypatch.setattr(nudge_mod, "update_todos_from_compaction", _slow_runner)
        _patch_todos(monkeypatch, [{"content": "ship", "status": "pending"}])
        created = _spy_create_task(monkeypatch)

        assert schedule_compression_todo_update("sess-fire", [HumanMessage("done")]) is True
        assert len(created) == 1
        assert len(_COMPRESSION_TODO_TASKS) == 1
        assert started.is_set() is False

        await asyncio.sleep(0)
        assert started.is_set() is True
        assert release.is_set() is False
        assert calls == [("sess-fire", [HumanMessage("done")])]

        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
            if not _COMPRESSION_TODO_TASKS:
                break
        assert _COMPRESSION_TODO_TASKS == set()

    def test_no_running_loop_skips_and_releases_lock(
        self, monkeypatch: pytest.MonkeyPatch, fake_nudge_state: _FakeStateRegister
    ) -> None:
        _patch_todos(monkeypatch, [{"content": "x", "status": "pending"}])
        created = _spy_create_task(monkeypatch)
        assert schedule_compression_todo_update("sess-sync", [HumanMessage("hi")]) is False
        assert created == []
        assert ("sess-sync", _COMPRESSION_TODO_LOCK_KEY) not in fake_nudge_state.data
        assert _COMPRESSION_TODO_TASKS == set()


# ---------------------------------------------------------------------------
# Reentrancy lock (real runner in flight)
# ---------------------------------------------------------------------------


class TestReentrancyLock:
    @pytest.mark.asyncio
    async def test_running_update_blocks_second_schedule(
        self, monkeypatch: pytest.MonkeyPatch, fake_nudge_state: _FakeStateRegister
    ) -> None:
        started = asyncio.Event()
        release = asyncio.Event()

        class _BlockingAgent:
            async def ainvoke(self, input: Any) -> dict:  # noqa: ARG002
                started.set()
                await release.wait()
                return {"messages": [AIMessage("ok")]}

        async def _create(
            system_prompt: str,
            allowed_metadata_key: str | None = None,
            tools: list | None = None,
        ) -> Any:
            return _BlockingAgent()

        monkeypatch.setattr(nudge_mod, "_create_nudge_agent", _create)
        _patch_todos(monkeypatch, [{"content": "ship", "status": "pending"}])

        assert schedule_compression_todo_update("sess-reentry", [HumanMessage("a")]) is True
        await asyncio.wait_for(started.wait(), timeout=1)
        assert fake_nudge_state.get_state("sess-reentry", _COMPRESSION_TODO_LOCK_KEY, False) is True

        assert schedule_compression_todo_update("sess-reentry", [HumanMessage("b")]) is False
        assert len(_COMPRESSION_TODO_TASKS) == 1

        release.set()
        for _ in range(10):
            await asyncio.sleep(0)
            if not _COMPRESSION_TODO_TASKS:
                break
        assert fake_nudge_state.get_state("sess-reentry", _COMPRESSION_TODO_LOCK_KEY, True) is False


# ---------------------------------------------------------------------------
# Compression-path wiring (async + sync seams)
# ---------------------------------------------------------------------------


class TestCompressionPathWiring:
    @pytest.mark.asyncio
    async def test_async_path_fires_real_task_with_discarded_slice(
        self, monkeypatch: pytest.MonkeyPatch, fake_nudge_state: _FakeStateRegister, sid: str
    ) -> None:
        runs: list[tuple[str, int]] = []

        async def _runner(session_id: str, messages: list) -> None:
            runs.append((session_id, len(messages)))

        monkeypatch.setattr(nudge_mod, "update_todos_from_compaction", _runner)
        _patch_todos(monkeypatch, [{"content": "ship", "status": "pending"}])
        created = _spy_create_task(monkeypatch)

        messages = _large_history()
        result = await _make_summarization()._aapply_compression_under_lock(
            _request(messages, sid), sid
        )
        await asyncio.sleep(0)

        assert len(result.messages) < len(messages)
        assert len(created) == 1
        assert runs[0][0] == sid
        assert runs[0][1] > 0

    @pytest.mark.asyncio
    async def test_disabled_switch_does_not_fire_on_compression(
        self, monkeypatch: pytest.MonkeyPatch, fake_nudge_state: _FakeStateRegister, sid: str
    ) -> None:
        monkeypatch.setattr(
            nudge_mod,
            "SUMMARIZATION",
            {**SUMMARIZATION, "compression_todo_update_enabled": False},
        )
        runs: list[str] = []

        async def _runner(session_id: str, messages: list) -> None:
            runs.append(session_id)

        monkeypatch.setattr(nudge_mod, "update_todos_from_compaction", _runner)
        _patch_todos(monkeypatch, [{"content": "ship", "status": "pending"}])

        result = await _make_summarization()._aapply_compression_under_lock(
            _request(_large_history(), sid), sid
        )
        await asyncio.sleep(0)

        assert len(result.messages) > 0
        assert runs == []

    @pytest.mark.asyncio
    async def test_no_todos_does_not_fire_on_compression(
        self, monkeypatch: pytest.MonkeyPatch, fake_nudge_state: _FakeStateRegister, sid: str
    ) -> None:
        runs: list[str] = []

        async def _runner(session_id: str, messages: list) -> None:
            runs.append(session_id)

        monkeypatch.setattr(nudge_mod, "update_todos_from_compaction", _runner)
        _patch_todos(monkeypatch, [])

        await _make_summarization()._aapply_compression_under_lock(
            _request(_large_history(), sid), sid
        )
        await asyncio.sleep(0)

        assert runs == []

    @pytest.mark.asyncio
    async def test_no_cut_does_not_fire_on_compression(
        self, monkeypatch: pytest.MonkeyPatch, fake_nudge_state: _FakeStateRegister, sid: str
    ) -> None:
        runs: list[str] = []

        async def _runner(session_id: str, messages: list) -> None:
            runs.append(session_id)

        monkeypatch.setattr(nudge_mod, "update_todos_from_compaction", _runner)
        _patch_todos(monkeypatch, [{"content": "ship", "status": "pending"}])
        monkeypatch.setattr(Summarization, "_determine_cutoff", lambda self, messages: 0)

        await _make_summarization()._aapply_compression_under_lock(
            _request(_large_history(turns=6, out_chars=10), sid), sid
        )
        await asyncio.sleep(0)

        assert runs == []

    def test_sync_path_fires_when_scheduler_present(
        self, monkeypatch: pytest.MonkeyPatch, sid: str
    ) -> None:
        captured: list[tuple[str, list]] = []
        monkeypatch.setattr(
            "agent.middlewares.summarization.core._schedule_compression_todo_update",
            lambda session_id, messages: captured.append((session_id, list(messages))),
        )
        message_list = _large_history()
        result = _make_summarization()._apply_compression_under_lock(
            _request(message_list, sid), sid
        )
        assert len(result.messages) < len(message_list)
        assert len(captured) == 1
        assert captured[0][0] == sid
        assert captured[0][1]

    def test_sync_path_without_loop_skips_and_compression_succeeds(
        self, monkeypatch: pytest.MonkeyPatch, fake_nudge_state: _FakeStateRegister, sid: str
    ) -> None:
        _patch_todos(monkeypatch, [{"content": "ship", "status": "pending"}])
        message_list = _large_history()

        result = _make_summarization()._apply_compression_under_lock(
            _request(message_list, sid), sid
        )

        assert len(result.messages) < len(message_list)
        assert _COMPRESSION_TODO_TASKS == set()
        assert (sid, _COMPRESSION_TODO_LOCK_KEY) not in fake_nudge_state.data


# ---------------------------------------------------------------------------
# Runner: prompt content + fail-open
# ---------------------------------------------------------------------------


class _CapturingAgent:
    def __init__(self) -> None:
        self.inputs: list[dict] = []

    async def ainvoke(self, input: dict) -> dict:
        self.inputs.append(input)
        return {"messages": [AIMessage("ok")]}


class TestUpdateTodosFromCompaction:
    @pytest.mark.asyncio
    async def test_prompt_carries_todos_slice_and_replacement_rules(
        self, monkeypatch: pytest.MonkeyPatch, fake_nudge_state: _FakeStateRegister
    ) -> None:
        captured_system: list[str] = []
        captured_kwargs: list[dict] = []
        agent = _CapturingAgent()

        async def _create(
            system_prompt: str,
            allowed_metadata_key: str | None = None,
            tools: list | None = None,
        ) -> Any:
            captured_system.append(system_prompt)
            captured_kwargs.append({"allowed_metadata_key": allowed_metadata_key, "tools": tools})
            return agent

        monkeypatch.setattr(nudge_mod, "_create_nudge_agent", _create)
        todos = [
            {
                "content": "ship feature",
                "status": "pending",
                "priority": "high",
                "category": "deep",
                "delegation": "self",
                "plan_ref": ".omo/plans/p.md",
                "flow_id": "flow-1",
                "step_id": "step-2",
            },
            {"content": "write docs", "status": "in_progress", "priority": "low"},
        ]
        _patch_todos(monkeypatch, todos)

        await update_todos_from_compaction(
            "sess-prompt", [HumanMessage(content="I finished shipping")]
        )

        assert captured_system
        system_prompt = captured_system[0]
        human_content = agent.inputs[0]["messages"][-1].content
        combined = f"{system_prompt}\n{human_content}"

        assert "ship feature" in combined
        assert ".omo/plans/p.md" in combined
        assert "I finished shipping" in combined
        assert "<discarded_conversation>" in human_content
        assert "<current_todos>" in human_content
        assert "todowrite" in combined
        assert "only tool available" in combined
        assert "full replacement" in combined
        assert "ACTUAL progress" in combined
        assert "completed" in combined
        assert "cancelled" in combined
        assert captured_kwargs[0]["allowed_metadata_key"] == _COMPRESSION_TODO_METADATA_KEY
        fork_tools = captured_kwargs[0]["tools"]
        assert [t.name for t in fork_tools] == ["todowrite"]
        assert fork_tools[0].metadata.get("todo_update") is True
        assert fork_tools[0].metadata.get("scope") == "main_only"

    @pytest.mark.asyncio
    async def test_session_scoped_plan_ref_passes_through(
        self, monkeypatch: pytest.MonkeyPatch, fake_nudge_state: _FakeStateRegister
    ) -> None:
        captured_system: list[str] = []
        agent = _CapturingAgent()

        async def _create(
            system_prompt: str,
            allowed_metadata_key: str | None = None,
            tools: list | None = None,
        ) -> Any:
            captured_system.append(system_prompt)
            return agent

        monkeypatch.setattr(nudge_mod, "_create_nudge_agent", _create)
        ref = "workspace/sessions/sess-prompt/plans/scoped.md"
        _patch_todos(
            monkeypatch,
            [{"content": "ship feature", "status": "pending", "plan_ref": ref}],
        )

        await update_todos_from_compaction("sess-prompt", [HumanMessage("done")])

        combined = f"{captured_system[0]}\n{agent.inputs[0]['messages'][-1].content}"
        assert ref in combined

    @pytest.mark.asyncio
    async def test_agent_error_is_swallowed_and_lock_released(
        self, monkeypatch: pytest.MonkeyPatch, fake_nudge_state: _FakeStateRegister
    ) -> None:
        async def _boom(
            system_prompt: str,
            allowed_metadata_key: str | None = None,
            tools: list | None = None,
        ) -> Any:
            raise RuntimeError("llm down")

        monkeypatch.setattr(nudge_mod, "_create_nudge_agent", _boom)
        _patch_todos(monkeypatch, [{"content": "ship", "status": "pending"}])

        await update_todos_from_compaction("sess-fail", [HumanMessage("hello")])

        assert fake_nudge_state.get_state("sess-fail", _COMPRESSION_TODO_LOCK_KEY, True) is False

    @pytest.mark.asyncio
    async def test_empty_todos_short_circuits_and_releases_lock(
        self, monkeypatch: pytest.MonkeyPatch, fake_nudge_state: _FakeStateRegister
    ) -> None:
        called: list[str] = []

        async def _create(
            system_prompt: str,
            allowed_metadata_key: str | None = None,
            tools: list | None = None,
        ) -> Any:
            called.append("create")
            return _CapturingAgent()

        monkeypatch.setattr(nudge_mod, "_create_nudge_agent", _create)
        _patch_todos(monkeypatch, [])

        await update_todos_from_compaction("sess-empty", [HumanMessage("hello")])

        assert called == []
        assert fake_nudge_state.get_state("sess-empty", _COMPRESSION_TODO_LOCK_KEY, True) is False


class TestCompressionResultUnaffectedByTodoFailure:
    @pytest.mark.asyncio
    async def test_failing_todo_agent_never_breaks_compression(
        self, monkeypatch: pytest.MonkeyPatch, fake_nudge_state: _FakeStateRegister, sid: str
    ) -> None:
        async def _boom(
            system_prompt: str,
            allowed_metadata_key: str | None = None,
            tools: list | None = None,
        ) -> Any:
            raise RuntimeError("llm down")

        monkeypatch.setattr(nudge_mod, "_create_nudge_agent", _boom)
        _patch_todos(monkeypatch, [{"content": "ship", "status": "pending"}])

        messages = _large_history()
        result = await _make_summarization()._aapply_compression_under_lock(
            _request(messages, sid), sid
        )
        for _ in range(10):
            await asyncio.sleep(0)

        assert len(result.messages) < len(messages)
        assert fake_nudge_state.get_state(sid, _COMPRESSION_TODO_LOCK_KEY, True) is False


# ---------------------------------------------------------------------------
# _NudgeLimitTool metadata-key gate
# ---------------------------------------------------------------------------


def _tool_request(name: str, metadata: dict | None = None) -> Any:
    return SimpleNamespace(
        tool_call={"name": name, "id": f"call-{name}"},
        tool=SimpleNamespace(metadata=metadata if metadata is not None else {}),
    )


async def _ok_handler(request: Any) -> ToolMessage:
    return ToolMessage(
        content="ok", tool_call_id=request.tool_call["id"], name=request.tool_call["name"]
    )


class TestNudgeLimitToolMetadataKey:
    def test_real_todowrite_carries_marker_and_todoread_does_not(self) -> None:
        tools = {t.name: t for t in build_todolist_tools()}

        assert tools["todowrite"].metadata.get("todo_update") is True
        assert tools["todowrite"].metadata.get("scope") == "main_only"
        assert "todo_update" not in (tools["todoread"].metadata or {})
        assert tools["todoread"].metadata.get("scope") == "main_only"

    @pytest.mark.asyncio
    async def test_real_todowrite_passes_the_gate(self) -> None:
        tools = {t.name: t for t in build_todolist_tools()}
        gate = _NudgeLimitTool(allowed_metadata_key=_COMPRESSION_TODO_METADATA_KEY)
        handled: list[str] = []

        async def handler(request: Any) -> ToolMessage:
            handled.append(request.tool_call["name"])
            return await _ok_handler(request)

        result = await gate.awrap_tool_call(
            _tool_request("todowrite", tools["todowrite"].metadata), handler
        )

        assert isinstance(result, ToolMessage)
        assert result.status == "success"
        assert handled == ["todowrite"]

    @pytest.mark.asyncio
    async def test_todoread_and_untagged_tools_are_denied(self) -> None:
        tools = {t.name: t for t in build_todolist_tools()}
        gate = _NudgeLimitTool(allowed_metadata_key=_COMPRESSION_TODO_METADATA_KEY)
        handled: list[str] = []

        async def handler(request: Any) -> ToolMessage:
            handled.append(request.tool_call["name"])
            return await _ok_handler(request)

        cases = (
            ("todoread", tools["todoread"].metadata),
            ("terminal", {}),
            ("todowrite", {"todo_update": False}),
            ("todowrite", {"todo_update": 1}),
        )
        for name, metadata in cases:
            result = await gate.awrap_tool_call(_tool_request(name, metadata), handler)
            assert isinstance(result, ToolMessage)
            assert result.status == "error", name
            assert "not allowed during nudge phase" in result.content

        assert handled == []

    @pytest.mark.asyncio
    async def test_nudge_metadata_rule_unchanged_when_no_key(self) -> None:
        gate = _NudgeLimitTool()
        handled: list[str] = []

        async def handler(request: Any) -> ToolMessage:
            handled.append(request.tool_call["name"])
            return await _ok_handler(request)

        allowed = await gate.awrap_tool_call(_tool_request("memory", {"nudge": True}), handler)
        assert isinstance(allowed, ToolMessage)
        assert allowed.status == "success"

        denied = await gate.awrap_tool_call(_tool_request("terminal", {}), handler)
        assert isinstance(denied, ToolMessage)
        assert denied.status == "error"
        assert handled == ["memory"]

    def test_is_nudge_allowed_static_metadata_rule(self) -> None:
        assert _NudgeLimitTool._is_nudge_allowed(SimpleNamespace(metadata={"nudge": True})) is True
        assert _NudgeLimitTool._is_nudge_allowed(SimpleNamespace(metadata={})) is False
        assert _NudgeLimitTool._is_nudge_allowed(None) is False


# ---------------------------------------------------------------------------
# Full-fork isolation (stub model, real middleware stack)
# ---------------------------------------------------------------------------


class _StubTodoUpdateModel(BaseChatModel):
    """Emits one ``todowrite`` call, then a final message after its result."""

    @property
    def _llm_type(self) -> str:
        return "stub-todo-update"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Any:  # noqa: ARG002
        return self

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,  # noqa: ARG002
        run_manager: Any = None,  # noqa: ARG002
        **kwargs: Any,  # noqa: ARG002
    ) -> ChatResult:
        if any(isinstance(m, ToolMessage) for m in messages):
            return ChatResult(generations=[ChatGeneration(message=AIMessage(content="updated"))])
        return ChatResult(
            generations=[
                ChatGeneration(
                    message=AIMessage(
                        content="",
                        tool_calls=[
                            {
                                "name": "todowrite",
                                "args": {
                                    "todos": [
                                        {
                                            "content": "todo updated by fork",
                                            "status": "completed",
                                        }
                                    ]
                                },
                                "id": "call-todo-1",
                                "type": "tool_call",
                            }
                        ],
                    )
                )
            ]
        )


class _FailingAfterToolModel(_StubTodoUpdateModel):
    """Executes the todowrite call, then raises on the follow-up model call."""

    @property
    def _llm_type(self) -> str:
        return "stub-failing-after-tool"

    def _generate(
        self,
        messages: list[Any],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        if any(isinstance(m, ToolMessage) for m in messages):
            raise RuntimeError("model exploded after tool result")
        return super()._generate(messages, stop, run_manager, **kwargs)


class _FakeTodoStore:
    def __init__(self) -> None:
        self.by_session: dict[str, list[dict]] = {}
        self.writes: list[tuple[str, list[dict], str | None]] = []

    def get_todos_sync(self, session_id: str) -> list[dict]:
        return list(self.by_session.get(session_id, []))

    def record_update(
        self, session_id: str, todos: list[dict], plan_ref: str | None = None
    ) -> list[dict]:
        self.writes.append((session_id, list(todos), plan_ref))
        self.by_session[session_id] = list(todos)
        return list(todos)


@pytest.fixture
def fake_todo_store(monkeypatch: pytest.MonkeyPatch) -> _FakeTodoStore:
    store = _FakeTodoStore()

    async def _update_todos(
        session_id: str, todos: list[dict], plan_ref: str | None = None
    ) -> list[dict]:
        return store.record_update(session_id, todos, plan_ref)

    monkeypatch.setattr(
        "agent.tools.todolist.registry.store_sqlite.get_todos_sync", store.get_todos_sync
    )
    monkeypatch.setattr(todo_service.TodoService, "update_todos", _update_todos)
    return store


class TestForkIsolation:
    @pytest.mark.asyncio
    async def test_engine_state_zero_pollution_and_derived_cleanup(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_todo_store: _FakeTodoStore,
        sid: str,
    ) -> None:
        derived = f"{sid}{_COMPRESSION_TODO_SESSION_SUFFIX}"
        guard_state = {"halt_decision": "sentinel"}
        seeded: dict[str, Any] = {
            "iteration_budget": 42,
            "iteration_budget_used": 7,
            "tool_guardrail_state": guard_state,
        }
        for key, value in seeded.items():
            state_register_mem.set_state(sid, key, value)
        fake_todo_store.by_session[sid] = [{"content": "initial", "status": "pending"}]
        monkeypatch.setattr("models.build_main_llm", lambda: _StubTodoUpdateModel())

        observed: dict[str, dict[str, Any]] = {}
        real_clear_session = state_register_mem.clear_session

        def _spy_clear_session(session: str) -> bool:
            if session == derived:
                observed["derived"] = state_register_mem.get_all_states(derived)
            return real_clear_session(session)

        monkeypatch.setattr(state_register_mem, "clear_session", _spy_clear_session)

        await update_todos_from_compaction(sid, [HumanMessage(content="did work")])

        assert state_register_mem.get_state(sid, "iteration_budget") == 42
        assert state_register_mem.get_state(sid, "iteration_budget_used") == 7
        assert state_register_mem.get_state(sid, "tool_guardrail_state") is guard_state
        # The fork's middlewares really ran — under the DERIVED key.
        assert observed["derived"]["iteration_budget"] == 90
        assert observed["derived"]["iteration_budget_used"] >= 1
        assert "tool_guardrail_state" in observed["derived"]
        # ...and the derived session is gone afterwards (no mem accumulation).
        assert state_register_mem.has_session(derived) is False
        assert state_register_mem.get_all_states(derived) == {}

    @pytest.mark.asyncio
    async def test_derived_state_cleared_on_failure_and_lock_released(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_todo_store: _FakeTodoStore,
        sid: str,
    ) -> None:
        derived = f"{sid}{_COMPRESSION_TODO_SESSION_SUFFIX}"
        fake_todo_store.by_session[sid] = [{"content": "initial", "status": "pending"}]
        monkeypatch.setattr("models.build_main_llm", lambda: _FailingAfterToolModel())

        await update_todos_from_compaction(sid, [HumanMessage(content="did work")])

        # The tool call went through (middleware state existed), then the model
        # raised — the finally cleanup still removed the derived session.
        assert fake_todo_store.get_todos_sync(sid) == [
            {"content": "todo updated by fork", "status": "completed"}
        ]
        assert state_register_mem.has_session(derived) is False
        assert state_register_mem.get_all_states(derived) == {}
        assert state_register_mem.get_state(sid, _COMPRESSION_TODO_LOCK_KEY, True) is False

    @pytest.mark.asyncio
    async def test_clear_session_failure_is_fail_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_nudge_state: _FakeStateRegister,
        fake_todo_store: _FakeTodoStore,
        sid: str,
    ) -> None:
        fake_todo_store.by_session[sid] = [{"content": "initial", "status": "pending"}]
        monkeypatch.setattr("models.build_main_llm", lambda: _StubTodoUpdateModel())

        def _boom(session: str) -> bool:
            raise RuntimeError("registry down")

        monkeypatch.setattr(fake_nudge_state, "clear_session", _boom)

        await update_todos_from_compaction(sid, [HumanMessage(content="did work")])

        assert fake_todo_store.get_todos_sync(sid) == [
            {"content": "todo updated by fork", "status": "completed"}
        ]
        assert fake_nudge_state.get_state(sid, _COMPRESSION_TODO_LOCK_KEY, True) is False

    @pytest.mark.asyncio
    async def test_shim_writes_main_session_todos_not_derived(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_nudge_state: _FakeStateRegister,
        fake_todo_store: _FakeTodoStore,
        sid: str,
    ) -> None:
        derived = f"{sid}{_COMPRESSION_TODO_SESSION_SUFFIX}"
        fake_todo_store.by_session[sid] = [{"content": "initial", "status": "pending"}]
        monkeypatch.setattr("models.build_main_llm", lambda: _StubTodoUpdateModel())

        await update_todos_from_compaction(sid, [HumanMessage(content="did work")])

        assert fake_todo_store.get_todos_sync(sid) == [
            {"content": "todo updated by fork", "status": "completed"}
        ]
        assert fake_todo_store.get_todos_sync(derived) == []
        assert [write[0] for write in fake_todo_store.writes] == [sid]

    @pytest.mark.asyncio
    async def test_fork_messages_isolated_and_no_checkpointer(
        self,
        monkeypatch: pytest.MonkeyPatch,
        fake_nudge_state: _FakeStateRegister,
        fake_todo_store: _FakeTodoStore,
        sid: str,
    ) -> None:
        created: list[dict] = []
        real_create_agent = nudge_mod.create_agent

        def _spy(**kwargs: Any) -> Any:
            created.append(kwargs)
            return real_create_agent(**kwargs)

        monkeypatch.setattr(nudge_mod, "create_agent", _spy)
        monkeypatch.setattr("models.build_main_llm", lambda: _StubTodoUpdateModel())
        fake_todo_store.by_session[sid] = [{"content": "initial", "status": "pending"}]
        discarded = [HumanMessage(content="hello"), AIMessage(content="world")]
        snapshot = list(discarded)

        await update_todos_from_compaction(sid, discarded)

        assert discarded == snapshot
        assert len(created) == 1
        assert created[0].get("checkpointer") is None
        assert state_register_mem.get_all_states(sid) == {}
        assert fake_todo_store.get_todos_sync(f"{sid}{_COMPRESSION_TODO_SESSION_SUFFIX}") == []


# ---------------------------------------------------------------------------
# todowrite shim: schema derived from the real tool
# ---------------------------------------------------------------------------


class TestTodowriteShim:
    def test_shim_reuses_real_schema_and_description(self) -> None:
        real = {t.name: t for t in build_todolist_tools()}["todowrite"]
        shim = nudge_mod._build_main_session_todowrite("sess-shim")
        real_schema = real.args_schema
        shim_schema = shim.args_schema
        assert isinstance(real_schema, type) and issubclass(real_schema, BaseModel)
        assert isinstance(shim_schema, type) and issubclass(shim_schema, BaseModel)

        assert shim.name == real.name == "todowrite"
        assert shim.description == real.description
        assert shim_schema is real_schema
        assert set(shim_schema.model_fields) == set(real_schema.model_fields)
        assert shim.metadata is not None
        assert shim.metadata.get("todo_update") is True
        assert shim.metadata.get("scope") == "main_only"
        assert shim.handle_tool_error is True

    def test_shim_preserves_injected_state_annotation(self) -> None:
        real = {t.name: t for t in build_todolist_tools()}["todowrite"]
        shim = nudge_mod._build_main_session_todowrite("sess-shim")
        real_schema = real.args_schema
        shim_schema = shim.args_schema
        assert isinstance(real_schema, type) and issubclass(real_schema, BaseModel)
        assert isinstance(shim_schema, type) and issubclass(shim_schema, BaseModel)

        for schema in (real_schema, shim_schema):
            field = schema.model_fields["session_id"]
            assert any(isinstance(meta, InjectedState) for meta in field.metadata)

    @pytest.mark.asyncio
    async def test_shim_ignores_injected_session_and_writes_main(
        self, fake_todo_store: _FakeTodoStore
    ) -> None:
        main_sid = "shim-main"
        derived = f"{main_sid}{_COMPRESSION_TODO_SESSION_SUFFIX}"
        fake_todo_store.by_session[main_sid] = [{"content": "initial", "status": "pending"}]
        shim = nudge_mod._build_main_session_todowrite(main_sid)

        out = await shim.ainvoke(
            {
                "todos": [{"content": "from shim", "status": "completed"}],
                "session_id": derived,
            }
        )

        assert json.loads(out) == [{"content": "from shim", "status": "completed"}]
        assert fake_todo_store.get_todos_sync(main_sid) == [
            {"content": "from shim", "status": "completed"}
        ]
        assert fake_todo_store.get_todos_sync(derived) == []
        assert [write[0] for write in fake_todo_store.writes] == [main_sid]

    @pytest.mark.asyncio
    async def test_shim_forwards_plan_ref(self, fake_todo_store: _FakeTodoStore) -> None:
        main_sid = "shim-plan"
        shim = nudge_mod._build_main_session_todowrite(main_sid)

        await shim.ainvoke(
            {
                "todos": [{"content": "x", "status": "pending"}],
                "plan_ref": ".omo/plans/p.md",
                "session_id": "derived",
            }
        )

        assert fake_todo_store.writes == [
            (main_sid, [{"content": "x", "status": "pending"}], ".omo/plans/p.md")
        ]
