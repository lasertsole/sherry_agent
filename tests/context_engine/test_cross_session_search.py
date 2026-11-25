"""Cross-session scope for context_engine.search_messages (FTS / trigram / LIKE)."""

import sqlite3
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

pytestmark = [pytest.mark.module]


CURRENT = "session-current"
OTHER = "session-other"


@pytest.fixture
def cross_session_db():
    """Two sessions with distinct searchable phrases in both FTS5 tables."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = sqlite3.connect(
            str(Path(tmpdir) / "cross.db"),
            check_same_thread=False,
            timeout=1.0,
            isolation_level=None,
        )
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
            CREATE VIRTUAL TABLE messages_fts USING fts5(content);
            CREATE VIRTUAL TABLE messages_fts_trigram USING fts5(
                content, tokenize='trigram'
            );
        """)
        for table in ("messages_fts", "messages_fts_trigram"):
            db.executescript(f"""
                CREATE TRIGGER {table}_insert AFTER INSERT ON messages BEGIN
                    INSERT INTO {table}(rowid, content)
                    VALUES (new.id, COALESCE(new.content, ''));
                END;
                CREATE TRIGGER {table}_delete AFTER DELETE ON messages BEGIN
                    DELETE FROM {table} WHERE rowid = old.id;
                END;
                CREATE TRIGGER {table}_update AFTER UPDATE ON messages BEGIN
                    DELETE FROM {table} WHERE rowid = old.id;
                    INSERT INTO {table}(rowid, content)
                    VALUES (new.id, COALESCE(new.content, ''));
                END;
            """)

        rows = [
            (CURRENT, "herecurrenttoken alphaonlyunique elsewhere"),
            (CURRENT, "本会话专属内容 当前会话"),
            (OTHER, "thereothertoken bravoonlyunique hereother"),
            (OTHER, "跨会话测试内容 另一会话"),
            (OTHER, "短词跨会匹配"),
        ]
        for idx, (session_id, content) in enumerate(rows, start=1):
            db.execute(
                """INSERT INTO messages (
                    turn_num, session_id, role, content, timestamp
                ) VALUES (?, ?, ?, ?, ?)""",
                (idx, session_id, "human", content, f"20261123{idx:04d}"),
            )
        yield db
        db.close()


def _search(db, query, cross_session=False, session_id=CURRENT, **kwargs):
    from context_engine.core import search_messages

    with (
        patch("context_engine.core._db", db),
        patch("context_engine.core._lock", threading.Lock()),
    ):
        return search_messages(
            query=query,
            session_id=session_id,
            cross_session=cross_session,
            **kwargs,
        )


def test_search_messages_cross_session_fts(cross_session_db):
    """English FTS: cross_session=True finds the other session only."""
    hits = _search(cross_session_db, "bravoonlyunique", cross_session=True)
    assert len(hits) == 1
    assert hits[0]["session_id"] == OTHER


def test_search_messages_same_session_default(cross_session_db):
    """Default scope stays on the current session; no silent scope widening."""
    assert _search(cross_session_db, "alphaonlyunique")[0]["session_id"] == CURRENT
    assert _search(cross_session_db, "alphaonlyunique", cross_session=True) == []


def test_search_messages_cross_session_trigram(cross_session_db):
    """CJK >=3 chars routes to trigram and honours cross_session."""
    assert _search(cross_session_db, "跨会话") == []
    hits = _search(cross_session_db, "跨会话", cross_session=True)
    assert len(hits) == 1
    assert hits[0]["session_id"] == OTHER


def test_search_messages_cross_session_like(cross_session_db):
    """Short CJK query routes to LIKE and honours cross_session."""
    hits = _search(cross_session_db, "跨会", cross_session=True)
    assert len(hits) >= 1
    assert all(hit["session_id"] == OTHER for hit in hits)
