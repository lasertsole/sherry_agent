"""P1-b: the Summarization cooldown path must not re-inject an unchanged prompt.

The cooldown branch delivers the rebuilt system prompt for chains without
``ContextEngineHook`` (subagent / nudge pipelines). It now skips the
``request.override`` when the request already carries a ``SystemMessage`` with
identical content, and still overrides on a real change.
"""

import uuid
from types import SimpleNamespace

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage

import agent.middlewares.summarization.core as summarization_module
from runtime import state_register_mem

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]

_COOLDOWN_ROUNDS_KEY = summarization_module._COOLDOWN_ROUNDS_KEY


class StubModel:
    _llm_type = "fake"

    def invoke(self, prompt, config=None):
        return SimpleNamespace(text="summary")

    async def ainvoke(self, prompt, config=None):
        return SimpleNamespace(text="summary")


@pytest.fixture
def sid():
    value = "p1b-" + uuid.uuid4().hex
    yield value
    state_register_mem.clear_session(value)


def _middleware() -> summarization_module.Summarization:
    return summarization_module.Summarization(
        model=StubModel(),
        trigger=[("tokens", 80000)],
        keep=("messages", 10),
        main_llm_context_window=41600,
        need_update_system_prompt=True,
    )


def _request(sid: str, system_message: SystemMessage | None) -> ModelRequest:
    messages = [HumanMessage(content="q"), AIMessage(content="a")]
    return ModelRequest(
        model=StubModel(),
        messages=list(messages),
        system_message=system_message,
        state={"session_id": sid, "messages": list(messages)},
    )


def _arm_cooldown(sid: str) -> None:
    state_register_mem.set_state(sid, _COOLDOWN_ROUNDS_KEY, 3)
    state_register_mem.set_state(sid, "system_prompt", "REBUILT")


class TestCooldownPromptDedup:
    def test_identical_prompt_is_not_overridden(self, sid):
        _arm_cooldown(sid)
        middleware = _middleware()
        middleware._compaction_just_happened = True
        request = _request(sid, SystemMessage(content="REBUILT"))
        captured: dict[str, object] = {}

        def handler(inner: ModelRequest) -> AIMessage:
            captured["request"] = inner
            return AIMessage(content="ok")

        middleware.wrap_model_call(request, handler)

        assert captured["request"] is request

    def test_changed_prompt_is_overridden(self, sid):
        _arm_cooldown(sid)
        middleware = _middleware()
        middleware._compaction_just_happened = True
        request = _request(sid, SystemMessage(content="OLD"))
        captured: dict[str, object] = {}

        def handler(inner: ModelRequest) -> AIMessage:
            captured["request"] = inner
            return AIMessage(content="ok")

        middleware.wrap_model_call(request, handler)

        delivered = captured["request"]
        assert delivered is not request
        assert delivered.system_message is not None
        assert delivered.system_message.content == "REBUILT"

    @pytest.mark.asyncio
    async def test_async_identical_prompt_is_not_overridden(self, sid):
        _arm_cooldown(sid)
        middleware = _middleware()
        middleware._compaction_just_happened = True
        request = _request(sid, SystemMessage(content="REBUILT"))
        captured: dict[str, object] = {}

        async def handler(inner: ModelRequest) -> AIMessage:
            captured["request"] = inner
            return AIMessage(content="ok")

        await middleware.awrap_model_call(request, handler)

        assert captured["request"] is request

    @pytest.mark.asyncio
    async def test_async_changed_prompt_is_overridden(self, sid):
        _arm_cooldown(sid)
        middleware = _middleware()
        middleware._compaction_just_happened = True
        request = _request(sid, SystemMessage(content="OLD"))
        captured: dict[str, object] = {}

        async def handler(inner: ModelRequest) -> AIMessage:
            captured["request"] = inner
            return AIMessage(content="ok")

        await middleware.awrap_model_call(request, handler)

        delivered = captured["request"]
        assert delivered is not request
        assert delivered.system_message is not None
        assert delivered.system_message.content == "REBUILT"
