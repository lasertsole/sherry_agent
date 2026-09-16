"""P1-a: history image stripping runs only when an image_url block exists.

``MultimodalProcessor.before_agent`` walks history on every turn. The strip is
now preceded by a cheap ``image_url`` presence check, and the content is only
reassigned when stripping actually produces text — a text-only block list (the
common shape left behind by an earlier turn) is no longer rewritten.
"""

import pytest
from langchain_core.messages import HumanMessage

from agent.middlewares import media_pipeline as mm_mod

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


@pytest.fixture()
def processor(tmp_path, monkeypatch):
    monkeypatch.setattr(mm_mod, "SRC_DIR", tmp_path / "src")
    return mm_mod.MultimodalProcessor()


def _state(messages: list, session_id: str = "p1a-session") -> dict:
    return {"session_id": session_id, "messages": messages}


def _image_content(text: str) -> list:
    return [
        {"type": "text", "text": text},
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}},
    ]


class TestHistoryImageStrip:
    def test_history_image_block_is_stripped(self, processor):
        old = HumanMessage(content=_image_content("旧的"))
        current = HumanMessage(content=[{"type": "text", "text": "新的"}])

        processor._before_agent_impl(_state([old, current]))

        assert old.content == "旧的"

    def test_second_run_leaves_stripped_message_untouched(self, processor):
        old = HumanMessage(content=_image_content("旧的"))
        processor._before_agent_impl(
            _state([old, HumanMessage(content=[{"type": "text", "text": "新的"}])])
        )
        stripped = old.content

        processor._before_agent_impl(
            _state([old, HumanMessage(content=[{"type": "text", "text": "第三轮"}])])
        )

        assert old.content is stripped

    def test_text_only_list_history_is_not_rewritten(self, processor):
        old = HumanMessage(content=[{"type": "text", "text": "只有文本"}])
        original_content = old.content

        processor._before_agent_impl(
            _state([old, HumanMessage(content=[{"type": "text", "text": "新的"}])])
        )

        assert old.content is original_content
        assert isinstance(old.content, list)

    def test_image_only_history_keeps_its_content(self, processor):
        # Pre-existing semantics: without any text block the strip yields "",
        # so the content is left as-is (image included).
        old = HumanMessage(
            content=[{"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}]
        )
        original_content = old.content

        processor._before_agent_impl(
            _state([old, HumanMessage(content=[{"type": "text", "text": "新的"}])])
        )

        assert old.content is original_content
