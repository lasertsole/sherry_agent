"""Unit tests for the Phase-B todo-complete plan-extraction trigger.

Covers:
- ``_detect_todo_all_complete`` — the four decision branches (no todos /
  first all-complete fire / already-fired / not-all-complete reset).
- ``_after_agent_impl`` — the 5-tuple return contract, memory-counter reset,
  and the lock short-circuit.
- ``after_agent`` / ``aafter_agent`` — nudge dispatch through the
  monkeypatched ``_nudge_plan_extraction`` stand-in.
- ``_build_plan_context`` — plan_ref resolution (state first, todo fallback,
  session-<id> fallback) and the ``.omo/start-work/ledger.jsonl`` read.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import HumanMessage

from agent.middlewares.context_engine import core as ce_core
from agent.middlewares.context_engine import nudge as nudge_mod
from agent.middlewares.context_engine.core import (
    ContextEngineHook,
    _PLAN_EXTRACTION_FIRED_KEY,
    _detect_todo_all_complete,
)

pytestmark = pytest.mark.unit


class _FakeStateRegister:
    """Minimal in-memory stand-in for state_register_db / state_register_mem."""

    def __init__(self) -> None:
        self.data: dict[tuple[str, str], object] = {}

    def get_state(self, session_id: str, key: str, default: object = None) -> object:
        return self.data.get((session_id, key), default)

    def set_state(self, session_id: str, key: str, value: object) -> None:
        self.data[(session_id, key)] = value


@pytest.fixture
def fake_state_db(monkeypatch):
    fake = _FakeStateRegister()
    monkeypatch.setattr(ce_core, "state_register_db", fake)
    monkeypatch.setattr(nudge_mod, "state_register_db", fake)
    return fake


@pytest.fixture
def hook(monkeypatch):
    monkeypatch.setattr(
        ContextEngineHook,
        "_get_and_reload_system_prompt",
        staticmethod(lambda session_id: "sys-prompt"),
    )
    return ContextEngineHook()


def _patch_todos(monkeypatch, todos: list[dict]) -> None:
    monkeypatch.setattr(
        "agent.tools.todolist.registry.store_sqlite.get_todos_sync",
        lambda session_id: todos,
    )


# ---------------------------------------------------------------------------
# _detect_todo_all_complete
# ---------------------------------------------------------------------------


class TestDetectTodoAllComplete:
    def test_no_todos_returns_false(self, monkeypatch, fake_state_db):
        _patch_todos(monkeypatch, [])
        assert _detect_todo_all_complete("sess-none") is False
        assert fake_state_db.get_state("sess-none", _PLAN_EXTRACTION_FIRED_KEY, False) is False

    def test_all_complete_fires_on_first_call(self, monkeypatch, fake_state_db):
        _patch_todos(monkeypatch, [{"status": "completed"}, {"status": "cancelled"}])
        assert _detect_todo_all_complete("sess-first") is True
        assert fake_state_db.get_state("sess-first", _PLAN_EXTRACTION_FIRED_KEY, False) is True

    def test_already_fired_returns_false(self, monkeypatch, fake_state_db):
        _patch_todos(monkeypatch, [{"status": "completed"}])
        fake_state_db.set_state("sess-fired", _PLAN_EXTRACTION_FIRED_KEY, True)
        assert _detect_todo_all_complete("sess-fired") is False

    def test_not_all_complete_resets_fired(self, monkeypatch, fake_state_db):
        fake_state_db.set_state("sess-reset", _PLAN_EXTRACTION_FIRED_KEY, True)
        _patch_todos(monkeypatch, [{"status": "completed"}, {"status": "in_progress"}])
        assert _detect_todo_all_complete("sess-reset") is False
        assert fake_state_db.get_state("sess-reset", _PLAN_EXTRACTION_FIRED_KEY, False) is False


# ---------------------------------------------------------------------------
# _after_agent_impl
# ---------------------------------------------------------------------------


class TestAfterAgentImpl:
    def test_returns_plan_bit_and_resets_memory_counter(self, hook, fake_state_db, monkeypatch):
        fake_state_db.set_state(
            "sess-bits",
            ce_core._NUDGE_MEMORY_COUNT_KEY,
            ce_core._NUDGE_MEMORY_THRESHOLD - 1,
        )
        monkeypatch.setattr(ContextEngineHook, "_is_lock", staticmethod(lambda session_id: False))
        monkeypatch.setattr(ce_core, "_detect_todo_all_complete", lambda session_id: True)

        result = hook._after_agent_impl(
            {"session_id": "sess-bits", "messages": [HumanMessage("hi")]}
        )

        assert result is not None
        session_id, system_prompt, _messages, need_memory, need_plan = result
        assert session_id == "sess-bits"
        assert system_prompt == "sys-prompt"
        assert need_memory is True
        assert need_plan is True
        assert fake_state_db.get_state("sess-bits", ce_core._NUDGE_MEMORY_COUNT_KEY, 0) == 0

    def test_lock_short_circuits_before_todo_detection(self, hook, fake_state_db, monkeypatch):
        monkeypatch.setattr(ContextEngineHook, "_is_lock", staticmethod(lambda session_id: True))
        detected: list[str] = []
        monkeypatch.setattr(
            ce_core,
            "_detect_todo_all_complete",
            lambda session_id: detected.append(session_id) or True,
        )

        result = hook._after_agent_impl({"session_id": "sess-lock", "messages": []})

        assert result is None
        assert detected == []
        assert fake_state_db.get_state("sess-lock", ce_core._NUDGE_MEMORY_COUNT_KEY, 0) == 1

    def test_missing_session_id_raises(self, hook):
        with pytest.raises(RuntimeError):
            hook._after_agent_impl({"session_id": "   ", "messages": []})


# ---------------------------------------------------------------------------
# after_agent / aafter_agent dispatch
# ---------------------------------------------------------------------------


class TestNudgeDispatch:
    @pytest.mark.asyncio
    async def test_aafter_agent_dispatches_plan_extraction(self, monkeypatch):
        hook = ContextEngineHook()
        monkeypatch.setattr(
            hook,
            "_after_agent_impl",
            lambda state: ("sess-dispatch", "sys", [HumanMessage("hi")], True, True),
        )
        calls: list[str] = []

        async def _persist(session_id, messages):
            calls.append("persist")

        async def _memory(session_id, system_prompt, messages):
            calls.append("memory")

        async def _plan(session_id, system_prompt, messages):
            calls.append("plan")

        monkeypatch.setattr(ce_core, "add_messages", _persist)
        monkeypatch.setattr(ce_core, "_nudge_memory", _memory)
        monkeypatch.setattr(ce_core, "_nudge_plan_extraction", _plan)

        await hook.aafter_agent(
            {"session_id": "sess-dispatch", "messages": [HumanMessage("hi")]}, None
        )

        assert sorted(calls) == ["memory", "persist", "plan"]

    def test_after_agent_never_dispatches_nudge(self, monkeypatch):
        """Sync after_agent is protocol-only: nudge dispatch belongs to aafter_agent.

        The old sync path bridged through run_async() (a fresh thread + loop),
        which cannot acquire the loop-bound NUDGE lane semaphore.
        """
        hook = ContextEngineHook()
        monkeypatch.setattr(
            hook,
            "_after_agent_impl",
            lambda state: ("sess-sync", "sys", [], True, True),
        )
        calls: list[str] = []
        monkeypatch.setattr(ce_core, "_nudge_memory", lambda *args: calls.append("memory"))
        monkeypatch.setattr(ce_core, "_nudge_plan_extraction", lambda *args: calls.append("plan"))

        hook.after_agent({"session_id": "sess-sync", "messages": []}, None)

        assert calls == []


# ---------------------------------------------------------------------------
# _build_plan_context
# ---------------------------------------------------------------------------


class TestBuildPlanContext:
    def test_empty_without_todos(self, monkeypatch, fake_state_db):
        _patch_todos(monkeypatch, [])
        assert nudge_mod._build_plan_context("sess-notodos") == {}

    def test_reads_plan_ref_file_and_ledger(self, monkeypatch, tmp_path, fake_state_db):
        plan_file = tmp_path / ".omo" / "plans" / "my-plan.md"
        plan_file.parent.mkdir(parents=True)
        plan_file.write_text("# My Plan\n", encoding="utf-8")
        ledger = tmp_path / ".omo" / "start-work" / "ledger.jsonl"
        ledger.parent.mkdir(parents=True)
        ledger.write_text(
            '{"plan": "my-plan", "step": 1}\n{"plan": "other", "step": 2}\nnot-json\n',
            encoding="utf-8",
        )
        monkeypatch.setattr(nudge_mod, "ROOT_DIR", tmp_path)
        _patch_todos(monkeypatch, [{"status": "completed", "plan_ref": ".omo/plans/my-plan.md"}])
        monkeypatch.setattr(nudge_mod, "_read_subagent_runs", lambda session_id: [])

        context = nudge_mod._build_plan_context("sess-ctx")

        assert context["plan_name"] == "my-plan"
        assert context["plan_path"] == ".omo/plans/my-plan.md"
        assert context["plan_content"] == "# My Plan\n"
        assert context["ledger_entries"] == [{"plan": "my-plan", "step": 1}]

    def test_session_state_plan_ref_wins_and_missing_file_falls_back(
        self, monkeypatch, tmp_path, fake_state_db
    ):
        monkeypatch.setattr(nudge_mod, "ROOT_DIR", tmp_path)
        fake_state_db.set_state("abcdefgh-session", "plan_ref", ".omo/plans/missing.md")
        _patch_todos(monkeypatch, [{"status": "completed", "plan_ref": ".omo/plans/other.md"}])
        monkeypatch.setattr(nudge_mod, "_read_subagent_runs", lambda session_id: [])

        context = nudge_mod._build_plan_context("abcdefgh-session")

        assert context["plan_path"] == ".omo/plans/missing.md"
        assert context["plan_name"] == "session-abcdefgh"
        assert context["plan_content"] == ""


# ---------------------------------------------------------------------------
# _nudge_plan_extraction (plan-context injection + fail-open)
# ---------------------------------------------------------------------------


class _CapturingAgent:
    def __init__(self, sink: list[str]) -> None:
        self._sink = sink

    async def ainvoke(self, input):
        self._sink.append(input["messages"][-1].content)
        return {"messages": [HumanMessage("extracted")]}


class _FailingAgent:
    async def ainvoke(self, input):
        raise RuntimeError("llm down")


class TestNudgePlanExtraction:
    @pytest.mark.asyncio
    async def test_injects_plan_context_and_renders_both_parts(self, monkeypatch):
        prompts: list[str] = []

        async def _create_nudge_agent(system_prompt):
            return _CapturingAgent(prompts)

        monkeypatch.setattr(nudge_mod, "_build_plan_context", lambda session_id: {"plan_name": "p"})
        monkeypatch.setattr(nudge_mod, "_create_nudge_agent", _create_nudge_agent)
        monkeypatch.setattr(nudge_mod, "state_register_mem", _FakeStateRegister())

        await nudge_mod._nudge_plan_extraction("sess-prompt", "sys", [HumanMessage("hi")])

        assert len(prompts) == 1
        prompt = prompts[0]
        assert '"plan_name": "p"' in prompt
        assert "## Part 1: Structured Knowledge Extraction" in prompt
        assert "## Part 2: Skill Library Update" in prompt

    @pytest.mark.asyncio
    async def test_agent_failure_is_swallowed_and_lock_released(self, monkeypatch):
        async def _create_nudge_agent(system_prompt):
            return _FailingAgent()

        fake_state = _FakeStateRegister()
        monkeypatch.setattr(nudge_mod, "_build_plan_context", lambda session_id: {"plan_name": "p"})
        monkeypatch.setattr(nudge_mod, "_create_nudge_agent", _create_nudge_agent)
        monkeypatch.setattr(nudge_mod, "state_register_mem", fake_state)

        await nudge_mod._nudge_plan_extraction("sess-fail", "sys", [])

        assert (
            fake_state.get_state("sess-fail", nudge_mod._PLAN_EXTRACTION_LOCK_KEY, False) is False
        )

    @pytest.mark.asyncio
    async def test_no_plan_context_skips_the_agent(self, monkeypatch):
        prompts: list[str] = []

        async def _create_nudge_agent(system_prompt):
            return _CapturingAgent(prompts)

        monkeypatch.setattr(nudge_mod, "_build_plan_context", lambda session_id: {})
        monkeypatch.setattr(nudge_mod, "_create_nudge_agent", _create_nudge_agent)
        monkeypatch.setattr(nudge_mod, "state_register_mem", _FakeStateRegister())

        await nudge_mod._nudge_plan_extraction("sess-nocontext", "sys", [])

        assert prompts == []
