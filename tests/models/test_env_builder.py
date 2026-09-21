"""ModelEnvBuilder: shared env→client-kwargs assembly (DESIGN_PATTERN §5.3).

Pins both the helper behavior and the per-model client kwargs, so the extraction
can never silently change an env key name, default, strip semantic, or the shape
of the dict handed to ``init_chat_model``.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

import config
from config.features import LLM_CLIENT_DEFAULTS

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_ROOT = Path(__file__).parents[2]


@pytest.fixture(autouse=True)
def _hermetic_env(monkeypatch, tmp_path):
    """Stop the repo ``.env`` from leaking into spec-loaded modules."""
    monkeypatch.setattr(config, "ENV_PATH", str(tmp_path / "missing.env"))


def _load(name: str, relpath: str):
    spec = importlib.util.spec_from_file_location(name, _ROOT / relpath)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class TestReadEnv:
    def test_missing_and_blank_collapse_to_none(self, monkeypatch):
        monkeypatch.delenv("SHERRY_TEST_ENV", raising=False)
        from models.env_builder import read_env

        assert read_env("SHERRY_TEST_ENV") is None
        monkeypatch.setenv("SHERRY_TEST_ENV", "")
        assert read_env("SHERRY_TEST_ENV") is None
        monkeypatch.setenv("SHERRY_TEST_ENV", "   ")
        assert read_env("SHERRY_TEST_ENV") is None

    def test_strips_by_default(self, monkeypatch):
        from models.env_builder import read_env

        monkeypatch.setenv("SHERRY_TEST_ENV", "  value  ")
        assert read_env("SHERRY_TEST_ENV") == "value"

    def test_strip_false_preserves_raw_but_still_collapses_empty(self, monkeypatch):
        from models.env_builder import read_env

        monkeypatch.setenv("SHERRY_TEST_ENV", "  value  ")
        assert read_env("SHERRY_TEST_ENV", strip=False) == "  value  "
        monkeypatch.setenv("SHERRY_TEST_ENV", "")
        assert read_env("SHERRY_TEST_ENV", strip=False) is None


class TestCleanClientKwargs:
    def test_drops_none_and_empty_string_only(self):
        from models.env_builder import clean_client_kwargs

        cleaned = clean_client_kwargs({"a": None, "b": "", "c": 0, "d": False, "e": [], "f": "x"})
        assert cleaned == {"c": 0, "d": False, "e": [], "f": "x"}


class TestBuilder:
    def test_reads_credentials_merges_extra_and_filters(self, monkeypatch):
        from models.env_builder import ModelEnvBuilder

        monkeypatch.setenv("T_MODEL_PROVIDER", "openai")
        monkeypatch.setenv("T_API_NAME", "gpt-x")
        monkeypatch.delenv("T_API_KEY", raising=False)
        monkeypatch.setenv("T_API_BASE", "https://t.invalid/v1")

        config_dict = ModelEnvBuilder(
            {
                "model_provider": "T_MODEL_PROVIDER",
                "model": "T_API_NAME",
                "api_key": "T_API_KEY",
                "base_url": "T_API_BASE",
            }
        ).build({"temperature": 0, "max_retries": 2})

        assert config_dict == {
            "model_provider": "openai",
            "model": "gpt-x",
            "base_url": "https://t.invalid/v1",
            "temperature": 0,
            "max_retries": 2,
        }


class TestPerModelKwargs:
    """Each model's assembled client kwargs, key-for-key."""

    def test_ittt_remote_kwargs(self, monkeypatch):
        monkeypatch.setenv("ITTT_MODEL_LOCAL", "false")
        monkeypatch.setenv("ITTT_MODEL_PROVIDER", "openai")
        monkeypatch.setenv("ITTT_API_NAME", "gpt-4o-mini")
        monkeypatch.setenv("ITTT_API_KEY", "sk-ittt")
        monkeypatch.setenv("ITTT_API_BASE", "https://ittt.invalid/v1")
        module = _load("_ittt_env_builder", "models/ITTT_model/core.py")

        assert module._model_config == {
            "model_provider": "openai",
            "model": "gpt-4o-mini",
            "api_key": "sk-ittt",
            "base_url": "https://ittt.invalid/v1",
            "temperature": LLM_CLIENT_DEFAULTS["ittt_remote_temperature"],
            "max_retries": LLM_CLIENT_DEFAULTS["ittt_remote_max_retries"],
            "timeout": LLM_CLIENT_DEFAULTS["ittt_remote_timeout"],
        }

    def test_vttt_remote_kwargs(self, monkeypatch):
        monkeypatch.setenv("VTTT_MODEL_LOCAL", "false")
        monkeypatch.setenv("VTTT_MODEL_PROVIDER", "openai")
        monkeypatch.setenv("VTTT_API_NAME", "gpt-4o-mini")
        monkeypatch.setenv("VTTT_API_KEY", "sk-vttt")
        monkeypatch.setenv("VTTT_API_BASE", "https://vttt.invalid/v1")
        module = _load("_vttt_env_builder", "models/VTTT_model/core.py")

        assert module._model_config == {
            "model_provider": "openai",
            "model": "gpt-4o-mini",
            "api_key": "sk-vttt",
            "base_url": "https://vttt.invalid/v1",
            "temperature": LLM_CLIENT_DEFAULTS["vttt_remote_temperature"],
            "max_retries": LLM_CLIENT_DEFAULTS["vttt_remote_max_retries"],
            "timeout": LLM_CLIENT_DEFAULTS["vttt_remote_timeout"],
        }

    def test_main_llm_kwargs_read_verbatim_and_keep_extra_keys(self, monkeypatch):
        monkeypatch.setenv("MAIN_LLM_PROVIDER", "openai")
        monkeypatch.setenv("MAIN_LLM_NAME", "gpt-4o-mini")
        monkeypatch.setenv("MAIN_LLM_API_KEY", "  ")
        monkeypatch.setenv("MAIN_LLM_API_BASE", "https://main.invalid/v1")
        monkeypatch.setenv("MAIN_LLM_MAX_TOKEN", "131072")
        monkeypatch.setenv("MAIN_LLM_ENABLE_THINKING", "false")
        module = _load("_main_env_builder", "models/LLMs/main_llm.py")

        assert module.model_config == {
            "model_provider": "openai",
            "model": "gpt-4o-mini",
            # strip=False keeps the verbatim whitespace value (pre-refactor
            # behavior: a plain os.getenv).
            "api_key": "  ",
            "base_url": "https://main.invalid/v1",
            "temperature": 0,
            "max_retries": LLM_CLIENT_DEFAULTS["main_max_retries"],
            "timeout": LLM_CLIENT_DEFAULTS["main_timeout"],
            "stream_chunk_timeout": LLM_CLIENT_DEFAULTS["main_stream_chunk_timeout"],
            "profile": {"max_input_tokens": 131072},
        }

    def test_reasoner_kwargs(self, monkeypatch):
        monkeypatch.setenv("REASONER_LLM_PROVIDER", "openai")
        monkeypatch.setenv("REASONER_LLM_NAME", "o4-mini")
        monkeypatch.setenv("REASONER_LLM_API_KEY", "sk-rea")
        monkeypatch.setenv("REASONER_LLM_API_BASE", "https://rea.invalid/v1")
        monkeypatch.setenv("REASONER_LLM_MAX_TOKEN", "64000")
        module = _load("_reasoner_env_builder", "models/LLMs/reasoner_llm.py")

        assert module.model_config == {
            "model_provider": "openai",
            "model": "o4-mini",
            "api_key": "sk-rea",
            "base_url": "https://rea.invalid/v1",
            "temperature": 0.5,
            "max_retries": LLM_CLIENT_DEFAULTS["reasoner_max_retries"],
            "timeout": LLM_CLIENT_DEFAULTS["reasoner_timeout"],
            "profile": {"max_input_tokens": 64000},
        }

    def test_auxiliary_remote_kwargs(self, monkeypatch):
        monkeypatch.setenv("AUXILIARY_LLM_MODEL_LOCAL", "false")
        monkeypatch.setenv("AUXILIARY_LLM_PROVIDER", "openai")
        monkeypatch.setenv("AUXILIARY_LLM_API_NAME", "gpt-4o-mini")
        monkeypatch.setenv("AUXILIARY_LLM_API_KEY", "sk-aux")
        monkeypatch.setenv("AUXILIARY_LLM_API_BASE", "https://aux.invalid/v1")
        monkeypatch.setenv("AUXILIARY_LLM_MAX_TOKEN", "65536")
        module = _load("_aux_env_builder", "models/LLMs/auxiliary_llm/core.py")

        captured: dict = {}
        monkeypatch.setattr(
            "langchain.chat_models.init_chat_model",
            lambda **kwargs: captured.update(kwargs) or object(),
        )
        monkeypatch.setattr(module, "NormalizingChatModel", lambda inner: inner)
        module.build_auxiliary_llm()

        assert captured == {
            "model_provider": "openai",
            "model": "gpt-4o-mini",
            "api_key": "sk-aux",
            "base_url": "https://aux.invalid/v1",
            "temperature": 0,
            "max_retries": LLM_CLIENT_DEFAULTS["aux_max_retries"],
            "timeout": LLM_CLIENT_DEFAULTS["aux_timeout"],
            "profile": {"max_input_tokens": 65536},
        }
