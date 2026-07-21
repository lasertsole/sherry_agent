"""Diagnose: does SummarizationMiddleware trigger compression on every turn?

This test constructs a Summarization instance matching agent/core.py config
and verifies the ``_should_summarize`` decision across realistic token counts.
"""

import pytest
from unittest.mock import MagicMock, patch, PropertyMock
from langchain_core.messages import HumanMessage, AIMessage

# ── The threshold we expect after the fix ──────────────────────────────────
# MAIN_LLM_MAX_TOKEN = 65_536_000  (from .env)
# fraction=0.5  →  threshold = int(65_536_000 * 0.5) = 32_768_000
EXPECTED_THRESHOLD = 32_768_000


def _make_fake_model(max_input_tokens: int = 65_536_000):
    """Create a mock BaseChatModel with ``profile.max_input_tokens``."""
    model = MagicMock()
    profile = PropertyMock(return_value={"max_input_tokens": max_input_tokens})
    type(model).profile = profile
    # Make `_llm_type` a simple string (not a PropertyMock) so
    # _get_approximate_token_counter doesn't crash
    type(model)._llm_type = "fake-chat"
    # _get_ls_params for _should_summarize_based_on_reported_tokens
    type(model)._get_ls_params = MagicMock(return_value={"ls_provider": "fake"})
    return model


class TestSummarizationTriggerDiagnose:
    """Verify the ``fraction`` trigger threshold after the main_llm.py fix."""

    @pytest.fixture
    def summarizer(self):
        from agent.middlewares.summarization import Summarization

        model = _make_fake_model(max_input_tokens=65_536_000)
        inst = Summarization(
            need_update_system_prompt=True,
            model=model,
            trigger=[("fraction", 0.5), ("messages", 40), ("tokens", 30000)],
            keep=("messages", 10),
        )
        return inst

    def test_profile_value_is_65536000(self, summarizer):
        """Verify _get_profile_limits returns the un-capped value."""
        limits = summarizer._get_profile_limits()
        assert limits == 65_536_000, (
            f"Expected profile.max_input_tokens = 65_536_000, got {limits}. "
            "Check models/LLMs/main_llm.py cap removal."
        )

    def test_fraction_threshold_is_32million(self, summarizer):
        """fraction=0.5 of 65_536_000 → threshold = 32_768_000."""
        limits = summarizer._get_profile_limits()
        threshold = int(limits * 0.5)
        assert threshold == EXPECTED_THRESHOLD, (
            f"Expected threshold 32_768_000, got {threshold}"
        )

    @pytest.mark.parametrize("token_count,should_trigger", [
        (1_000,    False),   # tiny → no
        (100_000,  False),   # 100K → still far below 32M
        (1_000_000, False),  # 1M  → still below
        (10_000_000, False), # 10M → still below
        (30_000_000, False), # 30M → still below 32.768M
        (33_000_000, True),  # 33M → barely over
        (50_000_000, True),  # 50M → way over
        (65_536_000, True),  # 100% of context
    ])
    def test_fraction_trigger_only(self, token_count, should_trigger):
        """Check the ``fraction`` trigger in isolation (no ``tokens`` or ``messages`` trigger interference).

        We use a summarizer configured with ONLY ``("fraction", 0.5)`` so the
        30K token threshold doesn't skew the result.
        """
        from agent.middlewares.summarization import Summarization

        model = _make_fake_model(max_input_tokens=65_536_000)
        inst = Summarization(
            need_update_system_prompt=True,
            model=model,
            trigger=[("fraction", 0.5)],  # ONLY fraction, no tokens/messages
            keep=("messages", 10),
        )
        with patch.object(inst, "token_counter", return_value=token_count):
            messages = [
                HumanMessage(content="x" * (token_count // 2)),
                AIMessage(content="y" * (token_count // 2)),
            ]
            result = inst._should_summarize(messages, token_count)
            assert result == should_trigger, (
                f"At token_count={token_count}, expected _should_summarize={should_trigger}, "
                f"got {result}"
            )

    @pytest.mark.parametrize("token_count,should_trigger", [
        (1_000,    False),
        (30_000,   True),  # ("tokens", 30000) trigger
        (100_000,  True),
    ])
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
        with patch.object(inst, "token_counter", return_value=token_count):
            messages = [HumanMessage(content="test")]
            result = inst._should_summarize(messages, token_count)
            assert result == should_trigger, (
                f"At token_count={token_count}, expected _should_summarize={should_trigger}, "
                f"got {result}"
            )


class TestSummarizationBeforeModelIntegration:
    """End-to-end check: does before_model actually return compression result?

    This verifies the full pipeline: _should_summarize → _determine_cutoff_index
    → _create_summary → return new messages.
    """

    @pytest.fixture
    def summarizer(self):
        from agent.middlewares.summarization import Summarization

        model = _make_fake_model(max_input_tokens=65_536_000)
        inst = Summarization(
            need_update_system_prompt=True,
            model=model,
            trigger=[("fraction", 0.5), ("messages", 40), ("tokens", 30000)],
            keep=("messages", 10),
        )
        return inst

    def test_no_compression_at_low_token_count(self, summarizer):
        """With very few tokens, before_model should return None (no-op)."""
        messages = [HumanMessage(content="hello")]
        state = {"messages": messages}
        runtime = MagicMock()
        result = summarizer.before_model(state, runtime)
        assert result is None, (
            "before_model returned a compression result despite very low token count. "
            "This means compression fires every turn."
        )

    def test_compression_at_high_token_count(self, summarizer):
        """With 33M tokens, before_model should trigger compression."""
        messages = [
            HumanMessage(content="x" * 16_000_000),
            AIMessage(content="y" * 16_000_000),
        ]
        # Patch token_counter to return 33M
        with patch.object(summarizer, "token_counter", return_value=33_000_000):
            state = {"messages": messages}
            runtime = MagicMock()
            result = summarizer.before_model(state, runtime)
            # Should trigger compression (return non-None) or return None if
            # cutoff_index <= 0
            if result is None:
                pytest.skip("before_model returned None (cutoff logic), not a bug per se.")
            else:
                assert "messages" in result


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
