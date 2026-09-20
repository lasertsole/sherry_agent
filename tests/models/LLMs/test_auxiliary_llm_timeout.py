"""Auxiliary client timeout wiring: a stalled remote call must fail bounded.

Regression: the aux remote branch once constructed ``ChatOpenAI`` without
``timeout``, leaving the OpenAI SDK default (600 s) on every attempt, so one
stalled aux call could silently hang for ~30 min (600 s x (max_retries + 1)).
"""

import pytest

from models.LLMs.auxiliary_llm.core import build_auxiliary_llm

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_AUX_REMOTE_ENV = {
    "AUXILIARY_LLM_MODEL_LOCAL": "false",
    "AUXILIARY_LLM_PROVIDER": "openai",
    "AUXILIARY_LLM_API_NAME": "gpt-4o-mini",
    "AUXILIARY_LLM_API_KEY": "sk-test",
    "AUXILIARY_LLM_API_BASE": "https://example.invalid/v1",
}


@pytest.fixture(autouse=True)
def _aux_remote_env(monkeypatch):
    for key, value in _AUX_REMOTE_ENV.items():
        monkeypatch.setenv(key, value)


def test_remote_aux_client_sets_bounded_request_timeout():
    model = build_auxiliary_llm()

    assert model.inner.request_timeout == 120
