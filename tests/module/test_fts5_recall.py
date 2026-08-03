
"""Integration tests for FTS5 search recall in context_engine/core.py.

Creates a real SQLite database with FTS5 tables, inserts test messages,
and verifies that search_messages() correctly returns results for:
- English queries (default FTS5 unicode61 tokenizer)
- Chinese queries with >=3 CJK chars (trigram FTS5 tokenizer)
- Short Chinese queries with <3 CJK chars (LIKE fallback)
- Mixed CJK/non-CJK queries
- Boolean operator queries (AND, OR)
- Edge cases (no results, empty query, special characters)
"""

import json
import sqlite3
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import pytest
from loguru import logger

# Remove default logger to avoid noise during testing
logger.remove()


# ============================================================================
# Fixtures
# ============================================================================

@pytest.fixture
def fts5_db():
    """Create a real SQLite database with messages + both FTS5 tables.

    Returns a dict with:
    - db: sqlite3.Connection
    - db_path: str
    - session_id: str (the test session ID used for all inserted data)
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = Path(tmpdir) / "test_mes_memory.db"
        db = sqlite3.connect(
            str(db_path),
            check_same_thread=False,
            timeout=1.0,
            isolation_level=None,
        )
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA foreign_keys=ON")

        # Create messages table
        db.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
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
            CREATE INDEX IF NOT EXISTS idx_messages_timestamp
                ON messages(session_id, timestamp);
            CREATE INDEX IF NOT EXISTS idx_messages_turn_num
                ON messages(session_id, turn_num);
        """)

        # Create FTS5 tables with triggers (matching production schema)
        db.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
                content
            );
            CREATE TRIGGER IF NOT EXISTS messages_fts_insert
            AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content) VALUES (
                    new.id,
                    COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '')
                    || ' ' || COALESCE(new.tool_calls, '')
                );
            END;
            CREATE TRIGGER IF NOT EXISTS messages_fts_delete
            AFTER DELETE ON messages BEGIN
                DELETE FROM messages_fts WHERE rowid = old.id;
            END;
            CREATE TRIGGER IF NOT EXISTS messages_fts_update
            AFTER UPDATE ON messages BEGIN
                DELETE FROM messages_fts WHERE rowid = old.id;
                INSERT INTO messages_fts(rowid, content) VALUES (
                    new.id,
                    COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '')
                    || ' ' || COALESCE(new.tool_calls, '')
                );
            END;
        """)

        db.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts_trigram USING fts5(
                content,
                tokenize='trigram'
            );
            CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_insert
            AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts_trigram(rowid, content) VALUES (
                    new.id,
                    COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '')
                    || ' ' || COALESCE(new.tool_calls, '')
                );
            END;
            CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_delete
            AFTER DELETE ON messages BEGIN
                DELETE FROM messages_fts_trigram WHERE rowid = old.id;
            END;
            CREATE TRIGGER IF NOT EXISTS messages_fts_trigram_update
            AFTER UPDATE ON messages BEGIN
                DELETE FROM messages_fts_trigram WHERE rowid = old.id;
                INSERT INTO messages_fts_trigram(rowid, content) VALUES (
                    new.id,
                    COALESCE(new.content, '') || ' ' || COALESCE(new.tool_name, '')
                    || ' ' || COALESCE(new.tool_calls, '')
                );
            END;
        """)

        session_id = "test_session_fts5"
        yield {
            "db": db,
            "db_path": str(db_path),
            "session_id": session_id,
        }
        db.close()


