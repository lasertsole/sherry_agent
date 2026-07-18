"""Tests for xp_graph.export_skill — _clean_dir, export_all_skills, export_all_communities.

Patches SKILLS_DIR → tempdir via unit_test_config fixture.
Patches get_db() to return an in-memory SQLite database with migrations applied.
"""

import json
import time
import sqlite3
import itertools
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from context_engine.xp_graph.export_skill import (
    _clean_dir,
    _build_skill_md,
    export_all_skills,
    export_all_communities,
    AUTO_SINGLE_DIR,
    AUTO_COMMUNITY_DIR,
)
from context_engine.xp_graph.type import GmNode, GmEdge, NodeType


# ─── Helpers ──────────────────────────────────────────────────────────


def _in_memory_db() -> sqlite3.Connection:
    """Create an in-memory SQLite DB with full xp_graph schema + migrations."""
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row

    # Core tables (same as m1_core in db.py)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS gm_nodes (
            id              TEXT PRIMARY KEY,
            type            TEXT NOT NULL CHECK(type IN ('TASK','SKILL','EVENT')),
            name            TEXT NOT NULL,
            description     TEXT NOT NULL DEFAULT '',
            content         TEXT NOT NULL,
            validated_count INTEGER NOT NULL DEFAULT 1,
            source_sessions TEXT NOT NULL DEFAULT '[]',
            community_id    TEXT,
            pagerank        REAL NOT NULL DEFAULT 0,
            created_at      INTEGER NOT NULL,
            updated_at      INTEGER NOT NULL
        );
        CREATE UNIQUE INDEX IF NOT EXISTS ux_gm_nodes_name ON gm_nodes(name);

        CREATE TABLE IF NOT EXISTS gm_edges (
            id          TEXT PRIMARY KEY,
            from_id     TEXT NOT NULL REFERENCES gm_nodes(id),
            to_id       TEXT NOT NULL REFERENCES gm_nodes(id),
            type        TEXT NOT NULL CHECK(type IN ('USED_SKILL','SOLVED_BY','REQUIRES','PATCHES','CONFLICTS_WITH')),
            instruction TEXT NOT NULL,
            condition   TEXT,
            session_id  TEXT NOT NULL,
            created_at  INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS gm_communities (
            id          TEXT PRIMARY KEY,
            summary     TEXT NOT NULL,
            node_count  INTEGER NOT NULL DEFAULT 0,
            node_ids_snapshot    TEXT NOT NULL DEFAULT '[]',
            embedding   BLOB,
            created_at  INTEGER NOT NULL,
            updated_at  INTEGER NOT NULL
        );
    """)
    return conn


_counter = itertools.count()


def _insert_skill(db: sqlite3.Connection, name: str, content: str = "",
                  description: str = "", community_id: str | None = None) -> str:
    """Insert a SKILL node and return its id."""
    seq = next(_counter)
    now = int(time.time() * 1000)
    node_id = f"n-{now}-{seq}"
    db.execute("""
        INSERT INTO gm_nodes (id, type, name, description, content, validated_count,
                              source_sessions, community_id, pagerank, created_at, updated_at)
        VALUES (?, 'SKILL', ?, ?, ?, 1, '["s1"]', ?, 0, ?, ?)
    """, (node_id, name, description, content, community_id, now, now))
    db.commit()
    return node_id


def _insert_community(db: sqlite3.Connection, cid: str, summary: str = "") -> None:
    """Insert a community row."""
    now = int(time.time() * 1000)
    db.execute("""
        INSERT INTO gm_communities (id, summary, node_count, node_ids_snapshot, created_at, updated_at)
        VALUES (?, ?, 0, '[]', ?, ?)
    """, (cid, summary, now, now))
    db.commit()


def _insert_edge(db: sqlite3.Connection, from_id: str, to_id: str,
                 etype: str = "USED_SKILL") -> str:
    """Insert an edge and return its id."""
    seq = next(_counter)
    now = int(time.time() * 1000)
    edge_id = f"e-{now}-{seq}"
    db.execute("""
        INSERT INTO gm_edges (id, from_id, to_id, type, instruction, session_id, created_at)
        VALUES (?, ?, ?, ?, '', 's1', ?)
    """, (edge_id, from_id, to_id, etype, now))
    db.commit()
    return edge_id


# ─── Fixtures ─────────────────────────────────────────────────────────


@pytest.fixture
def mem_db():
    """Provide an in-memory xp_graph DB."""
    db = _in_memory_db()
    yield db
    db.close()


@pytest.fixture(autouse=True)
def export_skill_env(unit_test_config, mem_db):
    """Patch export_skill module for testing.

    Strategy:
      1. Reload the module FIRST (outside any mock) so AUTO_SINGLE_DIR etc. pick
         up the patched SKILLS_DIR from unit_test_config, and get_db gets a fresh
         reference that we can patch.
      2. Then patch get_db within a with-block so it returns the in-memory DB.
      3. After the test, reload again to restore the unpatched module.
    """
    import importlib
    from context_engine.xp_graph import export_skill

    # Step 1: reload so module-level path constants reflect patched SKILLS_DIR
    importlib.reload(export_skill)

    # Now re-pin our test-module-level constants to the reloaded module
    global AUTO_SINGLE_DIR, AUTO_COMMUNITY_DIR
    from context_engine.xp_graph.export_skill import (
        AUTO_SINGLE_DIR,
        AUTO_COMMUNITY_DIR,
    )

    # Step 2: apply the DB patch
    with patch("context_engine.xp_graph.export_skill.get_db", return_value=mem_db):
        yield unit_test_config   # test body runs here

    # After test: reload to restore unpatched state
    importlib.reload(export_skill)
    global AUTO_SINGLE_DIR, AUTO_COMMUNITY_DIR
    from context_engine.xp_graph.export_skill import (
        AUTO_SINGLE_DIR,
        AUTO_COMMUNITY_DIR,
    )


# ─── Tests: _clean_dir ────────────────────────────────────────────────


class TestCleanDir:
    """Tests for the _clean_dir helper."""

    def test_clean_nonexistent_dir(self, export_skill_env):
        """Cleaning a non-existent directory creates it."""
        d = export_skill_env / "new_dir"
        assert not d.exists()
        _clean_dir(d)
        assert d.exists()
        assert d.is_dir()

    def test_clean_existing_dir_removes_contents(self, export_skill_env):
        """Cleaning a directory with files removes them."""
        d = export_skill_env / "dir_with_files"
        d.mkdir(parents=True)
        (d / "file1.txt").write_text("hello")
        (d / "sub").mkdir()
        (d / "sub" / "nested.txt").write_text("nested")

        _clean_dir(d)

        assert d.exists()
        assert not (d / "file1.txt").exists()
        assert not (d / "sub").exists()

    def test_clean_twice_is_idempotent(self, export_skill_env):
        """Cleaning an already-clean directory works."""
        d = export_skill_env / "empty_dir"
        d.mkdir(parents=True)
        _clean_dir(d)  # first
        assert d.exists()
        _clean_dir(d)  # second
        assert d.exists()


# ─── Tests: _build_skill_md ───────────────────────────────────────────


class TestBuildSkillMd:
    """Tests for _build_skill_md internal function."""

    def test_basic_skill_no_relations(self):
        """A skill with no edges produces valid frontmatter + body + meta comment."""
        skill = GmNode(
            id="n-1", name="test-skill", type=NodeType.SKILL,
            description="A test skill", content="This is the body.",
            validated_count=1, source_sessions=["s1"],
            community_id=None, pagerank=0.0,
            created_at=1000, updated_at=1000,
        )
        md = _build_skill_md(skill, {}, {}, {})

        assert "---\n" in md
        assert "name: test-skill" in md
        assert "description: A test skill" in md
        assert "This is the body." in md
        # No Relations section
        assert "## Relations" not in md
        # Meta comment
        assert "<!--" in md
        assert "validated_count: 1" in md

    def test_skill_with_outgoing_edge(self):
        """An outgoing USED_SKILL edge appears in Relations."""
        skill = GmNode(
            id="n-1", name="source-skill", type=NodeType.SKILL,
            description="Source", content="Body",
            validated_count=1, source_sessions=[], community_id=None,
            pagerank=0.0, created_at=1000, updated_at=1000,
        )
        peer = GmNode(
            id="n-2", name="target-skill", type=NodeType.SKILL,
            description="Target desc", content="...",
            validated_count=1, source_sessions=[], community_id=None,
            pagerank=0.0, created_at=1000, updated_at=1000,
        )
        edge = GmEdge(
            id="e-1", from_id="n-1", to_id="n-2", type="USED_SKILL",
            instruction="use it like this", condition="when needed",
            session_id="s1", created_at=1000,
        )

        nodes_by_id = {"n-1": skill, "n-2": peer}
        edges_by_from = {"n-1": [edge]}
        edges_by_to = {}

        md = _build_skill_md(skill, nodes_by_id, edges_by_from, edges_by_to)

        assert "## Relations" in md
        assert "USED_SKILL" in md
        assert "→" in md  # outgoing arrow
        assert "target-skill" in md
        assert "Target desc" in md
        assert "use it like this" in md
        assert "when needed" in md

    def test_skill_with_incoming_edge(self):
        """An incoming edge uses the ← arrow."""
        skill = GmNode(
            id="n-2", name="target-skill", type=NodeType.SKILL,
            description="Target", content="Body",
            validated_count=1, source_sessions=[], community_id=None,
            pagerank=0.0, created_at=1000, updated_at=1000,
        )
        peer = GmNode(
            id="n-1", name="source-skill", type=NodeType.SKILL,
            description="Source desc", content="...",
            validated_count=1, source_sessions=[], community_id=None,
            pagerank=0.0, created_at=1000, updated_at=1000,
        )
        edge = GmEdge(
            id="e-1", from_id="n-1", to_id="n-2", type="REQUIRES",
            instruction="depends on", condition=None,
            session_id="s1", created_at=1000,
        )

        nodes_by_id = {"n-1": peer, "n-2": skill}
        edges_by_from = {}
        edges_by_to = {"n-2": [edge]}

        md = _build_skill_md(skill, nodes_by_id, edges_by_from, edges_by_to)

        assert "## Relations" in md
        assert "REQUIRES" in md
        assert "←" in md  # incoming arrow
        assert "source-skill" in md


# ─── Tests: export_all_skills ─────────────────────────────────────────


class TestExportAllSkills:
    """Tests for export_all_skills()."""

    def test_no_skills_returns_empty(self, export_skill_env):
        """No SKILL nodes → empty list; _clean_dir is NOT called so dir may not exist."""
        result = export_all_skills()
        assert result == []

    def test_exports_single_skill(self, export_skill_env, mem_db):
        """Single SKILL node generates one file."""
        _insert_skill(mem_db, "my-skill", content="My content", description="My skill desc")

        result = export_all_skills()

        assert len(result) == 1
        md_path = result[0]
        assert md_path.exists()
        text = md_path.read_text(encoding="utf-8")
        assert "name: my-skill" in text
        assert "description: My skill desc" in text
        assert "My content" in text

    def test_exports_multiple_skills(self, export_skill_env, mem_db):
        """Multiple SKILL nodes produce separate directories."""
        _insert_skill(mem_db, "skill-a", content="A", description="Skill A")
        _insert_skill(mem_db, "skill-b", content="B", description="Skill B")

        result = export_all_skills()

        assert len(result) == 2
        names = {p.parent.name for p in result}
        assert "skill-a" in names
        assert "skill-b" in names

    def test_clean_dir_before_export(self, export_skill_env, mem_db):
        """Previous contents of AUTO_SINGLE_DIR are removed before export."""
        # Create stale content
        stale_dir = AUTO_SINGLE_DIR / "stale"
        stale_dir.mkdir(parents=True)
        (stale_dir / "SKILL.md").write_text("stale")

        _insert_skill(mem_db, "fresh-skill", content="Fresh")

        result = export_all_skills()

        assert len(result) == 1
        assert not (AUTO_SINGLE_DIR / "stale").exists()
        assert (AUTO_SINGLE_DIR / "fresh-skill" / "SKILL.md").exists()

    def test_export_with_relations(self, export_skill_env, mem_db):
        """Edges between skills appear in the Relations section."""
        id1 = _insert_skill(mem_db, "parent", content="Parent body", description="Parent desc")
        id2 = _insert_skill(mem_db, "child", content="Child body", description="Child desc")
        _insert_edge(mem_db, id1, id2, "USED_SKILL")

        result = export_all_skills()

        # Find the parent file — should have Relations section
        parent_md = AUTO_SINGLE_DIR / "parent" / "SKILL.md"
        child_md = AUTO_SINGLE_DIR / "child" / "SKILL.md"

        assert parent_md.exists()
        assert child_md.exists()

        parent_text = parent_md.read_text(encoding="utf-8")
        assert "## Relations" in parent_text
        assert "child" in parent_text

        child_text = child_md.read_text(encoding="utf-8")
        assert "## Relations" in child_text
        assert "parent" in child_text


# ─── Tests: export_all_communities ────────────────────────────────────


class TestExportAllCommunities:
    """Tests for export_all_communities()."""

    def test_no_data_returns_empty(self, export_skill_env):
        """Empty DB → empty list, output dir created."""
        result = export_all_communities()
        assert result == []
        assert AUTO_COMMUNITY_DIR.exists()

    def test_fallback_no_communities(self, export_skill_env, mem_db):
        """No gm_communities rows → fallback: skills exported as individual files."""
        _insert_skill(mem_db, "fallback-skill", content="FB content", description="FB desc")

        result = export_all_communities()

        assert len(result) == 1
        md_path = result[0]
        assert md_path.exists()
        text = md_path.read_text(encoding="utf-8")
        assert "name: fallback-skill" in text
        assert "FB content" in text
        assert AUTO_COMMUNITY_DIR in md_path.parents

    def test_fallback_clean_dir(self, export_skill_env, mem_db):
        """Fallback path cleans the community dir before writing."""
        stale_dir = AUTO_COMMUNITY_DIR / "stale"
        stale_dir.mkdir(parents=True)
        (stale_dir / "SKILL.md").write_text("stale")

        _insert_skill(mem_db, "fresh", content="Fresh")

        result = export_all_communities()

        assert len(result) == 1
        assert not (AUTO_COMMUNITY_DIR / "stale").exists()

    def test_fallback_multiple_skills(self, export_skill_env, mem_db):
        """Fallback exports all SKILL nodes even when multiple exist."""
        _insert_skill(mem_db, "alpha", content="A")
        _insert_skill(mem_db, "beta", content="B")

        result = export_all_communities()

        assert len(result) == 2
        names = {p.parent.name for p in result}
        assert "alpha" in names
        assert "beta" in names

    def test_community_mode(self, export_skill_env, mem_db):
        """When communities exist, skills are grouped by community_id."""
        _insert_community(mem_db, "c1", summary="Community One")
        _insert_community(mem_db, "c2", summary="Community Two")

        _insert_skill(mem_db, "skill-1", description="S1", community_id="c1")
        _insert_skill(mem_db, "skill-2", description="S2", community_id="c1")
        _insert_skill(mem_db, "skill-3", description="S3", community_id="c2")

        result = export_all_communities()

        # c1 and c2 should each produce a SKILL.md
        assert len(result) == 2
        c1_md = AUTO_COMMUNITY_DIR / "c1" / "SKILL.md"
        c2_md = AUTO_COMMUNITY_DIR / "c2" / "SKILL.md"
        assert c1_md.exists()
        assert c2_md.exists()

        c1_text = c1_md.read_text(encoding="utf-8")
        assert "Community One" in c1_text
        assert "skill-1" in c1_text
        assert "skill-2" in c1_text

        c2_text = c2_md.read_text(encoding="utf-8")
        assert "Community Two" in c2_text
        assert "skill-3" in c2_text

    def test_community_clean_dir(self, export_skill_env, mem_db):
        """Community mode cleans target dir before writing."""
        stale_dir = AUTO_COMMUNITY_DIR / "stale"
        stale_dir.mkdir(parents=True)
        (stale_dir / "SKILL.md").write_text("stale")

        _insert_community(mem_db, "c1", summary="Active")
        _insert_skill(mem_db, "alive", description="Alive", community_id="c1")

        result = export_all_communities()

        assert len(result) == 1
        assert not (AUTO_COMMUNITY_DIR / "stale").exists()
        assert (AUTO_COMMUNITY_DIR / "c1" / "SKILL.md").exists()

    def test_skills_without_community_skipped(self, export_skill_env, mem_db):
        """In community mode, skills with community_id=None are skipped."""
        _insert_community(mem_db, "c1", summary="Active")
        _insert_skill(mem_db, "orphan", description="No community")
        _insert_skill(mem_db, "member", description="Has community", community_id="c1")

        result = export_all_communities()

        assert len(result) == 1
        assert (AUTO_COMMUNITY_DIR / "c1" / "SKILL.md").exists()
        assert not (AUTO_COMMUNITY_DIR / "orphan").exists()
