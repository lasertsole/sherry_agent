"""Malformed JSON at the HTTP boundary: every body-reading handler answers 400.

``request.json()`` raises on a malformed body and the generic funnel turns that
into a 500, which the client reports as a server bug. Every handler now parses
through ``helpers.read_body``, which returns ``None`` for a malformed/non-object
body; these tests drive each migrated handler directly and pin the structured
400. One positive case pins the helper's second job: the Robyn test client hands
over a BYTES body, which ``request.json()`` rejects but ``read_body`` decodes.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from server.trigger.http import (
    channels,
    curator,
    env,
    heartbeat,
    memory,
    messages,
    models_config,
    network_config,
    sherry_config,
    workplace,
)
from server.trigger.http.skills import lifecycle as lifecycle_http
from server.trigger.http.skills import manage as manage_http

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _MalformedRequest:
    """A request whose JSON body cannot be parsed."""

    query_params: dict = {}
    body = "{not a json body"

    def json(self):
        raise ValueError("malformed JSON")


class _BytesRequest:
    """The test-client shape: ``json()`` rejects bytes, ``body`` holds them."""

    query_params: dict = {}

    def __init__(self, payload: dict) -> None:
        self.body = json.dumps(payload).encode("utf-8")

    def json(self):
        raise TypeError("bytes body is not text")


def _response(coro):
    return asyncio.run(coro)


def _assert_400(response) -> None:
    assert int(getattr(response, "status_code", 0)) == 400
    payload = json.loads(response.description)
    assert payload.get("success") is False or "error" in payload


def _request() -> _MalformedRequest:
    return _MalformedRequest()


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda: env.write_env_handler(_request()), id="env"),
        pytest.param(lambda: heartbeat.write_heartbeat_handler(_request()), id="heartbeat"),
        pytest.param(
            lambda: heartbeat.set_heartbeat_status_handler(_request()),
            id="heartbeat-status",
        ),
        pytest.param(lambda: memory.write_memory_handler(_request()), id="memory"),
        pytest.param(
            lambda: models_config.write_models_config_handler(_request()),
            id="models-config",
        ),
        pytest.param(
            lambda: network_config.write_network_config_handler(_request()),
            id="network-config",
        ),
        pytest.param(
            lambda: sherry_config.write_sherry_config_handler(_request()),
            id="sherry-config",
        ),
        pytest.param(
            lambda: sherry_config.write_sherry_list_handler(
                _request(), {"list_key": "yolo_deny_paths"}
            ),
            id="sherry-config-list",
        ),
        pytest.param(
            lambda: workplace.write_system_prompt_file_handler(_request()),
            id="system-prompt-put",
        ),
        pytest.param(
            lambda: workplace.update_system_prompt_file_handler(_request()),
            id="system-prompt-patch",
        ),
        pytest.param(
            lambda: workplace.write_preset_system_prompt_handler(_request()),
            id="system-prompt-preset",
        ),
        pytest.param(
            lambda: curator.put_curator_settings_handler(_request()),
            id="curator-settings",
        ),
        pytest.param(lambda: messages.clear_session_handler(_request()), id="clear-session"),
        pytest.param(
            lambda: manage_http.delete_auto_skill_handler(_request()),
            id="skill-delete",
        ),
        pytest.param(lambda: manage_http.pin_auto_skill_handler(_request()), id="skill-pin"),
        pytest.param(
            lambda: lifecycle_http.toggle_skill_handler(_request()),
            id="skill-toggle",
        ),
    ],
)
def test_a_malformed_json_body_answers_400(call):
    _assert_400(_response(call()))


def test_channel_updates_answer_400_for_a_malformed_body(monkeypatch):
    monkeypatch.setattr(channels, "discover_channel_names", lambda: ["demo"])

    _assert_400(_response(channels.update_channel_handler(_request(), {"channel_name": "demo"})))
    _assert_400(
        _response(channels.update_channel_config_handler(_request(), {"channel_name": "demo"}))
    )


def test_a_bytes_body_still_parses(monkeypatch):
    """The test client sends encoded bytes; read_body decodes them for the handler."""
    written: list[dict] = []
    monkeypatch.setattr(env, "write_env_file", lambda changes: written.append(changes))

    response = _response(env.write_env_handler(_BytesRequest({"changes": {"LOG_LEVEL": "INFO"}})))

    assert json.loads(response.description) == {
        "success": True,
        "message": "Environment config updated",
    }
    assert written == [{"LOG_LEVEL": "INFO"}]
