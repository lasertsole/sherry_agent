"""Unit tests for the read_file recoverable slice in target truncation.

The truncate track clips an oversized ``ToolMessage`` head+tail. Generic
tools keep the anonymous ``...[truncated N chars]...`` marker; ``read_file``
results get a recovery notice carrying the original path and a 1-based
continuation offset, because the full file is still on disk.

Covered:
- read_file over budget -> notice with path + legal 1-based offset that does
  not skip any line fully retained in the head;
- page not starting at line 1 -> offset stays absolute (no off-by-page);
- non-read_file tools -> byte-identical legacy marker (regression lock);
- content within budget / protected tools -> untouched;
- missing tool metadata / unparseable payload -> safe generic fallback.
"""

import json
import re

import pytest
from langchain_core.messages import AIMessage, BaseMessage, ToolMessage

from config.features import TOOLS_TIMEOUTS
from pub.func.message.target_truncation import (
    CONTENT_HEAD_RATIO,
    CONTENT_TAIL_RATIO,
    target_truncate_tool_outputs,
)

pytestmark = [pytest.mark.unit]

MAX_CHARS = 2000
HEAD_CHARS = int(MAX_CHARS * CONTENT_HEAD_RATIO)
TAIL_CHARS = int(MAX_CHARS * CONTENT_TAIL_RATIO)
READ_LIMIT = TOOLS_TIMEOUTS["file_tools_read_default_limit"]
LINE = "x" * 40
HUGE_TARGET = 100_000


def _read_file_json(first_line: int, page_lines: int, total_lines: int = 500) -> str:
    """Build a read_file result JSON payload with lines numbered from first_line."""
    inner = "\n".join(f"{n}|{LINE}" for n in range(first_line, first_line + page_lines))
    return json.dumps(
        {
            "content": inner,
            "total_lines": total_lines,
            "file_size": 30_000,
            "truncated": True,
            "hint": (
                f"Use offset={first_line + page_lines} to continue reading "
                f"(showing {first_line}-{first_line + page_lines - 1} of {total_lines} lines)"
            ),
        },
        ensure_ascii=False,
    )


def _messages(tool_name: str, tc_id: str, args: dict, content: str) -> list[BaseMessage]:
    return [
        AIMessage(content="", tool_calls=[{"name": tool_name, "args": args, "id": tc_id}]),
        ToolMessage(content=content, tool_call_id=tc_id),
    ]


def _clip(messages: list[BaseMessage], **overrides) -> tuple[list[BaseMessage], int]:
    kwargs = {
        "target_reduction_tokens": HUGE_TARGET,
        "min_output_chars": 500,
        "max_output_chars": MAX_CHARS,
    }
    kwargs.update(overrides)
    return target_truncate_tool_outputs(messages, **kwargs)


def _notice_offset(clipped: str) -> int:
    match = re.search(r"Use offset=(\d+) to continue reading", clipped)
    assert match is not None, f"no continuation offset in: {clipped[:200]}"
    return int(match.group(1))


