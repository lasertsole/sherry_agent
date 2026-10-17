"""Unit tests for the plan-extraction trigger and the memory-review dispatch.

Covers:
- ``_detect_todo_all_complete`` — the decision branches (no todos / first
  all-complete fire / already-fired fingerprint / not-all-complete reset).
- ``schedule_memory_review`` — per-compression memory-review dispatch and
  lock semantics. Plan extraction no longer rides this path (it forks on plan
  completion — see ``tests/agent/middlewares/test_plan_extraction_trigger.py``).
- ``system_prompt_injection`` — the ``@dynamic_prompt`` middleware does not
  override the after-agent hooks (nudge dispatch moved to the compression
  pipeline).
- ``_build_plan_context`` — plan_ref resolution (state first, todo fallback,
  session-hash fallback) and the reverse lock that an external-orchestration
  plan reference never resolves.
"""

from __future__ import annotations

import asyncio

import pytest
from langchain.agents.middleware import AgentMiddleware
from langchain_core.messages import HumanMessage

from agent.middlewares.system_prompt import core as ce_core
from agent.middlewares.summarization import nudges as nudge_mod
from agent.middlewares.summarization.nudges import (
    _PLAN_EXTRACTION_FIRED_KEY,
    _detect_todo_all_complete,
    schedule_memory_review,
)
from agent.tools.todolist.knowledge.identity import fallback_plan_name

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
    monkeypatch.setattr(nudge_mod, "state_register_db", fake)
    return fake


@pytest.fixture
def fake_state_mem(monkeypatch):
    fake = _FakeStateRegister()
    monkeypatch.setattr(nudge_mod, "state_register_mem", fake)
    return fake


@pytest.fixture
def prompt_stub(monkeypatch):
    monkeypatch.setattr(ce_core, "_get_and_reload_system_prompt", lambda session_id: "sys-prompt")


def _patch_todos(monkeypatch, todos: list[dict]) -> None:
    monkeypatch.setattr(
        "agent.tools.todolist.registry.store_sqlite.get_todos_sync",
        lambda session_id: todos,
    )


def _nudge_spies(monkeypatch) -> list[tuple]:
    calls: list[tuple] = []

    async def _memory(session_id, system_prompt, messages):
        calls.append(("memory", session_id, system_prompt))

    async def _plan(session_id):
        calls.append(("plan", session_id))

    monkeypatch.setattr(nudge_mod, "_nudge_memory", _memory)
    monkeypatch.setattr(nudge_mod, "_run_plan_extraction", _plan)
    return calls


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


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

    def test_already_fired_with_an_unchanged_plan_returns_false(self, monkeypatch, fake_state_db):
        todos = [{"status": "completed", "id": "t1", "content": "a"}]
        _patch_todos(monkeypatch, todos)
        fake_state_db.set_state("sess-fired", _PLAN_EXTRACTION_FIRED_KEY, True)
        # The fired flag alone is no longer enough: the fingerprint of what was
        # extracted has to match the current plan (that is what catches an
        # in-place edit of a completed todo — see the trigger suite).
        fake_state_db.set_state(
            "sess-fired",
            nudge_mod._PLAN_SNAPSHOT_HASH_KEY,
            nudge_mod._plan_snapshot_hash(todos),
        )
        assert _detect_todo_all_complete("sess-fired") is False

    def test_not_all_complete_resets_fired(self, monkeypatch, fake_state_db):
        fake_state_db.set_state("sess-reset", _PLAN_EXTRACTION_FIRED_KEY, True)
        _patch_todos(monkeypatch, [{"status": "completed"}, {"status": "in_progress"}])
        assert _detect_todo_all_complete("sess-reset") is False
        assert fake_state_db.get_state("sess-reset", _PLAN_EXTRACTION_FIRED_KEY, False) is False


# ---------------------------------------------------------------------------
# schedule_memory_review (compression-time trigger)
# ---------------------------------------------------------------------------


