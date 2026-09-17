"""Unit tests for the message_search checkpoint fallback (two-stage query).

When the FTS path over the persisted messages table has no match, the tool
falls back to the session's newest checkpoint state, keyword-matches
``state["messages"]`` newest-first, and tags every hit ``source="checkpoint"``
so the model knows the turn may not be persisted yet.
"""

import json
import sqlite3
from unittest.mock import MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from agent.tools import message_search as ms
from agent.tools.message_search import (
    _CHECKPOINT_SCAN_MAX_MESSAGES,
    _search_latest_checkpoint_messages,
    session_search,
)

pytestmark = [pytest.mark.unit]


class _FakeState:
    def __init__(self, messages):
        self.values = {"messages": messages}


def _run_async_returning(state):
    """Patch target for ``ms.run_async``: closes the coroutine, returns state."""

    def _run(coro, timeout=None):
        coro.close()
        return state

    return _run


def _enable_fallback(monkeypatch, messages):
    monkeypatch.setattr(ms, "_checkpoint_db_available", lambda: True)
    monkeypatch.setattr(ms, "_checkpoint_thread_has_state", lambda session_id: True)
    monkeypatch.setattr(ms, "run_async", _run_async_returning(_FakeState(messages)))


# ============================================================================
# session_search integration
# ============================================================================


class TestSessionSearchFallbackWiring:
    def test_checkpoint_hits_are_returned_with_source_marker(self):
        """FTS empty + fallback hit → result carries the checkpoint marker."""
        hit = {
            "id": None,
            "session_id": "sess-1",
            "turn_num": 2,
            "role": "human",
            "snippet": "[HUMAN]: needle",
            "timestamp": None,
            "tool_name": None,
            "context": [],
            "source": "checkpoint",
        }
        mock_db = MagicMock(spec=sqlite3.Connection)
        with (
            patch("agent.tools.message_search.get_db", return_value=mock_db),
            patch("agent.tools.message_search.search_messages", return_value=[]),
            patch(
                "agent.tools.message_search._search_latest_checkpoint_messages",
                return_value=[hit],
            ) as mock_fallback,
        ):
            result = json.loads(session_search("needle", "sess-1", limit=3))

        assert result["success"] is True
        assert result["source"] == "checkpoint"
        assert result["mode"] == "checkpoint_fallback"
        assert result["count"] == 1
        assert result["results"] == [hit]
        assert mock_fallback.call_args[0] == ("needle", "sess-1", None, 50)

    def test_checkpoint_fallback_failure_returns_original_empty_payload(self):
        """Fallback raising → the pre-existing empty payload, unchanged."""
        mock_db = MagicMock(spec=sqlite3.Connection)
        with (
            patch("agent.tools.message_search.get_db", return_value=mock_db),
            patch("agent.tools.message_search.search_messages", return_value=[]),
            patch(
                "agent.tools.message_search._search_latest_checkpoint_messages",
                side_effect=RuntimeError("checkpoint exploded"),
            ),
        ):
            result = json.loads(session_search("needle", "sess-1", limit=3))

        assert result == {
            "success": True,
            "query": "needle",
            "results": [],
            "count": 0,
            "message": "No matching sessions found.",
        }

    def test_empty_checkpoint_hits_return_original_empty_payload(self):
        """Fallback finds nothing → the pre-existing empty payload, unchanged."""
        mock_db = MagicMock(spec=sqlite3.Connection)
        with (
            patch("agent.tools.message_search.get_db", return_value=mock_db),
            patch("agent.tools.message_search.search_messages", return_value=[]),
            patch(
                "agent.tools.message_search._search_latest_checkpoint_messages",
                return_value=[],
            ),
        ):
            result = json.loads(session_search("needle", "sess-1", limit=3))

        assert result["count"] == 0
        assert result["success"] is True
        assert "source" not in result

    def test_end_to_end_uses_fake_checkpoint_state(self, monkeypatch):
        """FTS empty + real helper over a fake state → checkpoint result."""
        _enable_fallback(monkeypatch, [HumanMessage("we talked about the needle")])
        mock_db = MagicMock(spec=sqlite3.Connection)
        with (
            patch("agent.tools.message_search.get_db", return_value=mock_db),
            patch("agent.tools.message_search.search_messages", return_value=[]),
        ):
            result = json.loads(session_search("needle", "sess-1", limit=3))

        assert result["source"] == "checkpoint"
        assert result["count"] == 1
        assert result["results"][0]["role"] == "human"
        assert result["results"][0]["session_id"] == "sess-1"
        assert "needle" in result["results"][0]["snippet"]


