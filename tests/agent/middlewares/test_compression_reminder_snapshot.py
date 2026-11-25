"""B-class patch #13: keep the last pre-cutoff reminders across compression.

A mid-turn compression replaced the cutoff window — and every
``<system-reminder>`` inside it — with a summary pair. The model then executed
the remaining turn with no todo/TaskFlow anchor. ``capture_reminder_snapshot``
re-inserts the last non-transient reminder per source between the summary and
the preserved tail, with byte-identical dedup against the oldest preserved copy.

These tests are hermetic: they exercise the pure capture helper and the
fail-open insertion helper directly, with no compression harness or DB.
"""

from __future__ import annotations

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.middlewares.reminder_catalog import capture_reminder_snapshot
from agent.middlewares.summarization.compression import _insert_reminder_snapshot
from pub.func.message.system_reminder import system_reminder_metadata, wrap_reminder

pytestmark = [pytest.mark.unit]


def _reminder(source: str, content: str, *, transient: bool = False) -> HumanMessage:
    """A catalog-shaped reminder; transient sources use the real transient set."""
    return HumanMessage(
        content=wrap_reminder(source, content),
        metadata=system_reminder_metadata(source, compress_exclude=not transient),
    )


def _transient_reminder(source: str, content: str) -> HumanMessage:
    return _reminder(source, content, transient=True)


# ── capture: per-source, last wins ──────────────────────────────────────────


def test_capture_basic_latest_per_source():
    window = [
        _reminder("todo_block", "A pending"),
        _reminder("taskflow_status", "flow running"),
        _reminder("todo_block", "A done, B in progress"),
    ]
    snap = capture_reminder_snapshot(window, [])
    assert len(snap) == 2
    assert snap[0].metadata["origin"] == "taskflow_status"
    assert snap[1].metadata["origin"] == "todo_block"
    assert "A done, B in progress" in snap[1].content
    assert "A pending" not in snap[1].content


def test_capture_skips_transient_sources():
    window = [
        _reminder("todo_block", "plan"),
        _transient_reminder("current_time", "2026-01-01"),
        _transient_reminder("lsp_diagnostics", "3 errors"),
    ]
    snap = capture_reminder_snapshot(window, [])
    assert [m.metadata["origin"] for m in snap] == ["todo_block"]


def test_capture_no_reminders_returns_empty():
    window = [
        HumanMessage(content="a user turn"),
        AIMessage(content="an answer"),
        ToolMessage(content="a tool result", tool_call_id="1"),
    ]
    assert capture_reminder_snapshot(window, []) == []


def test_capture_keeps_only_last_of_a_source():
    window = [_reminder("todo_block", f"state {i}") for i in range(5)]
    snap = capture_reminder_snapshot(window, [])
    assert len(snap) == 1
    assert "state 4" in snap[0].content


def test_capture_preserves_metadata():
    window = [_reminder("todo_block", "plan")]
    snap = capture_reminder_snapshot(window, [])
    assert snap[0].metadata["origin"] == "todo_block"
    assert snap[0].metadata["internal"] is True
    assert snap[0].metadata["provenance"] == "reminder_catalog"


# ── dedup: compare to the OLDEST preserved copy ─────────────────────────────


def test_dedup_skips_identical_to_oldest_preserved():
    window = [_reminder("todo_block", "same state")]
    preserved = [
        _reminder("todo_block", "same state"),  # oldest
        _reminder("todo_block", "newer state"),
    ]
    assert capture_reminder_snapshot(window, preserved) == []


def test_dedup_keeps_different_from_oldest_preserved():
    window = [_reminder("todo_block", "snapshot state")]
    preserved = [_reminder("todo_block", "oldest state")]
    snap = capture_reminder_snapshot(window, preserved)
    assert len(snap) == 1
    assert "snapshot state" in snap[0].content


def test_dedup_compares_to_oldest_not_latest():
    """State changed then changed back: snapshot == newest but != oldest."""
    window = [_reminder("todo_block", "state A")]
    preserved = [
        _reminder("todo_block", "state A"),  # oldest — wait, must differ
        _reminder("todo_block", "state B"),
    ]
    # Make the oldest genuinely different so the snapshot must survive.
    preserved[0] = _reminder("todo_block", "state Z")
    snap = capture_reminder_snapshot(window, preserved)
    assert len(snap) == 1, "snapshot matches newest but not oldest — must be kept"


def test_dedup_is_per_source():
    window = [
        _reminder("todo_block", "same as preserved"),
        _reminder("taskflow_status", "changed"),
    ]
    preserved = [
        _reminder("todo_block", "same as preserved"),
        _reminder("taskflow_status", "old"),
    ]
    snap = capture_reminder_snapshot(window, preserved)
    assert [m.metadata["origin"] for m in snap] == ["taskflow_status"]


# ── fail-open ───────────────────────────────────────────────────────────────


def test_capture_fail_open_on_broken_input(monkeypatch):
    class _Broken:
        @property
        def metadata(self):
            raise RuntimeError("broken metadata")

    # The helper catches all exceptions and returns [] rather than propagating.
    assert capture_reminder_snapshot([_Broken()], []) == []


# ── insertion into the compressed transcript ───────────────────────────────


def test_insert_snapshot_between_summary_and_preserved():
    summary_pair = [
        HumanMessage(content="What did we do so far?"),
        AIMessage(content="<summary>...</summary>"),
    ]
    preserved = [ToolMessage(content="latest result", tool_call_id="1")]
    window = [_reminder("todo_block", "plan snapshot")]
    final = [*summary_pair, *preserved]

    result = _insert_reminder_snapshot(final, window, preserved)

    assert result[0] is summary_pair[0]
    assert result[1] is summary_pair[1]
    assert result[2].metadata["origin"] == "todo_block"
    assert result[3] is preserved[0]
    assert len(result) == 4


def test_insert_no_snapshot_when_window_empty():
    summary_pair = [HumanMessage(content="q"), AIMessage(content="a")]
    preserved = [ToolMessage(content="r", tool_call_id="1")]
    final = [*summary_pair, *preserved]
    assert _insert_reminder_snapshot(final, [], preserved) == final


def test_insert_fail_open_when_capture_raises(monkeypatch):
    import agent.middlewares.reminder_catalog as rc

    def _boom(*args, **kwargs):
        raise RuntimeError("capture exploded")

    monkeypatch.setattr(rc, "capture_reminder_snapshot", _boom)
    final = [
        HumanMessage(content="q"),
        AIMessage(content="a"),
        ToolMessage(content="r", tool_call_id="1"),
    ]
    window = [_reminder("todo_block", "state")]
    assert _insert_reminder_snapshot(final, window, []) == final


def test_insert_skips_when_no_summary_pair():
    """No summary pair (< 2 messages) → unchanged, even with a captured window."""
    final = [HumanMessage(content="only one")]
    window = [_reminder("todo_block", "state")]
    assert _insert_reminder_snapshot(final, window, []) == final