class TestScheduleCompressionNudges:
    @pytest.mark.asyncio
    async def test_first_compression_dispatches_memory(
        self, monkeypatch, fake_state_db, fake_state_mem, prompt_stub
    ):
        calls = _nudge_spies(monkeypatch)
        monkeypatch.setattr(nudge_mod, "_detect_todo_all_complete", lambda session_id: False)

        scheduled = schedule_memory_review("sess-first", [HumanMessage("hi")])
        await _settle()

        assert scheduled is True
        assert calls == [("memory", "sess-first", "sys-prompt")]
        assert fake_state_db.data == {}

    @pytest.mark.asyncio
    async def test_consecutive_compressions_both_dispatch(
        self, monkeypatch, fake_state_db, fake_state_mem, prompt_stub
    ):
        calls = _nudge_spies(monkeypatch)
        monkeypatch.setattr(nudge_mod, "_detect_todo_all_complete", lambda session_id: False)

        first = schedule_memory_review("sess-twice", [HumanMessage("hi")])
        await _settle()
        second = schedule_memory_review("sess-twice", [HumanMessage("hi")])
        await _settle()

        assert first is True
        assert second is True
        assert calls == [
            ("memory", "sess-twice", "sys-prompt"),
            ("memory", "sess-twice", "sys-prompt"),
        ]

    @pytest.mark.asyncio
    async def test_a_finished_plan_still_does_not_dispatch_the_fork(
        self, monkeypatch, fake_state_db, fake_state_mem, prompt_stub
    ):
        """Compaction is memory-only: the plan fork belongs to the completion hook."""
        calls = _nudge_spies(monkeypatch)
        monkeypatch.setattr(nudge_mod, "_PLAN_EXTRACTION_ENABLED", True)
        monkeypatch.setattr(nudge_mod, "_detect_todo_all_complete", lambda session_id: True)

        scheduled = schedule_memory_review("sess-plan", [HumanMessage("hi")])
        await _settle()

        assert scheduled is True
        assert calls == [("memory", "sess-plan", "sys-prompt")]

    @pytest.mark.asyncio
    async def test_lock_skips_dispatch(
        self, monkeypatch, fake_state_db, fake_state_mem, prompt_stub
    ):
        calls = _nudge_spies(monkeypatch)
        detect_calls: list[str] = []
        monkeypatch.setattr(
            nudge_mod,
            "_detect_todo_all_complete",
            lambda session_id: detect_calls.append(session_id) or True,
        )
        fake_state_mem.set_state("sess-locked", nudge_mod._NUDGE_MEMORY_LOCK_KEY, True)

        scheduled = schedule_memory_review("sess-locked", [HumanMessage("hi")])
        await _settle()

        assert scheduled is False
        assert calls == []
        assert detect_calls == []

    def test_no_running_loop_skips_dispatch(self, monkeypatch, fake_state_db, fake_state_mem):
        calls = _nudge_spies(monkeypatch)
        detect_calls: list[str] = []
        monkeypatch.setattr(
            nudge_mod,
            "_detect_todo_all_complete",
            lambda session_id: detect_calls.append(session_id) or True,
        )

        scheduled = schedule_memory_review("sess-sync", [HumanMessage("hi")])

        assert scheduled is False
        assert calls == []
        assert detect_calls == []


# ---------------------------------------------------------------------------
# system_prompt_injection: after-agent hooks absent
# ---------------------------------------------------------------------------


class TestAfterAgentHooksRemoved:
    def test_middleware_does_not_override_after_agent(self):
        assert type(ce_core.system_prompt_injection).after_agent is AgentMiddleware.after_agent
        assert type(ce_core.system_prompt_injection).aafter_agent is AgentMiddleware.aafter_agent


# ---------------------------------------------------------------------------
# _build_plan_context
# ---------------------------------------------------------------------------


class TestBuildPlanContext:
    def test_empty_without_todos(self, monkeypatch, fake_state_db):
        _patch_todos(monkeypatch, [])
        assert nudge_mod._build_plan_context("sess-notodos") == {}

    def test_reads_plan_ref_file_from_session_tree(self, monkeypatch, tmp_path, fake_state_db):
        plan_file = tmp_path / "workspace" / "sessions" / "sess-ctx" / "plans" / "my-plan.md"
        plan_file.parent.mkdir(parents=True)
        plan_file.write_text("# My Plan\n", encoding="utf-8")
        from config import path as config_path

        monkeypatch.setattr(config_path, "ROOT_DIR", tmp_path)
        monkeypatch.setattr(config_path, "SESSIONS_DIR", tmp_path / "workspace" / "sessions")
        _patch_todos(
            monkeypatch,
            [{"status": "completed", "plan_ref": "workspace/sessions/sess-ctx/plans/my-plan.md"}],
        )
        monkeypatch.setattr(nudge_mod, "_read_subagent_runs", lambda session_id: [])

        context = nudge_mod._build_plan_context("sess-ctx")

        assert context["plan_name"] == "my-plan"
        assert context["plan_path"] == str(plan_file)
        assert context["plan_content"] == "# My Plan\n"
        assert "ledger_entries" not in context

    def test_session_state_plan_ref_wins_and_missing_file_falls_back(
        self, monkeypatch, tmp_path, fake_state_db
    ):
        from config import path as config_path

        monkeypatch.setattr(config_path, "ROOT_DIR", tmp_path)
        monkeypatch.setattr(config_path, "SESSIONS_DIR", tmp_path / "workspace" / "sessions")
        missing_ref = "workspace/sessions/abcdefgh-session/plans/missing.md"
        fake_state_db.set_state("abcdefgh-session", "plan_ref", missing_ref)
        _patch_todos(monkeypatch, [{"status": "completed", "plan_ref": "other.md"}])
        monkeypatch.setattr(nudge_mod, "_read_subagent_runs", lambda session_id: [])

        context = nudge_mod._build_plan_context("abcdefgh-session")

        assert context["plan_path"] == missing_ref
        assert context["plan_name"] == fallback_plan_name("abcdefgh-session")
        assert context["plan_content"] == ""

    def test_resolves_session_scoped_plan_ref(self, monkeypatch, tmp_path, fake_state_db):
        plan_file = tmp_path / "workspace" / "sessions" / "sess-ctx" / "plans" / "new-plan.md"
        plan_file.parent.mkdir(parents=True)
        plan_file.write_text("# New Plan\n", encoding="utf-8")
        from config import path as config_path

        monkeypatch.setattr(config_path, "ROOT_DIR", tmp_path)
        monkeypatch.setattr(config_path, "SESSIONS_DIR", tmp_path / "workspace" / "sessions")
        plan_ref = "workspace/sessions/sess-ctx/plans/new-plan.md"
        _patch_todos(monkeypatch, [{"status": "completed", "plan_ref": plan_ref}])
        monkeypatch.setattr(nudge_mod, "_read_subagent_runs", lambda session_id: [])

        context = nudge_mod._build_plan_context("sess-ctx")

        assert context["plan_name"] == "new-plan"
        assert context["plan_path"] == str(plan_file)
        assert context["plan_content"] == "# New Plan\n"

    def test_legacy_external_reference_never_resolves(self, monkeypatch, tmp_path, fake_state_db):
        legacy = tmp_path / ".omo" / "plans" / "moved.md"
        legacy.parent.mkdir(parents=True)
        legacy.write_text("# Forbidden\n", encoding="utf-8")
        from config import path as config_path

        monkeypatch.setattr(config_path, "ROOT_DIR", tmp_path)
        monkeypatch.setattr(config_path, "SESSIONS_DIR", tmp_path / "workspace" / "sessions")
        _patch_todos(monkeypatch, [{"status": "completed", "plan_ref": ".omo/plans/moved.md"}])
        monkeypatch.setattr(nudge_mod, "_read_subagent_runs", lambda session_id: [])

        context = nudge_mod._build_plan_context("sess-ctx")

        assert context["plan_content"] == ""
        assert context["plan_path"] == ".omo/plans/moved.md"
        assert context["plan_name"] == fallback_plan_name("sess-ctx")


