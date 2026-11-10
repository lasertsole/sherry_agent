"""Tool screenshots are compressed before the native image block is built."""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from agent.middlewares.media_pipeline.tool_media import _image_block
from config.features import MEDIA_PIPELINE

pytestmark = [pytest.mark.unit]


def _write_noise(path, size: int = 700) -> bytes:
    image = Image.effect_noise((size, size), 100).convert("RGB")
    out = io.BytesIO()
    image.save(out, format="PNG")
    path.write_bytes(out.getvalue())
    return out.getvalue()


def test_tool_image_is_compressed_before_embedding(tmp_path):
    raw = _write_noise(tmp_path / "shot.png")

    block = _image_block(tmp_path / "shot.png")

    assert block is not None
    data = base64.b64decode(block["image_url"]["url"].split(",", 1)[1])
    assert len(data) < len(raw)


def test_unfit_tool_image_is_omitted(monkeypatch, tmp_path):
    _write_noise(tmp_path / "shot.png")
    monkeypatch.setitem(MEDIA_PIPELINE, "image_compress_max_tokens", 1)

    assert _image_block(tmp_path / "shot.png") is None


def test_compression_can_be_switched_off(monkeypatch, tmp_path):
    raw = _write_noise(tmp_path / "shot.png")
    monkeypatch.setitem(MEDIA_PIPELINE, "image_compress_enabled", False)

    block = _image_block(tmp_path / "shot.png")

    assert block is not None
    assert base64.b64decode(block["image_url"]["url"].split(",", 1)[1]) == raw
