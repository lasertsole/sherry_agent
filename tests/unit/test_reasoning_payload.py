# pyright: reportArgumentType=false
# pyright: reportUnknownParameterType=false
# pyright: reportAny=false
# pyright: reportMissingParameterType=false
# pyright: reportUnknownVariableType=false
# pyright: reportUnknownMemberType=false
"""Unit tests for models/LLMs/reasoning_payload.py — provider reasoning kwargs."""

from models.LLMs.reasoning_payload import (
    build_reasoning_kwargs,
    is_openai_reasoning_model,
    is_zhipu_reasoning_model,
)


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
