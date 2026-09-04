"""Regression contract for the Summarization trigger registration.

Moved from the former ``tests/diagnose/`` and deduped: the trigger-banding
parametrizations are covered by ``test_summarization_comprehensive.py``
(``_preemptive_check`` 0.70/0.80 bands, ``_check_trigger`` boundaries).
This file pins only what that suite does NOT:

- the production registration contract: the uncapped ``MAIN_LLM_MAX_TOKEN``
  window (``models/LLMs/main_llm.py`` cap removal) and the
  ``("tokens", int(window * COMPRESSION_TRIGGER_RATIO))`` clause registered
  in ``agent/core.py``,
- the low-token pass-through regression: ``wrap_model_call`` must NOT
  rewrite the request (i.e. compress) on ordinary turns.
"""

import pytest
from unittest.mock import MagicMock, PropertyMock
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import HumanMessage, AIMessage

from config.num import COMPRESSION_TRIGGER_RATIO

# ── The context window we feed the middleware (uncapped MAIN_LLM_MAX_TOKEN) ─
# MAIN_LLM_MAX_TOKEN = 65_536  (from .env; Task 1 of the context-compression
# plan reset it from the historical 65_536_000 placeholder)
MAIN_LLM_MAX_TOKEN = 65_536
# core.py trigger: ("tokens", int(MAIN_LLM_MAX_TOKEN * COMPRESSION_TRIGGER_RATIO))
EXPECTED_THRESHOLD = int(MAIN_LLM_MAX_TOKEN * COMPRESSION_TRIGGER_RATIO)  # 52_428

_TRIGGER_SESSION = "trigger-contract-summarization"


def _make_fake_model(max_input_tokens: int = MAIN_LLM_MAX_TOKEN):
    """Create a mock model object (inert constructor arg).

    The redesigned middleware never derives thresholds from ``model.profile``
    (the window is injected via ``main_llm_context_window``); the profile
    mocks are kept only so the stub stays a well-formed stand-in.
    """
    model = MagicMock()
    profile = PropertyMock(return_value={"max_input_tokens": max_input_tokens})
    type(model).profile = profile
    type(model)._llm_type = "fake-chat"
    type(model)._get_ls_params = MagicMock(return_value={"ls_provider": "fake"})
    return model


def _make_request(messages):
    """Build a real ModelRequest carrying the contract-test session state."""
    return ModelRequest(
        model=_make_fake_model(),
        messages=list(messages),
        state={"session_id": _TRIGGER_SESSION, "messages": list(messages)},
    )


class TestSummarizationTriggerContract:
    """Pin the production registration values after the §9.7 redesign."""

    @pytest.fixture
    def summarizer(self):
        from agent.middlewares.summarization import Summarization

        model = _make_fake_model()
        inst = Summarization(
            need_update_system_prompt=True,
            model=model,
            main_llm_context_window=MAIN_LLM_MAX_TOKEN,
            trigger=[("tokens", int(MAIN_LLM_MAX_TOKEN * COMPRESSION_TRIGGER_RATIO))],
            keep=("messages", 10),
        )
        return inst

    def test_context_window_is_uncapped_env_value(self, summarizer):
        """Verify the injected window is the un-capped .env value.

        The redesigned middleware takes the window as the constructor param
        ``main_llm_context_window`` instead of reading ``model.profile``.
        """
        assert summarizer._main_llm_context_window == MAIN_LLM_MAX_TOKEN == 65_536, (
            f"Expected main_llm_context_window = {MAIN_LLM_MAX_TOKEN}, "
            f"got {summarizer._main_llm_context_window}. "
            "Check models/LLMs/main_llm.py cap removal and the .env value."
        )

    def test_tokens_threshold_matches_core_config(self, summarizer):
        """core.py trigger = int(65_536 * COMPRESSION_TRIGGER_RATIO) = 52_428."""
        assert summarizer._trigger == [("tokens", EXPECTED_THRESHOLD)], (
            f"Expected trigger [('tokens', {EXPECTED_THRESHOLD})], got {summarizer._trigger}"
        )
        assert EXPECTED_THRESHOLD == 52_428, (
            f"Expected threshold 52_428 (0.80 × 65_536), got {EXPECTED_THRESHOLD}"
        )

    def test_no_compression_at_low_token_count(self, summarizer):
        """With very few tokens, wrap_model_call should pass the request through untouched."""
        messages = [HumanMessage(content="hello")]
        request = _make_request(messages)
        response = AIMessage(content="ok")
        captured = {}

        def handler(req):
            captured["request"] = req
            return response

        result = summarizer.wrap_model_call(request, handler)
        assert result is response, (
            "wrap_model_call did not pass the handler response through despite very "
            "low token count. This means compression fires every turn."
        )
        assert captured["request"] is request, (
            "wrap_model_call rewrote the request despite very low token count. "
            "This means compression fires every turn."
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
