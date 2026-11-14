"""The image_to_text skill's remote download path is SSRF-guarded.

The script used to call ``requests.get`` on any string ``is_url`` accepted
(including internal addresses such as the cloud metadata endpoint). It now
downloads through ``safe_fetch``: public http(s) only, DNS-pinned, every
redirect hop re-checked, and a body cap.
"""

from __future__ import annotations

import base64
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from pub.func.validator.safe_fetch import FetchResult
from skills.builtin.core.image_to_text.scripts import core as script

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


class _FakeModel:
    def __init__(self) -> None:
        self.calls: list[list] = []

    def invoke(self, messages):
        self.calls.append(messages)
        return SimpleNamespace(content="a red square")


def _png(path: Path) -> None:
    Image.new("RGB", (4, 4), (255, 0, 0)).save(path, format="PNG")


def _png_bytes() -> bytes:
    from io import BytesIO

    buffer = BytesIO()
    Image.new("RGB", (4, 4), (255, 0, 0)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.fixture
def fake_model(monkeypatch):
    model = _FakeModel()
    monkeypatch.setattr(script, "ITTT_model", model)
    return model


def _patch_fetch(monkeypatch, result: FetchResult) -> list[dict]:
    calls: list[dict] = []

    def _fetch(url: str, **kwargs):
        calls.append({"url": url, **kwargs})
        return result

    monkeypatch.setattr(script, "safe_fetch", _fetch)
    return calls


def test_a_public_url_downloads_through_safe_fetch(monkeypatch, fake_model):
    calls = _patch_fetch(monkeypatch, FetchResult(ok=True, body=_png_bytes(), status=200))

    result = script.itt("https://example.com/cat.png")

    assert "[success]" in result and "a red square" in result
    assert calls == [
        {"url": "https://example.com/cat.png", "timeout": 60, "max_bytes": 50 * 1024 * 1024}
    ]
    # The model receives the downloaded bytes as a data URL.
    content = fake_model.calls[0][0].content
    image_url = next(part["image_url"]["url"] for part in content if part["type"] == "image_url")
    assert base64.b64decode(image_url.split(",", 1)[1]) == _png_bytes()


def test_the_temp_file_is_removed_after_a_url_success(monkeypatch, fake_model):
    _patch_fetch(monkeypatch, FetchResult(ok=True, body=_png_bytes(), status=200))
    created: list[Path] = []
    real = tempfile.NamedTemporaryFile

    def _tracking(*args, **kwargs):
        handle = real(*args, **kwargs)
        created.append(Path(handle.name))
        return handle

    monkeypatch.setattr(script.tempfile, "NamedTemporaryFile", _tracking)

    script.itt("https://example.com/cat.png")

    assert created and not created[0].exists()


def test_an_ssrf_refusal_is_reported_and_the_model_is_never_called(monkeypatch, fake_model):
    _patch_fetch(monkeypatch, FetchResult(ok=False, reason="ssrf"))

    result = script.itt("http://169.254.169.254/latest/meta-data")

    assert "[Error]" in result
    assert "private, loopback or link-local" in result
    assert fake_model.calls == []


def test_a_non_http_scheme_is_refused(monkeypatch, fake_model):
    _patch_fetch(monkeypatch, FetchResult(ok=False, reason="scheme"))

    result = script.itt("file://localhost/etc/hosts")

    assert "only public http(s) URLs" in result
    assert fake_model.calls == []


def test_a_non_2xx_response_is_an_error(monkeypatch, fake_model):
    _patch_fetch(monkeypatch, FetchResult(ok=True, body=b"not found", status=404))

    result = script.itt("https://example.com/missing.png")

    assert "HTTP 404" in result
    assert fake_model.calls == []


def test_an_oversize_body_is_refused(monkeypatch, fake_model):
    _patch_fetch(monkeypatch, FetchResult(ok=False, reason="oversize"))

    result = script.itt("https://example.com/huge.png")

    assert "download limit" in result
    assert fake_model.calls == []


def test_a_local_path_never_touches_the_network(monkeypatch, fake_model, tmp_path):
    calls = _patch_fetch(monkeypatch, FetchResult(ok=True, body=b"", status=200))
    image = tmp_path / "local.png"
    _png(image)

    result = script.itt(str(image))

    assert "[success]" in result
    assert calls == []
