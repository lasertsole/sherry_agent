"""Per-provider sampling-time repetition penalties.

The provider table mirrors ``provider_max_output``: unsupported providers get
NO field, a ``None`` value is omitted, and a per-family ``.models`` override
falls back to the global :data:`REPETITION_PENALTY` default one value at a
time. The builders are pinned at their capture point (the kwargs handed to
``init_chat_model``) so a provider that cannot accept the field never sees it.
"""

from __future__ import annotations

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from config.features import REPETITION_PENALTY
from config.models_settings import MODEL_SETTING_KEYS
from models.LLMs import main_llm as main_llm_module
from models.LLMs.auxiliary_llm.core import build_auxiliary_llm
from models.LLMs.provider_repetition_penalty import (
    PENALTY_MAX,
    PENALTY_MIN,
    family_repetition_penalty_kwargs,
    resolve_repetition_penalty_kwargs,
)
from models.LLMs.reasoning_normalizer import NormalizingChatModel
from models.LLMs.taskflow_llm.core import build_taskflow_llm

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_PENALTY_ENV_KEYS = tuple(
    key
    for key in MODEL_SETTING_KEYS
    if key.endswith("_FREQUENCY_PENALTY") or key.endswith("_PRESENCE_PENALTY")
)


@pytest.fixture(autouse=True)
def _clean_penalty_env(monkeypatch: pytest.MonkeyPatch):
    """Drop hydrated `.models` values so each test states its own overrides."""
    for key in _PENALTY_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    yield


class TestProviderTable:
    def test_openai_compatible_gets_both_penalties(self):
        assert resolve_repetition_penalty_kwargs("openai", 0.3, 0.2) == {
            "frequency_penalty": 0.3,
            "presence_penalty": 0.2,
        }
        assert resolve_repetition_penalty_kwargs("deepseek", 0.3, 0.2) == {
            "frequency_penalty": 0.3,
            "presence_penalty": 0.2,
        }

    def test_google_aliases_get_both_penalties(self):
        for provider in ("google", "google_genai", "vertexai", "gemini"):
            assert resolve_repetition_penalty_kwargs(provider, 0.3, 0.2) == {
                "frequency_penalty": 0.3,
                "presence_penalty": 0.2,
            }

    def test_ollama_gets_native_repeat_penalty_too(self):
        assert resolve_repetition_penalty_kwargs("ollama", 0.3, 0.2, repeat_penalty=1.1) == {
            "repeat_penalty": 1.1,
            "frequency_penalty": 0.3,
            "presence_penalty": 0.2,
        }

    @pytest.mark.parametrize(
        "provider",
        ["anthropic", "anthropic_bedrock", "bedrock", "bedrock_converse", "custom-gateway", None],
    )
    def test_unsupported_provider_gets_no_field(self, provider):
        """Anthropic/Bedrock/unknown: its own default, never an unknown kwarg."""
        assert resolve_repetition_penalty_kwargs(provider, 0.3, 0.2, repeat_penalty=1.1) == {}

    def test_none_values_are_omitted_not_zeroed(self):
        assert resolve_repetition_penalty_kwargs("openai", None, 0.2) == {"presence_penalty": 0.2}
        assert resolve_repetition_penalty_kwargs("openai", 0.3, None) == {"frequency_penalty": 0.3}

    def test_zero_is_a_real_value(self):
        """0 disables the penalty; it must not be confused with "unset"."""
        assert resolve_repetition_penalty_kwargs("openai", 0.0, 0.0) == {
            "frequency_penalty": 0.0,
            "presence_penalty": 0.0,
        }

    def test_bounds_are_the_openai_google_range(self):
        assert (PENALTY_MIN, PENALTY_MAX) == (-2.0, 2.0)

    @pytest.mark.parametrize(
        "model", ["o1-mini", "o3-mini", "o4-mini", "gpt-5", "openai/gpt-5-mini"]
    )
    def test_openai_reasoning_models_reject_no_field(self, model):
        """o-series / gpt-5 answer 400 to a penalty parameter — send none."""
        assert (
            resolve_repetition_penalty_kwargs(
                "openai", 0.3, 0.2, repeat_penalty=1.1, model_name=model
            )
            == {}
        )
        assert family_repetition_penalty_kwargs("MAIN_LLM", "openai", model) == {}

    def test_a_non_reasoning_openai_model_still_gets_the_fields(self):
        assert resolve_repetition_penalty_kwargs("openai", 0.3, 0.2, model_name="gpt-4o") == {
            "frequency_penalty": 0.3,
            "presence_penalty": 0.2,
        }