# ============================================================================
# _search_latest_checkpoint_messages
# ============================================================================


class TestCheckpointSearch:
    def test_hit_is_newest_first_and_fts_shaped(self, monkeypatch):
        messages = [
            HumanMessage("an older needle mention"),
            AIMessage("the needle is right here"),
            ToolMessage("tool output", name="web_search", tool_call_id="call_1"),
        ]
        _enable_fallback(monkeypatch, messages)

        hits = _search_latest_checkpoint_messages("needle", "sess-1", None, 50)

        assert len(hits) == 2
        newest = hits[0]
        assert newest["role"] == "ai"
        assert newest["turn_num"] == 2
        assert newest["source"] == "checkpoint"
        assert newest["snippet"].startswith("[ASSISTANT]:")
        assert "needle" in newest["snippet"]
        required_fields = {
            "id",
            "session_id",
            "turn_num",
            "role",
            "snippet",
            "timestamp",
            "tool_name",
            "context",
        }
        assert required_fields <= set(newest)
        assert hits[1]["role"] == "human"
        assert hits[1]["turn_num"] == 1

    def test_tool_hit_exposes_tool_name(self, monkeypatch):
        _enable_fallback(
            monkeypatch,
            [ToolMessage("needle inside a tool result", name="web_search", tool_call_id="call_1")],
        )

        hits = _search_latest_checkpoint_messages("needle", "sess-1", None, 50)

        assert len(hits) == 1
        assert hits[0]["role"] == "tool"
        assert hits[0]["tool_name"] == "web_search"
        assert hits[0]["snippet"].startswith("[TOOL:web_search]:")

    def test_multimodal_list_content_is_searchable(self, monkeypatch):
        _enable_fallback(
            monkeypatch,
            [HumanMessage(content=[{"type": "text", "text": "needle in a multimodal list"}])],
        )

        hits = _search_latest_checkpoint_messages("needle", "sess-1", None, 50)

        assert len(hits) == 1
        assert hits[0]["role"] == "human"

    @pytest.mark.parametrize("role_filter", ["human", "user"])
    def test_role_filter_selects_human_messages(self, monkeypatch, role_filter):
        _enable_fallback(
            monkeypatch,
            [
                HumanMessage("needle in human"),
                AIMessage("needle in ai"),
                ToolMessage("needle in tool", name="web_search", tool_call_id="call_1"),
            ],
        )

        hits = _search_latest_checkpoint_messages("needle", "sess-1", role_filter, 50)

        assert [hit["role"] for hit in hits] == ["human"]
        assert "needle in human" in hits[0]["snippet"]

    @pytest.mark.parametrize("role_filter", ["ai", "assistant"])
    def test_role_filter_selects_ai_messages(self, monkeypatch, role_filter):
        _enable_fallback(
            monkeypatch,
            [
                HumanMessage("needle in human"),
                AIMessage("needle in ai"),
                ToolMessage("needle in tool", name="web_search", tool_call_id="call_1"),
            ],
        )

        hits = _search_latest_checkpoint_messages("needle", "sess-1", role_filter, 50)

        assert [hit["role"] for hit in hits] == ["ai"]

    def test_role_filter_selects_tool_messages(self, monkeypatch):
        _enable_fallback(
            monkeypatch,
            [
                HumanMessage("needle in human"),
                AIMessage("needle in ai"),
                ToolMessage("needle in tool", name="web_search", tool_call_id="call_1"),
            ],
        )

        hits = _search_latest_checkpoint_messages("needle", "sess-1", "tool", 50)

        assert [hit["role"] for hit in hits] == ["tool"]

    def test_role_filter_list_is_honoured(self, monkeypatch):
        _enable_fallback(
            monkeypatch,
            [
                HumanMessage("needle in human"),
                AIMessage("needle in ai"),
                ToolMessage("needle in tool", name="web_search", tool_call_id="call_1"),
            ],
        )

        hits = _search_latest_checkpoint_messages("needle", "sess-1", "user, assistant", 50)

        assert {hit["role"] for hit in hits} == {"human", "ai"}

    def test_unknown_role_filter_matches_nothing(self, monkeypatch):
        _enable_fallback(monkeypatch, [HumanMessage("needle in human")])

        assert _search_latest_checkpoint_messages("needle", "sess-1", "bogus", 50) == []

    @pytest.mark.parametrize("role_filter", [None, "", "   "])
    def test_blank_role_filter_matches_every_role(self, monkeypatch, role_filter):
        _enable_fallback(
            monkeypatch,
            [
                HumanMessage("needle in human"),
                AIMessage("needle in ai"),
                ToolMessage("needle in tool", name="web_search", tool_call_id="call_1"),
            ],
        )

        hits = _search_latest_checkpoint_messages("needle", "sess-1", role_filter, 50)

        assert {hit["role"] for hit in hits} == {"human", "ai", "tool"}

    def test_scan_honours_message_cap(self, monkeypatch):
        newest_case = [
            HumanMessage(f"filler {i}") for i in range(_CHECKPOINT_SCAN_MAX_MESSAGES + 10)
        ]
        newest_case[-1] = HumanMessage("needle at the newest message")
        oldest_case = [
            HumanMessage(f"filler {i}") for i in range(_CHECKPOINT_SCAN_MAX_MESSAGES + 10)
        ]
        oldest_case[0] = HumanMessage("needle at the oldest message")

        _enable_fallback(monkeypatch, newest_case)
        newest_hits = _search_latest_checkpoint_messages("needle", "sess-1", None, 50)
        _enable_fallback(monkeypatch, oldest_case)
        oldest_hits = _search_latest_checkpoint_messages("needle", "sess-1", None, 50)

        assert len(newest_hits) == 1
        assert oldest_hits == []

    def test_scan_honours_char_budget(self, monkeypatch):
        filler = "x" * 2000
        messages = [HumanMessage(filler) for _ in range(60)]
        messages[0] = HumanMessage("needle beyond the char budget")

        _enable_fallback(monkeypatch, messages)
        hits = _search_latest_checkpoint_messages("needle", "sess-1", None, 50)

        assert hits == []

    def test_hit_limit_caps_returned_results(self, monkeypatch):
        _enable_fallback(monkeypatch, [HumanMessage("needle") for _ in range(10)])

        hits = _search_latest_checkpoint_messages("needle", "sess-1", None, 2)

        assert len(hits) == 2

    def test_disabled_switch_returns_no_hits(self, monkeypatch):
        _enable_fallback(monkeypatch, [HumanMessage("needle")])
        monkeypatch.setitem(ms.TOOLS_TIMEOUTS, "message_search_checkpoint_fallback_enabled", False)

        assert _search_latest_checkpoint_messages("needle", "sess-1", None, 50) == []

    def test_missing_checkpoint_db_skips_state_load(self, monkeypatch):
        mock_run_async = MagicMock()
        monkeypatch.setattr(ms, "_checkpoint_db_available", lambda: False)
        monkeypatch.setattr(ms, "run_async", mock_run_async)

        assert _search_latest_checkpoint_messages("needle", "sess-1", None, 50) == []
        mock_run_async.assert_not_called()

    def test_thread_without_checkpoint_skips_state_load(self, monkeypatch):
        mock_run_async = MagicMock()
        monkeypatch.setattr(ms, "_checkpoint_db_available", lambda: True)
        monkeypatch.setattr(ms, "_checkpoint_thread_has_state", lambda session_id: False)
        monkeypatch.setattr(ms, "run_async", mock_run_async)

        assert _search_latest_checkpoint_messages("needle", "sess-1", None, 50) == []
        mock_run_async.assert_not_called()
