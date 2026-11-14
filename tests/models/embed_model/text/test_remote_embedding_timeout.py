"""Remote embedding calls must carry a timeout (audit M23).

``post_json`` defaults to ``timeout=None``, i.e. ``requests`` blocks forever.
A hung MaaS endpoint therefore pinned a worker thread indefinitely; the call
now uses the same auxiliary timeout ceiling as the other remote requests.
"""

from __future__ import annotations

import pytest

from config.features import LLM_CLIENT_DEFAULTS
from models.embed_model.text import core

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


class _Client:
    def __init__(self, api_base: str, api_key: str) -> None:  # noqa: ARG002 - fake surface
        self.calls: list[dict] = []

    def post_json(self, path: str, payload: dict, *, timeout: float | None = None) -> dict:
        self.calls.append({"path": path, "payload": payload, "timeout": timeout})
        return {"data": [{"index": 0, "embedding": [0.25, 0.75]}]}


def test_remote_embed_passes_the_auxiliary_timeout(monkeypatch):
    made: list[_Client] = []

    def _factory(api_base: str, api_key: str) -> _Client:
        client = _Client(api_base, api_key)
        made.append(client)
        return client

    monkeypatch.setattr(core, "OpenAICompatibleClient", _factory)
    backend = core._RemoteEmbeddingBackend(
        {"api_base": "https://embed.invalid/v1", "api_key": "k", "api_name": "m"}
    )

    embeddings = backend.embed(["hello"])

    assert embeddings == [[0.25, 0.75]]
    assert made[0].calls[0]["timeout"] == float(LLM_CLIENT_DEFAULTS["aux_timeout"])
    assert made[0].calls[0]["path"] == "/embeddings"
