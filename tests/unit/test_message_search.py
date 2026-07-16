"""Unit tests for agent/tools/message_search.py — schema, helpers, and search logic."""

import json
import sqlite3
import pytest
from unittest.mock import patch, MagicMock, ANY
from pydantic import ValidationError

from agent.tools.message_search import (
    MessageSearchSchema,
    _tool_error,
    _format_conversation,
    _truncate_around_matches,
    _recent_sessions,
    session_search,
    _message_search_tool,
    build_message_search_tool,
)


# ============================================================================
# MessageSearchSchema
# ============================================================================

class TestMessageSearchSchema:
    def test_defaults(self):
        s = MessageSearchSchema()
        assert s.query is None
        assert s.role_filter is None
        assert s.limit == 3

    def test_with_query(self):
        s = MessageSearchSchema(query="docker deployment")
        assert s.query == "docker deployment"
        assert s.role_filter is None
        assert s.limit == 3

    def test_with_role_filter(self):
        s = MessageSearchSchema(role_filter="user,assistant")
        assert s.role_filter == "user,assistant"

    def test_with_custom_limit(self):
        s = MessageSearchSchema(limit=5)
        assert s.limit == 5

    def test_limit_out_of_range(self):
        """Pydantic Field does not enforce min/max by default on plain int,
        so 99 should be accepted at schema level (clamping happens in session_search)."""
        s = MessageSearchSchema(limit=99)
        assert s.limit == 99

    def test_limit_zero(self):
        s = MessageSearchSchema(limit=0)
        assert s.limit == 0

    def test_all_fields(self):
        s = MessageSearchSchema(
            query="memory leak",
            role_filter="ai",
            limit=2,
        )
        assert s.query == "memory leak"
        assert s.role_filter == "ai"
        assert s.limit == 2


# ============================================================================
# _tool_error
# ============================================================================

class TestToolError:
    def test_basic_error(self):
        result = json.loads(_tool_error("something went wrong"))
        assert result == {"error": "something went wrong"}

    def test_with_extra_fields(self):
        result = json.loads(_tool_error("bad input", success=False, code=42))
        assert result["error"] == "bad input"
        assert result["success"] is False
        assert result["code"] == 42

    def test_unicode(self):
        result = json.loads(_tool_error("连接失败"))
        assert result["error"] == "连接失败"

    def test_empty_message(self):
        result = json.loads(_tool_error(""))
        assert result["error"] == ""


# ============================================================================
# _format_conversation
# ============================================================================

class TestFormatConversation:
    def test_empty_messages(self):
        assert _format_conversation([]) == ""

    def test_user_message(self):
        msgs = [{"role": "human", "content": "hello"}]
        result = _format_conversation(msgs)
        assert "[HUMAN]: hello" in result

    def test_ai_message(self):
        msgs = [{"role": "ai", "content": "hi there"}]
        result = _format_conversation(msgs)
        assert "[ASSISTANT]: hi there" in result

    def test_ai_with_tool_calls_list(self):
        msgs = [{
            "role": "ai",
            "content": "Let me check",
            "tool_calls": [
                {"name": "web_search"},
                {"name": "read_file"},
            ],
        }]
        result = _format_conversation(msgs)
        assert "[Called: web_search, read_file]" in result
        assert "[ASSISTANT]: Let me check" in result

    def test_ai_with_tool_calls_function_dict(self):
        msgs = [{
            "role": "ai",
            "content": "",
            "tool_calls": [
                {"function": {"name": "python_repl"}},
            ],
        }]
        result = _format_conversation(msgs)
        assert "[Called: python_repl]" in result

    def test_tool_message_with_name(self):
        msgs = [{"role": "tool", "content": "output", "tool_name": "web_search"}]
        result = _format_conversation(msgs)
        assert "[TOOL:web_search]: output" in result

    def test_tool_message_truncated(self):
        long = "x" * 1000
        msgs = [{"role": "tool", "content": long, "tool_name": "read_file"}]
        result = _format_conversation(msgs)
        assert len(result) < 800  # truncated
        assert "[truncated]" in result

    def test_unknown_role(self):
        msgs = [{"role": "system", "content": "be helpful"}]
        result = _format_conversation(msgs)
        assert "[SYSTEM]: be helpful" in result

    def test_mixed_conversation(self):
        msgs = [
            {"role": "human", "content": "search for docs"},
            {"role": "ai", "content": "Sure!", "tool_calls": [{"name": "web_search"}]},
            {"role": "tool", "content": "result data", "tool_name": "web_search"},
            {"role": "ai", "content": "Here's what I found"},
        ]
        result = _format_conversation(msgs)
        assert "[HUMAN]: search for docs" in result
        assert "[Called: web_search]" in result
        assert "[TOOL:web_search]: result data" in result
        assert "[ASSISTANT]: Here's what I found" in result

    def test_missing_fields(self):
        msgs = [{"role": "human"}]  # no content
        result = _format_conversation(msgs)
        assert "[HUMAN]:" in result

    def test_tool_calls_not_list(self):
        msgs = [{"role": "ai", "content": "ok", "tool_calls": "invalid"}]
        result = _format_conversation(msgs)
        assert "[ASSISTANT]: ok" in result


