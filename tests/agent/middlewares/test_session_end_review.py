"""Session-end memory salvage: snapshot once, then review two compression waves."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage

import agent.middlewares.summarization.background_reviews as review_mod
import agent.tools.memory as memory_module
from agent.tools.memory import MemoryStore
from config.features import BACKGROUND_REVIEW_CONFIG

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _FakeAgent:
    def __init__(self, calls: list[dict[str, Any]], wave: int) -> None:
        self._calls = calls
        self._wave = wave

    async def ainvoke(self, payload: dict[str, Any]) -> dict[str, Any]:
        self._calls.append({"wave": self._wave, "payload": payload})
        return {"messages": [AIMessage(content=f"wave-{self._wave}")]}


class _FakeRegister:
    def __init__(self) -> None:
        self.cleared: list[str] = []

    def clear_session(self, session_id: str) -> None:
        self.cleared.append(session_id)


def _tool() -> SimpleNamespace:
    return SimpleNamespace(
        name="memory",
        metadata={"background_review": True, "background_review_memory": True},
    )


def test_the_session_end_tool_face_carries_the_memory_only_gate():
    tool = review_mod._memory_only_tool_face()[0]
    assert tool.name == "memory"
    assert tool.metadata["background_review_memory"] is True


def test_the_cutoff_is_clamped_to_the_snapshot(monkeypatch: pytest.MonkeyPatch):
    from agent.middlewares.summarization import core as summarization_core

    monkeypatch.setattr(
        summarization_core.Summarization,
        "_determine_cutoff",
        lambda _self, _messages, _session_id: 99,
    )

    assert review_mod._session_end_cutoff(_messages(), "sess-clamp") == 3


@pytest.fixture(autouse=True)
def _reset(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(BACKGROUND_REVIEW_CONFIG, "session_end_review_enabled", True)
    monkeypatch.setitem(BACKGROUND_REVIEW_CONFIG, "session_end_review_timeout_s", 5)
    review_mod._SESSION_END_REVIEW_TASKS.clear()
    review_mod._SESSION_END_REVIEW_INFLIGHT.clear()
    yield
    review_mod._SESSION_END_REVIEW_TASKS.clear()
    review_mod._SESSION_END_REVIEW_INFLIGHT.clear()


async def _settle() -> None:
    for _ in range(10):
        await asyncio.sleep(0)


def _messages() -> list[HumanMessage]:
    return [HumanMessage(content="one"), HumanMessage(content="two"), HumanMessage(content="three")]


@pytest.mark.asyncio
async def test_salvage_splits_at_the_compression_cutoff_and_schedules(
    monkeypatch: pytest.MonkeyPatch,
):
    messages = _messages()
    model = object()
    tools = [_tool()]
    scheduled: list[dict[str, Any]] = []

    async def _snapshot(_session_id: str):
        return messages, model, "captured prompt", tools, 2

    async def _run(**kwargs):
        scheduled.append(kwargs)

    monkeypatch.setattr(review_mod, "_snapshot_session_for_salvage", _snapshot)
    monkeypatch.setattr(review_mod, "_run_session_end_salvage", _run)

    assert await review_mod.salvage_session_memory("sess-two-wave") is True
    await _settle()

    assert len(scheduled) == 1
    call = scheduled[0]
    assert [m.content for m in call["first_wave"]] == ["one", "two"]
    assert [m.content for m in call["rest_wave"]] == ["three"]
    assert call["model"] is model
    assert call["system_prompt"] == "captured prompt"
    assert call["tools"] == tools
    assert "sess-two-wave" not in review_mod._SESSION_END_REVIEW_INFLIGHT
    assert not review_mod._SESSION_END_REVIEW_TASKS


@pytest.mark.asyncio
async def test_no_compression_cut_is_one_wave_over_all_messages(
    monkeypatch: pytest.MonkeyPatch,
):
    messages = _messages()
    scheduled: list[dict[str, Any]] = []

    async def _snapshot(_session_id: str):
        return messages, object(), "prompt", [_tool()], 0

    async def _run(**kwargs):
        scheduled.append(kwargs)

    monkeypatch.setattr(review_mod, "_snapshot_session_for_salvage", _snapshot)
    monkeypatch.setattr(review_mod, "_run_session_end_salvage", _run)

    assert await review_mod.salvage_session_memory("sess-no-cut") is True
    await _settle()

    assert [m.content for m in scheduled[0]["first_wave"]] == ["one", "two", "three"]
    assert scheduled[0]["rest_wave"] == []


@pytest.mark.asyncio
async def test_a_full_cut_is_one_wave_over_all_messages(
    monkeypatch: pytest.MonkeyPatch,
):
    messages = _messages()
    scheduled: list[dict[str, Any]] = []

    async def _snapshot(_session_id: str):
        return messages, object(), "prompt", [_tool()], len(messages)

    async def _run(**kwargs):
        scheduled.append(kwargs)

    monkeypatch.setattr(review_mod, "_snapshot_session_for_salvage", _snapshot)
    monkeypatch.setattr(review_mod, "_run_session_end_salvage", _run)

    assert await review_mod.salvage_session_memory("sess-full-cut") is True
    await _settle()

    assert len(scheduled[0]["first_wave"]) == 3
    assert scheduled[0]["rest_wave"] == []


@pytest.mark.asyncio
async def test_snapshot_captures_model_prompt_tools_and_cutoff(
    monkeypatch: pytest.MonkeyPatch,
):
    messages = _messages()
    model = object()
    tools = [_tool()]

    async def _read_messages(_session_id: str):
        return messages

    monkeypatch.setattr("agent.state_port.read_messages", _read_messages)
    monkeypatch.setattr(
        "agent.middlewares.system_prompt.core._get_and_reload_system_prompt",
        lambda _session_id: "captured prompt",
    )
    monkeypatch.setattr(review_mod, "_build_session_model", lambda _session_id: model)
    monkeypatch.setattr(review_mod, "_session_tool_face", lambda _session_id: tools)
    monkeypatch.setattr(review_mod, "_session_end_cutoff", lambda _messages, _sid: 2)

    snapshot = await review_mod._snapshot_session_for_salvage("sess-snapshot")

    assert snapshot is not None
    snapshot_messages, snapshot_model, prompt, snapshot_tools, cutoff = snapshot
    assert snapshot_messages == messages
    assert snapshot_model is model
    assert prompt == "captured prompt"
    assert snapshot_tools == tools
    assert cutoff == 2


@pytest.mark.asyncio
async def test_empty_transcript_skips_salvage(monkeypatch: pytest.MonkeyPatch):
    async def _snapshot(_session_id: str):
        return None

    monkeypatch.setattr(review_mod, "_snapshot_session_for_salvage", _snapshot)

    assert await review_mod.salvage_session_memory("sess-empty") is False
    assert "sess-empty" not in review_mod._SESSION_END_REVIEW_INFLIGHT


@pytest.mark.asyncio
async def test_snapshot_failure_is_fail_open(monkeypatch: pytest.MonkeyPatch):
    async def _snapshot(_session_id: str):
        raise RuntimeError("checkpoint unavailable")

    monkeypatch.setattr(review_mod, "_snapshot_session_for_salvage", _snapshot)

    assert await review_mod.salvage_session_memory("sess-broken") is False
    assert "sess-broken" not in review_mod._SESSION_END_REVIEW_INFLIGHT


@pytest.mark.asyncio
async def test_snapshot_timeout_is_fail_open(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(review_mod, "_SESSION_END_SNAPSHOT_TIMEOUT_S", 0.01)

    async def _snapshot(_session_id: str):
        await asyncio.sleep(1)
        return None

    monkeypatch.setattr(review_mod, "_snapshot_session_for_salvage", _snapshot)

    assert await review_mod.salvage_session_memory("sess-timeout") is False
    assert "sess-timeout" not in review_mod._SESSION_END_REVIEW_INFLIGHT


@pytest.mark.asyncio
async def test_master_switch_off_skips_snapshot(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(BACKGROUND_REVIEW_CONFIG, "session_end_review_enabled", False)
    called: list[str] = []

    async def _snapshot(session_id: str):
        called.append(session_id)
        return None

    monkeypatch.setattr(review_mod, "_snapshot_session_for_salvage", _snapshot)

    assert await review_mod.salvage_session_memory("sess-off") is False
    assert called == []


@pytest.mark.asyncio
async def test_duplicate_salvage_is_refused(monkeypatch: pytest.MonkeyPatch):
    review_mod._SESSION_END_REVIEW_INFLIGHT.add("sess-duplicate")
    called: list[str] = []

    async def _snapshot(session_id: str):
        called.append(session_id)
        return None

    monkeypatch.setattr(review_mod, "_snapshot_session_for_salvage", _snapshot)

    assert await review_mod.salvage_session_memory("sess-duplicate") is False
    assert called == []


@pytest.mark.asyncio
async def test_two_wave_runner_refreshes_the_prompt_between_waves(
    monkeypatch: pytest.MonkeyPatch,
):
    waves: list[dict[str, Any]] = []
    rebuilds: list[tuple[str, str]] = []

    async def _wave(**kwargs):
        waves.append(kwargs)
        return True

    def _rebuild(session_id: str, fallback: str) -> str:
        rebuilds.append((session_id, fallback))
        return "updated prompt"

    monkeypatch.setattr(review_mod, "_run_session_end_wave", _wave)
    monkeypatch.setattr(review_mod, "_rebuild_session_end_prompt", _rebuild)

    first = [HumanMessage(content="first")]
    rest = [HumanMessage(content="second")]
    tools = [_tool()]
    await review_mod._run_session_end_salvage(
        session_id="sess-order",
        model=object(),
        system_prompt="captured prompt",
        tools=tools,
        first_wave=first,
        rest_wave=rest,
    )

    assert [wave["wave"] for wave in waves] == [1, 2]
    assert waves[0]["system_prompt"] == "captured prompt"
    assert waves[0]["messages"] == first
    assert waves[0]["tools"] == tools
    assert waves[1]["system_prompt"] == "updated prompt"
    assert waves[1]["messages"] == rest
    assert [tool.name for tool in waves[1]["tools"]] == ["memory"]
    assert rebuilds == [("sess-order", "captured prompt")]


@pytest.mark.asyncio
async def test_failed_first_wave_keeps_the_captured_prompt_for_wave_two(
    monkeypatch: pytest.MonkeyPatch,
):
    waves: list[dict[str, Any]] = []
    rebuilds: list[str] = []

    async def _wave(**kwargs):
        waves.append(kwargs)
        return False

    monkeypatch.setattr(review_mod, "_run_session_end_wave", _wave)
    monkeypatch.setattr(
        review_mod,
        "_rebuild_session_end_prompt",
        lambda *_args: rebuilds.append("called") or "unexpected",
    )

    await review_mod._run_session_end_salvage(
        session_id="sess-failed-first",
        model=object(),
        system_prompt="captured prompt",
        tools=[_tool()],
        first_wave=[HumanMessage(content="first")],
        rest_wave=[HumanMessage(content="second")],
    )

    assert [wave["wave"] for wave in waves] == [1, 2]
    assert waves[1]["system_prompt"] == "captured prompt"
    assert rebuilds == []


@pytest.mark.asyncio
async def test_wave_uses_a_derived_session_key_and_clears_it(
    monkeypatch: pytest.MonkeyPatch,
):
    built: list[dict[str, Any]] = []
    register = _FakeRegister()

    def _build(*args, **kwargs):
        built.append({"args": args, "kwargs": kwargs})
        return _FakeAgent([], 1)

    seen: dict[str, Any] = {}

    async def _invoke(_agent, _session_id, _messages, _prompt, *, origin_session=None):
        seen["origin_session"] = origin_session
        return AIMessage(content="ok")

    monkeypatch.setattr(review_mod, "_build_memory_review_agent", _build)
    monkeypatch.setattr(review_mod, "_invoke_memory_review", _invoke)
    monkeypatch.setattr(review_mod, "state_register_mem", register)

    ok = await review_mod._run_session_end_wave(
        session_id="sess-derived",
        wave=1,
        model=object(),
        system_prompt="prompt",
        tools=[_tool()],
        messages=[HumanMessage(content="hello")],
        prompt_template="review",
    )

    assert ok is True
    assert built[0]["kwargs"]["allowed_metadata_key"] == "background_review_memory"
    assert seen["origin_session"] == "sess-derived"
    assert register.cleared == ["sess-derived::session-end-salvage:1"]


@pytest.mark.asyncio
async def test_run_session_end_salvage_writes_both_waves_to_memory(
    tmp_path, monkeypatch: pytest.MonkeyPatch
):
    from context_engine.store.memory_entry_repository import MemoryEntryRepository

    repo = MemoryEntryRepository(db_path=tmp_path / "memory.db")
    monkeypatch.setattr(memory_module, "memory_entry_repo", repo)
    store = MemoryStore()
    store.load_from_store()
    monkeypatch.setattr(memory_module, "memory_store", store)

    calls: list[dict[str, Any]] = []
    wave_number = {"value": 0}

    def _build(*args, **kwargs):
        wave_number["value"] += 1
        return _FakeAgent(calls, wave_number["value"])

    async def _invoke(agent, _session_id, messages, _prompt, *, origin_session=None):
        wave = wave_number["value"]
        memory_module.memory_store.add("user", f"wave-{wave}-fact", origin_session=origin_session)
        return await agent.ainvoke({})

    monkeypatch.setattr(review_mod, "_build_memory_review_agent", _build)
    monkeypatch.setattr(review_mod, "_invoke_memory_review", _invoke)
    monkeypatch.setattr(
        review_mod,
        "_rebuild_session_end_prompt",
        lambda _sid, _fallback: "updated prompt",
    )
    monkeypatch.setattr(review_mod, "state_register_mem", _FakeRegister())

    await review_mod._run_session_end_salvage(
        session_id="sess-memory",
        model=object(),
        system_prompt="captured prompt",
        tools=[_tool()],
        first_wave=[HumanMessage(content="first")],
        rest_wave=[HumanMessage(content="second")],
    )

    text = store.format_live_content("user")
    assert "wave-1-fact" in text
    assert "wave-2-fact" in text
    origins = {entry["origin_session"] for entry in repo.list_entries("user", limit=None)}
    assert origins == {"sess-memory"}
    assert [call["wave"] for call in calls] == [1, 2]
