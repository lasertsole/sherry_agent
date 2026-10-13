"""Provider-registry injection contract for ``config.schema`` (cycle #6 break).

``config`` must not import ``models``: the provider metadata is pushed in via
``set_provider_registry`` callbacks, and provider matching must fail closed
when nothing is registered. These tests pin the injected behavior as
equivalent to the previous direct import.
"""

from __future__ import annotations

import pytest

import config.schema as schema
from config.schema import Config
from models.providers.registry import find_by_name, register_provider_registry

pytestmark = [pytest.mark.unit]


def _auto_config(model: str, **providers: dict) -> Config:
    return Config(
        agents={"defaults": {"provider": "auto", "model": model}},
        providers=providers,
    )


class TestUnregisteredFailsClosed:
    def test_provider_matching_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(schema, "_provider_registry_fn", None)
        monkeypatch.setattr(schema, "_provider_find_fn", None)
        c = _auto_config("deepseek-chat", deepseek={"api_key": "k"})

        with pytest.raises(RuntimeError, match="set_provider_registry"):
            c.get_provider_name("deepseek-chat")

    def test_api_base_lookup_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(schema, "_provider_find_fn", None)
        c = Config(
            agents={"defaults": {"provider": "openrouter", "model": "openrouter/anthropic/claude"}},
            providers={"openrouter": {"api_key": "k"}},
        )

        with pytest.raises(RuntimeError, match="set_provider_registry"):
            c.get_api_base("openrouter/anthropic/claude")


class TestInjectedRegistryEquivalence:
    def test_keyword_match_returns_provider(self) -> None:
        c = _auto_config("deepseek-chat", deepseek={"api_key": "k"})

        assert c.get_provider_name("deepseek-chat") == "deepseek"
        assert c.get_api_key("deepseek-chat") == "k"

    def test_explicit_prefix_beats_keyword_and_allows_oauth(self) -> None:
        c = _auto_config("github-copilot/gpt-4o")

        assert c.get_provider_name("github-copilot/gpt-4o") == "github_copilot"

    def test_local_provider_detected_by_api_base_keyword(self) -> None:
        c = _auto_config("phi3:mini", ollama={"api_base": "http://localhost:11434"})

        assert c.get_provider_name("phi3:mini") == "ollama"

    def test_gateway_default_api_base_comes_from_find_by_name(self) -> None:
        c = _auto_config("openrouter/anthropic/claude", openrouter={"api_key": "k"})

        assert c.get_api_base("openrouter/anthropic/claude") == "https://openrouter.ai/api/v1"

    def test_unknown_provider_is_rejected(self) -> None:
        c = Config(agents={"defaults": {"provider": "not_a_real_provider", "model": "x"}})

        assert c.get_provider("x") is None
        assert c.get_provider_name("x") is None
        assert c.get_api_key("x") is None
        assert c.get_api_base("x") is None

    def test_unmatched_model_yields_no_provider(self) -> None:
        c = _auto_config("totally-unknown-model")

        assert c.get_provider_name("totally-unknown-model") is None
        assert c.get_api_base("totally-unknown-model") is None

    def test_registry_lookup_consistent(self) -> None:
        spec = find_by_name("deepseek")

        assert spec is not None
        assert spec.name == "deepseek"
        assert find_by_name("no_such_provider") is None

    def test_registration_is_idempotent(self) -> None:
        register_provider_registry()
        register_provider_registry()

        assert schema._provider_registry_fn is not None
        assert schema._provider_find_fn is not None
