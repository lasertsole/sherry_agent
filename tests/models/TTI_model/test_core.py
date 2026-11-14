"""TTI Images API wrapper: request shape, model guards, response decoding."""

from __future__ import annotations

import base64
from types import SimpleNamespace

import pytest

from config.features import IMAGE_GEN
from models.TTI_model import core
from models.TTI_model.core import (
    IMAGE_GEN_MAX_OUTPUT,
    TTIError,
    TTIConfig,
    TTIModel,
    max_images_for,
    should_force_response_format,
    tti_configured,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]

_PNG = b"\x89PNG\r\n\x1a\n" + b"png-body"
_JPEG = b"\xff\xd8\xff" + b"jpeg-body"
_B64_PNG = base64.b64encode(_PNG).decode()


class _FakeImages:
    def __init__(self, response):
        self._response = response
        self.calls: list[dict] = []

    def generate(self, **kwargs):
        self.calls.append(kwargs)
        return self._response


def _client(response):
    images = _FakeImages(response)
    return SimpleNamespace(images=images), images


def _model(response, *, model_name="dall-e-3", api_key="sk-test"):
    client, images = _client(response)
    config = TTIConfig(provider="openai", model=model_name, api_base="", api_key=api_key)
    return TTIModel(client=client, config=config), images


def _b64_response(*payloads: bytes):
    return SimpleNamespace(
        data=[SimpleNamespace(b64_json=base64.b64encode(body).decode()) for body in payloads]
    )


def test_dall_e_request_forces_b64_json_and_returns_bytes():
    model, images = _model(_b64_response(_PNG))

    generated = model.generate("a red square", size="512x512", n=3)

    sent = images.calls[0]
    assert sent["model"] == "dall-e-3"
    assert sent["prompt"] == "a red square"
    assert sent["n"] == 1  # dall-e-3 accepts one image
    assert sent["size"] == "512x512"
    assert sent["response_format"] == "b64_json"
    assert generated[0].data == _PNG
    assert generated[0].ext == ".png"


def test_gpt_image_omits_response_format_and_sends_newer_options():
    model, images = _model(_b64_response(_PNG), model_name="gpt-image-1")

    model.generate(
        "a cat",
        n=10,
        background="transparent",
        output_format="png",
        output_compression=80,
    )

    sent = images.calls[0]
    assert "response_format" not in sent
    assert sent["n"] == IMAGE_GEN["max_n"]
    assert sent["background"] == "transparent"
    assert sent["output_format"] == "png"
    assert sent["output_compression"] == 80


def test_legacy_quality_is_dropped_for_a_newer_model():
    model, images = _model(_b64_response(_PNG), model_name="gpt-image-1")

    model.generate("a cat", quality="standard")

    assert "quality" not in images.calls[0]


def test_newer_quality_passes_through():
    model, images = _model(_b64_response(_PNG), model_name="gpt-image-1")

    model.generate("a cat", quality="high")

    assert images.calls[0]["quality"] == "high"


def test_style_is_only_sent_to_older_models():
    older, older_calls = _model(_b64_response(_PNG), model_name="dall-e-3")
    newer, newer_calls = _model(_b64_response(_PNG), model_name="gpt-image-1")

    older.generate("a cat", style="vivid")
    newer.generate("a cat", style="vivid")

    assert older_calls.calls[0]["style"] == "vivid"
    assert "style" not in newer_calls.calls[0]


def test_url_response_is_downloaded(monkeypatch):
    class _Response:
        def raise_for_status(self):
            return None

        def iter_content(self, chunk_size):
            yield _JPEG

        def close(self):
            return None

    seen: list[str] = []

    def fake_get(url, timeout=None, stream=None):
        seen.append(url)
        return _Response()

    monkeypatch.setattr(core.requests, "get", fake_get)
    model, _ = _model(SimpleNamespace(data=[SimpleNamespace(url="https://cdn/x.jpg")]))

    generated = model.generate("a cat")

    assert seen == ["https://cdn/x.jpg"]
    assert generated[0].data == _JPEG
    assert generated[0].ext == ".jpg"


def test_output_format_decides_the_extension():
    model, _ = _model(_b64_response(_PNG), model_name="gpt-image-1")

    generated = model.generate("a cat", output_format="webp")

    assert generated[0].ext == ".webp"


def test_max_images_for_table_and_prefix():
    assert max_images_for("dall-e-3") == 1
    assert max_images_for("dall-e-2") == 10
    assert max_images_for("gpt-image-2") == 10
    assert max_images_for("gpt-image-9") == 10
    assert max_images_for("chatgpt-image-latest") == 10
    assert max_images_for("some-other-model") == 1
    assert set(IMAGE_GEN_MAX_OUTPUT) >= {"dall-e-3", "dall-e-2", "gpt-image-1"}


def test_format_guard_classifies_old_and_new_models():
    assert should_force_response_format("dall-e-3")
    assert should_force_response_format("dall-e-2")
    assert not should_force_response_format("gpt-image-1")
    assert not should_force_response_format("gpt-image-2")
    assert not should_force_response_format("chatgpt-image-latest")


def test_empty_prompt_raises():
    model, _ = _model(_b64_response(_PNG))

    with pytest.raises(TTIError, match="prompt is required"):
        model.generate("   ")


def test_missing_model_raises():
    model, _ = _model(_b64_response(_PNG), model_name="")

    with pytest.raises(TTIError, match="no image model"):
        model.generate("a cat")


def test_missing_key_raises_before_the_provider_call():
    config = TTIConfig(provider="openai", model="dall-e-3", api_base="", api_key="")
    model = TTIModel(config=config)

    with pytest.raises(TTIError, match="no image API key"):
        model.generate("a cat")


def test_provider_error_becomes_tti_error():
    class _Failing:
        def generate(self, **kwargs):
            raise RuntimeError("upstream exploded")

    config = TTIConfig(provider="openai", model="dall-e-3", api_base="", api_key="sk-test")
    model = TTIModel(client=SimpleNamespace(images=_Failing()), config=config)

    with pytest.raises(TTIError, match="upstream exploded"):
        model.generate("a cat")


def test_empty_provider_response_raises():
    model, _ = _model(SimpleNamespace(data=[]))

    with pytest.raises(TTIError, match="no images"):
        model.generate("a cat")


def test_item_without_payload_raises():
    model, _ = _model(SimpleNamespace(data=[SimpleNamespace()]))

    with pytest.raises(TTIError, match="neither b64_json nor url"):
        model.generate("a cat")


def test_tti_configured_needs_model_and_key(monkeypatch):
    monkeypatch.setenv("TTI_API_NAME", "gpt-image-1")
    monkeypatch.setenv("TTI_API_KEY", "sk-test")
    assert tti_configured()

    monkeypatch.setenv("TTI_API_KEY", "")
    assert not tti_configured()
