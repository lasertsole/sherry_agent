"""read_file's native image branch (ZCode-style vision read)."""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from agent.tools.file_tools import read_file as read_file_mod
from agent.tools.file_tools.read_file import build_read_file_tool
from config.features import TOOLS_TIMEOUTS

pytestmark = [pytest.mark.unit]


@pytest.fixture
def image_root(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    monkeypatch.setenv("SHERRY_PROJECT_DIR", str(root))
    monkeypatch.setattr(read_file_mod, "TEMP_DIR", root / "temp")
    from runtime import state_register_mem
    import runtime

    class _FakeDB:
        def get_state(self, session_id, key, default=None):
            return default

        def set_state(self, session_id, key, value):
            return True

        def delete_state(self, session_id, key):
            return True

    monkeypatch.setattr(state_register_mem, "_states", {})
    monkeypatch.setattr(runtime, "state_register_db", _FakeDB())
    state_register_mem.clear_session("img-session")
    return root


def _png(path, size: int = 64, noise: bool = False) -> None:
    if noise:
        image = Image.effect_noise((size, size), 100).convert("RGB")
    else:
        image = Image.new("RGB", (size, size), (10, 120, 220))
    out = io.BytesIO()
    image.save(out, format="PNG")
    path.write_bytes(out.getvalue())


def test_image_read_returns_a_native_image_block(image_root, monkeypatch):
    monkeypatch.setenv("MAIN_LLM_PROVIDER", "openai")
    monkeypatch.setenv("MAIN_LLM_NAME", "gpt-4o")
    _png(image_root / "photo.png")

    result = build_read_file_tool()._core("photo.png", session_id="img-session")

    assert isinstance(result, list)
    assert result[0]["type"] == "text"
    assert "Image read" in result[0]["text"]
    assert result[1]["type"] == "image_url"
    assert result[1]["image_url"]["url"].startswith("data:image/png;base64,")


def test_non_vision_model_gets_the_skill_fallback(image_root, monkeypatch):
    monkeypatch.setenv("MAIN_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("MAIN_LLM_NAME", "deepseek-chat")
    _png(image_root / "photo.png")

    result = build_read_file_tool()._core("photo.png", session_id="img-session")

    assert isinstance(result, str)
    assert "image_to_text" in result


def test_oversized_image_is_refused(image_root, monkeypatch):
    _png(image_root / "photo.png", noise=True)
    monkeypatch.setitem(TOOLS_TIMEOUTS, "read_image_max_bytes", 10)

    result = build_read_file_tool()._core("photo.png", session_id="img-session")

    payload = json.loads(result)
    assert payload["error"] == "read_image_input_too_large"


def test_large_image_is_compressed_before_the_block(image_root, monkeypatch):
    monkeypatch.setenv("MAIN_LLM_PROVIDER", "openai")
    monkeypatch.setenv("MAIN_LLM_NAME", "gpt-4o")
    _png(image_root / "photo.png", size=700, noise=True)

    result = build_read_file_tool()._core("photo.png", session_id="img-session")

    assert isinstance(result, list)
    url = result[1]["image_url"]["url"]
    assert len(url) < 700 * 700  # the original noisy PNG is far larger


def test_repeated_reads_reuse_the_content_addressed_copy(image_root, monkeypatch):
    monkeypatch.setenv("MAIN_LLM_PROVIDER", "openai")
    monkeypatch.setenv("MAIN_LLM_NAME", "gpt-4o")
    _png(image_root / "photo.png")

    build_read_file_tool()._core("photo.png", session_id="img-session")
    build_read_file_tool()._core("photo.png", session_id="img-session")

    assert len(list((image_root / "temp" / "read_images").glob("*.png"))) == 1
