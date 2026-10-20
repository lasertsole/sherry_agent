"""User-configurable pricing table + litellm cache resolution."""

from __future__ import annotations

import pytest

from config.features.infra_side import model_pricing

pytestmark = [pytest.mark.unit]


def test_the_shipped_default_table_lists_the_common_models():
    from config.models_settings import MODEL_SETTING_DEFAULTS

    table = MODEL_SETTING_DEFAULTS["MODEL_PRICING_TABLE"]
    assert table["glm-5"] == {"input": 0.5, "output": 1.5}
    assert table["deepseek-chat"] == {"input": 0.14, "output": 0.28}
    assert table["_default"] == {"input": 0.5, "output": 1.5}


def test_load_pricing_table_parses_a_valid_json_object():
    table = model_pricing._load_pricing_table(
        '{"claude-3-5-sonnet": {"input": 3.0, "output": 15.0}}'
    )

    assert table["claude-3-5-sonnet"] == {"input": 3.0, "output": 15.0}
    assert table["_default"] == model_pricing._FALLBACK_DEFAULT


def test_load_pricing_table_falls_back_on_garbage():
    assert model_pricing._load_pricing_table("not json") == {
        "_default": model_pricing._FALLBACK_DEFAULT
    }
    assert model_pricing._load_pricing_table("") == {"_default": model_pricing._FALLBACK_DEFAULT}


def test_resolve_prefers_the_user_table(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(
        model_pricing.MODEL_PRICING,
        "model_pricing_per_m_tokens",
        {
            "gpt-4o": {"input": 2.5, "output": 10.0},
            "_default": {"input": 0.5, "output": 1.5},
        },
    )
    monkeypatch.setattr(
        model_pricing, "_LITELLM_CACHE", {"gpt-4o": {"input": 99.0, "output": 99.0}}
    )

    assert model_pricing.resolve_model_pricing("gpt-4o") == {"input": 2.5, "output": 10.0}


def test_resolve_falls_back_to_the_litellm_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(
        model_pricing.MODEL_PRICING,
        "model_pricing_per_m_tokens",
        {"_default": {"input": 0.5, "output": 1.5}},
    )
    monkeypatch.setattr(
        model_pricing, "_LITELLM_CACHE", {"openai/gpt-4o": {"input": 2.5, "output": 10.0}}
    )

    assert model_pricing.resolve_model_pricing("openai/gpt-4o") == {
        "input": 2.5,
        "output": 10.0,
    }


def test_resolve_falls_back_to_default(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(
        model_pricing.MODEL_PRICING,
        "model_pricing_per_m_tokens",
        {"_default": {"input": 0.1, "output": 0.3}},
    )
    monkeypatch.setattr(model_pricing, "_LITELLM_CACHE", {})

    assert model_pricing.resolve_model_pricing("unknown-model") == {
        "input": 0.1,
        "output": 0.3,
    }


def test_resolve_accepts_a_provider_prefixed_name(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(
        model_pricing.MODEL_PRICING,
        "model_pricing_per_m_tokens",
        {"gpt-4o": {"input": 2.5, "output": 10.0}, "_default": {"input": 0.5, "output": 1.5}},
    )
    monkeypatch.setattr(model_pricing, "_LITELLM_CACHE", {})

    assert model_pricing.resolve_model_pricing("openai/gpt-4o") == {
        "input": 2.5,
        "output": 10.0,
    }