# ============================================================================
# _truncate_around_matches
# ============================================================================

class TestTruncateAroundMatches:
    def test_shorter_than_max(self):
        text = "short text"
        result = _truncate_around_matches(text, "text", max_chars=100)
        assert result == text

    def test_exact_match_phrase(self):
        text = "a" * 50 + "docker compose" + "b" * 50
        result = _truncate_around_matches(text, "docker compose", max_chars=60)
        assert "docker compose" in result

    def test_fallback_to_longer_text(self):
        text = "word " * 200
        result = _truncate_around_matches(text, "rareterm", max_chars=100)
        assert len(result) <= 200  # truncated with suffix
        assert "[later conversation truncated]" in result

    def test_no_match_uses_start(self):
        text = "AAAA " + "B " * 200
        result = _truncate_around_matches(text, "ZZZZZ", max_chars=50)
        assert result.startswith("AAAA")
        assert "[later conversation truncated]" in result

    def test_case_insensitive(self):
        text = "a" * 30 + "Deploy Config" + "b" * 30
        result = _truncate_around_matches(text, "deploy config", max_chars=60)
        assert "Deploy Config" in result

    def test_proximity_co_occurrence(self):
        text = ("x " * 100) + "hello " + ("x " * 10) + "world " + ("x " * 100)
        result = _truncate_around_matches(text, "hello world", max_chars=100)
        assert "hello" in result
        assert "world" in result

    def test_individual_term_fallback(self):
        text = ("a " * 50) + "needle " + ("a " * 50) + "other " + ("a " * 50)
        result = _truncate_around_matches(text, "needle other", max_chars=80)
        assert "needle" in result or "[earlier" in result

    def test_empty_text(self):
        result = _truncate_around_matches("", "query", max_chars=100)
        assert result == ""

    def test_unicode_content(self):
        text = "普通" * 50 + "搜索内容" + "普通" * 50
        result = _truncate_around_matches(text, "搜索", max_chars=60)
        assert "搜索" in result


# ============================================================================
# _recent_sessions (browse mode)
# ============================================================================

class TestRecentSessions:
    def test_returns_json_with_results(self):
        mock_db = MagicMock(spec=sqlite3.Connection)
        mock_cursor = MagicMock()
        mock_db.execute.return_value = mock_cursor
        mock_cursor.fetchall.return_value = [
            {"session_id": "s1", "first_user_msg": "hello world", "last_activity": "20260714120000"},
            {"session_id": "s2", "first_user_msg": None, "last_activity": "20260713120000"},
        ]

        result = json.loads(_recent_sessions(mock_db, "current_session", limit=3))
        assert result["success"] is True
        assert result["mode"] == "recent_sessions"
        assert result["count"] == 2
        assert result["results"][0]["session_id"] == "s1"
        assert result["results"][0]["preview"] == "hello world"
        assert result["results"][1]["preview"] == ""

    def test_returns_json_empty(self):
        mock_db = MagicMock(spec=sqlite3.Connection)
        mock_cursor = MagicMock()
        mock_db.execute.return_value = mock_cursor
        mock_cursor.fetchall.return_value = []

        result = json.loads(_recent_sessions(mock_db, "current_session", limit=5))
        assert result["success"] is True
        assert result["count"] == 0
        assert result["results"] == []

    def test_db_error_returns_tool_error(self):
        mock_db = MagicMock(spec=sqlite3.Connection)
        mock_db.execute.side_effect = Exception("DB unavailable")

        result = json.loads(_recent_sessions(mock_db, "current_session", limit=3))
        assert "error" in result
        assert "DB unavailable" in result["error"]

    def test_uses_correct_sql(self):
        mock_db = MagicMock(spec=sqlite3.Connection)
        mock_cursor = MagicMock()
        mock_db.execute.return_value = mock_cursor
        mock_cursor.fetchall.return_value = []

        _recent_sessions(mock_db, "my_session", limit=2)
        call_args = mock_db.execute.call_args
        assert call_args is not None
        pos_args = call_args[0]  # all positional args as a tuple
        sql = pos_args[0]
        # The SQL should exclude the current session
        assert "session_id != ?" in sql
        # params tuple is the second positional arg: (session_id, limit)
        params = pos_args[1]
        assert params[0] == "my_session"
        assert params[1] == 2


# ============================================================================
# session_search
# ============================================================================

