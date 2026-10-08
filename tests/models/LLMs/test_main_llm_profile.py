"""``build_main_llm_for_profile`` — session model override construction.

The per-session model picker (chat toolbar → ``PUT /sessions/model``) hands the
agent a profile descriptor from the client's env-config profiles. The builder
mirrors ``build_main_llm``'s contract (fresh client on the calling loop,
``NormalizingChatModel`` wrapper, optional temperature bind) but takes the
provider/model/credentials from that profile, and it dispatches the thinking
payload for the OVERRIDE identity rather than the env one.
"""

import pytest

from config.features import REASONING_BUDGET
from models.LLMs import main_llm as main_llm_module
from models.LLMs.reasoning_payload import get_thinking_budget

pytestmark = [pytest.mark.unit]

_ENV_KEY = "env-key"
_ENV_BASE = "https://env.example/v1"


@pytest.fixture(autouse=True)
def env_credentials(monkeypatch):
    """Pin the env credential fallbacks the builder reads at call time."""
    monkeypatch.setenv("MAIN_LLM_API_KEY", _ENV_KEY)
    monkeypatch.setenv("MAIN_LLM_API_BASE", _ENV_BASE)
    monkeypatch.setattr(main_llm_module, "reasoning_effort", None)


def _reasoning_keys(cfg):
    return {k: cfg[k] for k in ("extra_body", "reasoning_effort", "thinking") if k in cfg}


class TestProfileClientConfig:
    def test_identity_and_credentials_come_from_the_profile(self):
        cfg = main_llm_module._profile_client_config(
            provider="openai",
            model="kimi-k2",
            api_key="sk-profile",
            base_url="https://api.moonshot.cn/v1",
        )

        assert cfg["model_provider"] == "openai" and cfg["model"] == "kimi-k2"
        assert cfg["api_key"] == "sk-profile"
        assert cfg["base_url"] == "https://api.moonshot.cn/v1"
        # The client extras match the env-config path (same retries/timeouts).
        assert cfg["temperature"] == 0
        assert "profile" in cfg and "max_retries" in cfg and "stream_chunk_timeout" in cfg

    def test_missing_credentials_fall_back_to_env(self):
        cfg = main_llm_module._profile_client_config(provider="zhipu", model="glm-4.6")

        assert cfg["api_key"] == _ENV_KEY
        assert cfg["base_url"] == _ENV_BASE

    def test_missing_provider_falls_back_to_env(self, monkeypatch):
        # The module global is snapshotted from the env at import, so pin it:
        # asserting against the ambient value only holds where a .env exists.
        monkeypatch.setattr(main_llm_module, "model_provider", "zhipu")

        cfg = main_llm_module._profile_client_config(provider=None, model="glm-4.6")

        assert cfg["model_provider"] == "zhipu"

    def test_unset_provider_everywhere_lets_the_client_infer(self, monkeypatch):
        """No provider in the profile and none in the env → the key is dropped
        so ``init_chat_model`` infers the provider from the model name."""
        monkeypatch.setattr(main_llm_module, "model_provider", None)

        cfg = main_llm_module._profile_client_config(provider=None, model="glm-4.6")

        assert "model_provider" not in cfg

    def test_thinking_dispatch_uses_the_override_identity(self):
        """glm-4.6 on the openai provider → the GLM enable/disable payload."""
        enabled = main_llm_module._profile_client_config(
            provider="openai", model="glm-4.6", thinking=True
        )
        assert _reasoning_keys(enabled) == {"extra_body": {"thinking": {"type": "enabled"}}}
        budget = get_thinking_budget("openai", "glm-4.6", True)
        assert enabled["max_tokens"] == main_llm_module.OUTPUT_MAX_TOKEN + budget

        disabled = main_llm_module._profile_client_config(
            provider="openai", model="glm-4.6", thinking=False
        )
        assert _reasoning_keys(disabled) == {"extra_body": {"thinking": {"type": "disabled"}}}
        # Thinking off keeps the key absent so the provider applies its own cap.
        assert "max_tokens" not in disabled

        level = main_llm_module._profile_client_config(
            provider="openai", model="glm-4.6", thinking_level="max"
        )
        assert _reasoning_keys(level) == {"extra_body": {"thinking": {"type": "max"}}}

        floor = main_llm_module._profile_client_config(
            provider="openai", model="glm-4.6", thinking=False, thinking_floor=True
        )
        assert _reasoning_keys(floor) == {"extra_body": {"thinking": {"type": "low"}}}
        assert floor["max_tokens"] == main_llm_module.OUTPUT_MAX_TOKEN + budget

    def test_thinking_none_keeps_the_env_default(self, monkeypatch):
        monkeypatch.setattr(main_llm_module, "enable_thinking", False)
        off_default = main_llm_module._profile_client_config(provider="openai", model="glm-4.6")
        # The env-default-off path adds nothing (mirrors the module init).
        assert _reasoning_keys(off_default) == {}
        assert "max_tokens" not in off_default

        monkeypatch.setattr(main_llm_module, "enable_thinking", True)
        on_default = main_llm_module._profile_client_config(provider="openai", model="glm-4.6")
        assert _reasoning_keys(on_default) == {"extra_body": {"thinking": {"type": "enabled"}}}
        assert on_default["max_tokens"] > main_llm_module.OUTPUT_MAX_TOKEN

    def test_non_reasoning_override_carries_no_thinking_keys(self):
        cfg = main_llm_module._profile_client_config(
            provider="openai", model="kimi-k2", thinking=True
        )

        assert _reasoning_keys(cfg) == {}
        assert "max_tokens" not in cfg


