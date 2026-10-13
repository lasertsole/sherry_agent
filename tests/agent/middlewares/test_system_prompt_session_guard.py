"""Regression tests: the system-prompt middleware must validate session_id.

The 1.1.10 refactor moved session-id extraction into the shared helper
``agent.middlewares.base.require_session_id``. ``system_prompt/core.py``
calls it directly from the ``@dynamic_prompt`` function; a missing import
would raise ``NameError`` on every model call and kill the whole agent turn
before streaming anything (user-visible: no reply bubbles in the client
chat). These tests lock the wiring in at the real middleware entry point.
"""

import uuid

import pytest
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import AIMessage, HumanMessage

from agent.middlewares.base import require_session_id
from agent.middlewares.system_prompt.core import system_prompt_injection
from runtime import state_register_mem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _StubModel:
    _llm_type = "fake"


def _request(state: dict) -> ModelRequest:
    return ModelRequest(
        model=_StubModel(),
        messages=[HumanMessage(content="q")],
        state=state,
    )


class TestSystemPromptSessionGuard:
    def test_returns_session_id(self):
        assert require_session_id({"session_id": "s1"}, "Not pass session_id") == "s1"

    def test_missing_session_raises_runtime_error_not_name_error(self):
        """Must raise RuntimeError (guard) — a NameError means the shared
        helper import is missing from system_prompt/core.py."""
        request = _request({"messages": []})

        def handler(inner_request: ModelRequest) -> AIMessage:
            return AIMessage(content="ok")

        with pytest.raises(RuntimeError, match="Not pass session_id"):
            system_prompt_injection.wrap_model_call(request, handler)

    def test_session_id_reaches_handler(self):
        sid = "guard-" + uuid.uuid4().hex
        state_register_mem.set_state(sid, "system_prompt", "PROMPT")
        reached: list[bool] = []

        def handler(inner_request: ModelRequest) -> AIMessage:
            reached.append(True)
            return AIMessage(content="ok")

        try:
            system_prompt_injection.wrap_model_call(_request({"session_id": sid}), handler)
        finally:
            state_register_mem.clear_session(sid)

        assert reached == [True]
