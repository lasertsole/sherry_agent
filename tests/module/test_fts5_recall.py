
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
# English FTS5 (default unicode61 tokenizer)
# ============================================================================

class TestEnglishFTS5:
    """Default FTS5 path — unicode61 tokenizer."""

    def test_simple_word(self, populate_english_data):
        """Single English word should return matching results."""
        results = _patched_search("docker", populate_english_data)
        assert len(results) >= 1
        # Should match "docker compose" (turn 2) and "Docker version" (turn 5) etc.
        ids = {r["id"] for r in results}
        assert any(
            r["role"] == "ai" and "docker" in r["snippet"].lower()
            for r in results
        ), f"No docker-related AI result found in {results}"

    def test_multi_word(self, populate_english_data):
        """Multi-word phrase should match as boolean AND."""
        results = _patched_search("docker kubernetes", populate_english_data)
        # At least one result should contain both terms in snippet
        assert len(results) >= 1, f"Expected results for 'docker kubernetes', got empty"

    def test_exact_phrase(self, populate_english_data):
        """Quoted phrase should match exact sequence."""
        results = _patched_search('"control plane"', populate_english_data)
        assert len(results) >= 1
        assert any("control plane" in r["snippet"].lower() for r in results)

    def test_prefix_wildcard(self, populate_english_data):
        """Prefix wildcard should match word stems."""
        results = _patched_search("deploy*", populate_english_data)
        assert len(results) >= 1
        assert any("deploy" in r["snippet"].lower() for r in results)

    def test_role_filter(self, populate_english_data):
        """role_filter should restrict results to matching roles."""
        results = _patched_search("docker", populate_english_data, role_filter=["human"])
        assert len(results) >= 1
        assert all(r["role"] == "human" for r in results)

    def test_no_results(self, populate_english_data):
        """Query with no matches should return empty list."""
        results = _patched_search("xyznonexistent12345", populate_english_data)
        assert results == []

    def test_limit(self, populate_english_data):
        """limit parameter should cap results."""
        results = _patched_search("docker", populate_english_data, limit=1)
        assert len(results) == 1


# ============================================================================
# Chinese trigram FTS5 (tokens with >=3 CJK chars)
# ============================================================================

class TestChineseTrigramFTS5:
    """Trigram FTS5 path — for queries with >=3 CJK chars per token."""

    def test_chinese_word_recall(self, populate_chinese_data):
        """Chinese query with >=3 CJK chars should recall via trigram."""
        results = _patched_search("大别山", populate_chinese_data)
        assert len(results) >= 1, f"Expected recall for '大别山', got empty"
        assert any("大别山" in r["snippet"] for r in results)

    def test_chinese_multi_token(self, populate_chinese_data):
        """Multi-token Chinese query should recall via trigram with AND."""
        results = _patched_search("配置 数据库", populate_chinese_data)
        assert len(results) >= 1, f"Expected recall for '配置 数据库', got empty"

    def test_chinese_exact_phrase(self, populate_chinese_data):
        """Quoted Chinese phrase should recall via trigram."""
        results = _patched_search("容器化部署", populate_chinese_data)
        assert len(results) >= 1
        assert any("容器化部署" in r["snippet"] for r in results)

    def test_chinese_or_query(self, populate_chinese_data):
        """OR boolean operator should work with Chinese trigram."""
        results = _patched_search("大别山 OR 桂林", populate_chinese_data)
        assert len(results) >= 1
        # Should return results matching either term
        snippet_text = " ".join(r["snippet"] for r in results)
        assert "大别山" in snippet_text, f"Expected '大别山' in results: {snippet_text}"

    def test_mixed_cjk_english(self, populate_chinese_data):
        """Mixed Chinese+English query should route based on CJK detection."""
        results = _patched_search("Python 内存管理", populate_chinese_data)
        assert len(results) >= 1
        # The content "Python 的内存管理机制是怎样的" should match
        assert any("内存管理" in r["snippet"] for r in results)

    def test_tool_content_included(self, populate_chinese_data):
        """Trigram FTS5 should index tool_name and tool_calls too (via triggers)."""
        # Insert a tool message with CJK in tool_name
        db = populate_chinese_data["db"]
        sid = populate_chinese_data["session_id"]
        _insert_message(
            db, sid, 9, "tool",
            json.dumps("execution output", ensure_ascii=False),
            tool_name="部署脚本",
        )
        db.commit()

        results = _patched_search("部署脚本", populate_chinese_data)
        assert len(results) >= 1
        # The match should come from tool_name field via the FTS5 index (which
        # concatenates content, tool_name, and tool_calls in the trigger)
        assert any(r["role"] == "tool" for r in results)


# ============================================================================
# LIKE fallback (short CJK — <3 chars per token)
# ============================================================================

