"""PDF as a first-class media family (2026-11-10 image plan #0)."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage

from agent.middlewares.media_pipeline.media_handlers import (
    MEDIA_TYPE_BY_ITEM,
    MediaPaths,
    _MEDIA_HANDLERS,
)
from agent.middlewares.media_pipeline.scrub import scrub_messages

pytestmark = [pytest.mark.unit]


def test_pdf_item_types_belong_to_the_pdf_family():
    assert MEDIA_TYPE_BY_ITEM["document"] == "pdf"
    assert MEDIA_TYPE_BY_ITEM["file"] == "pdf"


def test_pdf_handler_records_presence_without_decoding():
    paths = MediaPaths()

    _MEDIA_HANDLERS["document"].process({}, "sid", paths, Path("."))

    assert paths.pdf_present is True
    assert paths.persisted_images == []


def test_unsupported_pdf_becomes_a_text_placeholder():
    message = HumanMessage(
        content=[
            {"type": "text", "text": "read this"},
            {"type": "document", "source": {"type": "base64", "data": "AAAA"}},
        ]
    )

    scrubbed, changed = scrub_messages(
        [message], "deepseek", "deepseek-chat", "deepseek/deepseek-chat"
    )

    assert changed is True
    content = scrubbed[0].content
    assert isinstance(content, list)
    assert "PDF" in content[1]["text"]
    assert "Provide the document as images or text instead." in content[1]["text"]
