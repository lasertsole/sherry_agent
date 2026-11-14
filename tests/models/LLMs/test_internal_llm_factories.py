"""The optional internal model families (``TASKFLOW_LLM_*`` / ``COMPRESSION_LLM_*``).

Three contracts are pinned here:

* an UNCONFIGURED family returns ``build_auxiliary_llm()`` — the
  backward-compatible fallback (T3 / C2);
* local mode reuses the auxiliary GGUF even when a provider is configured
  (T4 / C7, decision A);
* a configured family builds through the same plumbing as the auxiliary
  client (retry / timeout / output-cap kwargs).
"""

import pytest
from langchain_core.language_models.fake_chat_models import FakeListChatModel

from config.models_settings import MODEL_SETTING_KEYS
from models.LLMs.compression_llm.core import build_compression_llm
from models.LLMs.reasoning_normalizer import NormalizingChatModel
from models.LLMs.taskflow_llm.core import build_taskflow_llm

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_PREFIXES = ("TASKFLOW_LLM_", "COMPRESSION_LLM_")


@pytest.fixture(autouse=True)
def _clean_family_env(monkeypatch: pytest.MonkeyPatch):
    """Drop the hydrated .models values so each test states its own config."""
    for key in MODEL_SETTING_KEYS:
        if key.startswith(_PREFIXES):
            monkeypatch.delenv(key, raising=False)
    yield


def _sentinel_auxiliary(monkeypatch: pytest.MonkeyPatch) -> object:
    sentinel = object()
    calls: list[float | None] = []

    def _build(temperature: float | None = None):
        calls.append(temperature)
        return sentinel

    monkeypatch.setattr("models.build_auxiliary_llm", _build)
    return sentinel


@pytest.mark.parametrize("builder", [build_taskflow_llm, build_compression_llm])
def test_unconfigured_family_falls_back_to_auxiliary(builder, monkeypatch):
    sentinel = _sentinel_auxiliary(monkeypatch)

    assert builder(temperature=0) is sentinel


@pytest.mark.parametrize("builder", [build_taskflow_llm, build_compression_llm])
def test_local_mode_reuses_the_auxiliary_branch_even_when_remote_is_configured(
    builder, monkeypatch
):
    prefix = "TASKFLOW_LLM" if builder is build_taskflow_llm else "COMPRESSION_LLM"
    monkeypatch.setenv(f"{prefix}_MODEL_LOCAL", "true")
    monkeypatch.setenv(f"{prefix}_PROVIDER", "openai")
    monkeypatch.setenv(f"{prefix}_API_BASE", "https://example.invalid/v1")
    sentinel = _sentinel_auxiliary(monkeypatch)

    assert builder(temperature=0) is sentinel


@pytest.mark.parametrize("builder", [build_taskflow_llm, build_compression_llm])
def test_configured_family_builds_a_remote_client(builder, monkeypatch):
    prefix = "TASKFLOW_LLM" if builder is build_taskflow_llm else "COMPRESSION_LLM"
    monkeypatch.setenv(f"{prefix}_MODEL_LOCAL", "false")
    monkeypatch.setenv(f"{prefix}_PROVIDER", "openai")
    monkeypatch.setenv(f"{prefix}_API_NAME", "gpt-4o-mini")
    monkeypatch.setenv(f"{prefix}_API_BASE", "https://example.invalid/v1")
    monkeypatch.setenv(f"{prefix}_API_KEY", "sk-test")
    monkeypatch.setenv(f"{prefix}_MAX_TOKEN", "200000")
    monkeypatch.setenv(f"{prefix}_MAX_OUTPUT_TOKEN", "8192")

    captured: dict[str, object] = {}

    def _fake_init(**kwargs):
        captured.update(kwargs)
        return FakeListChatModel(responses=["ok"])

    monkeypatch.setattr("langchain.chat_models.init_chat_model", _fake_init)

    model = builder(temperature=0)

    assert isinstance(model, NormalizingChatModel)
    assert captured["model_provider"] == "openai"
    assert captured["model"] == "gpt-4o-mini"
    assert captured["base_url"] == "https://example.invalid/v1"
    assert captured["api_key"] == "sk-test"
    assert captured["temperature"] == 0
    assert captured["max_tokens"] == 8192
    assert captured["profile"] == {"max_input_tokens": 200000}


