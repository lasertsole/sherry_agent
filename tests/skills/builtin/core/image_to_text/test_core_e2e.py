"""End-to-end download path: the REAL safe_fetch + a local HTTP server.

The unit tests mock ``safe_fetch``; these prove the wiring against the real
guard: a loopback URL is refused (SSRF), and the documented development escape
hatch (``SHERRY_ALLOW_PRIVATE_MEDIA_URLS=1``) lets the same URL through, with
the real HTTP fetch, PIL validation and data-URL handoff to the (faked) vision
model.
"""

from __future__ import annotations

import contextlib
import functools
import http.server
import threading
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from skills.builtin.core.image_to_text.scripts import core as script

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args, **kwargs):  # noqa: D102 - silence the test server
        pass


@contextlib.contextmanager
def _serving(directory: Path, filename: str) -> Iterator[str]:
    handler = functools.partial(_QuietHandler, directory=str(directory))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[:2]
        yield f"http://{host}:{port}/{filename}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


class _FakeModel:
    def __init__(self) -> None:
        self.calls: list = []

    def invoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content="a red square")


@pytest.fixture
def image_server(tmp_path, monkeypatch):
    Image.new("RGB", (4, 4), (255, 0, 0)).save(tmp_path / "cat.png", format="PNG")
    monkeypatch.setattr(script, "ITTT_model", _FakeModel())
    with _serving(tmp_path, "cat.png") as url:
        yield url


def test_the_real_guard_refuses_loopback(image_server, monkeypatch):
    monkeypatch.delenv("SHERRY_ALLOW_PRIVATE_MEDIA_URLS", raising=False)

    result = script.itt(image_server)

    assert "[Error]" in result
    assert "private, loopback or link-local" in result
    assert script.ITTT_model.calls == []


def test_the_documented_escape_hatch_allows_the_local_server(image_server, monkeypatch):
    monkeypatch.setenv("SHERRY_ALLOW_PRIVATE_MEDIA_URLS", "1")

    result = script.itt(image_server)

    assert "[success]" in result
    assert "a red square" in result
    assert len(script.ITTT_model.calls) == 1
