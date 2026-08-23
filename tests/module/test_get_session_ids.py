"""Tests for ``context_engine.store.core.get_session_ids``.

Verifies the session-list enumeration logic against a real SQLite database:

- Title derivation uses the **latest human** message (not any other role).
- An empty/no-usable-text title stays ``""`` (the client renders an i18n
  placeholder rather than leaking the raw session_id).
- Subagent sessions (``agent:<agent_id>:subagent:`` hierarchy) are excluded.
- Rows are ordered newest-activity first.
"""

import json
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest


# ============================================================================
# Fixture: real SQLite DB (only the columns get_session_ids reads)
# ============================================================================

@pytest.fixture
def sid_db():
    """Create a real SQLite DB with the ``messages`` table, patch module ``_db``.

    Yields a dict with:
    - db: sqlite3.Connection (row_factory = sqlite3.Row)
    - insert: helper to insert a message row
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_sessions.db"
        db = sqlite3.connect(str(db_path), check_same_thread=False, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.executescript("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_num INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_call_id TEXT,
                tool_calls TEXT,
                tool_status TEXT,
                tool_name TEXT,
                timestamp TEXT NOT NULL,
                finish_reason TEXT,
                reasoning TEXT,
                reasoning_content TEXT
            );
        """)

        def insert(
            session_id: str,
            turn_num: int,
            role: str,
            content: str,
            timestamp: str,
        ) -> int:
            cur = db.execute(
                """INSERT INTO messages (
                    session_id, turn_num, role, content, timestamp
                ) VALUES (?, ?, ?, ?, ?)""",
                (session_id, turn_num, role, content, timestamp),
            )
            return cur.lastrowid

        yield {"db": db, "insert": insert}

        db.close()


@pytest.fixture
def patch_db(sid_db):
    """Context manager patching ``context_engine.store.core._db``."""
    with patch("context_engine.store.core._db", sid_db["db"]):
        yield


# ============================================================================
# Tests
# ============================================================================

class TestGetSessionIds:
    def test_title_uses_latest_human_message(self, sid_db, patch_db):
        """Title should be the latest human message, ignoring newer AI/tool rows."""
        from context_engine.store.core import get_session_ids

        ins = sid_db["insert"]
        ins("s1", 1, "human", json.dumps("first question", ensure_ascii=False), "20260101100000")
        ins("s1", 1, "ai", json.dumps("first answer", ensure_ascii=False), "20260101100000")
        # Newer turn: a human message and an even newer AI message.
        ins("s1", 2, "human", json.dumps("latest question", ensure_ascii=False), "20260101110000")
        ins("s1", 2, "ai", json.dumps("latest answer", ensure_ascii=False), "20260101120000")

        result = get_session_ids()
        assert len(result) == 1
        assert result[0]["session_id"] == "s1"
        assert result[0]["title"] == "latest question"

    def test_title_ignores_ai_message_after_human_in_same_turn(
        self, sid_db, patch_db
    ):
        """A newer AI message in the last turn must NOT override the human title."""
        from context_engine.store.core import get_session_ids

        ins = sid_db["insert"]
        ins("s1", 1, "human", json.dumps("question", ensure_ascii=False), "20260101100000")
        ins("s1", 1, "ai", json.dumps("answer", ensure_ascii=False), "20260101110000")

        result = get_session_ids()
        assert result[0]["title"] == "question"

    def test_empty_title_when_last_human_has_no_usable_text(
        self, sid_db, patch_db
    ):
        """A last human message with no usable text yields an empty title ("")."""
        from context_engine.store.core import get_session_ids

        ins = sid_db["insert"]
        # Human message is a multimodal array with only an image, no text.
        ins(
            "s1", 1, "human",
            json.dumps([{"type": "image", "image": "x.png"}], ensure_ascii=False),
            "20260101100000",
        )
        ins("s1", 1, "ai", json.dumps("answer", ensure_ascii=False), "20260101110000")

        result = get_session_ids()
        assert result[0]["session_id"] == "s1"
        assert result[0]["title"] == ""

    def test_empty_title_when_content_null(self, sid_db, patch_db):
        """Null content on the last human message yields an empty title."""
        from context_engine.store.core import get_session_ids

        ins = sid_db["insert"]
        ins("s1", 1, "human", None, "20260101100000")
        ins("s1", 1, "ai", json.dumps("answer", ensure_ascii=False), "20260101110000")

        result = get_session_ids()
        assert result[0]["title"] == ""

    def test_title_uses_multimodal_text_segment(self, sid_db, patch_db):
        """A multimodal last human message uses its first text segment."""
        from context_engine.store.core import get_session_ids

        ins = sid_db["insert"]
        ins(
            "s1", 1, "human",
            json.dumps(
                [{"type": "text", "text": "  describe this image  "}, {"type": "image", "image": "x.png"}],
                ensure_ascii=False,
            ),
            "20260101100000",
        )
        ins("s1", 1, "ai", json.dumps("answer", ensure_ascii=False), "20260101110000")

        result = get_session_ids()
        assert result[0]["title"] == "describe this image"

    def test_subagent_sessions_excluded(self, sid_db, patch_db):
        """agent:...:subagent:... sessions must not appear in the list."""
        from context_engine.store.core import get_session_ids

        ins = sid_db["insert"]
        ins("main-user-session", 1, "human", json.dumps("hi", ensure_ascii=False), "20260101100000")
        ins("agent:main:subagent::abc123", 1, "human", json.dumps("sub", ensure_ascii=False), "20260101110000")
        ins("agent:main:subagent::mother:subagent::child42", 1, "human", json.dumps("sub2", ensure_ascii=False), "20260101120000")

        result = get_session_ids()
        sessions = {r["session_id"] for r in result}
        assert sessions == {"main-user-session"}

    def test_ordered_newest_activity_first(self, sid_db, patch_db):
        """Sessions should be ordered by most recent activity first."""
        from context_engine.store.core import get_session_ids

        ins = sid_db["insert"]
        ins("s1", 1, "human", json.dumps("old", ensure_ascii=False), "20260101100000")
        ins("s2", 1, "human", json.dumps("newer", ensure_ascii=False), "20260101150000")
        ins("s3", 1, "human", json.dumps("newest", ensure_ascii=False), "20260101200000")

        result = get_session_ids()
        assert [r["session_id"] for r in result] == ["s3", "s2", "s1"]

    def test_empty_db_returns_empty_list(self, sid_db, patch_db):
        """No messages at all -> empty list."""
        from context_engine.store.core import get_session_ids

        result = get_session_ids()
        assert result == []
