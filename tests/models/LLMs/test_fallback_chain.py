"""Tests for build_fallback_chain env parsing (error-handling plan, module G)."""

import pytest

from models.LLMs.main_llm import build_fallback_chain

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_BASE = {
    "FALLBACK_LLM_1_PROVIDER": "deepseek",
    "FALLBACK_LLM_1_NAME": "deepseek-chat",
    "FALLBACK_LLM_1_API_KEY": "sk-test-1",
    "FALLBACK_LLM_1_API_BASE": "https://api.deepseek.com/v1",
    "FALLBACK_LLM_2_NAME": "kimi-latest",
    "FALLBACK_LLM_2_API_KEY": "sk-test-2",
}


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in list(__import__("os").environ):
        if key.startswith("FALLBACK_LLM_"):
            monkeypatch.delenv(key, raising=False)
    yield


def _set(monkeypatch, values: dict[str, str]) -> None:
    for key, value in values.items():
        monkeypatch.setenv(key, value)


class TestBuildFallbackChain:
    def test_empty_when_no_env(self):
        assert build_fallback_chain() == []

    def test_parses_two_candidates_and_stops_at_missing_name(self, monkeypatch):
        _set(monkeypatch, _BASE)
        _set(monkeypatch, {"FALLBACK_LLM_4_NAME": "never-reached"})
        chain = build_fallback_chain()
        assert [c.model_name for c in chain] == ["deepseek-chat", "kimi-latest"]
        assert chain[0].provider == "deepseek"

    def test_provider_defaults_to_openai(self, monkeypatch):
        _set(monkeypatch, {"FALLBACK_LLM_1_NAME": "gpt-x", "FALLBACK_LLM_1_API_KEY": "sk-t"})
        chain = build_fallback_chain()
        assert len(chain) == 1
        assert chain[0].provider == "openai"

    def test_candidate_model_wrapped_in_normalizer(self, monkeypatch):
        _set(monkeypatch, dict(_BASE))
        chain = build_fallback_chain()
        from models.LLMs.reasoning_normalizer import NormalizingChatModel

        assert all(isinstance(c.model, NormalizingChatModel) for c in chain)

    def test_unusable_candidate_skipped_without_raising(self, monkeypatch):
        _set(
            monkeypatch,
            {"FALLBACK_LLM_1_NAME": "m1", "FALLBACK_LLM_1_PROVIDER": "no-such-provider"},
        )
        chain = build_fallback_chain()
        assert chain == []
