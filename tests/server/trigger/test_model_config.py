"""Integration contract for ``GET /model-config``.

Drives the registered Robyn handler directly (no server started): Robyn's route
wrapper formats the handler's dict into a JSON ``Response``, which these tests
parse back and assert on.
"""

import asyncio
import json

import pytest

from config.features import LLM_CLIENT_DEFAULTS, MIN_REQUIRED_MAX_TOKEN
from server.trigger.http import model_config as model_config_http

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


class _FakeRequest:
    """Non-Robyn request: the route wrapper forwards it to the bare handler."""


def _call_handler() -> dict:
    response = asyncio.run(model_config_http.model_config_handler(_FakeRequest()))
    return json.loads(response.description)


def test_valid_when_both_values_at_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIN_LLM_MAX_TOKEN", "131072")
    monkeypatch.setenv("AUXILIARY_LLM_MAX_TOKEN", "131072")

    result = _call_handler()

    assert result == {
        "main_max_token": MIN_REQUIRED_MAX_TOKEN,
        "aux_max_token": MIN_REQUIRED_MAX_TOKEN,
        "min_required": MIN_REQUIRED_MAX_TOKEN,
        "valid": True,
    }


def test_invalid_when_main_below_minimum(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIN_LLM_MAX_TOKEN", "65536")
    monkeypatch.setenv("AUXILIARY_LLM_MAX_TOKEN", "131072")

    result = _call_handler()

    assert result["main_max_token"] == 65_536
    assert result["valid"] is False


def test_unset_auxiliary_reports_default_and_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIN_LLM_MAX_TOKEN", "131072")
    monkeypatch.delenv("AUXILIARY_LLM_MAX_TOKEN", raising=False)

    result = _call_handler()

    assert result["aux_max_token"] == LLM_CLIENT_DEFAULTS["aux_remote_max_tokens"]
    assert result["valid"] is False


def test_unset_main_reports_null_and_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MAIN_LLM_MAX_TOKEN", raising=False)
    monkeypatch.setenv("AUXILIARY_LLM_MAX_TOKEN", "131072")

    result = _call_handler()

    assert result["main_max_token"] is None
    assert result["min_required"] == MIN_REQUIRED_MAX_TOKEN
    assert result["valid"] is False