# ---------------------------------------------------------------------------
# _run_plan_extraction (plan-context injection + fail-open)
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


class TestRunPlanExtraction:
    """The fork: plan context in, prompt rendered, lock released, fail-open."""

    @pytest.mark.asyncio
    async def test_injects_plan_context_and_renders_both_parts(self, monkeypatch, prompt_stub):
        prompts: list[str] = []

        async def _create_plan_extraction_agent(system_prompt):
            return _CapturingAgent(prompts)

        monkeypatch.setattr(nudge_mod, "_build_plan_context", lambda session_id: {"plan_name": "p"})
        monkeypatch.setattr(
            nudge_mod, "_create_plan_extraction_agent", _create_plan_extraction_agent
        )
        monkeypatch.setattr(nudge_mod, "state_register_mem", _FakeStateRegister())
        monkeypatch.setattr(nudge_mod, "state_register_db", _FakeStateRegister())

        await nudge_mod._run_plan_extraction("sess-prompt")

        assert len(prompts) == 1
        prompt = prompts[0]
        assert '"plan_name": "p"' in prompt
        assert "## Part 1: Structured Knowledge Extraction" in prompt
        assert "## Part 2: Skill Library Update" in prompt

    @pytest.mark.asyncio
    async def test_agent_failure_is_swallowed_and_lock_released(self, monkeypatch, prompt_stub):
        async def _create_plan_extraction_agent(system_prompt):
            return _FailingAgent()

        fake_state = _FakeStateRegister()
        monkeypatch.setattr(nudge_mod, "_build_plan_context", lambda session_id: {"plan_name": "p"})
        monkeypatch.setattr(
            nudge_mod, "_create_plan_extraction_agent", _create_plan_extraction_agent
        )
        monkeypatch.setattr(nudge_mod, "state_register_mem", fake_state)
        fake_db = _FakeStateRegister()
        monkeypatch.setattr(nudge_mod, "state_register_db", fake_db)

        await nudge_mod._run_plan_extraction("sess-fail")

        assert (
            fake_state.get_state("sess-fail", nudge_mod._PLAN_EXTRACTION_LOCK_KEY, False) is False
        )
        assert fake_db.get_state("sess-fail", nudge_mod._PLAN_SNAPSHOT_HASH_KEY, "") == "", (
            "a failed pass must not claim the plan was extracted"
        )

    @pytest.mark.asyncio
    async def test_no_plan_context_skips_the_agent(self, monkeypatch, prompt_stub):
        prompts: list[str] = []

        async def _create_plan_extraction_agent(system_prompt):
            return _CapturingAgent(prompts)

        monkeypatch.setattr(nudge_mod, "_build_plan_context", lambda session_id: {})
        monkeypatch.setattr(
            nudge_mod, "_create_plan_extraction_agent", _create_plan_extraction_agent
        )
        monkeypatch.setattr(nudge_mod, "state_register_mem", _FakeStateRegister())
        monkeypatch.setattr(nudge_mod, "state_register_db", _FakeStateRegister())

        await nudge_mod._run_plan_extraction("sess-nocontext")

        assert prompts == []
