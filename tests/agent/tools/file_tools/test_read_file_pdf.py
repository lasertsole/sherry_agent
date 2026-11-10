"""read_file's PDF branch: native blocks, page images, and refusals."""

from __future__ import annotations

import io
import json

import pytest
from PIL import Image

from agent.tools.file_tools import read_file as read_file_mod
from agent.tools.file_tools.read_file import build_read_file_tool
from agent.tools.pub_base.pdf_render import PdfPage, PdfRenderUnavailable
from config.features import TOOLS_TIMEOUTS

pytestmark = [pytest.mark.unit]


@pytest.fixture
def pdf_root(tmp_path, monkeypatch):
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
    state_register_mem.clear_session("pdf-session")
    (root / "doc.pdf").write_bytes(b"%PDF-1.7\nfake\n")
    return root


def _set_model(monkeypatch, provider: str, model: str) -> None:
    monkeypatch.setenv("MAIN_LLM_PROVIDER", provider)
    monkeypatch.setenv("MAIN_LLM_NAME", model)


def _jpeg_page() -> bytes:
    image = Image.new("RGB", (32, 32), (200, 10, 10))
    out = io.BytesIO()
    image.save(out, format="JPEG")
    return out.getvalue()


def test_native_pdf_uses_the_openai_file_block(pdf_root, monkeypatch):
    _set_model(monkeypatch, "openai", "gpt-4o")

    result = build_read_file_tool()._core("doc.pdf", session_id="pdf-session")

    assert isinstance(result, list)
    assert result[1]["type"] == "file"
    assert result[1]["file"]["file_data"].startswith("data:application/pdf;base64,")


def test_native_pdf_uses_the_anthropic_document_block(pdf_root, monkeypatch):
    _set_model(monkeypatch, "anthropic", "claude-sonnet-4")

    result = build_read_file_tool()._core("doc.pdf", session_id="pdf-session")

    assert result[1]["type"] == "document"
    assert result[1]["source"]["media_type"] == "application/pdf"


def test_non_vision_model_gets_pdf_not_supported(pdf_root, monkeypatch):
    _set_model(monkeypatch, "deepseek", "deepseek-chat")

    result = build_read_file_tool()._core("doc.pdf", session_id="pdf-session")

    assert json.loads(result)["error"] == "pdf_not_supported"


def test_pdf_unsupported_but_vision_supported_renders_pages(pdf_root, monkeypatch):
    import agent.middlewares.llm_capability_cache as cache
    import agent.tools.pub_base.pdf_render as render

    monkeypatch.setattr(
        cache,
        "get_capability",
        lambda provider, model, media: "supported" if media == "vision" else "unsupported",
    )
    monkeypatch.setattr(
        render,
        "render_pdf_pages",
        lambda *args, **kwargs: [PdfPage(1, _jpeg_page(), 32, 32)],
    )

    result = build_read_file_tool()._core("doc.pdf", session_id="pdf-session")

    assert isinstance(result, list)
    assert result[1]["type"] == "image_url"
    assert result[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")


def test_missing_renderer_is_reported(pdf_root, monkeypatch):
    import agent.middlewares.llm_capability_cache as cache
    import agent.tools.pub_base.pdf_render as render

    monkeypatch.setattr(
        cache,
        "get_capability",
        lambda provider, model, media: "supported" if media == "vision" else "unsupported",
    )

    def _unavailable(*args, **kwargs):
        raise PdfRenderUnavailable("no renderer")

    monkeypatch.setattr(render, "render_pdf_pages", _unavailable)

    result = build_read_file_tool()._core("doc.pdf", session_id="pdf-session")

    assert json.loads(result)["error"] == "pdf_render_unavailable"


def test_oversized_pdf_is_refused(pdf_root, monkeypatch):
    monkeypatch.setitem(TOOLS_TIMEOUTS, "pdf_max_file_bytes", 4)

    result = build_read_file_tool()._core("doc.pdf", session_id="pdf-session")

    assert json.loads(result)["error"] == "pdf_too_large"
