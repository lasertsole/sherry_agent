"""Graph queries over the extended code_intel index.

Pins the new import/inherit/mixin queries, the bidirectional ``path`` BFS
(independent of ``impact``), god-node ranking, Louvain communities with the
no-networkx fallback, and the old-schema compatibility contract (C1).
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.tools.code_intel import CodeIndexer, CodeQuery  # noqa: E402
from config.features.agent_side import CODE_INTEL  # noqa: E402

pytestmark = [pytest.mark.unit]


@pytest.fixture()
def edge_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    files = {
        "py/base.py": (
            "class Base:\n    pass\n\nclass MixinA:\n    pass\n\ndef base_fn():\n    return 1\n"
        ),
        "py/child.py": (
            "from py.base import Base, MixinA, base_fn\n\n"
            "class Child(Base, MixinA):\n"
            "    def run(self):\n"
            "        return base_fn()\n"
        ),
        "web/app.ts": (
            'import { Base } from "./base";\n'
            "interface Thing {}\n"
            "class App extends Base implements Thing {}\n"
        ),
        "web/base.ts": "export class Base {}\n",
        "chain/l1.py": "def l1():\n    return 1\n",
        "chain/l2.py": "from chain.l1 import l1\n\ndef l2():\n    return l1()\n",
        "chain/l3.py": "from chain.l2 import l2\n\ndef l3():\n    return l2()\n",
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


@pytest.fixture()
def edge_query(edge_repo: Path, tmp_path: Path, intel_config) -> tuple[CodeQuery, Path]:
    db = tmp_path / "codeintel" / "index.db"
    query = CodeQuery(str(db), intel_config)
    query._ensure_index(edge_repo)
    return query, edge_repo


def test_imports_of_returns_symbol_and_module_scope_imports(edge_query):
    query, root = edge_query

    names = {item.name for item in query.imports_of("Child", root)}
    assert {"Base", "MixinA", "base_fn"} <= names
    assert all(item.confidence == "EXTRACTED" for item in query.imports_of("Child", root))

    # A dotted module name maps onto its indexed file.
    module_imports = {item.name for item in query.imports_of("chain.l2", root)}
    assert module_imports == {"l1"}


def test_importers_of_reports_module_scope_importers(edge_query):
    query, root = edge_query

    importers = query.importers_of("base_fn", root)
    assert importers
    assert importers[0].kind == "module"
    assert importers[0].file_path == "py/child.py"
    assert importers[0].confidence == "EXTRACTED"


def test_inheritance_and_mixin_queries(edge_query):
    query, root = edge_query

    parents = {(item.name, item.inherit_type) for item in query.inherits_from("Child", root)}
    assert parents == {("Base", "class"), ("MixinA", "class")}
    assert {item.name for item in query.subclasses_of("Base", root)} == {"Child", "App"}

    mixins = query.mixins_of("App", root)
    assert [item.name for item in mixins] == ["Thing"]
    assert [item.name for item in query.mixins_in_by("Thing", root)] == ["App"]


def test_path_is_bidirectional_across_call_chains(edge_query):
    query, root = edge_query

    result = query.path("l3", "l1", root)
    assert result.found
    assert [hop.symbol.name for hop in result.hops] == ["l3", "l2", "l1"]
    assert [hop.edge_type for hop in result.hops] == ["root", "calls", "calls"]
    assert all(hop.edge_confidence == "EXTRACTED" for hop in result.hops[1:])

    # Edges are directed: l1 never calls back, so the reverse query has no path.
    assert not query.path("l1", "l3", root).found


def test_path_honours_the_edge_type_filter(edge_query):
    query, root = edge_query

    inherits = query.path("Child", "Base", root, edge_types=["inherits"])
    assert inherits.found
    assert [hop.edge_type for hop in inherits.hops] == ["root", "inherits"]

    no_calls = query.path("Child", "Base", root, edge_types=["calls"])
    assert not no_calls.found


def test_path_does_not_change_impact(edge_query):
    query, root = edge_query

    impact = query.impact("l1", root)
    assert [entry.symbol.name for entry in impact.entries] == ["l2", "l3"]
    assert impact.entries[0].via == "l1"
    assert [entry.depth for entry in impact.entries] == [1, 2]


def test_god_nodes_rank_by_resolved_degree(edge_query):
    query, root = edge_query

    ranked = query.god_nodes(root, limit=5)
    assert ranked
    assert ranked[0].degree >= ranked[-1].degree
    assert any(node.symbol.name == "Base" for node in ranked)


def test_communities_use_louvain_when_networkx_is_present(edge_query):
    pytest.importorskip("networkx")
    query, root = edge_query

    communities = query.communities(root)
    assert communities
    assert communities[0].engine == "networkx"
    assert any("Child" in community.symbols for community in communities)
    assert query.community_of("Child", root) is not None


def test_communities_degrade_to_components_without_networkx(
    edge_query, monkeypatch: pytest.MonkeyPatch
):
    query, root = edge_query
    monkeypatch.setitem(sys.modules, "networkx", None)

    communities = query.communities(root)
    assert communities
    assert all(community.engine == "components" for community in communities)
    assert any("Child" in community.symbols for community in communities)


def test_old_schema_database_is_readable_and_upgraded(tmp_path: Path):
    """C1: a DB holding only the original three tables opens without a schema error."""
    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
            file_path TEXT NOT NULL, line_start INTEGER NOT NULL, line_end INTEGER NOT NULL,
            parent_id INTEGER, language TEXT NOT NULL, source_snippet TEXT, doc_string TEXT
        );
        CREATE TABLE call_edges (
            id INTEGER PRIMARY KEY, caller_id INTEGER NOT NULL, callee_id INTEGER,
            callee_name TEXT NOT NULL, file_path TEXT NOT NULL, line INTEGER NOT NULL,
            resolved INTEGER DEFAULT 0
        );
        CREATE TABLE index_meta (
            file_path TEXT PRIMARY KEY, mtime REAL NOT NULL, size INTEGER NOT NULL DEFAULT 0,
            language TEXT NOT NULL, indexed_at REAL NOT NULL, symbol_count INTEGER, error TEXT
        );
        INSERT INTO symbols(name, kind, file_path, line_start, line_end, language)
            VALUES ('legacy_fn', 'function', 'legacy.py', 1, 2, 'python');
        """
    )
    conn.commit()
    conn.close()

    empty = tmp_path / "empty"
    empty.mkdir()
    indexer = CodeIndexer(str(db), CODE_INTEL)
    result = indexer.index_directory(empty)
    assert result.errors == []

    query = CodeQuery(str(db), CODE_INTEL)
    assert query.impact("legacy_fn", empty).entries == []
    assert query.imports_of("legacy_fn", empty) == []
    tables = {
        row[0]
        for row in sqlite3.connect(str(db)).execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }
    assert {"import_edges", "inherit_edges", "mixin_edges"} <= tables