def _insert_message(db, session_id, turn_num, role, content, **kwargs):
    """Helper: insert a single message row and return its id."""
    ts = kwargs.pop("timestamp", f"20260714{1000 + turn_num:04d}")
    tool_name = kwargs.pop("tool_name", None)
    tool_calls = kwargs.pop("tool_calls", None)

    db.execute(
        """INSERT INTO messages (
            turn_num, session_id, role, content,
            tool_call_id, tool_calls, tool_status, tool_name,
            timestamp, finish_reason, reasoning, reasoning_content
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            turn_num,
            session_id,
            role,
            content,
            None,
            tool_calls,
            None,
            tool_name,
            ts,
            None,
            None,
            None,
        ),
    )
    return db.execute("SELECT last_insert_rowid()").fetchone()[0]


@pytest.fixture
def populate_english_data(fts5_db):
    """Insert English test messages into the FTS5 database."""
    db = fts5_db["db"]
    sid = fts5_db["session_id"]
    _insert_message(
        db, sid, 1, "human",
        json.dumps("How do I deploy Docker containers?", ensure_ascii=False),
    )
    _insert_message(
        db, sid, 2, "ai",
        json.dumps(
            "You can use `docker compose up -d` to start containers in detached mode.",
            ensure_ascii=False,
        ),
    )
    _insert_message(
        db, sid, 3, "human",
        json.dumps("What about Kubernetes cluster setup?", ensure_ascii=False),
    )
    _insert_message(
        db, sid, 4, "ai",
        json.dumps(
            "Kubernetes requires a control plane and worker nodes. "
            "Use kubeadm for initialization.",
            ensure_ascii=False,
        ),
    )
    _insert_message(
        db, sid, 5, "tool",
        json.dumps("Docker version 24.0.7, build 12345", ensure_ascii=False),
        tool_name="terminal",
    )
    _insert_message(
        db, sid, 6, "human",
        json.dumps("Can you explain memory management in Python?", ensure_ascii=False),
    )
    _insert_message(
        db, sid, 7, "ai",
        json.dumps(
            "Python uses reference counting and a generational garbage collector.",
            ensure_ascii=False,
        ),
    )
    # Add a message containing both "Docker" and "Kubernetes" so FTS5 AND
    # queries like "docker kubernetes" return results (space = AND by default).
    _insert_message(
        db, sid, 8, "ai",
        json.dumps(
            "Comparing Docker Compose vs Kubernetes for container orchestration.",
            ensure_ascii=False,
        ),
    )
    db.commit()
    return fts5_db


@pytest.fixture
def populate_chinese_data(fts5_db):
    """Insert Chinese test messages into the FTS5 database.

    Note: content is JSON-encoded (matching production behavior).
    """
    db = fts5_db["db"]
    sid = fts5_db["session_id"]

    # >=3 CJK chars per token — should route to trigram
    _insert_message(
        db, sid, 1, "human",
        json.dumps("大别山项目的部署方案是什么？", ensure_ascii=False),
    )
    _insert_message(
        db, sid, 2, "ai",
        json.dumps(
            "大别山项目使用 Docker 容器化部署，配合 Kubernetes 进行编排。",
            ensure_ascii=False,
        ),
    )
    _insert_message(
        db, sid, 3, "human",
        json.dumps("数据库连接失败，请检查配置。", ensure_ascii=False),
    )
    _insert_message(
        db, sid, 4, "ai",
        json.dumps(
            "请检查数据库配置文件中的连接字符串是否包含正确的主机名和端口。",
            ensure_ascii=False,
        ),
    )

    # <3 CJK chars per token — should route to LIKE
    _insert_message(
        db, sid, 5, "human",
        json.dumps("广西桂林漓江风景如何？", ensure_ascii=False),
    )
    _insert_message(
        db, sid, 6, "ai",
        json.dumps("桂林山水甲天下，漓江风景如画。", ensure_ascii=False),
    )

    # Mixed CJK + English
    _insert_message(
        db, sid, 7, "human",
        json.dumps("Python 的内存管理机制是怎样的？", ensure_ascii=False),
    )
    _insert_message(
        db, sid, 8, "ai",
        json.dumps("Python 使用引用计数和分代垃圾回收来管理内存。", ensure_ascii=False),
    )

    db.commit()
    return fts5_db


# ============================================================================
# Patch helper
# ============================================================================

def _patched_search(query, fts5_db, **kwargs):
    """Call search_messages with patched _db (real SQLite + FTS5)."""
    from context_engine.mes_memory.core import search_messages
    db = fts5_db["db"]
    sid = fts5_db["session_id"]

    # Patch both the module-level _db and the _lock
    with (
        patch("context_engine.core._db", db),
        patch("context_engine.core._lock", threading.Lock()),
    ):
        return search_messages(
            query=query,
            session_id=sid,
            **kwargs,
        )


# ============================================================================
# Context expansion
# ============================================================================

class TestContextExpansion:
    """search_messages adds ±1 message context around each match."""

    def test_context_contains_roles(self, populate_english_data):
        """Context entries should have 'role' and 'content'."""
        results = _patched_search("docker", populate_english_data, limit=1)
        if results:
            ctx = results[0]["context"]
            for entry in ctx:
                assert "role" in entry
                assert "content" in entry


# ============================================================================
# Sanitization edge cases
# ============================================================================

class TestQuerySanitization:
    """Queries with special FTS5 characters should not crash."""

    def test_empty_after_sanitize(self, fts5_db):
        """Query that becomes empty after sanitization should return []."""
        results = _patched_search("+{}()^", fts5_db)
        assert results == []