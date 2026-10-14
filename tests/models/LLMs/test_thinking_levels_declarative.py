"""Declarative thinking levels: 7-level enum, level maps, clamp, wire formats.

The engine (``models/LLMs/reasoning_payload.py``) resolves a level request
against a per-model ``thinkingLevelMap`` and clamps to what the model has. The
compatibility pins for the four original provider families live in
``test_reasoning_payload.py`` / ``test_main_llm_thinking_variants.py``; this file
covers the declarative layer itself and the newly supported formats.
"""

from config.features import EXTENDED_THINKING_LEVELS, THINKING_LEVEL_CONFIG
from models.LLMs import reasoning_payload as rp

import pytest

pytestmark = [pytest.mark.unit]


class TestSupportedLevels:
    def test_the_enum_is_seven_levels_in_clamp_order(self):
        assert EXTENDED_THINKING_LEVELS == (
            "off",
            "minimal",
            "low",
            "medium",
            "high",
            "xhigh",
            "max",
        )
        assert THINKING_LEVEL_CONFIG["default_level"] in EXTENDED_THINKING_LEVELS

    @pytest.mark.parametrize(
        ("provider", "model", "expected"),
        [
            # Always-think GLM-5: no off, and exactly low/high/max.
            ("openai", "glm-5.3-flash", ["low", "high", "max"]),
            ("zhipu", "glm-5", ["low", "high", "max"]),
            # GLM-4.5/4.6: switchable, still only low/high/max.
            ("openai", "glm-4.6", ["off", "low", "high", "max"]),
            # DeepSeek: the full seven.
            ("deepseek", "deepseek-flash", list(EXTENDED_THINKING_LEVELS)),
            # o-series: no off (no disable wire), effort levels up to high.
            ("openai", "o3-mini", ["minimal", "low", "medium", "high", "xhigh", "max"]),
            # Router names strip the org prefix.
            ("openrouter", "zhipu/glm-4.6", ["off", "low", "high", "max"]),
            # Unknown pairs: the one level that sends nothing.
            ("ollama", "llama3", ["off"]),
        ],
    )
    def test_level_lists_per_family(self, provider, model, expected):
        assert rp.get_supported_thinking_levels(provider, model) == expected


class TestClamp:
    def test_exact_hit_wins(self):
        assert rp.clamp_thinking_level("deepseek", "deepseek-flash", "medium") == "medium"

    def test_unsupported_mid_level_clamps_up(self):
        # glm-4.6 has no "medium": the clamp searches UP first (more thinking).
        assert rp.clamp_thinking_level("openai", "glm-4.6", "medium") == "high"
        assert rp.build_thinking_kwargs("openai", "glm-4.6", "medium") == {
            "extra_body": {"thinking": {"type": "enabled"}}
        }

    def test_off_clamps_up_on_always_think(self):
        assert rp.clamp_thinking_level("openai", "glm-5.3-flash", "off") == "low"

    def test_xhigh_clamps_up_when_absent(self):
        # glm-4.6 declares no xhigh: the clamp searches UP first, so max wins.
        assert rp.clamp_thinking_level("openai", "glm-4.6", "xhigh") == "max"

    def test_unknown_token_passes_through(self):
        assert rp.clamp_thinking_level("deepseek", "deepseek-flash", "turbo") == "turbo"

    def test_unmapped_pair_clamps_to_off(self):
        assert rp.clamp_thinking_level("ollama", "llama3", "medium") == "off"


