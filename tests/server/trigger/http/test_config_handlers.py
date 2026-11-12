"""Config HTTP handlers: a missing file is an INSTALL problem, never a bare 500.

The packaged app's first run can reach these routes before the writable home's
config files exist. Every reader answers a structured 503 (the panel can say
"load failed" instead of rendering a server bug) and every writer does the same
after the service's seed attempt failed too.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from server.trigger.http import env, models_config, network_config, sherry_config

pytestmark = [pytest.mark.unit]


class _Request:
    def __init__(self, payload: dict | None = None, query: dict | None = None) -> None:
        self._payload = payload or {}
        self.query_params = query or {}

    def json(self) -> dict:
        return self._payload


def _run(coro):
    return asyncio.run(coro)


def _assert_503(response) -> None:
    """Robyn's route decorator turns the returned ``(body, status)`` into a Response."""
    assert response.status_code == 503
    body = json.loads(response.description)
    assert body["success"] is False
    assert "not found" in body["message"].lower() or "unavailable" in body["message"].lower()


def test_env_read_is_a_structured_503(monkeypatch):
    def _missing() -> dict:
        raise FileNotFoundError("Environment file not found: /home/.env")

    monkeypatch.setattr(env, "read_env_file", _missing)

    _assert_503(_run(env.read_env_handler(_Request())))


def test_env_write_is_a_structured_503(monkeypatch):
    def _missing(_changes: dict) -> None:
        raise FileNotFoundError("Environment file not found: /home/.env")

    monkeypatch.setattr(env, "write_env_file", _missing)

    _assert_503(_run(env.write_env_handler(_Request({"changes": {"LOG_LEVEL": "INFO"}}))))


def test_models_config_read_is_a_structured_503(monkeypatch):
    def _missing() -> dict:
        raise FileNotFoundError(".models not found")

    monkeypatch.setattr(models_config, "read_models_config", _missing)

    _assert_503(_run(models_config.read_models_config_handler(_Request())))


def test_models_config_write_is_a_structured_503(monkeypatch):
    def _missing(_changes: dict) -> None:
        raise FileNotFoundError(".models not found")

    monkeypatch.setattr(models_config, "write_models_config", _missing)

    _assert_503(_run(models_config.write_models_config_handler(_Request({"changes": {}}))))


def test_network_config_read_is_a_structured_503(monkeypatch):
    def _missing() -> dict:
        raise FileNotFoundError(".network not found")

    monkeypatch.setattr(network_config, "read_network_config", _missing)

    _assert_503(_run(network_config.read_network_config_handler(_Request())))


def test_network_config_write_is_a_structured_503(monkeypatch):
    def _missing(_changes: dict) -> None:
        raise FileNotFoundError(".network not found")

    monkeypatch.setattr(network_config, "write_network_config", _missing)

    _assert_503(_run(network_config.write_network_config_handler(_Request({"changes": {}}))))


def test_sherry_config_read_is_a_structured_503(monkeypatch):
    def _missing() -> dict:
        raise FileNotFoundError("sherry.jsonc not found")

    monkeypatch.setattr(sherry_config, "read_sherry_config", _missing)

    _assert_503(_run(sherry_config.read_sherry_config_handler(_Request())))


def test_sherry_config_write_is_a_structured_503(monkeypatch):
    def _missing(_changes: dict) -> None:
        raise FileNotFoundError("sherry.jsonc not found")

    monkeypatch.setattr(sherry_config, "write_sherry_config", _missing)

    _assert_503(_run(sherry_config.write_sherry_config_handler(_Request({"changes": {}}))))


def test_model_caps_read_returns_the_persisted_entries(monkeypatch):
    monkeypatch.setattr(
        sherry_config,
        "read_model_caps",
        lambda: [
            {
                "provider": "deepseek",
                "model": "deepseek-flash",
                "level": 1,
                "effective_level": 1,
            }
        ],
    )

    response = _run(sherry_config.read_model_caps_handler(_Request()))

    assert response.status_code == 200
    assert json.loads(response.description)["entries"][0]["model"] == "deepseek-flash"


def test_model_caps_clear_one_uses_the_query_pair(monkeypatch):
    calls: list[tuple[str, str]] = []
    monkeypatch.setattr(
        sherry_config,
        "clear_model_cap",
        lambda provider, model: calls.append((provider, model)) or True,
    )

    response = _run(
        sherry_config.clear_model_caps_handler(
            _Request(query={"provider": "deepseek", "model": "deepseek/flash"})
        )
    )

    assert response.status_code == 200
    assert json.loads(response.description) == {"success": True, "deleted": 1}
    assert calls == [("deepseek", "deepseek/flash")]


def test_model_caps_clear_requires_both_query_segments(monkeypatch):
    response = _run(
        sherry_config.clear_model_caps_handler(_Request(query={"provider": "deepseek"}))
    )

    assert response.status_code == 400
    assert json.loads(response.description)["success"] is False


def test_model_caps_clear_all_without_a_query(monkeypatch):
    monkeypatch.setattr(sherry_config, "clear_all_model_caps", lambda: 3)

    response = _run(sherry_config.clear_model_caps_handler(_Request()))

    assert response.status_code == 200
    assert json.loads(response.description) == {"success": True, "deleted": 3}


def test_model_caps_clear_one_by_path_segments(monkeypatch):
    monkeypatch.setattr(sherry_config, "clear_model_cap", lambda provider, model: False)

    response = _run(
        sherry_config.clear_model_cap_handler(_Request(), {"provider": "zhipu", "model": "glm-4.6"})
    )

    assert response.status_code == 200
    assert json.loads(response.description) == {"success": True, "deleted": 0}
