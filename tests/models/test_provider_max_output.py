"""Per-provider max-output field mapping and the 8K business floor (M9)."""

from __future__ import annotations

import pytest

from config.features.agent_side.token_guard import MIN_MAX_OUTPUT_TOKEN
from models.LLMs.provider_max_output import (
    OPENAI_RESPONSES_MIN_OUTPUT_TOKENS,
    configured_max_output,
    resolve_max_output_kwargs,
)

pytestmark = [pytest.mark.unit]


@pytest.mark.parametrize(
    ("provider", "expected_field"),
    [
        ("openai", "max_tokens"),
        ("deepseek", "max_tokens"),
        ("anthropic", "max_tokens"),
        ("azure_openai", "max_tokens"),
        ("google_genai", "max_output_tokens"),
        ("google", "max_output_tokens"),
        ("vertexai", "max_output_tokens"),
        ("openai_responses", "max_output_tokens"),
    ],
)
def test_provider_field_mapping(provider: str, expected_field: str):
    assert resolve_max_output_kwargs(provider, "m", 32768) == {expected_field: 32768}


def test_openai_reasoning_models_use_max_completion_tokens():
    assert resolve_max_output_kwargs("openai", "o3-mini", 32768) == {"max_completion_tokens": 32768}
    assert resolve_max_output_kwargs("openai", "gpt-4o", 32768) == {"max_tokens": 32768}


def test_unset_value_passes_no_field():
    assert resolve_max_output_kwargs("openai", "m", None) == {}
    assert resolve_max_output_kwargs("openai", "m", 0) == {}


def test_unsupported_provider_passes_no_field():
    """M4: a gateway absent from the table keeps its own default."""
    assert resolve_max_output_kwargs("codex_gateway", "m", 32768) == {}


def test_openai_responses_clamps_to_its_api_floor():
    assert resolve_max_output_kwargs("openai_responses", "m", MIN_MAX_OUTPUT_TOKEN) == {
        "max_output_tokens": max(MIN_MAX_OUTPUT_TOKEN, OPENAI_RESPONSES_MIN_OUTPUT_TOKENS)
    }


def test_business_floor_is_enforced(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAIN_LLM_MAX_OUTPUT_TOKEN", "4096")
    with pytest.raises(ValueError, match="min 8192"):
        configured_max_output("MAIN_LLM_MAX_OUTPUT_TOKEN")

    monkeypatch.setenv("MAIN_LLM_MAX_OUTPUT_TOKEN", "32768")
    assert configured_max_output("MAIN_LLM_MAX_OUTPUT_TOKEN") == 32768


def test_unset_env_is_none(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("MAIN_LLM_MAX_OUTPUT_TOKEN", raising=False)
    assert configured_max_output("MAIN_LLM_MAX_OUTPUT_TOKEN") is None


def test_non_integer_value_names_the_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("MAIN_LLM_MAX_OUTPUT_TOKEN", "32K")
    with pytest.raises(ValueError, match="MAIN_LLM_MAX_OUTPUT_TOKEN"):
        configured_max_output("MAIN_LLM_MAX_OUTPUT_TOKEN")
