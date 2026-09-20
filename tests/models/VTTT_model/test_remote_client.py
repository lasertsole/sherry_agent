"""VTTT remote client: bounded timeout + registry-driven construction.

Regression: the remote branch hardcoded ``temperature: 0.8`` / ``max_retries: 2``
while ``LLM_CLIENT_DEFAULTS`` defined ``vttt_remote_temperature`` /
``vttt_remote_max_retries`` with zero consumers, and it built ``ChatOpenAI``
without ``timeout`` (SDK default 600 s on every attempt).
"""

import importlib.util
from pathlib import Path

import pytest

import config
from config.features import LLM_CLIENT_DEFAULTS

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_VTTT_PATH = Path(__file__).parents[3] / "models" / "VTTT_model" / "core.py"

_VTTT_REMOTE_ENV = {
    "VTTT_MODEL_LOCAL": "false",
    "VTTT_MODEL_PROVIDER": "openai",
    "VTTT_API_NAME": "gpt-4o-mini",
    "VTTT_API_KEY": "sk-test",
    "VTTT_API_BASE": "https://example.invalid/v1",
}


@pytest.fixture(autouse=True)
def _hermetic_remote_env(monkeypatch, tmp_path):
    """Force the remote branch against a fake env, never the repo ``.env``.

    ``core.py`` calls ``load_dotenv(ENV_PATH, override=True)``, so the
    ``ENV_PATH`` target is repointed at a missing file to keep the
    monkeypatched values authoritative on developer machines too.
    """
    for key, value in _VTTT_REMOTE_ENV.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(config, "ENV_PATH", str(tmp_path / "missing.env"))


def _load_vttt_module():
    """Execute ``core.py`` in a throwaway module namespace.

    The remote/local branch and the ``VTTT_model`` singleton are decided at
    import time, so env changes require a fresh execution. A spec-based load
    (instead of ``importlib.reload`` on the real module) keeps the test from
    leaving a test-configured singleton in ``sys.modules`` for the rest of the
    process — reloading back would rebuild the real branch, which needs a
    configured ``.env`` that hermetic CI does not have.
    """
    spec = importlib.util.spec_from_file_location("_vttt_core_under_test", _VTTT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_remote_vttt_client_sets_bounded_request_timeout():
    module = _load_vttt_module()

    # ``.default`` is the real ChatOpenAI behind RunnableConfigurableFields:
    # DynamicRunnable.__getattr__ delegates attribute access to it, and the
    # delegated ``.request_timeout`` reads the same instance attribute.
    assert module.VTTT_model.default.request_timeout == 120


def test_remote_vttt_client_consumes_registry_values(monkeypatch):
    # Distinct values prove construction reads the registry keys rather than
    # hardcoded literals that merely happen to equal the defaults.
    monkeypatch.setitem(LLM_CLIENT_DEFAULTS, "vttt_remote_temperature", 0.31)
    monkeypatch.setitem(LLM_CLIENT_DEFAULTS, "vttt_remote_max_retries", 7)
    monkeypatch.setitem(LLM_CLIENT_DEFAULTS, "vttt_remote_timeout", 240)
    module = _load_vttt_module()
    client = module.VTTT_model.default

    assert client.temperature == 0.31
    assert client.max_retries == 7
    assert client.request_timeout == 240