class TestPublicBuilder:
    def test_zhipu_override_uses_the_reasoning_capable_client(self, monkeypatch):
        """glm-* via the openai provider must keep delta.reasoning_content."""
        seen: dict = {}

        class _FakeReasoning:
            def __init__(self, **kwargs):
                seen["client"] = "reasoning"
                seen["kwargs"] = kwargs

        class _FakeNormalizer:
            def __init__(self, inner):
                self.inner = inner

            def bind(self, **kwargs):
                seen["temperature"] = kwargs.get("temperature")
                return self

        monkeypatch.setattr(main_llm_module, "ReasoningChatOpenAI", _FakeReasoning)
        monkeypatch.setattr(main_llm_module, "NormalizingChatModel", _FakeNormalizer)

        main_llm_module.build_main_llm_for_profile(
            provider="openai", model="glm-4.6", temperature=0.3
        )

        assert seen["client"] == "reasoning"
        assert seen["kwargs"]["model"] == "glm-4.6"
        assert seen["temperature"] == 0.3

    def test_plain_override_uses_init_chat_model(self, monkeypatch):
        seen: dict = {}

        def _fake_init_chat_model(**kwargs):
            seen["client"] = "init_chat_model"
            seen["kwargs"] = kwargs
            return object()

        class _FakeNormalizer:
            def __init__(self, inner):
                self.inner = inner

        monkeypatch.setattr(main_llm_module, "init_chat_model", _fake_init_chat_model)
        monkeypatch.setattr(main_llm_module, "NormalizingChatModel", _FakeNormalizer)

        main_llm_module.build_main_llm_for_profile(provider="openai", model="kimi-k2")

        assert seen["client"] == "init_chat_model"
        assert seen["kwargs"]["model"] == "kimi-k2"

    def test_thinking_budget_is_inflated_in_the_built_config(self, monkeypatch):
        """glm-* goes through ReasoningChatOpenAI, so spy on both client paths."""
        seen: dict = {}

        class _RecordingClient:
            def __init__(self, **kwargs):
                seen["kwargs"] = kwargs

        class _FakeNormalizer:
            def __init__(self, inner):
                self.inner = inner

        monkeypatch.setattr(main_llm_module, "init_chat_model", _RecordingClient)
        monkeypatch.setattr(main_llm_module, "ReasoningChatOpenAI", _RecordingClient)
        monkeypatch.setattr(main_llm_module, "NormalizingChatModel", _FakeNormalizer)

        main_llm_module.build_main_llm_for_profile(
            provider="openai", model="glm-4.6", thinking=True
        )

        budget = REASONING_BUDGET["non_anthropic_default_thinking_budget"]
        assert seen["kwargs"]["max_tokens"] == main_llm_module.OUTPUT_MAX_TOKEN + budget
