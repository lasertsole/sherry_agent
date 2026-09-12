"""P0-2: compression-failure cooldown must survive a process restart.

The anti-thrash counters live in ``state_register_mem`` (volatile). These
tests pin the persistence layer that mirrors them into ``state_register_db``
and rehydrates them on first access in a new process.
"""

import uuid
from types import SimpleNamespace

import pytest

from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

import agent.middlewares.summarization as summarization_module
from runtime import state_register_mem

pytestmark = [pytest.mark.module]

_SKIP_LLM_KEY = getattr(summarization_module, "_SKIP_LLM_KEY")
_COMPRESSION_COUNT_KEY = getattr(summarization_module, "_COMPRESSION_COUNT_KEY")
_COMPRESSION_INEFFECTIVE_KEY = getattr(summarization_module, "_COMPRESSION_INEFFECTIVE_KEY")
_LAST_STRATEGY_KEY = getattr(summarization_module, "_LAST_STRATEGY_KEY")


class _RecordingModel:
    _llm_type = "fake"

    def __init__(self):
        self.calls = []

    def invoke(self, prompt, config=None):
        self.calls.append(prompt)
        return SimpleNamespace(text="## Goal\n- summary " + "x" * 80)

    async def ainvoke(self, prompt, config=None):
        self.calls.append(prompt)
        return SimpleNamespace(text="## Goal\n- summary " + "x" * 80)


class _FakeStateDB:
    def __init__(self):
        self.store: dict[tuple[str, str], object] = {}

    def get_state(self, session_id, key, default=None):
        return self.store.get((session_id, key), default)

    def set_state(self, session_id, key, value):
        self.store[(session_id, key)] = value
        return True


@pytest.fixture
def sid():
    value = "p02-" + uuid.uuid4().hex
    yield value
    state_register_mem.clear_session(value)


@pytest.fixture
def fake_db(monkeypatch):
    db = _FakeStateDB()
    monkeypatch.setattr(summarization_module, "state_register_db", db)
    monkeypatch.setattr(summarization_module, "_RESTORED_COOLDOWN_SESSIONS", set())
    return db


def _make_middleware(model=None, context_window=8000):
    return summarization_module.Summarization(
        model=model or _RecordingModel(),
        trigger=[("tokens", 500)],
        keep=("messages", 10),
        main_llm_context_window=context_window,
        need_update_system_prompt=False,
    )


def _history(turns=6, out_chars=4000):
    messages = []
    for i in range(turns):
        tool_call_id = f"p02-call-{i}"
        messages.append(HumanMessage(content=f"question {i}"))
        messages.append(
            AIMessage(
                content="",
                tool_calls=[{"name": "search", "args": {"q": f"query-{i}"}, "id": tool_call_id}],
            )
        )
        messages.append(ToolMessage(content="x" * out_chars, tool_call_id=tool_call_id))
    return messages


def _make_request(messages, session_id, model):
    return ModelRequest(
        model=model,
        messages=list(messages),
        state={"session_id": session_id, "messages": list(messages)},
    )


def test_failed_compression_persists_skip_llm_to_db(fake_db, sid):
    mw = _make_middleware()
    messages = [HumanMessage(content="a"), AIMessage(content="b")]

    # Given one already-ineffective attempt in this session.
    state_register_mem.set_state(sid, _COMPRESSION_INEFFECTIVE_KEY, 1)

    # When a second ineffective compression is recorded (the failure path).
    mw._record_compression(sid, messages, list(messages))

    # Then the failure counter is mirrored to the durable register.
    assert fake_db.get_state(sid, _COMPRESSION_INEFFECTIVE_KEY) == 2

    # And the effectiveness gate flips the skip flag, also persisted.
    mw._should_skip_compression(sid)
    assert fake_db.get_state(sid, _SKIP_LLM_KEY) is True


def test_restart_restores_skip_flag_and_honors_it(fake_db, sid):
    # Given a prior process left a persisted failure cooldown behind.
    failing = _make_middleware()
    unchanged = [HumanMessage(content="a"), AIMessage(content="b")]
    state_register_mem.set_state(sid, _COMPRESSION_INEFFECTIVE_KEY, 1)
    failing._record_compression(sid, unchanged, list(unchanged))
    failing._should_skip_compression(sid)
    assert fake_db.get_state(sid, _SKIP_LLM_KEY) is True

    # When the process restarts: memory is empty and first access rehydrates.
    state_register_mem.clear_session(sid)
    assert not state_register_mem.get_state(sid, _SKIP_LLM_KEY, False)
    model = _RecordingModel()
    restarted = _make_middleware(model)
    restarted._maybe_restore_cooldown_state(sid)

    # Then the skip flag is back in memory...
    assert state_register_mem.get_state(sid, _SKIP_LLM_KEY) is True

    # ...and is honored: compression falls back without calling the LLM.
    request = _make_request(_history(), sid, model)
    restarted._apply_compression(request, sid)
    assert model.calls == []
    assert state_register_mem.get_state(sid, _LAST_STRATEGY_KEY) == "fallback"


def test_successful_compression_resets_persisted_state(fake_db, sid):
    mw = _make_middleware()

    # Given a persisted failure cooldown from earlier attempts.
    state_register_mem.set_state(sid, _SKIP_LLM_KEY, True)
    state_register_mem.set_state(sid, _COMPRESSION_INEFFECTIVE_KEY, 2)
    state_register_mem.set_state(sid, _COMPRESSION_COUNT_KEY, 3)
    mw._persist_cooldown_state(sid)

    # When an effective compression succeeds.
    before = [HumanMessage(content="a"), AIMessage(content="b")]
    after = [HumanMessage(content="summary")]
    mw._record_compression(sid, before, after, strategy_used="dedup")

    # Then the durable state is reset too.
    assert fake_db.get_state(sid, _COMPRESSION_INEFFECTIVE_KEY) == 0
    assert fake_db.get_state(sid, _SKIP_LLM_KEY) is False
    assert fake_db.get_state(sid, _COMPRESSION_COUNT_KEY) == 4
