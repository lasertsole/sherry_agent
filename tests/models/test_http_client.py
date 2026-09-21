"""Tests for the shared OpenAI-compatible HTTP client.

The adapter must be behavior-identical to the inline ``requests.post(...)``
calls it replaced: Bearer auth, JSON body, ``verify=False``, caller-supplied
timeout (``None`` = no timeout), ``raise_for_status`` on non-2xx, and NO retry.
"""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import requests

from models.http_client import OpenAICompatibleClient

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _RecordingHandler(BaseHTTPRequestHandler):
    """Records one request (path, body, auth) and replies with a canned status."""

    status_code = 200
    response_body: bytes = b'{"ok": true}'
    recorded: list[dict] = []

    def do_POST(self):  # noqa: N802 (BaseHTTPRequestHandler API)
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        type(self).recorded.append(
            {
                "path": self.path,
                "body": body,
                "content_type": self.headers.get("Content-Type"),
                "authorization": self.headers.get("Authorization"),
            }
        )
        self.send_response(self.status_code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(self.response_body)))
        self.end_headers()
        self.wfile.write(self.response_body)

    def log_message(self, *args):  # silence the test log
        pass


@pytest.fixture()
def stub_server():
    handler = type("_Handler", (_RecordingHandler,), {"recorded": [], "status_code": 200})
    server = HTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server, handler
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _base_url(server: HTTPServer) -> str:
    host, port = server.server_address[:2]
    return f"http://{host}:{port}"


class TestRequestShape:
    def test_posts_bearer_json_and_returns_decoded_body(self, stub_server):
        server, handler = stub_server
        handler.response_body = b'{"data": []}'
        client = OpenAICompatibleClient(_base_url(server), "sk-secret")

        result = client.post_json("/embeddings", {"model": "m", "input": ["a"]}, timeout=None)

        assert result == {"data": []}
        assert len(handler.recorded) == 1
        sent = handler.recorded[0]
        assert sent["path"] == "/embeddings"
        assert sent["authorization"] == "Bearer sk-secret"
        assert sent["content_type"] == "application/json"
        # Wire-identical to the pre-existing `data=json.dumps(payload)` call.
        assert sent["body"] == json.dumps({"model": "m", "input": ["a"]}).encode()

    def test_base_trailing_slash_is_trimmed(self, stub_server):
        server, handler = stub_server
        client = OpenAICompatibleClient(_base_url(server) + "/", "k")

        client.post_json("/rerank", {})

        assert handler.recorded[0]["path"] == "/rerank"

    def test_timeout_is_passed_through_unchanged(self, stub_server, monkeypatch):
        server, _handler = stub_server
        captured: dict = {}

        def fake_post(url, **kwargs):
            captured.update(kwargs)
            return _FakeResponse()

        monkeypatch.setattr("models.http_client.requests.post", fake_post)
        client = OpenAICompatibleClient(_base_url(server), "k")

        client.post_json("/rerank", {"a": 1}, timeout=30)
        assert captured["timeout"] == 30
        assert captured["verify"] is False
        assert captured["json"] == {"a": 1}

        client.post_json("/rerank", {"a": 1})
        assert captured["timeout"] is None


class _FakeResponse:
    def raise_for_status(self):
        return None

    def json(self):
        return {"ok": True}


class TestErrorPath:
    def test_non_2xx_raises_and_does_not_retry(self, stub_server):
        server, handler = stub_server
        handler.status_code = 500
        handler.response_body = b"boom"
        client = OpenAICompatibleClient(_base_url(server), "k")

        with pytest.raises(requests.HTTPError):
            client.post_json("/rerank", {"a": 1}, timeout=10)

        assert len(handler.recorded) == 1  # no retry was added


class TestWiring:
    def test_cloud_reranker_uses_shared_client(self):
        from models.reranker_model.core import CloudReranker

        cloud = CloudReranker(api_base="https://reranker.invalid/v1", api_key="sk-x")

        assert isinstance(cloud._http, OpenAICompatibleClient)

    def test_embed_model_uses_shared_client(self):
        # embed_model/core.py downloads the GGUF at import when the local file
        # is missing, so it is pinned at source level (same approach as
        # tests/models/test_models_utils.py).
        from pathlib import Path

        src = (
            next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
            / "models"
            / "embed_model"
            / "core.py"
        ).read_text(encoding="utf-8")
        assert "OpenAICompatibleClient(" in src
        assert "requests.post(" not in src
