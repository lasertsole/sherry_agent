"""Reasoner client timeout wiring: a stalled remote call must fail bounded.

Regression: ``reasoner_llm.model_config`` carried no ``timeout``, so the
OpenAI SDK default (600 s) applied to every attempt (max_retries + 1) — one
stalled reasoner call could hang for ~30 min, mirroring the aux bug fixed in
``8aa11b90``.
"""

import importlib.util
from pathlib import Path

import pytest

import config
from config.features import LLM_CLIENT_DEFAULTS

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_REASONER_PATH = Path(__file__).parents[3] / "models" / "LLMs" / "reasoner_llm.py"

_REASONER_REMOTE_ENV = {
    "REASONER_LLM_PROVIDER": "openai",
    "REASONER_LLM_NAME": "o4-mini",
    "REASONER_LLM_API_KEY": "sk-test",
    "REASONER_LLM_API_BASE": "https://example.invalid/v1",
    "REASONER_LLM_MAX_TOKEN": "64000",
}


@pytest.fixture(autouse=True)
def _hermetic_remote_env(monkeypatch, tmp_path):
    """Force the remote branch against a fake env, never the repo ``.env``.

    ``reasoner_llm`` calls ``load_dotenv(ENV_PATH, override=True)``, so the
    ``ENV_PATH`` target is repointed at a missing file to keep the
    monkeypatched values authoritative on developer machines too.
    """
    for key, value in _REASONER_REMOTE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(config, "ENV_PATH", str(tmp_path / "missing.env"))


def _load_reasoner_module():
    """Execute ``reasoner_llm.py`` in a throwaway module namespace.

    ``model_config`` is built at import time, so env changes require a fresh
    execution. A spec-based load (instead of ``importlib.reload`` on the real
    module) keeps the test from leaving a test-configured module in
    ``sys.modules`` for the rest of the process.
    """
    spec = importlib.util.spec_from_file_location("_reasoner_llm_under_test", _REASONER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_remote_reasoner_client_sets_bounded_request_timeout():
    module = _load_reasoner_module()

    model = module.build_reasoner_model()

    assert model.inner.request_timeout == 120


def test_remote_reasoner_client_consumes_registry_timeout(monkeypatch):
    # A distinct value proves the client reads the registry key rather than a
    # hardcoded literal that merely happens to equal the default.
    monkeypatch.setitem(LLM_CLIENT_DEFAULTS, "reasoner_timeout", 240)
    module = _load_reasoner_module()

    model = module.build_reasoner_model()

    assert model.inner.request_timeout == 240
