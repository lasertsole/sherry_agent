"""TDD tests for audit issue #20 — FTS5 MATCH abuse → DoS.

Audit finding: queries reach FTS5 MATCH as bound parameters (SQLi-safe) and
``_sanitize_fts5_query`` is best-effort; abusive queries (huge OR chains,
wildcard multiplication, giant tokens) burn CPU in the FTS5 parser/evaluator.

Dependency-config investigation (SQLite 3.53.1, measured): the connection
limits that DO exist do not reach the FTS5 parser —
``SQLITE_LIMIT_EXPR_DEPTH`` and ``SQLITE_LIMIT_LIKE_PATTERN_LENGTH`` leave
MATCH cost unchanged (measured), and ``SQLITE_LIMIT_LENGTH`` is global to
the shared connection (it would break big-content message writes). So the
mitigation is input-side caps in the sanitizer:

1. at most 64 whitespace-separated tokens (OR x20k measured 1.0s of pure
   parse+eval on a 50k-row table; 64 tokens ≈ 1-3ms);
2. each token truncated to 64 chars;
3. at most 4 wildcard (``*``) terms — excess ``*`` stripped so the cost
   multiplier cannot be dialed up.
"""

import sqlite3
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from context_engine.core import _sanitize_fts5_query

pytestmark = [pytest.mark.module, pytest.mark.timeout(120)]


# ---------------------------------------------------------------------------
# Sanitizer caps (pure function)
# ---------------------------------------------------------------------------


class TestSanitizerCaps:
    def test_huge_or_chain_capped_to_64_tokens(self):
        query = " OR ".join(f"tok{i}" for i in range(50_000))

        sanitized = _sanitize_fts5_query(query)

        tokens = sanitized.split()
        assert len(tokens) <= 64
        assert "tok31" in sanitized  # 32 terms + 32 ORs survive the cap
        assert "tok32" not in sanitized

    def test_long_token_truncated_to_64_chars(self):
        giant = "x" * 100_000

        sanitized = _sanitize_fts5_query(giant)

        assert len(sanitized) <= 64

    def test_wildcard_terms_capped_at_four(self):
        query = " ".join(f"abc{i}* def" for i in range(10))

        sanitized = _sanitize_fts5_query(query)

        wildcard_tokens = [t for t in sanitized.split() if "*" in t]
        assert len(wildcard_tokens) == 4
        # excess wildcard markers are stripped, terms survive as exact matches
        assert sanitized.count("*") == 4
        assert "abc9" in sanitized

    def test_normal_query_unchanged(self):
        assert _sanitize_fts5_query("docker kubernetes") == "docker kubernetes"

    def test_quoted_phrase_within_cap_survives(self):
        query = '"New York" OR boston'

        sanitized = _sanitize_fts5_query(query)

        assert '"New York"' in sanitized


# ---------------------------------------------------------------------------
# Integration: pathological query on a real FTS5 table is bounded
# ---------------------------------------------------------------------------


@pytest.fixture()
def big_fts5_db():
    """Real search-shaped store: messages table + FTS5 + trigger, 50k rows."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db = sqlite3.connect(str(Path(tmpdir) / "big.db"))
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.executescript("""
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                turn_num INTEGER NOT NULL,
                session_id TEXT NOT NULL,
                role TEXT NOT NULL,
                content TEXT,
                tool_name TEXT,
                timestamp TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE messages_fts USING fts5(content);
            CREATE TRIGGER messages_fts_insert AFTER INSERT ON messages BEGIN
                INSERT INTO messages_fts(rowid, content)
                VALUES (new.id, COALESCE(new.content, ''));
            END;
        """)
        db.executemany(
            "INSERT INTO messages (turn_num, session_id, role, content, timestamp)"
            " VALUES (?, ?, ?, ?, ?)",
            ((i % 10, "s1", "ai", f"tok{i} alpha beta gamma delta echo folge", "20260905120000")
             for i in range(50_000)),
        )
        db.commit()
        yield db
        db.close()


class TestPathologicalQueryBounded:
    def test_50k_term_query_returns_fast(self, big_fts5_db):
        """A 50k-term OR query must be capped and return in bounded time.

        Uncapped, measured on this store shape: the FTS5 MATCH alone burns
        multiple seconds of CPU per query (49s at 100k terms) — repeatable
        DoS. The cap brings it to milliseconds; the assert carries headroom
        for CI variance.
        """
        query = " OR ".join(f"tok{i}" for i in range(50_000))

        start = time.perf_counter()
        with (
            patch("context_engine.core._db", big_fts5_db),
            patch("context_engine.core._lock", threading.Lock()),
        ):
            from context_engine.core import search_messages

            results = search_messages(query=query, session_id="s1")
        elapsed = time.perf_counter() - start

        assert results, "capped query keeps its first 64 tokens — matches must survive"
        assert all(r["session_id"] == "s1" for r in results)
        assert elapsed < 1.5, f"pathological query took {elapsed:.2f}s — cap not effective"
