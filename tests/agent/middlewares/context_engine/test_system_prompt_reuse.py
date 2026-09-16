"""P0-2: ContextEngineHook reuses the request when the system prompt matches.

The hook used to call ``request.override(system_message=SystemMessage(...))`` on
every ``wrap_model_call``, even when the content was identical. It now returns
the original request object when ``request.system_message`` already carries the
same content, and only overrides on a real change (or a missing system message).
"""

import uuid

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import agent.middlewares.context_engine.core as ce_core
from agent.middlewares.context_engine.core import ContextEngineHook
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


class TestSystemPromptReuse:
    def test_identical_content_returns_same_request(self, sid):
        # Given a request whose system message already matches the register
        state_register_mem.set_state(sid, "system_prompt", "PROMPT-A")
        request = _request(sid, SystemMessage(content="PROMPT-A"))

        # When the injection helper runs
        result = ContextEngineHook()._wrap_model_call_impl(request)

        # Then no override (same object) is produced
        assert result is request

    def test_changed_content_overrides_with_new_message(self, sid):
        state_register_mem.set_state(sid, "system_prompt", "PROMPT-B")
        request = _request(sid, SystemMessage(content="PROMPT-A"))

        result = ContextEngineHook()._wrap_model_call_impl(request)

        assert result is not request
        assert result.system_message is not None
        assert result.system_message.content == "PROMPT-B"
        assert request.system_message is not None
        assert request.system_message.content == "PROMPT-A"

    def test_missing_system_message_overrides(self, sid):
        state_register_mem.set_state(sid, "system_prompt", "PROMPT-A")
        request = _request(sid, None)

        result = ContextEngineHook()._wrap_model_call_impl(request)

        assert result is not request
        assert result.system_message is not None
        assert result.system_message.content == "PROMPT-A"

    def test_missing_session_id_still_raises(self):
        request = ModelRequest(
            model=_StubModel(),
            messages=[HumanMessage(content="q")],
            state={"messages": [HumanMessage(content="q")]},
        )

        with pytest.raises(RuntimeError, match="Not pass session_id"):
            ContextEngineHook()._wrap_model_call_impl(request)

    def test_wrap_model_call_hands_identical_request_to_handler(self, sid):
        state_register_mem.set_state(sid, "system_prompt", "PROMPT-A")
        request = _request(sid, SystemMessage(content="PROMPT-A"))
        captured: dict[str, object] = {}

        def handler(inner_request: ModelRequest) -> AIMessage:
            captured["request"] = inner_request
            return AIMessage(content="ok")

        ContextEngineHook().wrap_model_call(request, handler)

        assert captured["request"] is request

    @pytest.mark.asyncio
    async def test_awrap_model_call_hands_identical_request_to_handler(self, sid):
        state_register_mem.set_state(sid, "system_prompt", "PROMPT-A")
        request = _request(sid, SystemMessage(content="PROMPT-A"))
        captured: dict[str, object] = {}

        async def handler(inner_request: ModelRequest) -> AIMessage:
            captured["request"] = inner_request
            return AIMessage(content="ok")

        await ContextEngineHook().awrap_model_call(request, handler)

        assert captured["request"] is request


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
        hook = ContextEngineHook()

        first = hook._get_and_reload_system_prompt(sid)
        second = hook._get_and_reload_system_prompt(sid)

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

        prompt = ContextEngineHook()._get_and_reload_system_prompt(sid)

        assert prompt == "BUILT"
        assert state_register_mem.get_state(sid, "system_prompt") == "BUILT"
        assert (sid, "system_prompt", "BUILT") in written
