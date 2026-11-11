"""Route-level checks for the LSP configuration tab's API."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from robyn.testing import TestClient as _TestClient

import server.service.lsp_config_service as service
import server.trigger.http.lsp_config  # noqa: F401  (route registration)
from server.trigger.core import app

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

client = _TestClient(app)
_SESSION = "lsp-config-route"
_JSON = {"Content-Type": "application/json"}


def _send(method, path: str, payload: dict):
    """Robyn's TestClient body (``json_data`` is not read by ``request.json``)."""
    return method(path, headers=_JSON, body=json.dumps(payload))


@pytest.fixture()
def configured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setattr(service, "session_root", lambda _sid: tmp_path)
    monkeypatch.setattr(service, "resolve_lsp_server", lambda *_args: None)
    return tmp_path


def _payload() -> dict:
    return {
        "session_id": _SESSION,
        "servers": {
            "elixir": {
                "command": ["elixir-ls"],
                "extensions": [".ex"],
                "language_id": "elixir",
            }
        },
        "enable": ["elixir"],
    }


def test_get_config_requires_a_session(configured):
    response = client.get("/lsp/config")

    assert response.status_code == 400
    assert "session_id" in response.text


def test_put_then_get_then_delete(configured):
    response = _send(client.put, "/lsp/config/project", _payload())
    assert response.status_code == 200
    assert json.loads(response.text)["success"] is True

    project = client.get("/lsp/config/project", query_params={"session_id": _SESSION})
    assert project.status_code == 200
    body = json.loads(project.text)
    assert body["servers"]["elixir"]["command"] == ["elixir-ls"]

    full = client.get("/lsp/config", query_params={"session_id": _SESSION})
    by_language = {entry["language"]: entry for entry in json.loads(full.text)["servers"]}
    assert by_language["elixir"]["source"] == "project"

    deleted = _send(
        client.delete,
        "/lsp/config/project/language",
        {"session_id": _SESSION, "language": "elixir"},
    )
    assert deleted.status_code == 200
    assert json.loads(deleted.text)["deleted"] == "elixir"


def test_an_unsafe_command_is_a_400(configured):
    payload = _payload()
    payload["servers"]["elixir"]["command"] = ["/usr/bin/elixir-ls"]

    response = _send(client.put, "/lsp/config/project", payload)

    assert response.status_code == 400
    assert not (configured / ".sherry" / "lsp.json").exists()


def test_upload_accepts_file_text(configured):
    content = json.dumps({"servers": _payload()["servers"], "enable": ["elixir"]})

    response = _send(
        client.post,
        "/lsp/config/project",
        {"session_id": _SESSION, "content": content},
    )

    assert response.status_code == 200
    assert (configured / ".sherry" / "lsp.json").is_file()


def test_upload_refuses_invalid_json(configured):
    response = _send(
        client.post,
        "/lsp/config/project",
        {"session_id": _SESSION, "content": "{nope"},
    )

    assert response.status_code == 400
    assert "invalid JSON" in response.text


def test_test_endpoint_reports_a_missing_binary(configured):
    _send(client.put, "/lsp/config/project", _payload())

    response = _send(
        client.post,
        "/lsp/config/test",
        {"session_id": _SESSION, "language": "elixir"},
    )

    assert response.status_code == 200
    body = json.loads(response.text)
    assert body["ok"] is False
    assert body["installed"] is False
