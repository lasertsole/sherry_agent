"""Explicit graph layer for llm_wiki: SQLite nodes/edges beside the markdown.

Markdown stays the source of truth (human-readable, editable). This module
derives a queryable graph from it — wikilinks, inline links, code-span mentions
and optional frontmatter-extracted relations — and keeps it incrementally in
sync by file mtime. Nothing in the existing markdown protocol changes: new
frontmatter fields are optional and a wikilink's ``|EXTRACTED`` / ``|INFERRED``
suffix is the only syntax extension (a plain link defaults to EXTRACTED).

The DB lives next to the wiki as ``.wiki_graph.db``; a missing DB is built on
demand. Every function takes an optional ``wiki_root`` so tests never touch the
live wiki.
"""

from __future__ import annotations

import importlib.util
import sys
import os
import re
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path

from loguru import logger

_scripts_dir = Path(__file__).resolve().parent
_core_spec = importlib.util.spec_from_file_location("wiki_core", str(_scripts_dir / "core.py"))
_core = importlib.util.module_from_spec(_core_spec)
sys.modules.setdefault("wiki_core", _core)
_core_spec.loader.exec_module(_core)
get_wiki_path = _core.get_wiki_path

SCHEMA: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS wiki_nodes (
        id INTEGER PRIMARY KEY,
        name TEXT NOT NULL,
        type TEXT NOT NULL,
        file_path TEXT,
        created_at REAL,
        updated_at REAL
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_edges (
        id INTEGER PRIMARY KEY,
        src_id INTEGER NOT NULL,
        dst_id INTEGER NOT NULL,
        edge_type TEXT NOT NULL,
        confidence TEXT NOT NULL,
        source_file TEXT NOT NULL,
        line INTEGER,
        evidence TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS wiki_graph_meta (
        file_path TEXT PRIMARY KEY,
        mtime REAL NOT NULL,
        sha256 TEXT,
        indexed_at REAL NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_edges_src ON wiki_edges(src_id)",
    "CREATE INDEX IF NOT EXISTS idx_edges_dst ON wiki_edges(dst_id)",
    "CREATE INDEX IF NOT EXISTS idx_edges_confidence ON wiki_edges(confidence)",
    "CREATE INDEX IF NOT EXISTS idx_nodes_name ON wiki_nodes(name COLLATE NOCASE)",
)

_EXCLUDED_DIRS = {".git", "__pycache__", "node_modules", ".venv", "venv", "dist", "build"}
_CONFIDENCE_TAGS = {"EXTRACTED", "INFERRED"}
_WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|([^\]]*))?\]\]")
_INLINE_LINK_RE = re.compile(r"(?<!!)\[([^\]]*)\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_CODE_SPAN_RE = re.compile(r"`([^`\n]{2,120})`")
_FENCE_RE = re.compile(r"```.*?```", re.DOTALL)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.:-]*$")

_NODE_TYPE_BY_DIR = {
    "entities": "entity",
    "concepts": "concept",
    "comparisons": "comparison",
    "queries": "query",
    "raw": "source",
}


@dataclass(slots=True)
class GraphBuildResult:
    """Outcome of one :func:`build_wiki_graph` call."""

    indexed_files: int = 0
    skipped_files: int = 0
    removed_files: int = 0
    nodes: int = 0
    edges: int = 0
    errors: list[str] = field(default_factory=list)


def get_graph_db_path(wiki_root: Path | None = None) -> Path:
    """The graph DB for *wiki_root* (default: the live wiki)."""
    return (wiki_root or get_wiki_path()) / ".wiki_graph.db"


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA journal_mode=WAL")
    for ddl in SCHEMA:
        conn.execute(ddl)
    return conn


def _iter_markdown(wiki_root: Path) -> list[Path]:
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(wiki_root):
        dirnames[:] = sorted(d for d in dirnames if d not in _EXCLUDED_DIRS)
        for filename in sorted(filenames):
            if filename.endswith(".md"):
                files.append(Path(dirpath) / filename)
    return files


def _node_type_for(rel_path: str) -> str:
    parts = Path(rel_path).parts
    if not parts:
        return "concept"
    return _NODE_TYPE_BY_DIR.get(parts[0], "source")


def _strip_fences(content: str) -> str:
    """Blank fenced code blocks (keeps line numbers stable)."""
    return _FENCE_RE.sub(lambda match: "\n" * match.group(0).count("\n"), content)


def _split_frontmatter(content: str) -> tuple[dict, str]:
    if not content.startswith("---"):
        return {}, content
    parts = content.split("---", 2)
    if len(parts) < 3:
        return {}, content
    raw = parts[1]
    try:
        import yaml

        parsed = yaml.safe_load(raw) or {}
        return (parsed if isinstance(parsed, dict) else {}), parts[2]
    except Exception:  # noqa: BLE001 - a malformed block must not break the build
        return {}, parts[2]


def _normalize_link_target(target: str) -> str:
    value = target.strip()
    if not value or value.startswith(("http://", "https://", "mailto:", "#")):
        return ""
    value = value.split("#", 1)[0]
    if value.endswith(".md"):
        value = value[:-3]
    value = value.rstrip("/").rsplit("/", 1)[-1]
    return value.strip()


def _normalize_mention(value: str) -> str:
    text = value.strip()
    if not text or " " in text or not _IDENTIFIER_RE.match(text):
        return ""
    text = text.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
    return text if len(text) >= 3 else ""


class _Writer:
    """Node/edge upserts scoped to one file build."""

    def __init__(self, conn: sqlite3.Connection, now: float) -> None:
        self._conn = conn
        self._now = now
        self.node_ids: dict[str, int] = {}

    def node(self, name: str, node_type: str, file_path: str | None = None) -> int | None:
        key = name.strip()
        if not key:
            return None
        if key in self.node_ids:
            return self.node_ids[key]
        row = self._conn.execute(
            "SELECT id FROM wiki_nodes WHERE name = ? COLLATE NOCASE", (key,)
        ).fetchone()
        if row is not None:
            node_id = int(row["id"])
            if file_path:
                self._conn.execute(
                    "UPDATE wiki_nodes SET file_path = ?, type = ?, updated_at = ? WHERE id = ?",
                    (file_path, node_type, self._now, node_id),
                )
        else:
            cursor = self._conn.execute(
                "INSERT INTO wiki_nodes(name, type, file_path, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (key, node_type, file_path, self._now, self._now),
            )
            node_id = int(cursor.lastrowid or 0)
        self.node_ids[key] = node_id
        return node_id

    def edge(
        self,
        src_id: int | None,
        dst_id: int | None,
        edge_type: str,
        confidence: str,
        source_file: str,
        line: int | None = None,
        evidence: str | None = None,
    ) -> None:
        if src_id is None or dst_id is None or src_id == dst_id:
            return
        confidence = confidence if confidence in _CONFIDENCE_TAGS else "INFERRED"
        self._conn.execute(
            "DELETE FROM wiki_edges WHERE source_file = ? AND src_id = ? AND dst_id = ? "
            "AND edge_type = ?",
            (source_file, src_id, dst_id, edge_type),
        )
        self._conn.execute(
            "INSERT INTO wiki_edges(src_id, dst_id, edge_type, confidence, source_file, line, "
            "evidence) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (src_id, dst_id, edge_type, confidence, source_file, line, evidence),
        )


def _frontmatter_edges(
    writer: _Writer, frontmatter: dict, source_file: str, source_id: int
) -> None:
    """Optional ``extracted_entities`` / ``extracted_relations`` (L4 additive)."""
    entities = frontmatter.get("extracted_entities") or []
    if isinstance(entities, list):
        for entity in entities:
            if isinstance(entity, str):
                writer.node(entity, "entity")
    relations = frontmatter.get("extracted_relations") or []
    if not isinstance(relations, list):
        return
    for relation in relations:
        if not isinstance(relation, dict):
            continue
        src = str(relation.get("src") or relation.get("source") or "").strip()
        dst = str(relation.get("dst") or relation.get("target") or "").strip()
        if not src or not dst:
            continue
        confidence = str(relation.get("confidence") or "EXTRACTED").upper()
        confidence = confidence if confidence in _CONFIDENCE_TAGS else "INFERRED"
        src_id = writer.node(src, "entity")
        dst_id = writer.node(dst, "entity")
        writer.edge(
            src_id,
            dst_id,
            str(relation.get("type") or "semantic"),
            confidence,
            source_file,
            None,
            str(relation.get("evidence") or "") or None,
        )


def _parse_markdown(content: str, rel_path: str, writer: _Writer) -> None:
    """Upsert one file's nodes/edges."""
    frontmatter, body = _split_frontmatter(content)
    stem = Path(rel_path).stem
    node_type = _node_type_for(rel_path)
    source_id = writer.node(stem, node_type, rel_path)

    _frontmatter_edges(writer, frontmatter, rel_path, source_id)

    stripped = _strip_fences(body)
    for line_no, line in enumerate(stripped.splitlines(), start=1):
        for match in _WIKILINK_RE.finditer(line):
            target = match.group(1).strip()
            suffix = (match.group(2) or "").strip()
            confidence = suffix.upper() if suffix.upper() in _CONFIDENCE_TAGS else "EXTRACTED"
            target_id = writer.node(target, "concept")
            writer.edge(source_id, target_id, "wikilink", confidence, rel_path, line_no)
        for match in _INLINE_LINK_RE.finditer(line):
            target = _normalize_link_target(match.group(2))
            if not target:
                continue
            target_id = writer.node(target, "concept")
            writer.edge(source_id, target_id, "inline_link", "EXTRACTED", rel_path, line_no)
        for match in _CODE_SPAN_RE.finditer(line):
            mention = _normalize_mention(match.group(1))
            if not mention:
                continue
            target_id = writer.node(mention, "concept")
            writer.edge(source_id, target_id, "mention", "EXTRACTED", rel_path, line_no)


def build_wiki_graph(force: bool = False, wiki_root: Path | None = None) -> dict:
    """Incrementally rebuild the explicit graph from the wiki's markdown.

    Returns a JSON-friendly summary. A missing wiki directory is a no-op (the
    skill may be called before ``init_wiki``), never an error.
    """
    root = wiki_root or get_wiki_path()
    result = GraphBuildResult()
    if not root.is_dir():
        return {
            "indexed_files": 0,
            "skipped_files": 0,
            "removed_files": 0,
            "nodes": 0,
            "edges": 0,
            "errors": ["wiki directory does not exist"],
        }
    conn = _connect(get_graph_db_path(root))
    try:
        known = {
            row["file_path"]: float(row["mtime"])
            for row in conn.execute("SELECT file_path, mtime FROM wiki_graph_meta")
        }
        seen: set[str] = set()
        now = time.time()
        for path in _iter_markdown(root):
            rel = path.relative_to(root).as_posix()
            seen.add(rel)
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if not force and known.get(rel) == mtime:
                result.skipped_files += 1
                continue
            try:
                content = path.read_text(encoding="utf-8", errors="replace")
            except OSError as exc:
                result.errors.append(f"{rel}: {exc}")
                continue
            writer = _Writer(conn, now)
            try:
                _parse_markdown(content, rel, writer)
            except Exception as exc:  # noqa: BLE001 - one bad file must not abort the build
                result.errors.append(f"{rel}: {exc}")
                continue
            conn.execute(
                "INSERT OR REPLACE INTO wiki_graph_meta(file_path, mtime, sha256, indexed_at) "
                "VALUES (?, ?, NULL, ?)",
                (rel, mtime, now),
            )
            result.indexed_files += 1

        removed = [rel for rel in known if rel not in seen]
        for rel in removed:
            conn.execute("DELETE FROM wiki_edges WHERE source_file = ?", (rel,))
            dead_ids = [
                int(row["id"])
                for row in conn.execute("SELECT id FROM wiki_nodes WHERE file_path = ?", (rel,))
            ]
            if dead_ids:
                placeholders = ",".join("?" * len(dead_ids))
                conn.execute(
                    f"DELETE FROM wiki_edges WHERE src_id IN ({placeholders}) "
                    f"OR dst_id IN ({placeholders})",
                    [*dead_ids, *dead_ids],
                )
                conn.execute(f"DELETE FROM wiki_nodes WHERE id IN ({placeholders})", dead_ids)
            conn.execute("DELETE FROM wiki_graph_meta WHERE file_path = ?", (rel,))
        result.removed_files = len(removed)
        conn.commit()
        result.nodes = int(conn.execute("SELECT COUNT(*) FROM wiki_nodes").fetchone()[0])
        result.edges = int(conn.execute("SELECT COUNT(*) FROM wiki_edges").fetchone()[0])
    except Exception as exc:  # noqa: BLE001 - the graph is additive, never fatal
        logger.warning("llm_wiki: build_wiki_graph failed: {}", exc)
        result.errors.append(str(exc))
    finally:
        conn.close()
    if result.errors:
        logger.debug("llm_wiki: graph build errors: {}", result.errors[:5])
    return {
        "indexed_files": result.indexed_files,
        "skipped_files": result.skipped_files,
        "removed_files": result.removed_files,
        "nodes": result.nodes,
        "edges": result.edges,
        "errors": result.errors,
    }


def graph_stats(wiki_root: Path | None = None) -> dict:
    """Read-only counts + confidence split (used by lint)."""
    root = wiki_root or get_wiki_path()
    db_path = get_graph_db_path(root)
    if not db_path.is_file():
        return {"available": False, "nodes": 0, "edges": 0, "confidence": {}}
    conn = _connect(db_path)
    try:
        nodes = int(conn.execute("SELECT COUNT(*) FROM wiki_nodes").fetchone()[0])
        edges = int(conn.execute("SELECT COUNT(*) FROM wiki_edges").fetchone()[0])
        confidence = {
            row["confidence"]: int(row["n"])
            for row in conn.execute(
                "SELECT confidence, COUNT(*) AS n FROM wiki_edges GROUP BY confidence"
            )
        }
    finally:
        conn.close()
    return {"available": True, "nodes": nodes, "edges": edges, "confidence": confidence}
