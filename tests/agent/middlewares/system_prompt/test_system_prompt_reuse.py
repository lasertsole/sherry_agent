"""The ``@dynamic_prompt`` middleware reuses an identical system message.

The middleware used to be an ``AgentMiddleware`` subclass that returned the
original *request* object when ``request.system_message`` already carried the
same content. ``@dynamic_prompt``'s generated wrapper always calls
``request.override(system_message=...)``, so the byte-identity guarantee now
rides on returning the existing ``SystemMessage`` object itself: the override
re-applies the same message object, keeping the serialized model-visible
prefix identical. Changed (or missing) content still injects a fresh message.
"""

import uuid

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import agent.middlewares.system_prompt.core as ce_core
from agent.middlewares.system_prompt.core import system_prompt_injection
from runtime import state_register_mem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _StubModel:
    _llm_type = "fake"


@pytest.fixture
def sid():
    value = "p02-reuse-" + uuid.uuid4().hex
    yield value
    state_register_mem.clear_session(value)


def _request(session_id: str, system_message: SystemMessage | None) -> ModelRequest:
    messages = [HumanMessage(content="q")]
    return ModelRequest(
        model=_StubModel(),
        messages=list(messages),
        system_message=system_message,
        state={"session_id": session_id, "messages": list(messages)},
    )


def _run_sync(request: ModelRequest) -> tuple[ModelRequest, int]:
    captured: dict[str, ModelRequest] = {}
    calls = 0

    def handler(inner_request: ModelRequest) -> AIMessage:
        nonlocal calls
        calls += 1
        captured["request"] = inner_request
        return AIMessage(content="ok")

    system_prompt_injection.wrap_model_call(request, handler)
    return captured["request"], calls


async def _run_async(request: ModelRequest) -> tuple[ModelRequest, int]:
    captured: dict[str, ModelRequest] = {}
    calls = 0

    async def handler(inner_request: ModelRequest) -> AIMessage:
        nonlocal calls
        calls += 1
        captured["request"] = inner_request
        return AIMessage(content="ok")

    await system_prompt_injection.awrap_model_call(request, handler)
    return captured["request"], calls


class TestSystemPromptReuse:
    def test_identical_content_reuses_same_system_message(self, sid):
        # Given a request whose system message already matches the register
        state_register_mem.set_state(sid, "system_prompt", "PROMPT-A")
        original = SystemMessage(content="PROMPT-A")
        request = _request(sid, original)

        # When the middleware wraps the model call
        inner, calls = _run_sync(request)

        # Then the override carries the same message object (byte-identical
        # serialization); only the request wrapper is new.
        assert calls == 1
        assert inner is not request
        assert inner.system_message is original
        assert request.system_message is original

    def test_changed_content_overrides_with_new_message(self, sid):
        state_register_mem.set_state(sid, "system_prompt", "PROMPT-B")
        request = _request(sid, SystemMessage(content="PROMPT-A"))

        inner, _ = _run_sync(request)

        assert inner is not request
        assert inner.system_message is not None
        assert inner.system_message.content == "PROMPT-B"
        assert request.system_message is not None
        assert request.system_message.content == "PROMPT-A"

    def test_missing_system_message_overrides(self, sid):
        state_register_mem.set_state(sid, "system_prompt", "PROMPT-A")
        request = _request(sid, None)

        inner, _ = _run_sync(request)

        assert inner is not request
        assert inner.system_message is not None
        assert inner.system_message.content == "PROMPT-A"

    def test_missing_session_id_still_raises(self):
        request = ModelRequest(
            model=_StubModel(),
            messages=[HumanMessage(content="q")],
            state={"messages": [HumanMessage(content="q")]},
        )

        def handler(inner_request: ModelRequest) -> AIMessage:
            return AIMessage(content="ok")

        with pytest.raises(RuntimeError, match="Not pass session_id"):
            system_prompt_injection.wrap_model_call(request, handler)

    @pytest.mark.asyncio
    async def test_async_path_injects_once_and_reuses(self, sid):
        state_register_mem.set_state(sid, "system_prompt", "PROMPT-A")
        original = SystemMessage(content="PROMPT-A")
        request = _request(sid, original)

        inner, calls = await _run_async(request)

        assert calls == 1
        assert inner.system_message is original

    @pytest.mark.asyncio
    async def test_async_path_missing_session_id_still_raises(self):
        request = ModelRequest(
            model=_StubModel(),
            messages=[HumanMessage(content="q")],
            state={"messages": [HumanMessage(content="q")]},
        )

        async def handler(inner_request: ModelRequest) -> AIMessage:
            return AIMessage(content="ok")

        with pytest.raises(RuntimeError, match="Not pass session_id"):
            await system_prompt_injection.awrap_model_call(request, handler)


class TestSystemPromptReloadSemantics:
    def test_db_fallback_is_cached_to_mem(self, sid, monkeypatch):
        reads: list[str] = []

        class _FakeDB:
            def get_state(self, session_id, key, default=None):
                reads.append(key)
                return "FROM-DB" if key == "system_prompt" else default

            def set_state(self, session_id, key, value):
                return True

        monkeypatch.setattr(ce_core, "state_register_db", _FakeDB())

        first = ce_core._get_and_reload_system_prompt(sid)
        second = ce_core._get_and_reload_system_prompt(sid)

        assert first == "FROM-DB"
        assert second == "FROM-DB"
        assert reads.count("system_prompt") == 1, "second call must be served from mem"
        assert state_register_mem.get_state(sid, "system_prompt") == "FROM-DB"

    def test_build_fallback_persists_to_both_registers(self, sid, monkeypatch):
        written: list[tuple] = []

        class _FakeDB:
            def get_state(self, session_id, key, default=None):
                return default

            def set_state(self, session_id, key, value):
                written.append((session_id, key, value))
                return True

        monkeypatch.setattr(ce_core, "state_register_db", _FakeDB())
        monkeypatch.setattr(ce_core, "build_system_prompt", lambda session_id="": "BUILT")

        prompt = ce_core._get_and_reload_system_prompt(sid)

        assert prompt == "BUILT"
        assert state_register_mem.get_state(sid, "system_prompt") == "BUILT"
        assert (sid, "system_prompt", "BUILT") in written


class TestInjectionWritesSystemPromptCache:
    """Regression lock: injection populates the ``system_prompt`` mem key that
    ``Summarization`` reads for token estimation and rewrites after a compact."""

    def test_first_injection_dual_writes_mem_and_db(self, sid, monkeypatch):
        written: list[tuple] = []

        class _FakeDB:
            def get_state(self, session_id, key, default=None):
                return default

            def set_state(self, session_id, key, value):
                written.append((session_id, key, value))
                return True

        monkeypatch.setattr(ce_core, "state_register_db", _FakeDB())
        monkeypatch.setattr(ce_core, "build_system_prompt", lambda session_id="": "BUILT")

        inner, _ = _run_sync(_request(sid, None))

        assert inner.system_message is not None
        assert inner.system_message.content == "BUILT"
        assert state_register_mem.get_state(sid, "system_prompt") == "BUILT"
        assert (sid, "system_prompt", "BUILT") in written