class TestWireFormats:
    """Each format's request fragment, pinned by level."""

    def test_on_matches_the_legacy_enabled_payload(self):
        # Byte-compatibility: the default ON level must reproduce the pre-engine
        # wire exactly for the two enabled/disabled formats.
        for provider, model in (("deepseek", "deepseek-flash"), ("openai", "glm-4.6")):
            assert rp.build_thinking_kwargs(provider, model, "high") == {
                "extra_body": {"thinking": {"type": "enabled"}}
            }

    def test_deepseek_explicit_levels(self):
        # thinking.type takes enabled/disabled only; the level rides the separate
        # reasoning_effort field (pi's own deepseek rule).
        assert rp.build_thinking_kwargs("deepseek", "deepseek-flash", "medium") == {
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": "medium",
        }
        assert rp.build_thinking_kwargs("deepseek", "deepseek-flash", "max") == {
            "extra_body": {"thinking": {"type": "enabled"}},
            "reasoning_effort": "high",  # deepseek's effort domain tops out at high
        }
        assert rp.build_thinking_kwargs("deepseek", "deepseek-flash", "off") == {
            "extra_body": {"thinking": {"type": "disabled"}}
        }

    def test_glm4_explicit_level(self):
        assert rp.build_thinking_kwargs("openai", "glm-4.6", "max") == {
            "extra_body": {"thinking": {"type": "max"}}
        }

    def test_glm5_off_resolves_to_the_floor(self):
        assert rp.build_thinking_kwargs("openai", "glm-5.3-flash", "off") == {
            "extra_body": {"thinking": {"type": "low"}}
        }

    def test_openai_off_is_param_free(self):
        assert rp.build_thinking_kwargs("openai", "o3-mini", "off") == {}

    def test_openai_effort_levels(self):
        # "minimal" exists on gpt-5 but not the o-series: it resolves to low.
        assert rp.build_thinking_kwargs("openai", "o3-mini", "minimal") == {
            "reasoning_effort": "low"
        }
        assert rp.build_thinking_kwargs("openai", "gpt-5", "max") == {"reasoning_effort": "high"}

    def test_openrouter_nested_effort(self):
        assert rp.build_thinking_kwargs("openrouter", "some/model", "xhigh") == {
            "reasoning": {"effort": "high"}
        }
        assert rp.build_thinking_kwargs("openrouter", "some/model", "off") == {
            "reasoning": {"effort": "none"}
        }

    def test_qwen_enable_thinking_flag(self):
        assert rp.build_thinking_kwargs("dashscope", "qwen3-max", "high") == {
            "extra_body": {"enable_thinking": True}
        }
        assert rp.build_thinking_kwargs("dashscope", "qwen3-max", "off") == {
            "extra_body": {"enable_thinking": False}
        }

    def test_together_reasoning_enabled(self):
        assert rp.build_thinking_kwargs("together", "some/model", "medium") == {
            "reasoning": {"enabled": True},
            "reasoning_effort": "medium",
        }

    def test_baseten_reasoning_enabled(self):
        assert rp.build_thinking_kwargs("baseten", "some/model", "off") == {
            "reasoning": {"enabled": False}
        }

    def test_google_discrete_levels(self):
        assert rp.build_thinking_kwargs("google", "gemini-3-pro", "medium") == {
            "thinking_level": "MEDIUM"
        }
        assert rp.build_thinking_kwargs("google", "gemini-3-pro", "off") == {}

    def test_anthropic_level_payloads(self):
        # Any enabled level sends the documented budget block; off is param-free.
        assert rp.build_thinking_kwargs("anthropic", "claude-opus-4-5", "max") == {
            "thinking": {"type": "enabled", "budget_tokens": rp._DEFAULT_ANTHROPIC_BUDGET}
        }
        assert rp.build_thinking_kwargs("anthropic", "claude-opus-4-5", "off") == {}

    def test_unknown_provider_stays_a_noop(self):
        assert rp.build_thinking_kwargs("ollama", "llama3", "high") == {}
        assert rp.build_thinking_kwargs(None, None, "high") == {}


class TestExplicitOverrides:
    """The ``thinking_format`` / ``thinking_level_map`` seams (custom-wire)."""

    def test_an_explicit_map_wins(self):
        custom = {"off": "none", "high": "ultra", "max": "ultra-max"}
        assert rp.build_thinking_kwargs(
            "openrouter", "some/model", "high", thinking_level_map=custom
        ) == {"reasoning": {"effort": "ultra"}}

    def test_an_explicit_format_wins(self):
        assert rp.build_thinking_kwargs(
            "openrouter", "some/model", "high", thinking_format="deepseek"
        ) == {"extra_body": {"thinking": {"type": "enabled"}}}
