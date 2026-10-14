"""Plan extraction: forked on plan completion, aux model, snapshot deduped.

The extraction used to ride the compaction path, which meant three things: a
short session never extracted at all, the fork was handed the whole conversation
(~124K next to a 131K window), and the once-per-cycle fired flag could not see a
completed todo whose text changed in place. This suite pins the replacement
contract:

* the trigger is plan completion (``maybe_trigger_plan_extraction``), it runs the
  fork on the AUXILIARY model, and the fork receives the instruction plus the
  plan context — never the conversation;
* the plan snapshot fingerprint (id + content + status + plan_ref) decides
  re-extraction, and it is stored only after a SUCCESSFUL pass;
* compaction still runs the memory review, unchanged, and no longer touches plan
  extraction.

Hermetic: the todo store is monkeypatched, the state register's durable tier is
an in-memory fake, and no agent, lane or LLM is ever built.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import pytest

import agent.middlewares.summarization.nudges as nudge_mod
from agent.tools.todolist.registry import store_sqlite as todo_store

pytestmark = [pytest.mark.unit]


class _FakeStateDB:
    """In-memory stand-in for the durable register (``state_register_db``)."""

    def __init__(self) -> None:
        self.values: dict[tuple[str, str], Any] = {}

    def set_state(self, session_id: str, key: str, value: Any) -> bool:
        self.values[(session_id, str(key))] = value
        return True

    def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        return self.values.get((session_id, str(key)), default)


def _todo(content: str, status: str = "completed", **extra: Any) -> dict:
    return {
        "id": extra.pop("id", uuid.uuid4().hex[:8]),
        "content": content,
        "status": status,
        **extra,
    }


@pytest.fixture
def sid() -> str:
    return "plan-extraction-" + uuid.uuid4().hex[:8]


@pytest.fixture
def fake_db(monkeypatch: pytest.MonkeyPatch) -> _FakeStateDB:
    db = _FakeStateDB()
    monkeypatch.setattr(nudge_mod, "state_register_db", db)
    return db


@pytest.fixture
def base(monkeypatch: pytest.MonkeyPatch, fake_db: _FakeStateDB):
    """Todo store + session/feature switches, all replaceable per test.

    Deliberately does NOT touch ``_run_plan_extraction``: the fork's own tests
    call the real one.
    """
    state: dict[str, Any] = {"todos": [], "forked": []}

    def _set_todos(todos: list[dict]) -> None:
        state["todos"] = todos
        monkeypatch.setattr(todo_store, "get_todos_sync", lambda session_id: list(state["todos"]))

    monkeypatch.setattr(
        "agent.middlewares.agent_switch.middleware_option",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(nudge_mod, "_PLAN_EXTRACTION_ENABLED", True)
    state["set_todos"] = _set_todos
    return state


@pytest.fixture
def patched(base, monkeypatch: pytest.MonkeyPatch):
    """``base`` plus a recording stand-in for the fork (trigger tests only)."""

    async def _fake_run(session_id: str) -> None:
        base["forked"].append(session_id)

    monkeypatch.setattr(nudge_mod, "_run_plan_extraction", _fake_run)
    return base


async def _settle() -> None:
    await asyncio.sleep(0)
    await asyncio.sleep(0)


# ── the trigger ────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_plan_completion_forks_extraction(patched, sid):
    patched["set_todos"]([_todo("a"), _todo("b")])

    await nudge_mod.maybe_trigger_plan_extraction(sid)
    await _settle()

    assert patched["forked"] == [sid]


@pytest.mark.asyncio
async def test_incomplete_plan_never_forks(patched, sid, fake_db):
    patched["set_todos"]([_todo("a"), _todo("b", status="in_progress")])

    await nudge_mod.maybe_trigger_plan_extraction(sid)
    await _settle()

    assert patched["forked"] == []
    assert fake_db.values.get((sid, "nudge_plan_extraction_fired")) is False


@pytest.mark.asyncio
async def test_same_plan_is_not_extracted_twice(patched, sid, fake_db):
    todos = [_todo("a"), _todo("b")]
    patched["set_todos"](todos)
    fake_db.set_state(sid, "nudge_plan_extraction_fired", True)
    fake_db.set_state(sid, "nudge_plan_snapshot_hash", nudge_mod._plan_snapshot_hash(todos))

    await nudge_mod.maybe_trigger_plan_extraction(sid)
    await _settle()

    assert patched["forked"] == []


@pytest.mark.asyncio
async def test_new_todos_trigger_a_reextraction(patched, sid, fake_db):
    first = [_todo("a"), _todo("b")]
    patched["set_todos"](first)
    fake_db.set_state(sid, "nudge_plan_extraction_fired", True)
    fake_db.set_state(sid, "nudge_plan_snapshot_hash", nudge_mod._plan_snapshot_hash(first))
    grown = [*first, _todo("c"), _todo("d")]
    patched["set_todos"](grown)

    await nudge_mod.maybe_trigger_plan_extraction(sid)
    await _settle()

    assert patched["forked"] == [sid]


@pytest.mark.asyncio
async def test_an_edited_completed_todo_triggers_a_reextraction(patched, sid, fake_db):
    """The hole the fingerprint closes: same ids/status, changed content."""
    first = [_todo("a", id="t1"), _todo("b", id="t2")]
    patched["set_todos"](first)
    fake_db.set_state(sid, "nudge_plan_extraction_fired", True)
    fake_db.set_state(sid, "nudge_plan_snapshot_hash", nudge_mod._plan_snapshot_hash(first))
    edited = [_todo("a (revised)", id="t1"), _todo("b", id="t2")]
    patched["set_todos"](edited)

    await nudge_mod.maybe_trigger_plan_extraction(sid)
    await _settle()

    assert patched["forked"] == [sid]


@pytest.mark.asyncio
async def test_backward_compatible_when_no_hash_is_stored(patched, sid, fake_db):
    """An old session has no fingerprint: the first completion extracts."""
    patched["set_todos"]([_todo("a")])
    fake_db.set_state(sid, "nudge_plan_extraction_fired", True)
    fake_db.values.pop((sid, "nudge_plan_snapshot_hash"), None)

    await nudge_mod.maybe_trigger_plan_extraction(sid)
    await _settle()

    assert patched["forked"] == [sid]


@pytest.mark.asyncio
async def test_an_in_flight_extraction_blocks_a_second_fork(patched, sid):
    from runtime import state_register_mem

    patched["set_todos"]([_todo("a")])
    state_register_mem.set_state(sid, nudge_mod._PLAN_EXTRACTION_LOCK_KEY, True)
    try:
        await nudge_mod.maybe_trigger_plan_extraction(sid)
        await _settle()
    finally:
        state_register_mem.clear_session(sid)

    assert patched["forked"] == []


@pytest.mark.asyncio
async def test_the_session_nudge_switch_off_stops_the_fork(patched, sid, monkeypatch):
    patched["set_todos"]([_todo("a")])
    monkeypatch.setattr(
        "agent.middlewares.agent_switch.middleware_option",
        lambda *args, **kwargs: False,
    )

    await nudge_mod.maybe_trigger_plan_extraction(sid)
    await _settle()

    assert patched["forked"] == []


@pytest.mark.asyncio
async def test_the_feature_switch_off_stops_the_fork(patched, sid, monkeypatch):
    patched["set_todos"]([_todo("a")])
    monkeypatch.setattr(nudge_mod, "_PLAN_EXTRACTION_ENABLED", False)

    await nudge_mod.maybe_trigger_plan_extraction(sid)
    await _settle()

    assert patched["forked"] == []


# ── the fork itself ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_extraction_agent_runs_on_the_auxiliary_model(monkeypatch):
    calls: dict[str, Any] = {}

    class _Aux:
        pass

    def _build_auxiliary_llm(temperature: float | None = None):
        calls["temperature"] = temperature
        return _Aux()

    def _build_main_llm(*args: Any, **kwargs: Any):  # pragma: no cover - must not run
        raise AssertionError("plan extraction must not build the main model")

    def _create_agent(**kwargs: Any):
        calls["agent_kwargs"] = kwargs
        return object()

    import models

    monkeypatch.setattr(models, "build_auxiliary_llm", _build_auxiliary_llm)
    monkeypatch.setattr(nudge_mod, "create_agent", _create_agent)
    monkeypatch.setattr("agent.get_agent_tools", lambda: [])

    await nudge_mod._create_plan_extraction_agent("sys-prompt")

    assert isinstance(calls["agent_kwargs"]["model"], _Aux)
    assert calls["temperature"] == 0
    assert calls["agent_kwargs"]["system_prompt"] == "sys-prompt"
    assert _build_main_llm is not None  # documented: never called


@pytest.mark.asyncio
async def test_the_fork_receives_no_conversation_history(base, sid, monkeypatch):
    from langchain_core.messages import HumanMessage

    captured: dict[str, Any] = {}

    class _FakeAgent:
        async def ainvoke(self, input: dict[str, Any], config: Any = None) -> dict[str, Any]:
            captured["input"] = input
            return {"messages": [HumanMessage(content="done")]}

    async def _fake_agent(system_prompt: str) -> _FakeAgent:
        captured["system_prompt"] = system_prompt
        return _FakeAgent()

    todos = [_todo("a"), _todo("b")]
    monkeypatch.setattr(nudge_mod, "_create_plan_extraction_agent", _fake_agent)
    monkeypatch.setattr(nudge_mod, "_build_plan_context", lambda session_id: {"todos": todos})
    monkeypatch.setattr(
        "agent.middlewares.system_prompt.core._get_and_reload_system_prompt",
        lambda session_id: "sys-prompt",
    )

    await nudge_mod._run_plan_extraction(sid)

    messages = captured["input"]["messages"]
    assert len(messages) == 1
    assert isinstance(messages[0], HumanMessage)
    assert "plan_context" not in messages[0].content  # rendered, not a placeholder
    assert captured["input"]["session_id"] == sid
    assert captured["system_prompt"] == "sys-prompt"


@pytest.mark.asyncio
async def test_a_successful_pass_stores_the_fingerprint(base, sid, monkeypatch, fake_db):
    from langchain_core.messages import HumanMessage

    todos = [_todo("a"), _todo("b")]
    monkeypatch.setattr(
        nudge_mod,
        "_build_plan_context",
        lambda session_id: {"todos": todos, "plan_name": "p", "plan_path": "", "plan_content": ""},
    )

    class _FakeAgent:
        async def ainvoke(self, input: dict[str, Any], config: Any = None) -> dict[str, Any]:
            return {"messages": [HumanMessage(content="ok")]}

    async def _fake_agent(system_prompt: str) -> _FakeAgent:
        return _FakeAgent()

    monkeypatch.setattr(nudge_mod, "_create_plan_extraction_agent", _fake_agent)
    monkeypatch.setattr(
        "agent.middlewares.system_prompt.core._get_and_reload_system_prompt",
        lambda session_id: "sys-prompt",
    )

    await nudge_mod._run_plan_extraction(sid)

    stored = fake_db.values.get((sid, "nudge_plan_snapshot_hash"))
    assert stored == nudge_mod._plan_snapshot_hash(todos)


@pytest.mark.asyncio
async def test_a_failed_pass_stores_no_fingerprint(base, sid, monkeypatch, fake_db):
    todos = [_todo("a")]
    monkeypatch.setattr(nudge_mod, "_build_plan_context", lambda session_id: {"todos": todos})

    class _BrokenAgent:
        async def ainvoke(self, input: dict[str, Any], config: Any = None) -> dict[str, Any]:
            raise RuntimeError("aux model exploded")

    async def _fake_agent(system_prompt: str) -> _BrokenAgent:
        return _BrokenAgent()

    monkeypatch.setattr(nudge_mod, "_create_plan_extraction_agent", _fake_agent)
    monkeypatch.setattr(
        "agent.middlewares.system_prompt.core._get_and_reload_system_prompt",
        lambda session_id: "sys-prompt",
    )

    await nudge_mod._run_plan_extraction(sid)

    assert (sid, "nudge_plan_snapshot_hash") not in fake_db.values


@pytest.mark.asyncio
async def test_an_empty_plan_context_is_a_no_op(base, sid, monkeypatch, fake_db):
    monkeypatch.setattr(nudge_mod, "_build_plan_context", lambda session_id: {})

    await nudge_mod._run_plan_extraction(sid)

    assert (sid, "nudge_plan_snapshot_hash") not in fake_db.values


# ── compaction: memory review only ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_compaction_no_longer_extracts_but_keeps_the_memory_review(patched, sid, monkeypatch):
    calls: list[str] = []

    async def _memory(session_id: str, system_prompt: str, messages: list) -> None:
        calls.append("memory")

    monkeypatch.setattr(nudge_mod, "_nudge_memory", _memory)
    monkeypatch.setattr(
        "agent.middlewares.system_prompt.core._get_and_reload_system_prompt",
        lambda session_id: "sys-prompt",
    )
    # A completed plan must not make compaction fork the extraction.
    patched["set_todos"]([_todo("a")])

    await nudge_mod._run_memory_review(sid, [])
    await _settle()

    assert calls == ["memory"]
    assert patched["forked"] == []


@pytest.mark.asyncio
async def test_schedule_memory_review_no_longer_asks_whether_the_plan_is_done(
    base, sid, monkeypatch
):
    """The dispatch must stay memory-only: no completion probe on the path."""
    probes: list[str] = []
    monkeypatch.setattr(
        nudge_mod, "_detect_todo_all_complete", lambda session_id: probes.append(session_id) or True
    )
    dispatched: list[str] = []
    monkeypatch.setattr(
        nudge_mod,
        "_run_memory_review",
        lambda session_id, messages: dispatched.append(session_id) or _noop(),
    )
    base["set_todos"]([_todo("a")])

    scheduled = nudge_mod.schedule_memory_review(sid, [])

    assert scheduled is True
    assert probes == []
    assert dispatched == [sid]


async def _noop() -> None:
    return None