class TestReadFileRecoverableSlice:
    def test_notice_has_path_and_1based_offset(self):
        # Head 600 chars of this payload retain exactly lines 1..13 (line 14
        # is cut mid-way), so the notice must resume at line 14, never 0.
        content = _read_file_json(1, 60)
        path = "docs/example.md"
        result, reduced = _clip(_messages("read_file", "r1", {"file_path": path}, content))

        clipped = result[1].content
        omitted = len(content) - HEAD_CHARS - TAIL_CHARS
        assert reduced > 0
        assert clipped.startswith(content[:HEAD_CHARS])
        assert clipped.endswith(content[-TAIL_CHARS:])
        assert f"...[truncated {omitted} chars of read_file output" in clipped
        assert "the file is unchanged on disk" in clipped
        assert f"read_file(file_path='{path}', offset=14, limit={READ_LIMIT})" in clipped
        assert "Use offset=14 to continue reading" in clipped

    def test_offset_does_not_skip_lines_retained_in_head(self):
        result, _ = _clip(
            _messages("read_file", "r1", {"file_path": "a.md"}, _read_file_json(1, 60))
        )
        clipped = result[1].content
        head = clipped[:HEAD_CHARS]
        offset = _notice_offset(clipped)

        assert offset >= 1
        for n in range(1, offset):
            assert f"{n}|{LINE}\\n" in head  # fully retained -> must stay before the resume point
        assert f"{offset}|{LINE}\\n" not in head  # partial line -> re-read it, never skip it

    def test_offset_is_absolute_for_pages_not_starting_at_line_1(self):
        # Same shape, but the page starts at line 100: head retains lines
        # 100..111, so the resume point is 112 - not the page-relative 13.
        result, _ = _clip(
            _messages("read_file", "r1", {"file_path": "b.md"}, _read_file_json(100, 60))
        )
        clipped = result[1].content
        head = clipped[:HEAD_CHARS]
        offset = _notice_offset(clipped)

        assert offset == 112
        for n in range(100, offset):
            assert f"{n}|{LINE}\\n" in head
        assert f"{offset}|{LINE}\\n" not in head

    def test_unparseable_read_file_content_falls_back_to_restart_notice(self):
        # Already-clipped or non-JSON content: an offset cannot be derived,
        # so the notice must restart from line 1 instead of guessing.
        content = "x" * 10_000
        result, _ = _clip(_messages("read_file", "r1", {"file_path": "a.md"}, content))
        clipped = result[1].content
        assert "Re-read it from the start in chunks" in clipped
        assert f"read_file(file_path='a.md', offset=1, limit={READ_LIMIT})" in clipped
        assert "the file is unchanged on disk" in clipped

    def test_json_without_content_field_falls_back_to_restart_notice(self):
        content = json.dumps({"error": "File not found: " + "x" * 3_000})
        result, _ = _clip(_messages("read_file", "r1", {"file_path": "a.md"}, content))
        assert "Re-read it from the start in chunks" in result[1].content


class TestGenericPathRegressionLock:
    def test_non_read_file_marker_is_byte_identical(self):
        content = "a" * 10_000
        result, reduced = _clip(_messages("search", "s1", {"q": "x"}, content))
        expected = "a" * 600 + "...[truncated 8800 chars]..." + "a" * 600
        assert result[1].content == expected
        assert reduced == (10_000 - len(expected)) // 4

    def test_missing_aimessage_tool_calls_uses_generic_path(self):
        messages: list[BaseMessage] = [ToolMessage(content="a" * 10_000, tool_call_id="r1")]
        result, reduced = _clip(messages)
        assert result[0].content == "a" * 600 + "...[truncated 8800 chars]..." + "a" * 600
        assert reduced > 0

    def test_tool_call_id_mismatch_uses_generic_path(self):
        messages: list[BaseMessage] = [
            AIMessage(
                content="",
                tool_calls=[{"name": "read_file", "args": {"file_path": "a.md"}, "id": "call-1"}],
            ),
            ToolMessage(content="a" * 10_000, tool_call_id="call-2"),
        ]
        result, _ = _clip(messages)
        assert result[1].content == "a" * 600 + "...[truncated 8800 chars]..." + "a" * 600

    def test_read_file_without_file_path_arg_uses_generic_path(self):
        result, _ = _clip(_messages("read_file", "r1", {"offset": 1}, "a" * 10_000))
        assert result[1].content == "a" * 600 + "...[truncated 8800 chars]..." + "a" * 600


class TestUnchangedPaths:
    def test_read_file_within_budget_is_untouched(self):
        content = _read_file_json(1, 12)  # >= min_output_chars, <= max_output_chars
        assert 500 <= len(content) <= MAX_CHARS
        result, reduced = _clip(_messages("read_file", "r1", {"file_path": "a.md"}, content))
        assert result[1].content == content
        assert reduced == 0

    def test_below_min_output_chars_is_untouched(self):
        result, reduced = _clip(
            _messages("search", "s1", {"q": "x"}, "a" * 300), min_output_chars=500
        )
        assert result[1].content == "a" * 300
        assert reduced == 0

    def test_protected_read_file_is_not_truncated(self):
        content = _read_file_json(1, 60)
        result, reduced = _clip(
            _messages("read_file", "r1", {"file_path": "a.md"}, content),
            protected_tools={"read_file"},
        )
        assert result[1].content == content
        assert reduced == 0
