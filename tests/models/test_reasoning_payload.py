"""Thinking-budget tests — ``get_thinking_budget`` + model-config inflation.

Covers the thinking-budget headroom added to the model config when the
universal reasoning switch is on (models/LLMs/reasoning_payload.py +
models/LLMs/main_llm.py): per-provider budget resolution, the shared
``MAIN_LLM_THINKING_BUDGET`` env override, and the inflation helper that
sets ``max_tokens = OUTPUT_MAX_TOKEN + budget`` (absent when disabled).
"""

import pytest

from config import features
from config.features import agent_side
from models.LLMs import main_llm
from models.LLMs import reasoning_payload as rp

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class TestGetThinkingBudget:
    def test_disabled_returns_zero(self):
        assert rp.get_thinking_budget("deepseek", "deepseek-chat", False) == 0

    def test_missing_provider_returns_zero(self):
        assert rp.get_thinking_budget(None, "deepseek-chat", True) == 0

    def test_anthropic_reasoning_model_gets_anthropic_budget(self):
        assert rp.get_thinking_budget("anthropic", "claude-opus-4-5", True) == (
            rp._DEFAULT_ANTHROPIC_BUDGET
        )

    def test_anthropic_non_reasoning_model_gets_zero(self):
        assert rp.get_thinking_budget("anthropic", "claude-3-5-haiku", True) == 0

    def test_deepseek_gets_default_budget(self):
        assert rp.get_thinking_budget("deepseek", "deepseek-chat", True) == 4096

    @pytest.mark.parametrize(
        "provider,model_name",
        [
            ("openai", "o3-mini"),
            ("openai", "gpt-5"),
            ("zhipu", "glm-4.6"),
            ("openrouter", "zhipu/glm-5"),
        ],
    )
    def test_openai_compatible_reasoning_models_get_default_budget(self, provider, model_name):
        assert rp.get_thinking_budget(provider, model_name, True) == 4096

    def test_openai_non_reasoning_model_gets_zero(self):
        assert rp.get_thinking_budget("openai", "gpt-4o", True) == 0

    def test_unmapped_provider_gets_zero(self):
        assert rp.get_thinking_budget("ollama", "llama3", True) == 0

    def test_env_override(self, monkeypatch):
        monkeypatch.setattr(rp, "_DEFAULT_NON_ANTHROPIC_BUDGET", 6000)
        assert rp.get_thinking_budget("deepseek", "deepseek-chat", True) == 6000


class TestApplyThinkingBudget:
    def test_thinking_enabled_inflates_max_tokens(self):
        config: dict = {}
        result = main_llm.apply_thinking_budget(config, "deepseek", "deepseek-chat", True)
        assert result["max_tokens"] == main_llm.OUTPUT_MAX_TOKEN + 4096

    def test_thinking_disabled_leaves_key_absent(self):
        config: dict = {}
        main_llm.apply_thinking_budget(config, "deepseek", "deepseek-chat", False)
        assert "max_tokens" not in config

    def test_anthropic_budget_is_anthropic_constant(self):
        config: dict = {}
        main_llm.apply_thinking_budget(config, "anthropic", "claude-opus-4-5", True)
        assert config["max_tokens"] == main_llm.OUTPUT_MAX_TOKEN + rp._DEFAULT_ANTHROPIC_BUDGET

    def test_no_budget_model_leaves_key_absent(self):
        config: dict = {}
        main_llm.apply_thinking_budget(config, "openai", "gpt-4o", True)
        assert "max_tokens" not in config

    def test_output_max_token_matches_boost_env_source(self, monkeypatch):
        # env read lives in config.features._build_max_tokens_boost since the
        # config consolidation; the middleware aliases the registry field.
        monkeypatch.setenv("MAIN_LLM_OUTPUT_MAX_TOKEN", "9000")
        import importlib

        importlib.reload(main_llm)
        boost = importlib.reload(importlib.import_module("agent.middlewares.max_tokens_boost"))
        try:
            # The builder reads the env at call time (env or os.environ).
            assert agent_side._build_max_tokens_boost()["base_max_tokens"] == 9000
            # The middleware binding tracks the shared registry field.
            assert boost._BASE_MAX_TOKENS == features.MAX_TOKENS_BOOST["base_max_tokens"]
            # Thinking-budget inflation still uses the patched output cap (9000).
            assert main_llm.OUTPUT_MAX_TOKEN == 9000
            config: dict = {}
            main_llm.apply_thinking_budget(config, "deepseek", "deepseek-chat", True)
            assert config["max_tokens"] == 9000 + rp._DEFAULT_NON_ANTHROPIC_BUDGET
        finally:
            importlib.reload(main_llm)
            importlib.reload(boost)
