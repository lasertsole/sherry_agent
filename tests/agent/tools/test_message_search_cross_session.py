"""Tool-level cross_session propagation and scope documentation."""

import json
from unittest.mock import patch

import pytest

import agent.tools.message_search as ms

pytestmark = [pytest.mark.unit]


@pytest.fixture
def tool_func():
    return ms.build_message_search_tool().func


def test_message_search_tool_cross_session(tool_func):
    """cross_session=True reaches the search layer."""
    with patch.object(ms, "session_search", return_value="{}") as mock_search:
        tool_func(query="bravo", cross_session=True, session_id="current")
    assert mock_search.call_args.args == ("bravo", "current", None, 3, True)


def test_message_search_tool_default_same_session(tool_func):
    """Omitting cross_session keeps the historical current-session behaviour."""
    with patch.object(ms, "session_search", return_value="{}") as mock_search:
        tool_func(query="alpha", session_id="current")
    assert mock_search.call_args.args == ("alpha", "current", None, 3, False)


def test_message_search_tool_semantic_cross_session(tool_func):
    """Semantic cross-session search passes session_id=None to the embedder."""
    with patch.object(ms, "_run_semantic_search", return_value="ok") as mock_sem:
        tool_func(query="alpha", semantic=True, cross_session=True, session_id="current")
    mock_sem.assert_called_once_with("alpha", "current", 3, True)


def test_semantic_search_cross_session():
    """_run_semantic_search maps cross_session to session_id=None."""
    seen: list[str | None] = []

    async def fake_semantic_search(query, session_id=None, limit=5):
        seen.append(session_id)
        return []

    with patch("context_engine.embeddings.semantic_search", new=fake_semantic_search):
        ms._run_semantic_search("alpha", "current", 3, cross_session=True)
        ms._run_semantic_search("alpha", "current", 3)
    assert seen == [None, "current"]


def test_docstring_mentions_cross_session():
    """Tool description and schema describe all three modes accurately."""
    built = ms.build_message_search_tool()
    description = built.description
    assert "THREE MODES" in description
    assert "cross_session=False" in description
    assert "cross_session=True" in description
    assert "CURRENT session" in description
    assert "ALL OTHER sessions" in description
    assert "every past session is searchable" not in description
    assert "The current session is excluded from results" not in description
    field = ms.MessageSearchSchema.model_fields["cross_session"]
    assert field.default is False
    assert "ALL other sessions" in field.description


def test_cross_session_does_not_use_current_session_checkpoint():
    """A cross-session miss must not fall back to the caller's own checkpoint."""
    hit = {"id": None, "session_id": "current", "snippet": "alpha"}

    def fake_checkpoint(*args, **kwargs):
        return [hit]

    with (
        patch.object(ms, "get_db", return_value=object()),
        patch.object(ms, "search_messages", return_value=[]),
        patch.object(ms, "_search_latest_checkpoint_messages", side_effect=fake_checkpoint) as cp,
    ):
        cross = json.loads(ms.session_search("alpha", "current", cross_session=True))
        same = json.loads(ms.session_search("alpha", "current"))

    cp.assert_called_once()
    assert cross["count"] == 0
    assert same["mode"] == "checkpoint_fallback"


def test_collect_tasks_attributes_match_session():
    """Summary tasks carry the session that contained the hit, not the caller's."""
    raw = [{"session_id": "other", "turn_num": 2, "id": 7}]
    with patch.object(
        ms, "get_turns_by_turn_num_scope", return_value=[{"role": "human", "content": "alpha"}]
    ):
        tasks = ms.SessionSearcher().collect_tasks("alpha", "current", raw)
    assert len(tasks) == 1
    assert tasks[0][0] == "other"
