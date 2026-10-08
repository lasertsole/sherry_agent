"""``build_main_llm`` thinking variants — symmetric on/off, levels, floor.

The per-session thinking control rebuilds the client config from the pristine
pre-reasoning snapshot: enabling adds the provider payload + budget headroom,
disabling restores the switch-off config, levels pin low/high/max, and the
floor swaps in the minimum thinking level for always-think models.
"""

import pytest

from config.features import REASONING_BUDGET
from models.LLMs import main_llm as main_llm_module
from models.LLMs.reasoning_payload import (
    get_thinking_budget,
    thinking_control_mode,
)

pytestmark = [pytest.mark.unit]

_PRISTINE = {
    "model_provider": "openai",
    "model": "glm-4.6",
    "api_key": "test-key",
    "base_url": "https://example.invalid/v4",
    "temperature": 0,
    "max_tokens": 8192,
    "max_retries": 2,
}


@pytest.fixture
def zhipu_gateway_env(monkeypatch):
    """Pin the module globals the variant builder reads (no .env dependence)."""
    monkeypatch.setattr(main_llm_module, "_pristine_config", dict(_PRISTINE))
    monkeypatch.setattr(main_llm_module, "model_provider", "openai")
    monkeypatch.setattr(main_llm_module, "api_name", "glm-4.6")
    monkeypatch.setattr(main_llm_module, "reasoning_effort", None)


def _reasoning_keys(cfg):
    return {k: cfg[k] for k in ("extra_body", "reasoning_effort", "thinking") if k in cfg}


def test_enabled_variant_carries_provider_payload(zhipu_gateway_env):
    cfg = main_llm_module._variant_client_config(enabled=True)
    # GLM via the bigmodel OpenAI-compatible API: DeepSeek-style extra_body.
    assert _reasoning_keys(cfg) == {"extra_body": {"thinking": {"type": "enabled"}}}
    budget = get_thinking_budget("openai", "glm-4.6", True)
    assert budget == REASONING_BUDGET["non_anthropic_default_thinking_budget"]
    assert cfg["max_tokens"] == 8192 + budget


def test_disabled_variant_explicitly_disables_glm_thinking(zhipu_gateway_env):
    """GLM's server-side default is thinking ON: off must send `disabled`."""
    cfg = main_llm_module._variant_client_config(enabled=False)
    assert _reasoning_keys(cfg) == {"extra_body": {"thinking": {"type": "disabled"}}}
    assert cfg["max_tokens"] == 8192  # pristine value, no budget inflation
    assert cfg["model"] == "glm-4.6" and cfg["api_key"] == "test-key"


def test_floor_variant_pins_minimum_level(zhipu_gateway_env):
    cfg = main_llm_module._variant_client_config(enabled=False, thinking_floor=True)
    assert _reasoning_keys(cfg) == {"extra_body": {"thinking": {"type": "low"}}}
    assert cfg["max_tokens"] == 8192 + REASONING_BUDGET["non_anthropic_default_thinking_budget"]


def test_level_variant_pins_explicit_level(zhipu_gateway_env):
    cfg = main_llm_module._variant_client_config(enabled=True, level="max")
    assert _reasoning_keys(cfg) == {"extra_body": {"thinking": {"type": "max"}}}
    assert cfg["max_tokens"] == 8192 + REASONING_BUDGET["non_anthropic_default_thinking_budget"]


def test_disabled_variant_is_param_free_for_default_off_providers(monkeypatch):
    """Anthropic / unknown providers default to OFF: absent param suffices."""
    monkeypatch.setattr(main_llm_module, "_pristine_config", dict(_PRISTINE))
    monkeypatch.setattr(main_llm_module, "model_provider", "anthropic")
    monkeypatch.setattr(main_llm_module, "api_name", "claude-sonnet-4")
    monkeypatch.setattr(main_llm_module, "reasoning_effort", None)
    cfg = main_llm_module._variant_client_config(enabled=False)
    assert _reasoning_keys(cfg) == {}
    assert cfg["max_tokens"] == 8192


def test_variants_do_not_mutate_pristine_or_module_config(zhipu_gateway_env):
    before_pristine = dict(main_llm_module._pristine_config)
    main_llm_module._variant_client_config(enabled=True)
    main_llm_module._variant_client_config(enabled=False, thinking_floor=True)
    assert main_llm_module._pristine_config == before_pristine
    # The env-default module config is a separate dict from the pristine copy.
    assert main_llm_module.model_config is not main_llm_module._pristine_config


def test_control_mode_detection(monkeypatch):
    cases = [
        ("openai", "glm-5.3-flash", "levels"),  # always-think glm-5 series
        ("openai", "glm-4.6", "on_off"),  # enabled/disabled switch
        ("openai", "o3-mini", "on_off"),
        ("openai", "zhipu/glm-5-flash", "levels"),  # router prefix stripped
        ("anthropic", "claude-sonnet-4", "on_off"),
        ("deepseek", "deepseek-chat", "on_off"),
        ("openai", None, "on_off"),
        (None, "glm-5.3-flash", "on_off"),
    ]
    for provider, model, expected in cases:
        assert thinking_control_mode(provider, model) == expected, (provider, model)


def test_build_main_llm_constructs_forced_variants(zhipu_gateway_env):
    """The factory path constructs real clients for the forced states."""
    on = main_llm_module.build_main_llm(temperature=0, thinking=True)
    off = main_llm_module.build_main_llm(temperature=0, thinking=False)
    level = main_llm_module.build_main_llm(temperature=0, thinking_level="low")
    # GLM via generic openai provider must use ReasoningChatOpenAI (reasoning
    # streaming), wrapped by NormalizingChatModel in every case.
    for model in (on, off, level):
        assert type(model.inner).__name__ == "ReasoningChatOpenAI"
    assert len({id(on), id(off), id(level)}) == 3
