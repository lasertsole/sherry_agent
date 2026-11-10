"""GET /artifacts/<session_id>/<name>: validation, byte serving, session scope."""

from __future__ import annotations

import asyncio
import json

import pytest

import server.trigger.http.artifacts as artifacts_http
from agent.tools.pub_base import artifact_store

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]

_PNG = b"\x89PNG\r\n\x1a\n" + b"served-artifact"


def _payload(response) -> dict:
    return json.loads(response.description)


def _call(session_id: str, name: str):
    return asyncio.run(artifacts_http.get_artifact(None, {"session_id": session_id, "name": name}))


def _name_of(uri: str) -> str:
    return uri.rsplit("/", 1)[-1]


@pytest.fixture()
def root(tmp_path, monkeypatch):
    monkeypatch.setattr(artifacts_http, "SRC_DIR", tmp_path)
    return tmp_path


def test_serves_the_stored_bytes_with_their_content_type(root):
    uri = artifact_store.write_artifact("s1", _PNG, media_type="image", ext=".png", root=root)
    assert uri is not None

    response = _call("s1", _name_of(uri))

    assert int(response.status_code) == 200
    assert response.description == _PNG
    assert response.headers.get("Content-Type") == "image/png"


def test_an_invalid_session_answers_400(root):
    response = _call("../escape", "image-" + "a" * 16 + ".png")

    assert int(response.status_code) == 400
    assert _payload(response)["message"] == "invalid session_id"


def test_an_invalid_name_answers_400(root):
    response = _call("s1", "../escape.png")

    assert int(response.status_code) == 400
    assert _payload(response)["message"] == "invalid artifact name"


def test_a_missing_artifact_answers_404(root):
    response = _call("s1", "image-" + "b" * 16 + ".png")

    assert int(response.status_code) == 404
    assert _payload(response)["message"] == "artifact not found"


def test_another_sessions_artifact_name_is_not_found(root):
    uri = artifact_store.write_artifact("s1", _PNG, media_type="image", ext=".png", root=root)
    assert uri is not None

    response = _call("s2", _name_of(uri))

    assert int(response.status_code) == 404