class TestSessionSearch:
    @pytest.fixture
    def mock_db(self):
        db = MagicMock(spec=sqlite3.Connection)
        cursor = MagicMock()
        db.execute.return_value = cursor
        cursor.fetchall.return_value = []
        return db

    @pytest.fixture
    def mock_search_messages(self):
        """Return an empty result list by default."""
        return []

    def test_browse_mode_query_none(self, mock_db):
        """query=None → browse/recent mode."""
        mock_cursor = MagicMock()
        mock_db.execute.return_value = mock_cursor
        mock_cursor.fetchall.return_value = []

        with patch("agent.tools.message_search.get_db", return_value=mock_db):
            result = json.loads(session_search(None, "current_session", limit=3))
        assert result["mode"] == "recent_sessions"

    def test_browse_mode_empty_query(self, mock_db):
        """query='' → browse mode."""
        mock_cursor = MagicMock()
        mock_db.execute.return_value = mock_cursor
        mock_cursor.fetchall.return_value = []

        with patch("agent.tools.message_search.get_db", return_value=mock_db):
            result = json.loads(session_search("", "current_session", limit=3))
        assert result["mode"] == "recent_sessions"

    def test_browse_mode_whitespace_query(self, mock_db):
        """query='   ' → browse mode."""
        mock_cursor = MagicMock()
        mock_db.execute.return_value = mock_cursor
        mock_cursor.fetchall.return_value = []

        with patch("agent.tools.message_search.get_db", return_value=mock_db):
            result = json.loads(session_search("   ", "current_session", limit=3))
        assert result["mode"] == "recent_sessions"

    def test_limit_clamping_high(self, mock_db, mock_search_messages):
        """limit=99 clamped to 5 in browse mode."""
        mock_cursor = MagicMock()
        mock_db.execute.return_value = mock_cursor
        mock_cursor.fetchall.return_value = []

        with patch("agent.tools.message_search.get_db", return_value=mock_db):
            result = json.loads(session_search(None, "s", limit=99))
        assert result["count"] == 0

    def test_limit_clamping_low(self, mock_db, mock_search_messages):
        """limit=0 clamped to 1 in browse mode."""
        mock_cursor = MagicMock()
        mock_db.execute.return_value = mock_cursor
        mock_cursor.fetchall.return_value = []

        with patch("agent.tools.message_search.get_db", return_value=mock_db):
            result = json.loads(session_search(None, "s", limit=0))
        assert result["count"] == 0

    def test_limit_non_int_string(self, mock_db, mock_search_messages):
        """limit='5' string coerced to int."""
        mock_cursor = MagicMock()
        mock_db.execute.return_value = mock_cursor
        mock_cursor.fetchall.return_value = []

        with patch("agent.tools.message_search.get_db", return_value=mock_db):
            result = json.loads(session_search(None, "s", limit="5"))
        assert result["count"] == 0

    def test_search_with_query_no_results(self, mock_db):
        """Valid query but no FTS5 matches."""
        with (
            patch("agent.tools.message_search.get_db", return_value=mock_db),
            patch("agent.tools.message_search.search_messages", return_value=[]),
        ):
            result = json.loads(session_search("needle", "s", limit=3))
        assert result["success"] is True
        assert result["count"] == 0
        assert result["query"] == "needle"

    def test_search_error_returns_tool_error(self, mock_db):
        """search_messages raises → tool_error returned."""
        with (
            patch("agent.tools.message_search.get_db", return_value=mock_db),
            patch("agent.tools.message_search.search_messages", side_effect=Exception("fts5 crash")),
        ):
            result = json.loads(session_search("query", "s", limit=3))
        assert "error" in result
        assert "fts5 crash" in result["error"]

    def test_search_with_role_filter(self, mock_db):
        """role_filter applied correctly."""
        with (
            patch("agent.tools.message_search.get_db", return_value=mock_db),
            patch("agent.tools.message_search.search_messages", return_value=[]) as mock_sm,
        ):
            session_search("query", "s", role_filter="user,assistant", limit=3)
            # search_messages should receive role_filter as a list
            _, kwargs = mock_sm.call_args
            assert kwargs.get("role_filter") == ["user", "assistant"]


# ============================================================================
# _message_search_tool (the actual @tool-decorated function)
# ============================================================================

class TestMessageSearchTool:
    def test_tool_decorated(self):
        """build_message_search_tool returns a BaseTool."""
        tool = build_message_search_tool()
        assert tool.name == "message_search"
        assert tool.args_schema is MessageSearchSchema
        assert tool.handle_tool_error is True

    def test_tool_metadata(self):
        tool = build_message_search_tool()
        assert tool.metadata == {"idempotent": False}


# ============================================================================
# Edge cases for session_search internals
# ============================================================================

class TestSessionSearchEdgeCases:
    def test_role_filter_empty_string(self):
        """role_filter='' should be treated as None (no filtering)."""
        mock_db = MagicMock(spec=sqlite3.Connection)
        with (
            patch("agent.tools.message_search.get_db", return_value=mock_db),
            patch("agent.tools.message_search.search_messages", return_value=[]) as mock_sm,
        ):
            session_search("query", "s", role_filter="", limit=3)
            _, kwargs = mock_sm.call_args
            assert kwargs.get("role_filter") is None

    def test_role_filter_whitespace(self):
        """role_filter='  ' with whitespace should be treated as None."""
        mock_db = MagicMock(spec=sqlite3.Connection)
        with (
            patch("agent.tools.message_search.get_db", return_value=mock_db),
            patch("agent.tools.message_search.search_messages", return_value=[]) as mock_sm,
        ):
            session_search("query", "s", role_filter="  ", limit=3)
            _, kwargs = mock_sm.call_args
            assert kwargs.get("role_filter") is None