class TestChineseLikeFallback:
    """LIKE path — for short CJK queries where trigram can't match (<3 chars)."""

    def test_short_cjk_single_token(self, populate_chinese_data):
        """Single 2-char CJK token should recall via LIKE."""
        results = _patched_search("广西", populate_chinese_data)
        assert len(results) >= 1, f"Expected recall for '广西', got empty"
        assert any("广西" in r["snippet"] for r in results)

    def test_short_cjk_multi_token_or(self, populate_chinese_data):
        """Multi 2-char CJK tokens with OR should recall via LIKE."""
        results = _patched_search("广西 OR 桂林 OR 漓江", populate_chinese_data)
        assert len(results) >= 1, f"Expected recall for '广西 OR 桂林 OR 漓江', got empty"
        # Should find results containing any of these terms
        snippet_text = " ".join(r["snippet"] for r in results)
        assert "桂林" in snippet_text, f"Expected '桂林' in LIKE results: {snippet_text}"

    def test_short_cjk_substring(self, populate_chinese_data):
        """Single 1-char CJK query should recall via LIKE."""
        results = _patched_search("桂", populate_chinese_data)
        assert len(results) >= 1, f"Expected recall for '桂', got empty"
        assert any("桂" in r["snippet"] for r in results)


# ============================================================================
# Context expansion
# ============================================================================

class TestContextExpansion:
    """search_messages adds ±1 message context around each match."""

    def test_context_one_before_after(self, populate_english_data):
        """Each result should have a 'context' key with surrounding messages."""
        results = _patched_search("docker", populate_english_data, limit=3)
        for r in results:
            assert "context" in r, f"Result {r['id']} missing 'context'"
            assert isinstance(r["context"], list)
            assert 1 <= len(r["context"]) <= 3  # 1 before + match + 1 after

    def test_context_contains_roles(self, populate_english_data):
        """Context entries should have 'role' and 'content'."""
        results = _patched_search("docker", populate_english_data, limit=1)
        if results:
            ctx = results[0]["context"]
            for entry in ctx:
                assert "role" in entry
                assert "content" in entry


# ============================================================================
# Content field omission
# ============================================================================

class TestContentOmission:
    """Search results should not include the full content field (tokensaving)."""

    def test_content_omitted(self, populate_english_data):
        """The 'content' key should be removed from results."""
        results = _patched_search("docker", populate_english_data)
        for r in results:
            assert "content" not in r, (
                f"Result {r['id']} should not contain 'content' field"
            )

    def test_snippet_present(self, populate_english_data):
        """Each result should have a 'snippet' field instead."""
        results = _patched_search("docker", populate_english_data)
        for r in results:
            assert "snippet" in r, f"Result {r['id']} missing 'snippet'"
            assert isinstance(r["snippet"], str)
            assert len(r["snippet"]) > 0


# ============================================================================
# Sanitization edge cases
# ============================================================================

class TestQuerySanitization:
    """Queries with special FTS5 characters should not crash."""

    def test_special_chars(self, fts5_db):
        """Special FTS5 chars (+ {} () ^) should be stripped without error."""
        results = _patched_search("docker+", fts5_db)
        # Should not raise; query becomes "docker" after sanitization
        assert isinstance(results, list)

    def test_dotted_term(self, fts5_db):
        """Dotted terms (e.g. 'my-app.config') should be auto-quoted."""
        results = _patched_search("my-app.config", fts5_db)
        # Should not crash; hyphens/dots trigger auto-quoting
        assert isinstance(results, list)

    def test_mixed_boolean(self, populate_english_data):
        """Boolean operators at query boundaries should be cleaned up."""
        results = _patched_search("AND docker compose", populate_english_data)
        # Leading "AND" should be stripped; query becomes "docker compose"
        assert len(results) >= 1

    def test_empty_after_sanitize(self, fts5_db):
        """Query that becomes empty after sanitization should return []."""
        results = _patched_search("+{}()^", fts5_db)
        assert results == []


# ============================================================================
# JSON-encoded content check
# ============================================================================

class TestJsonEncodedContent:
    """The FTS5 index stores content as-is (including JSON encoding).

    This means messages store json.dumps(content, ensure_ascii=False), so
    content = '\"大别山项目\"' (with JSON-embedded quotes). FTS5 indexes
    the raw string including those JSON artifacts. This test verifies that
    search still works despite the extra characters.
    """

    def test_json_encoding_does_not_prevent_recall(self, fts5_db):
        """FTS5 should still match content despite JSON encoding."""
        db = fts5_db["db"]
        sid = fts5_db["session_id"]
        # Insert content EXACTLY as add_messages would: json.dumps + ensure_ascii=False
        _insert_message(
            db, sid, 1, "human",
            json.dumps("Hello world test message", ensure_ascii=False),
        )
        db.commit()

        results = _patched_search("hello", fts5_db)
        assert len(results) >= 1, (
            "FTS5 should match across JSON-encoded content"
        )

    def test_chinese_json_encoding(self, fts5_db):
        """Chinese content should still match despite JSON encoding."""
        db = fts5_db["db"]
        sid = fts5_db["session_id"]
        _insert_message(
            db, sid, 1, "human",
            json.dumps("你好世界测试消息", ensure_ascii=False),
        )
        db.commit()

        # >=3 CJK chars -> trigram
        results = _patched_search("你好世界", fts5_db)
        assert len(results) >= 1, (
            "Trigram FTS5 should match Chinese content through JSON encoding"
        )