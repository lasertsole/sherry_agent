"""Declarative model capability catalog (2026-11-10 image plan #0)."""

from __future__ import annotations

import pytest

from config import model_capabilities as caps

pytestmark = [pytest.mark.unit]


@pytest.fixture(autouse=True)
def _fresh_catalog():
    caps.reset_catalog_cache()
    yield
    caps.reset_catalog_cache()


def test_catalog_answers_known_models():
    assert caps.get_declared_capability("openai", "gpt-4o", "vision") == "supported"
    assert caps.get_declared_capability("openai", "gpt-4o", "pdf") == "supported"
    assert caps.get_declared_capability("google", "gemini-2.5-flash", "video") == "supported"
    assert caps.get_declared_capability("deepseek", "deepseek-chat", "vision") == "unsupported"


def test_unknown_model_or_media_is_not_declared():
    assert caps.get_declared_capability("unknown", "model", "vision") is None
    assert caps.get_declared_capability("openai", "gpt-4o", "audio") is None


def test_user_override_merges_over_the_catalog(monkeypatch):
    from config import sherry_settings

    monkeypatch.setattr(
        sherry_settings,
        "load_sherry_settings",
        lambda: {
            "model_capabilities": {
                "custom-provider/my-model": {"supports_images": True},
                "openai/gpt-4o": {"supports_video": True},
            }
        },
    )
    caps.reset_catalog_cache()

    assert caps.get_declared_capability("custom-provider", "my-model", "vision") == "supported"
    # The catalog's own fields survive; the override only adds/overrides keys.
    assert caps.get_declared_capability("openai", "gpt-4o", "vision") == "supported"
    assert caps.get_declared_capability("openai", "gpt-4o", "video") == "supported"


def test_boolean_strings_are_tolerated(monkeypatch):
    from config import sherry_settings

    monkeypatch.setattr(
        sherry_settings,
        "load_sherry_settings",
        lambda: {"model_capabilities": {"probe/x": {"supports_images": "false"}}},
    )
    caps.reset_catalog_cache()

    assert caps.get_declared_capability("probe", "x", "vision") == "unsupported"
