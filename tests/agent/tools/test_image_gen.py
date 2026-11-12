"""``image_generation`` tool: artifact persistence, markdown, registration gate."""

from __future__ import annotations

import asyncio

import pytest

from agent.tools import image_gen
from agent.tools.image_gen import ImageGenerationTool, build_image_gen_tool
from agent.tools.pub_base import artifact_store
from models.TTI_model import GeneratedImage, TTIError

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]

_PNG = b"\x89PNG\r\n\x1a\n" + b"tool-png"


class _FakeTTI:
    def __init__(self, images=None, error: Exception | None = None):
        self.images = images if images is not None else [GeneratedImage(_PNG, ".png")]
        self.error = error
        self.calls: list[dict] = []

    def generate(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, **kwargs})
        if self.error is not None:
            raise self.error
        return list(self.images)


@pytest.fixture
def artifacts(tmp_path):
    def _write(session_id, data, *, media_type="binary", ext=".bin", root=None):
        return artifact_store.write_artifact(
            session_id, data, media_type=media_type, ext=ext, root=tmp_path
        )

    return _write


def test_run_persists_each_image_and_returns_markdown(monkeypatch, artifacts):
    fake = _FakeTTI([GeneratedImage(_PNG, ".png"), GeneratedImage(_PNG + b"2", ".png")])
    monkeypatch.setattr(image_gen, "TTI_model", fake)
    monkeypatch.setattr(image_gen, "write_artifact", artifacts)

    result = ImageGenerationTool()._run("a red square", n=2, session_id="s1")

    assert "Generated 2 image(s)" in result
    assert result.count("![generated image") == 2
    assert "sherry-artifact://s1/image-" in result
    assert fake.calls[0]["n"] == 2


def test_identical_images_deduplicate_to_one_artifact(monkeypatch, artifacts, tmp_path):
    fake = _FakeTTI([GeneratedImage(_PNG, ".png")])
    monkeypatch.setattr(image_gen, "TTI_model", fake)
    monkeypatch.setattr(image_gen, "write_artifact", artifacts)

    first = ImageGenerationTool()._run("a cat", session_id="s1")
    second = ImageGenerationTool()._run("a cat", session_id="s1")

    uri = first.split("](")[-1].rstrip(")")
    assert uri in second
    stored = list((tmp_path / "s1" / artifact_store.ARTIFACT_DIR_NAME).glob("*"))
    assert len(stored) == 1


def test_missing_session_id_refuses_before_the_provider(monkeypatch):
    fake = _FakeTTI()
    monkeypatch.setattr(image_gen, "TTI_model", fake)

    result = ImageGenerationTool()._run("a cat", session_id="")

    assert result.startswith("Error:")
    assert "session_id" in result
    assert fake.calls == []


def test_provider_error_is_reported_as_text(monkeypatch):
    monkeypatch.setattr(image_gen, "TTI_model", _FakeTTI(error=TTIError("bad key")))

    result = ImageGenerationTool()._run("a cat", session_id="s1")

    assert result == "Error: image generation failed: bad key"


def test_artifact_failure_reports_an_error(monkeypatch):
    monkeypatch.setattr(image_gen, "TTI_model", _FakeTTI())
    monkeypatch.setattr(image_gen, "write_artifact", lambda *a, **k: None)

    result = ImageGenerationTool()._run("a cat", session_id="s1")

    assert result.startswith("Error:")
    assert "could not be persisted" in result


def test_partial_artifact_failure_keeps_the_successes(monkeypatch, artifacts):
    calls = {"n": 0}

    def flaky(session_id, data, *, media_type="binary", ext=".bin", root=None):
        calls["n"] += 1
        if calls["n"] == 2:
            return None
        return artifacts(session_id, data, media_type=media_type, ext=ext)

    fake = _FakeTTI([GeneratedImage(_PNG, ".png"), GeneratedImage(_PNG + b"b", ".png")])
    monkeypatch.setattr(image_gen, "TTI_model", fake)
    monkeypatch.setattr(image_gen, "write_artifact", flaky)

    result = ImageGenerationTool()._run("a cat", n=2, session_id="s1")

    assert result.count("![generated image") == 1
    assert "could not be persisted" in result


def test_arun_offloads_the_provider_call(monkeypatch, artifacts):
    fake = _FakeTTI()
    monkeypatch.setattr(image_gen, "TTI_model", fake)
    monkeypatch.setattr(image_gen, "write_artifact", artifacts)

    result = asyncio.run(ImageGenerationTool()._arun(prompt="a cat", session_id="s1"))

    assert "sherry-artifact://s1/image-" in result


def test_builder_is_gated_on_tti_configuration(monkeypatch):
    monkeypatch.setattr(image_gen, "tti_configured", lambda: False)
    assert build_image_gen_tool() == []

    monkeypatch.setattr(image_gen, "tti_configured", lambda: True)
    tool = build_image_gen_tool()
    assert isinstance(tool, ImageGenerationTool)
    assert tool.name == "image_generation"
    assert tool.metadata["scope"] == "main_only"


def test_input_schema_hides_the_injected_session_id():
    schema = ImageGenerationTool().tool_call_schema.model_json_schema()

    properties = schema["properties"]
    assert "prompt" in properties
    assert "session_id" not in properties
