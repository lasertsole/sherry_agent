"""Image compression cascade with the per-image token gate."""

from __future__ import annotations

import base64
import io
import math

import pytest
from PIL import Image

from pub.func.media.image_compress import (
    ImageTooLargeError,
    prepare_for_model,
)

pytestmark = [pytest.mark.unit]


def _png(width: int, height: int, *, noise: bool = False) -> bytes:
    if noise:
        image = Image.effect_noise((width, height), 100).convert("RGB")
    else:
        image = Image.new("RGB", (width, height), (200, 120, 40))
    out = io.BytesIO()
    image.save(out, format="PNG")
    return out.getvalue()


def _jpeg(width: int, height: int) -> bytes:
    image = Image.effect_noise((width, height), 80).convert("RGB")
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=95)
    return out.getvalue()


def _fits(raw: bytes, max_tokens: int = 25000) -> bool:
    base64_len = math.ceil(len(raw) / 3) * 4
    return math.ceil(base64_len * 0.125) <= max_tokens


def test_small_image_passes_through_unchanged():
    raw = _png(64, 64)

    prepared = prepare_for_model(raw)

    assert prepared.strategy == "original"
    assert prepared.data == raw
    assert prepared.media_type == "image/png"


def test_large_noisy_png_is_compressed_and_fits_the_gate():
    raw = _png(1400, 1400, noise=True)
    assert not _fits(raw), "fixture must start over budget"

    prepared = prepare_for_model(raw)

    assert prepared.strategy != "original"
    assert prepared.data != raw
    assert _fits(prepared.data), "the returned candidate must pass the token gate"


def test_dimension_cap_is_enforced():
    raw = _jpeg(3000, 1200)

    prepared = prepare_for_model(raw, max_dimension=100)

    assert max(prepared.width, prepared.height) <= 100


def test_dimension_cap_wins_even_when_the_bytes_already_fit():
    # A solid 2500px PNG is small enough for the byte/token gate, so only the
    # dimension check forces a resize.
    raw = _png(2500, 2500)
    assert _fits(raw)

    prepared = prepare_for_model(raw, max_dimension=1000)

    assert max(prepared.width, prepared.height) <= 1000


def test_webp_passes_through_when_it_fits():
    image = Image.new("RGB", (80, 80), (10, 120, 220))
    out = io.BytesIO()
    image.save(out, format="WEBP", quality=90)
    raw = out.getvalue()

    prepared = prepare_for_model(raw)

    assert prepared.strategy == "webp-passthrough"
    assert prepared.media_type == "image/webp"
    assert prepared.data == raw


def test_webp_too_large_falls_back_to_a_fitting_candidate():
    image = Image.effect_noise((1200, 1200), 100).convert("RGB")
    out = io.BytesIO()
    image.save(out, format="WEBP", lossless=True)
    raw = out.getvalue()

    prepared = prepare_for_model(raw)

    assert prepared.strategy != "webp-passthrough"
    assert _fits(prepared.data)


def test_impossible_budget_raises():
    raw = _png(1200, 1200, noise=True)

    with pytest.raises(ImageTooLargeError):
        prepare_for_model(raw, max_tokens=1)


def test_non_image_bytes_are_rejected_by_pil():
    with pytest.raises(Exception):
        prepare_for_model(b"not an image at all")


def test_base64_length_gate_matches_zcode_formula():
    raw = _png(1400, 1400, noise=True)

    prepared = prepare_for_model(raw)
    base64_len = len(base64.b64encode(prepared.data))

    assert base64_len == math.ceil(len(prepared.data) / 3) * 4
    assert math.ceil(base64_len * 0.125) <= 25000
