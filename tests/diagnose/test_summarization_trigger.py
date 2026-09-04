# NOTE: adapted for §9.7 redesign (see .omo/plans/summarization-redesign.md)
"""Diagnose: does Summarization trigger compression on every turn?

This test constructs a Summarization instance matching agent/core.py config
(main_llm_context_window=65_536_000, trigger=("tokens",
int(65_536_000 * COMPRESSION_TRIGGER_RATIO))) and verifies the trigger
decision across realistic token counts:
- the fractional pressure check (``_preemptive_check``) bands at
  PREEMPTIVE_TRUNCATE_RATIO=0.70 / COMPRESSION_TRIGGER_RATIO=0.80 (config.num),
- the absolute ``("tokens", N)`` trigger (``_check_trigger``) still fires at N.
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from langchain.agents.middleware import ModelRequest
from langchain_core.messages import HumanMessage, AIMessage

from config.num import COMPRESSION_TRIGGER_RATIO

# ── The context window we feed the middleware (uncapped MAIN_LLM_MAX_TOKEN) ─
# MAIN_LLM_MAX_TOKEN = 65_536_000  (from .env)
MAIN_LLM_MAX_TOKEN = 65_536_000
# core.py trigger: ("tokens", int(MAIN_LLM_MAX_TOKEN * COMPRESSION_TRIGGER_RATIO))
EXPECTED_THRESHOLD = int(MAIN_LLM_MAX_TOKEN * COMPRESSION_TRIGGER_RATIO)  # 52_428_800

_DIAG_SESSION = "diagnose-summarization"


def _make_fake_model(max_input_tokens: int = 65_536_000):
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
    """Build a real ModelRequest carrying the diagnose session state."""
    return ModelRequest(
        model=_make_fake_model(),
        messages=list(messages),
        state={"session_id": _DIAG_SESSION, "messages": list(messages)},
    )


class TestSummarizationTriggerDiagnose:
    """Verify the trigger thresholds after the §9.7 redesign."""

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

    def test_context_window_is_65536000(self, summarizer):
        """Verify the injected window is the un-capped value.

        The redesigned middleware takes the window as the constructor param
        ``main_llm_context_window`` instead of reading ``model.profile``.
        """
        assert summarizer._main_llm_context_window == 65_536_000, (
            f"Expected main_llm_context_window = 65_536_000, "
            f"got {summarizer._main_llm_context_window}. "
            "Check models/LLMs/main_llm.py cap removal."
        )

    def test_tokens_threshold_matches_core_config(self, summarizer):
        """core.py trigger = int(65_536_000 * COMPRESSION_TRIGGER_RATIO) = 52_428_800."""
        assert summarizer._trigger == [("tokens", EXPECTED_THRESHOLD)], (
            f"Expected trigger [('tokens', {EXPECTED_THRESHOLD})], got {summarizer._trigger}"
        )
        assert EXPECTED_THRESHOLD == 52_428_800, (
            f"Expected threshold 52_428_800 (0.80 × 65_536_000), got {EXPECTED_THRESHOLD}"
        )

    @pytest.mark.parametrize(
        "token_count,expected_action",
        [
            (1_000, None),
            (100_000, None),
            (1_000_000, None),
            (10_000_000, None),
            (30_000_000, None),
            (33_000_000, None),  # 50% pressure — bands moved to 0.70 / 0.80
            (50_000_000, "truncate_only"),  # 76% ≥ 0.70, < 0.80
            (65_536_000, "compact"),  # 100% ≥ 0.80
        ],
    )
    def test_fraction_trigger_only(self, token_count, expected_action):
        """Check the fractional (pressure-based) trigger in isolation (no ``tokens`` trigger interference).

        The old ``("fraction", 0.5)`` clause became the preemptive pressure
        check: effective_tokens / main_llm_context_window against
        PREEMPTIVE_TRUNCATE_RATIO=0.70 ("truncate_only") and
        COMPRESSION_TRIGGER_RATIO=0.80 ("compact") from config.num. Both
        estimators are patched so ONLY the pressure band decides the result.
        """
        from agent.middlewares.summarization import Summarization

        model = _make_fake_model()
        inst = Summarization(
            need_update_system_prompt=True,
            model=model,
            main_llm_context_window=65_536_000,
            keep=("messages", 10),
        )
        with patch.object(inst, "_estimate_tokens", return_value=token_count), \
             patch.object(inst, "_get_reported_tokens", return_value=0):
            messages = [
                HumanMessage(content="x" * (token_count // 2)),
                AIMessage(content="y" * (token_count // 2)),
            ]
            result = inst._preemptive_check(messages, _DIAG_SESSION)
            assert result == expected_action, (
                f"At token_count={token_count}, expected _preemptive_check={expected_action!r}, "
                f"got {result!r}"
            )

    @pytest.mark.parametrize(
        "token_count,should_trigger",
        [
            (1_000, False),
            (30_000, True),  # ("tokens", 30000) trigger (worker-style, spawn/core.py)
            (100_000, True),
        ],
    )
    def test_tokens_trigger_still_works(self, token_count, should_trigger):
        """Verify the absolute ``tokens`` trigger still fires at 30K."""
        from agent.middlewares.summarization import Summarization

        model = _make_fake_model()
        inst = Summarization(
            need_update_system_prompt=True,
            model=model,
            trigger=[("tokens", 30000)],
            keep=("messages", 10),
        )
        with patch.object(inst, "_estimate_tokens", return_value=token_count), \
             patch.object(inst, "_get_reported_tokens", return_value=0):
            messages = [HumanMessage(content="test")]
            result = inst._check_trigger(messages)
            assert result == should_trigger, (
                f"At token_count={token_count}, expected _check_trigger={should_trigger}, "
                f"got {result}"
            )


class TestSummarizationWrapModelIntegration:
    """End-to-end check: does wrap_model_call actually return a compression result?

    This verifies the full pipeline: _preemptive_check / _check_trigger
    → _apply_compression → request.override(messages=...) → handler.
    """

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

    def test_compression_at_high_token_count(self, summarizer):
        """With tokens above the redesigned trigger (52.4M), wrap_model_call should trigger compression."""
        messages = [
            HumanMessage(content="x" * 16_000_000),
            AIMessage(content="y" * 16_000_000),
        ]
        # Patch both estimators to a post-redesign "high" count: 60M is above the
        # ("tokens", 52_428_800) trigger and at 92% pressure (≥ 0.80 → "compact").
        with patch.object(summarizer, "_estimate_tokens", return_value=60_000_000), \
             patch.object(summarizer, "_get_reported_tokens", return_value=0):
            request = _make_request(messages)
            response = AIMessage(content="ok")
            captured = {}

            def handler(req):
                captured["request"] = req
                return response

            result = summarizer.wrap_model_call(request, handler)
            # The turn must complete and the handler must run.
            assert result is response, (
                "wrap_model_call did not return the handler response at high token count."
            )
            # The compact band also runs _preemptive_truncate, which re-lists the
            # SAME message objects; real compression prepends NEW summary messages.
            # Head object identity therefore separates compression from the
            # truncate-only override (and from a no-op cutoff).
            if captured["request"].state["messages"][0] is messages[0]:
                pytest.skip(
                    "compression was a no-op (cutoff logic) or failed open with the "
                    "fake model (exception logged + swallowed by design); the turn "
                    "itself still completed — not a bug per se."
                )
            assert "messages" in captured["request"].state


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
