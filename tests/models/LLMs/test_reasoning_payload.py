# pyright: reportArgumentType=false
# pyright: reportUnknownParameterType=false
# pyright: reportAny=false
# pyright: reportMissingParameterType=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
"""Unit tests for models/LLMs/reasoning_payload.py — provider reasoning kwargs + budgets.

Covers provider reasoning kwargs (``build_reasoning_kwargs`` and the provider
predicates) plus the thinking-budget headroom added to the model config when
the universal reasoning switch is on (``get_thinking_budget`` and the
``main_llm`` inflation helper that sets ``max_tokens = OUTPUT_MAX_TOKEN +
budget``, absent when disabled).
"""

from config import features
from config.features.agent_side.max_tokens_boost import _build_max_tokens_boost
from models.LLMs import main_llm
from models.LLMs import reasoning_payload as rp
from models.LLMs.reasoning_payload import (
    build_reasoning_kwargs,
    is_openai_reasoning_model,
    is_zhipu_reasoning_model,
)


import pytest

pytestmark = [pytest.mark.unit]


class TestBuildReasoningKwargsGlm:
    """Zhipu GLM through an OpenAI-compatible gateway (bigmodel v4 API)."""

    def test_glm5_flash_gets_extra_body_thinking(self):
        assert build_reasoning_kwargs("openai", "glm-5.3-flash", True) == {
            "extra_body": {"thinking": {"type": "enabled"}}
        }

    def test_glm46_gets_extra_body_thinking(self):
        assert build_reasoning_kwargs("openai", "glm-4.6", True) == {
            "extra_body": {"thinking": {"type": "enabled"}}
        }

    def test_glm45_gets_extra_body_thinking(self):
        assert build_reasoning_kwargs("openai", "glm-4.5-air", True) == {
            "extra_body": {"thinking": {"type": "enabled"}}
        }

    def test_glm_router_style_name_matches(self):
        assert build_reasoning_kwargs("openrouter", "zhipu/glm-4.6", True) == {
            "extra_body": {"thinking": {"type": "enabled"}}
        }

    def test_legacy_glm4_is_noop(self):
        # glm-4 / glm-4v families predate the thinking param and reject it (400).
        assert build_reasoning_kwargs("openai", "glm-4-flash", True) == {}
        assert build_reasoning_kwargs("openai", "glm-4v-flash", True) == {}

    def test_disabled_switch_is_noop(self):
        assert build_reasoning_kwargs("openai", "glm-5.3-flash", False) == {}

    def test_reasoning_effort_ignored_for_glm(self):
        # GLM uses the body-level thinking key; reasoning_effort must not leak in.
        kwargs = build_reasoning_kwargs("openai", "glm-5.3-flash", True, reasoning_effort="low")
        assert kwargs == {"extra_body": {"thinking": {"type": "enabled"}}}


class TestBuildReasoningKwargsRegression:
    """Pre-existing mappings must be unchanged by the GLM branch."""

    def test_deepseek_unchanged(self):
        assert build_reasoning_kwargs("deepseek", "deepseek-chat", True) == {
            "extra_body": {"thinking": {"type": "enabled"}}
        }

    def test_openai_o_series_unchanged(self):
        assert build_reasoning_kwargs("openai", "o3-mini", True) == {"reasoning_effort": "high"}

    def test_openai_gpt5_uses_effort(self):
        assert build_reasoning_kwargs("openai", "gpt-5", True, reasoning_effort="low") == {
            "reasoning_effort": "low"
        }

    def test_openai_non_reasoning_model_noop(self):
        assert build_reasoning_kwargs("openai", "gpt-4o-mini", True) == {}

    def test_anthropic_reasoning_model_unchanged(self):
        assert build_reasoning_kwargs("anthropic", "claude-opus-4-5", True) == {
            "thinking": {"type": "enabled", "budget_tokens": 2000}
        }

    def test_unknown_provider_noop(self):
        assert build_reasoning_kwargs("ollama", "llama3", True) == {}

    def test_never_raises_on_garbage(self):
        assert build_reasoning_kwargs(None, None, True) == {}
        assert build_reasoning_kwargs("", "", False) == {}


class TestIsZhipuReasoningModel:
    def test_glm5_flash_true(self):
        assert is_zhipu_reasoning_model("glm-5.3-flash") is True

    def test_glm46_true(self):
        assert is_zhipu_reasoning_model("glm-4.6") is True

    def test_org_prefix_stripped(self):
        assert is_zhipu_reasoning_model("zhipu/glm-4.5-air") is True

    def test_legacy_glm4_false(self):
        assert is_zhipu_reasoning_model("glm-4-flash") is False

    def test_non_glm_false(self):
        assert is_zhipu_reasoning_model("gpt-5") is False
        assert is_zhipu_reasoning_model("o3-mini") is False


class TestIsOpenaiReasoningModel:
    def test_o_series(self):
        assert is_openai_reasoning_model("o3-mini") is True

    def test_glm_not_openai_reasoning(self):
        assert is_openai_reasoning_model("glm-5.3-flash") is False


@pytest.mark.timeout(60)
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


@pytest.mark.timeout(60)
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
        # env read lives in config.features.agent_side.max_tokens_boost since
        # the config consolidation; the middleware aliases the registry field.
        monkeypatch.setenv("MAIN_LLM_OUTPUT_MAX_TOKEN", "9000")
        import importlib

        importlib.reload(main_llm)
        boost = importlib.reload(importlib.import_module("agent.middlewares.max_tokens_boost.core"))
        try:
            # The builder reads the env at call time (env or os.environ).
            assert _build_max_tokens_boost()["base_max_tokens"] == 9000
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
