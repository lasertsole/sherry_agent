"""PI-parity provider overflow patterns (plan §2).

Pins three things: the original seven substring patterns still classify (P1),
the 25 PI regex patterns cover the documented provider wordings, and a broken
regex table degrades to substring matching instead of raising (P6).
"""

from __future__ import annotations

import pytest

from pub.func.message import llm_error_classifier as classifier

pytestmark = [pytest.mark.unit]

PROVIDER_SAMPLES = [
    "Prompt is too long: 210000 tokens > 200000 maximum",  # Anthropic
    "prompt exceeds max length",  # z.ai CN
    "request_too_large",  # Anthropic 413
    "input is too long for requested model",  # Amazon Bedrock
    "This model's maximum context length is 128000 tokens; your request exceeds the context window",
    "exceeds the context window",  # OpenAI
    "input token count (2100) exceeds the maximum number of tokens allowed (2000)",  # Gemini
    "maximum prompt length is 131072",  # xAI Grok
    "Please reduce the length of the messages",  # Groq
    "maximum context length is 4096 tokens",  # OpenRouter
    "exceeds the maximum allowed input length of 8192 tokens",  # Poolside
    "input (1000 tokens) is longer than the model's context length (512 tokens)",  # Together
    "exceeds the limit of 8192",  # GitHub Copilot
    "the prompt exceeds the available context size",  # llama.cpp
    "token count is greater than the context length",  # LM Studio
    "context window exceeds limit",  # MiniMax
    "exceeded model token limit",  # Kimi
    "too large for model with 32768 maximum context length",  # Mistral
    "Prompt has 1000 tokens, but the configured context size is 512 tokens",  # DS4
    "model_context_window_exceeded",  # z.ai finish_reason
    "Prompt too long; exceeded max context length",  # Ollama
    "Range of input length should be [1, 1000]",  # DashScope
    "context_length_exceeded",  # generic
    "too many tokens in the request",  # generic
    "token limit exceeded",  # generic
]


@pytest.mark.parametrize("message", PROVIDER_SAMPLES)
def test_every_pi_provider_sample_is_context_overflow(message: str):
    assert classifier.classify_provider_error(Exception(message)) == "context_overflow"


@pytest.mark.parametrize(
    "message",
    [
        "context_length_exceeded",
        "maximum context length exceeded",
        "context length too big",
        "context window is full",
        "input length exceeds the model limit",
        "reduce the length of the prompt",
        "too many tokens",
    ],
)
def test_original_seven_substrings_still_match(message: str):
    """P1: the pre-change substring table keeps classifying exactly as before."""
    assert classifier.classify_provider_error(Exception(message)) == "context_overflow"


def test_provider_sample_matches_through_the_cause_chain():
    inner = Exception("prompt is too long: 210000 tokens")
    outer = Exception("LLM call failed")
    outer.__cause__ = inner
    assert classifier.classify_provider_error(outer) == "context_overflow"


def test_regex_compilation_failure_degrades_to_an_empty_table():
    assert classifier._compile_ignore_case(("([",), "test") == ()


def test_broken_regex_matcher_falls_back_to_substrings(monkeypatch: pytest.MonkeyPatch):
    class _BrokenPattern:
        def search(self, text: str) -> None:
            raise RuntimeError("matcher exploded")

    monkeypatch.setattr(classifier, "_CONTEXT_OVERFLOW_REGEXES", (_BrokenPattern(),))

    # A substring-only message still classifies (P1/P6).
    assert (
        classifier.classify_provider_error(Exception("CONTEXT LENGTH EXCEEDED"))
        == "context_overflow"
    )
    # A regex-only message does not raise; it falls through the pipeline.
    assert classifier.classify_provider_error(Exception("prompt is too long")) is None