def test_a_provider_that_cannot_be_built_falls_back_to_auxiliary(monkeypatch):
    monkeypatch.setenv("TASKFLOW_LLM_PROVIDER", "not-a-real-provider")
    monkeypatch.setenv("TASKFLOW_LLM_API_NAME", "whatever")
    sentinel = _sentinel_auxiliary(monkeypatch)

    assert build_taskflow_llm() is sentinel


def test_lazy_exports_are_available_from_the_models_package():
    import models

    assert models.build_taskflow_llm is not None
    assert models.build_compression_llm is not None


# ── Per-role subagent families (EXECUTOR_LLM_* / …) ──────────────────────────


@pytest.fixture(autouse=True)
def _clean_role_family_env(monkeypatch: pytest.MonkeyPatch):
    """Drop the hydrated role-family values so each test states its own config."""
    for key in MODEL_SETTING_KEYS:
        if key.split("_", 1)[0] in {"EXECUTOR", "RESEARCHER", "REVIEWER", "LIBRARIAN"}:
            monkeypatch.delenv(key, raising=False)
    yield


def _sentinel_main(monkeypatch: pytest.MonkeyPatch) -> object:
    sentinel = object()
    calls: list[float | None] = []

    def _build(temperature: float | None = None):
        calls.append(temperature)
        return sentinel

    monkeypatch.setattr("models.build_main_llm", _build)
    return sentinel


def test_an_unconfigured_role_family_builder_falls_back_to_main(monkeypatch):
    from models.LLMs.role_llm import build_role_llm

    sentinel = _sentinel_main(monkeypatch)

    assert build_role_llm("executor", temperature=0.3) is sentinel


def test_local_role_family_reuses_the_main_branch(monkeypatch):
    from models.LLMs.role_llm import build_role_llm

    monkeypatch.setenv("EXECUTOR_LLM_MODEL_LOCAL", "true")
    monkeypatch.setenv("EXECUTOR_LLM_PROVIDER", "openai")
    monkeypatch.setenv("EXECUTOR_LLM_API_BASE", "https://example.invalid/v1")
    sentinel = _sentinel_main(monkeypatch)

    assert build_role_llm("executor") is sentinel


def test_a_configured_role_family_builds_a_remote_client(monkeypatch):
    from models.LLMs.role_llm import build_role_llm

    monkeypatch.setenv("REVIEWER_LLM_PROVIDER", "openai")
    monkeypatch.setenv("REVIEWER_LLM_API_NAME", "gpt-4o-mini")
    monkeypatch.setenv("REVIEWER_LLM_API_BASE", "https://example.invalid/v1")
    monkeypatch.setenv("REVIEWER_LLM_API_KEY", "sk-test")
    monkeypatch.setenv("REVIEWER_LLM_MAX_TOKEN", "200000")
    monkeypatch.setenv("REVIEWER_LLM_MAX_OUTPUT_TOKEN", "8192")
    captured: dict[str, object] = {}

    def _fake_init(**kwargs):
        captured.update(kwargs)
        return FakeListChatModel(responses=["ok"])

    monkeypatch.setattr("langchain.chat_models.init_chat_model", _fake_init)

    model = build_role_llm("reviewer", temperature=0)

    assert isinstance(model, NormalizingChatModel)
    assert captured["model_provider"] == "openai"
    assert captured["model"] == "gpt-4o-mini"
    assert captured["base_url"] == "https://example.invalid/v1"
    assert captured["max_tokens"] == 8192


def test_role_llm_configured_is_false_until_provider_base_or_local(monkeypatch):
    from models.LLMs.role_llm import role_llm_configured

    assert role_llm_configured("executor") is False

    monkeypatch.setenv("EXECUTOR_LLM_API_BASE", "https://example.invalid/v1")
    assert role_llm_configured("executor") is True

    monkeypatch.delenv("EXECUTOR_LLM_API_BASE")
    monkeypatch.setenv("EXECUTOR_LLM_MODEL_LOCAL", "true")
    assert role_llm_configured("executor") is True


def test_the_identity_role_has_no_family():
    from models.LLMs.role_llm import build_role_llm, role_family, role_llm_configured

    assert role_family("general") is None
    assert role_llm_configured("general") is False
    with pytest.raises(ValueError):
        build_role_llm("general")


def test_family_configured_reports_remote_or_local():
    from models.LLMs.family_llm import family_configured

    assert family_configured("TASKFLOW_LLM") is False