class TestFamilyFallback:
    def test_empty_overrides_use_the_global_defaults(self):
        assert family_repetition_penalty_kwargs("MAIN_LLM", "openai") == {
            "frequency_penalty": REPETITION_PENALTY["frequency_penalty"],
            "presence_penalty": REPETITION_PENALTY["presence_penalty"],
        }

    def test_each_value_falls_back_independently(self, monkeypatch):
        monkeypatch.setenv("MAIN_LLM_FREQUENCY_PENALTY", "0.8")

        assert family_repetition_penalty_kwargs("MAIN_LLM", "openai") == {
            "frequency_penalty": 0.8,
            "presence_penalty": REPETITION_PENALTY["presence_penalty"],
        }

    def test_an_explicit_zero_wins_over_the_default(self, monkeypatch):
        monkeypatch.setenv("MAIN_LLM_FREQUENCY_PENALTY", "0")
        monkeypatch.setenv("MAIN_LLM_PRESENCE_PENALTY", "0")

        assert family_repetition_penalty_kwargs("MAIN_LLM", "openai") == {
            "frequency_penalty": 0.0,
            "presence_penalty": 0.0,
        }

    @pytest.mark.parametrize("raw", ["not-a-number", "2.5", "-2.5", "nan", "inf"])
    def test_invalid_override_falls_back_to_the_default(self, monkeypatch, raw):
        monkeypatch.setenv("MAIN_LLM_FREQUENCY_PENALTY", raw)

        assert family_repetition_penalty_kwargs("MAIN_LLM", "openai") == {
            "frequency_penalty": REPETITION_PENALTY["frequency_penalty"],
            "presence_penalty": REPETITION_PENALTY["presence_penalty"],
        }

    def test_override_belongs_to_its_own_family(self, monkeypatch):
        monkeypatch.setenv("REASONER_LLM_FREQUENCY_PENALTY", "0.1")

        main = family_repetition_penalty_kwargs("MAIN_LLM", "openai")
        reasoner = family_repetition_penalty_kwargs("REASONER_LLM", "openai")

        assert main["frequency_penalty"] == REPETITION_PENALTY["frequency_penalty"]
        assert reasoner["frequency_penalty"] == 0.1


class TestBuilderIntegration:
    def test_main_profile_config_carries_penalties(self):
        cfg = main_llm_module._profile_client_config(provider="openai", model="gpt-4o")

        assert cfg["frequency_penalty"] == REPETITION_PENALTY["frequency_penalty"]
        assert cfg["presence_penalty"] == REPETITION_PENALTY["presence_penalty"]

    def test_main_profile_config_honours_the_family_override(self, monkeypatch):
        monkeypatch.setenv("MAIN_LLM_FREQUENCY_PENALTY", "0.7")

        cfg = main_llm_module._profile_client_config(provider="openai", model="gpt-4o")

        assert cfg["frequency_penalty"] == 0.7

    def test_anthropic_profile_config_carries_none(self):
        cfg = main_llm_module._profile_client_config(provider="anthropic", model="claude-x")

        assert "frequency_penalty" not in cfg
        assert "presence_penalty" not in cfg

    def test_openai_reasoning_profile_config_carries_none(self):
        cfg = main_llm_module._profile_client_config(provider="openai", model="o3-mini")

        assert "frequency_penalty" not in cfg
        assert "presence_penalty" not in cfg

    def test_configured_family_reaches_init_chat_model(self, monkeypatch):
        monkeypatch.setenv("TASKFLOW_LLM_MODEL_LOCAL", "false")
        monkeypatch.setenv("TASKFLOW_LLM_PROVIDER", "openai")
        monkeypatch.setenv("TASKFLOW_LLM_API_NAME", "gpt-4o-mini")
        monkeypatch.setenv("TASKFLOW_LLM_API_BASE", "https://example.invalid/v1")
        monkeypatch.setenv("TASKFLOW_LLM_API_KEY", "sk-test")
        monkeypatch.setenv("TASKFLOW_LLM_FREQUENCY_PENALTY", "0.4")

        captured: dict[str, object] = {}

        def _fake_init(**kwargs):
            captured.update(kwargs)
            return FakeListChatModel(responses=["ok"])

        monkeypatch.setattr("langchain.chat_models.init_chat_model", _fake_init)

        model = build_taskflow_llm(temperature=0)

        assert isinstance(model, NormalizingChatModel)
        assert captured["frequency_penalty"] == 0.4
        assert captured["presence_penalty"] == REPETITION_PENALTY["presence_penalty"]

    def test_auxiliary_remote_branch_reaches_init_chat_model(self, monkeypatch):
        monkeypatch.setenv("AUXILIARY_LLM_MODEL_LOCAL", "false")
        monkeypatch.setenv("AUXILIARY_LLM_PROVIDER", "openai")
        monkeypatch.setenv("AUXILIARY_LLM_API_NAME", "gpt-4o-mini")
        monkeypatch.setenv("AUXILIARY_LLM_API_BASE", "https://example.invalid/v1")
        monkeypatch.setenv("AUXILIARY_LLM_API_KEY", "sk-test")
        monkeypatch.setenv("AUXILIARY_LLM_PRESENCE_PENALTY", "0.6")

        captured: dict[str, object] = {}

        def _fake_init(**kwargs):
            captured.update(kwargs)
            return FakeListChatModel(responses=["ok"])

        monkeypatch.setattr("langchain.chat_models.init_chat_model", _fake_init)

        model = build_auxiliary_llm()

        assert isinstance(model, NormalizingChatModel)
        assert captured["frequency_penalty"] == REPETITION_PENALTY["frequency_penalty"]
        assert captured["presence_penalty"] == 0.6
