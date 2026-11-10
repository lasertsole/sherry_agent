"""Outgoing user images are compressed before the data URI is built."""

from __future__ import annotations

import base64
import io

import pytest
from PIL import Image

from pub.types.message import MultiModalMessage
from server.service.messages import _get_content_list

pytestmark = [pytest.mark.unit]


def _noisy_png(width: int = 700, height: int = 700) -> bytes:
    image = Image.effect_noise((width, height), 100).convert("RGB")
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _image_url(content: list) -> str:
    return next(item for item in content if isinstance(item, dict) and item["type"] == "image_url")[
        "image_url"
    ]["url"]


def test_base64_list_is_compressed_before_the_data_uri():
    raw = _noisy_png()
    original = f"data:image/png;base64,{base64.b64encode(raw).decode()}"

    content = _get_content_list(MultiModalMessage(text="x", image_base64_list=[original]))
    url = _image_url(content)

    assert url.startswith("data:image/")
    assert len(url) < len(original)


def test_bytes_list_is_compressed_before_the_data_uri():
    raw = _noisy_png()

    content = _get_content_list(MultiModalMessage(text="x", image_bytes_list=[raw]))
    url = _image_url(content)
    payload = base64.b64decode(url.split(",", 1)[1])

    assert len(payload) < len(raw)


def test_small_image_stays_byte_identical():
    image = Image.new("RGB", (32, 32), (1, 2, 3))
    out = io.BytesIO()
    image.save(out, format="PNG")
    raw = out.getvalue()

    content = _get_content_list(MultiModalMessage(text="x", image_bytes_list=[raw]))
    url = _image_url(content)

    assert base64.b64decode(url.split(",", 1)[1]) == raw
