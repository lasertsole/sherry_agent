"""Unit tests for the P0-2/P1-9/P2-4 eviction primitives (``pub/func/message/eviction.py``).

Covered: threshold boundary, ``excluded_tools`` opt-out, preview template with
head/tail line limits, byte-identical disk round-trip (``load_evicted``), id /
field preservation, multimodal text-only replacement, eviction idempotency,
unsafe session segments, the single-giant-line guard, the P2-4 read_file
slice (boundary + idempotency), and the P1-9 human-message path (tag-only
state update, content/id preservation, human preview).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from langchain_core.messages import HumanMessage, ToolMessage

from config.features import TOOL_RESULT_EVICTION
from pub.func.message import eviction as eviction_module
from pub.func.message.eviction import (
    _READ_FILE_SLICE_NOTICE,
    EVICTED_TO_KEY,
    build_human_preview,
    build_preview,
    evict_human_message,
    evict_tool_result,
    get_eviction_dir,
    human_eviction_notice,
    load_evicted,
    slice_read_file_result,
)

pytestmark = [pytest.mark.unit]

SID = "eviction-session"
THRESHOLD = TOOL_RESULT_EVICTION["evict_threshold_chars"]
HUMAN_THRESHOLD = TOOL_RESULT_EVICTION["human_evict_threshold_chars"]


@pytest.fixture
def sessions_dir(tmp_path, monkeypatch):
    root = tmp_path / "sessions"
    monkeypatch.setattr(eviction_module, "SESSIONS_DIR", root)
    return root


def _lines(total_chars: int) -> str:
    """Multiline content of exactly ``total_chars`` characters (99-char lines)."""
    return ("x" * 99 + "\n") * (total_chars // 100) + "x" * (total_chars % 100)


def _eviction_path(content: str) -> Path:
    match = re.search(r"\[evicted to: (.*?)\]", content)
    assert match is not None, f"preview carries no eviction path: {content[:120]}"
    return Path(match.group(1))


class TestPreviewTemplate:
    def test_head_tail_and_metadata(self, tmp_path):
        lines = [f"line-{n}" for n in range(1, 21)]
        content = "\n".join(lines)

        preview = build_preview(content, tmp_path / "x.txt")

        assert preview.startswith(f"[evicted to: {tmp_path / 'x.txt'}]\n")
        assert "--- head (5 lines) ---\nline-1\nline-2\nline-3\nline-4\nline-5\n" in preview
        assert "\n...\n" in preview
        assert "--- tail (5 lines) ---\nline-16\nline-17\nline-18\nline-19\nline-20\n" in preview
        assert re.search(
            r"\[full content: \d+ chars, evicted at \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\]", preview
        )
        assert f"[full content: {len(content)} chars, evicted at " in preview
        assert "Use read_file(file_path='" in preview and "offset=0, limit=100" in preview
        assert "line-6" not in preview
        assert "line-15" not in preview

    def test_short_content_shows_all_lines_without_midline(self, tmp_path):
        content = "alpha\nbeta\ngamma"
        preview = build_preview(content, tmp_path / "y.txt")

        assert "--- head (3 lines) ---" in preview
        assert "--- tail (3 lines) ---" in preview
        assert "..." not in preview
        assert "alpha" in preview and "beta" in preview and "gamma" in preview


class TestThresholdBoundary:
    def test_at_threshold_is_not_evicted(self, sessions_dir):
        message = ToolMessage(
            content=_lines(THRESHOLD), name="terminal", tool_call_id="c1", id="t1"
        )

        assert evict_tool_result(message, SID) is None
        assert not sessions_dir.exists()

    def test_one_char_over_threshold_is_evicted(self, sessions_dir):
        content = _lines(THRESHOLD + 1)
        message = ToolMessage(content=content, name="terminal", tool_call_id="c1", id="t1")

        evicted = evict_tool_result(message, SID)

        assert evicted is not None
        path = _eviction_path(evicted.content)
        assert path.parent == sessions_dir / SID / "evicted"
        assert path.name.startswith("c1_") and path.suffix == ".txt"
        assert load_evicted(path) == content

    def test_single_giant_line_is_not_evicted(self, sessions_dir):
        message = ToolMessage(content="x" * (THRESHOLD + 1), name="terminal", tool_call_id="c1")

        assert evict_tool_result(message, SID) is None
        assert not sessions_dir.exists()


class TestExcludedTools:
    @pytest.mark.parametrize("tool_name", sorted(TOOL_RESULT_EVICTION["excluded_tools"]))
    def test_excluded_tool_is_never_offloaded(self, sessions_dir, tool_name):
        message = ToolMessage(content=_lines(THRESHOLD + 5_000), name=tool_name, tool_call_id="c1")

        assert evict_tool_result(message, SID) is None
        assert not sessions_dir.exists()


class TestEvictedMessage:
    def test_id_and_fields_survive_the_replacement(self, sessions_dir):
        content = _lines(THRESHOLD + 1)
        message = ToolMessage(
            content=content,
            name="terminal",
            tool_call_id="c1",
            id="t9",
            status="error",
        )

        evicted = evict_tool_result(message, SID)

        assert evicted is not None
        assert evicted is not message
        assert evicted.id == "t9"
        assert evicted.tool_call_id == "c1"
        assert evicted.name == "terminal"
        assert evicted.status == "error"
        assert evicted.content.startswith("[evicted to: ")

    def test_second_pass_over_a_preview_is_a_no_op(self, sessions_dir):
        content = _lines(THRESHOLD + 1)
        evicted = evict_tool_result(
            ToolMessage(content=content, name="terminal", tool_call_id="c1"), SID
        )
        assert evicted is not None
        # Make it oversized again so the threshold cannot mask the marker guard.
        replayed = evicted.model_copy(
            update={"content": _lines(THRESHOLD + 1).replace("x", "[evicted to: x]\n")}
        )
        assert replayed.content.startswith("[evicted to: ")

        assert evict_tool_result(replayed, SID) is None

    def test_multimodal_content_replaces_only_text_blocks(self, sessions_dir):
        text = _lines(THRESHOLD + 1)
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        message = ToolMessage(
            content=[{"type": "text", "text": text}, image],
            name="terminal",
            tool_call_id="c1",
        )

        evicted = evict_tool_result(message, SID)

        assert evicted is not None
        assert isinstance(evicted.content, list)
        assert evicted.content[0]["type"] == "text"
        assert evicted.content[0]["text"].startswith("[evicted to: ")
        assert evicted.content[1] == image
        assert load_evicted(_eviction_path(evicted.content[0]["text"])) == text


class TestPathSafety:
    @pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\\b", "../escape"])
    def test_unsafe_session_segment_skips_every_write(self, sessions_dir, bad):
        message = ToolMessage(content=_lines(THRESHOLD + 1), name="terminal", tool_call_id="c1")

        assert get_eviction_dir(bad) is None
        assert evict_tool_result(message, bad) is None
        assert not sessions_dir.exists() or not any(sessions_dir.rglob("*"))

    def test_safe_session_creates_the_eviction_directory(self, sessions_dir):
        directory = get_eviction_dir("session-1")

        assert directory == sessions_dir / "session-1" / "evicted"
        assert directory.is_dir()


class TestLoadEvicted:
    def test_missing_file_returns_none(self, tmp_path):
        assert load_evicted(tmp_path / "nope.txt") is None


class TestHumanEviction:
    def test_at_threshold_is_not_evicted(self, sessions_dir):
        message = HumanMessage(content=_lines(HUMAN_THRESHOLD), id="h1")

        assert evict_human_message(message, SID) is None
        assert not sessions_dir.exists()

    def test_one_char_over_is_tagged_with_full_text_on_disk(self, sessions_dir):
        content = _lines(HUMAN_THRESHOLD + 1)
        message = HumanMessage(content=content, id="h1")

        tagged = evict_human_message(message, SID)

        assert tagged is not None
        assert tagged is not message
        assert tagged.id == "h1"
        # State keeps the full content: only additional_kwargs changed.
        assert tagged.content == content
        path = Path(tagged.additional_kwargs[EVICTED_TO_KEY])
        assert path.parent == sessions_dir / SID / "evicted"
        assert path.name.startswith("human-h1") and path.suffix == ".md"
        assert load_evicted(path) == content

    def test_already_tagged_is_idempotent(self, sessions_dir):
        tagged = evict_human_message(
            HumanMessage(content=_lines(HUMAN_THRESHOLD + 1), id="h1"), SID
        )
        assert tagged is not None

        assert evict_human_message(tagged, SID) is None

    def test_media_blocks_stay_in_content_and_text_is_offloaded(self, sessions_dir):
        text = _lines(HUMAN_THRESHOLD + 1)
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
        message = HumanMessage(content=[{"type": "text", "text": text}, image], id="h1")

        tagged = evict_human_message(message, SID)

        assert tagged is not None
        assert tagged.content == message.content
        path = Path(tagged.additional_kwargs[EVICTED_TO_KEY])
        assert load_evicted(path) == text

    def test_disabled_flag_skips_every_write(self, sessions_dir, monkeypatch):
        monkeypatch.setitem(TOOL_RESULT_EVICTION, "human_evict_enabled", False)
        message = HumanMessage(content=_lines(HUMAN_THRESHOLD + 1), id="h1")

        assert evict_human_message(message, SID) is None
        assert not sessions_dir.exists()

    def test_missing_id_uses_a_timestamp_name(self, sessions_dir):
        message = HumanMessage(content=_lines(HUMAN_THRESHOLD + 1))

        tagged = evict_human_message(message, SID)

        assert tagged is not None
        assert Path(tagged.additional_kwargs[EVICTED_TO_KEY]).name.startswith("human-")

    def test_single_giant_line_is_not_evicted(self, sessions_dir):
        message = HumanMessage(content="u" * (HUMAN_THRESHOLD + 1), id="h1")

        assert evict_human_message(message, SID) is None
        assert not sessions_dir.exists()

    @pytest.mark.parametrize("bad", ["", ".", "..", "a/b", "a\\b", "../escape"])
    def test_unsafe_session_segment_skips_every_write(self, sessions_dir, bad):
        message = HumanMessage(content=_lines(HUMAN_THRESHOLD + 1), id="h1")

        assert evict_human_message(message, bad) is None
        assert not sessions_dir.exists() or not any(sessions_dir.rglob("*"))


class TestHumanPreview:
    def test_head_tail_path_and_read_file_hint(self, tmp_path):
        lines = [f"line-{n}" for n in range(1, 21)]
        content = "\n".join(lines)
        file_path = tmp_path / "human-h1.md"

        preview = build_human_preview(content, file_path)

        assert preview.startswith(f"[evicted to: {file_path}]\n")
        assert "--- head (5 lines) ---\nline-1\nline-2\nline-3\nline-4\nline-5\n" in preview
        assert "\n...\n" in preview
        assert "--- tail (5 lines) ---\nline-16\nline-17\nline-18\nline-19\nline-20\n" in preview
        assert f"[full content: {len(content)} chars, evicted at " in preview
        assert human_eviction_notice(file_path) in preview
        assert "read_file(file_path=" in preview and "offset=0, limit=100" in preview
        assert "line-6" not in preview and "line-15" not in preview

    def test_short_content_shows_all_lines_without_midline(self, tmp_path):
        preview = build_human_preview("alpha\nbeta", tmp_path / "human-h2.md")

        assert "--- head (2 lines) ---" in preview
        assert "..." not in preview


class TestReadFileSlice:
    def test_at_slice_limit_is_untouched(self):
        message = ToolMessage(content="x" * 4_000, name="read_file", tool_call_id="r1", id="t2")

        assert slice_read_file_result(message) is message

    def test_over_slice_limit_keeps_head_and_notice(self):
        content = "x" * 4_001
        message = ToolMessage(content=content, name="read_file", tool_call_id="r1", id="t2")

        sliced = slice_read_file_result(message)

        assert sliced is not message
        assert sliced.id == "t2"
        assert sliced.tool_call_id == "r1"
        assert sliced.content == content[:4_000] + _READ_FILE_SLICE_NOTICE

    def test_already_sliced_is_idempotent(self):
        message = ToolMessage(content="x" * 8_000, name="read_file", tool_call_id="r1")

        first = slice_read_file_result(message)
        second = slice_read_file_result(first)

        assert second is first
        assert first.content.count("Output was truncated due to eviction threshold") == 1
