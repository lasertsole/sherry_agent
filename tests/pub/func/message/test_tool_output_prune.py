"""P0-4: one-line tool-output summaries replace the prune marker.

Covers `_summarize_tool_result` dispatch (bash/read/grep/glob/default,
200-char cap, exception fallback) and the summarised replacement performed
by `prune_tool_outputs` (summary content plus `status=compacted` /
`original_length` metadata; protected tools untouched).
"""

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from pub.func.message import tool_output_prune
from pub.func.message.tool_output_prune import (
    _PRUNE_MARKER,
    _summarize_tool_result,
    prune_tool_outputs,
)

pytestmark = [pytest.mark.unit]


def _ai_with_call(tc_id: str, name: str, args: dict) -> AIMessage:
    return AIMessage(content="", tool_calls=[{"name": name, "args": args, "id": tc_id}])


def test_summarize_bash() -> None:
    content = "Exit code: 0\n" + "x" * (1200 - len("Exit code: 0\n"))
    assert len(content) == 1200

    summary = _summarize_tool_result("bash", content)

    assert summary == "[bash] exit_code=0, output 1200 chars"


def test_summarize_bash_no_exit_code() -> None:
    summary = _summarize_tool_result("bash", "hello world\n")

    assert summary == "[bash] exit_code=unknown, output 12 chars"


def test_summarize_read() -> None:
    content = "line1\nline2\nline3"

    summary = _summarize_tool_result("read", content)

    assert summary == f"[read] read file, {len(content)} chars, 3 lines"


def test_summarize_grep() -> None:
    summary = _summarize_tool_result("grep", "a\nb\nc")

    assert summary == "[grep] search done, 3 matches"


def test_summarize_glob() -> None:
    summary = _summarize_tool_result("glob", "a.py\nb.py\nc.py\n")

    assert summary == "[glob] matched 3 files"


def test_summarize_unknown_tool() -> None:
    content = "y" * 150

    summary = _summarize_tool_result("frobnicate", content)

    assert summary == f"[tool] output 150 chars, first 100: {'y' * 100}..."


def test_summarize_truncates_at_200(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(tool_output_prune._TOOL_SUMMARY_TEMPLATES, "chatty", lambda _r: "z" * 500)

    summary = _summarize_tool_result("chatty", "ignored")

    assert summary == "z" * 197 + "..."
    assert len(summary) == 200


def test_summarize_exception_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    def _boom(_result_text: str) -> str:
        raise ValueError("template exploded")

    monkeypatch.setitem(tool_output_prune._TOOL_SUMMARY_TEMPLATES, "explode", _boom)

    summary = _summarize_tool_result("explode", "abc")

    assert summary == "[explode] output 3 chars"


def test_prune_uses_summary_not_marker() -> None:
    content = "Exit code: 0\n" + "x" * (5000 - len("Exit code: 0\n"))
    messages = [
        HumanMessage(content="run it"),
        _ai_with_call("c1", "bash", {"script": "make"}),
        ToolMessage(content=content, tool_call_id="c1"),
    ]

    result, reduced = prune_tool_outputs(messages, protect_tokens=10, min_reduction_tokens=1)

    assert reduced == 5000 // 4
    assert result[2].content == "[bash] exit_code=0, output 5000 chars"
    assert _PRUNE_MARKER not in result[2].content


def test_prune_protected_tools_not_affected() -> None:
    content = "m" * 8000
    messages = [
        HumanMessage(content="q"),
        _ai_with_call("m1", "memory", {"q": "x"}),
        ToolMessage(content=content, tool_call_id="m1"),
    ]

    result, reduced = prune_tool_outputs(
        messages, protect_tokens=10, min_reduction_tokens=1, protected_tools={"memory"}
    )

    assert reduced == 0
    assert result[2].content == content


def test_prune_marks_additional_kwargs() -> None:
    content = "z" * 8000
    messages = [
        HumanMessage(content="q"),
        _ai_with_call("c1", "search", {"q": "x"}),
        ToolMessage(
            content=content,
            tool_call_id="c1",
            additional_kwargs={"existing_key": "preserved"},
        ),
    ]

    result, reduced = prune_tool_outputs(messages, protect_tokens=10, min_reduction_tokens=1)

    assert reduced == 8000 // 4
    assert result[2].additional_kwargs["status"] == "compacted"
    assert result[2].additional_kwargs["original_length"] == 8000
    assert result[2].additional_kwargs["existing_key"] == "preserved"
