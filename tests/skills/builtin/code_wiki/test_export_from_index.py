"""Deterministic code_wiki export from a code_intel SQLite index."""

from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.tools.code_intel import CodeIndexer  # noqa: E402
from config.features.agent_side import CODE_INTEL  # noqa: E402
from skills.builtin.code_wiki.scripts import core  # noqa: E402

pytestmark = [pytest.mark.unit]


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    files = {
        "pkg/base.py": "class Base:\n    pass\n\nclass Marker:\n    pass\n",
        "pkg/child.py": (
            "from pkg.base import Base, Marker\n\n"
            "def helper():\n    return 1\n\n"
            "class Child(Base, Marker):\n"
            "    def run(self):\n"
            "        return helper()\n"
        ),
        "web/app.ts": (
            "interface Thing {}\n"
            "export class App implements Thing {\n  render(): number { return 1; }\n}\n"
        ),
        "chain/l1.py": "def l1():\n    return 1\n",
        "chain/l2.py": "from chain.l1 import l1\n\ndef l2():\n    return l1()\n",
    }
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


@pytest.fixture()
def index_db(repo: Path, tmp_path: Path) -> Path:
    db = tmp_path / "codeintel" / "index.db"
    result = CodeIndexer(str(db), CODE_INTEL).index_directory(repo)
    assert result.errors == []
    return db


def test_export_generates_the_full_document_tree(
    index_db: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    wiki_root = tmp_path / "wiki"
    monkeypatch.setattr(core, "get_wiki_path", lambda: wiki_root)

    result = core.export_wiki_from_index("demo", str(index_db))

    assert result["fallback"] is False
    assert result["generator"] == "code_intel_export"
    assert result["symbols"] > 0
    assert result["modules"]
    output = wiki_root / "demo"
    for name in (
        "README.md",
        "architecture.md",
        "diagrams/class-diagram.md",
        "diagrams/sequences.md",
        ".codewiki-state.json",
    ):
        assert (output / name).is_file(), name
    assert list((output / "modules").glob("*.md"))

    readme = (output / "README.md").read_text(encoding="utf-8")
    assert "# demo" in readme
    assert "Hub symbols" in readme
    architecture = (output / "architecture.md").read_text(encoding="utf-8")
    assert "```mermaid" in architecture
    assert architecture.count("```") >= 2
    class_diagram = (output / "diagrams/class-diagram.md").read_text(encoding="utf-8")
    assert "Base <|-- Child" in class_diagram
    assert "Thing <|.. App" in class_diagram
    state = json.loads((output / ".codewiki-state.json").read_text(encoding="utf-8"))
    assert state["generator"] == "code_intel_export"
    assert state["modules_documented"] == result["modules"]


def test_export_falls_back_when_the_index_is_missing(tmp_path: Path):
    result = core.export_wiki_from_index("demo", str(tmp_path / "absent.db"))
    assert result == {
        "fallback": True,
        "reason": "index_not_found",
        "db_path": str(tmp_path / "absent.db"),
    }


def test_export_falls_back_for_an_unsupported_language(tmp_path: Path):
    db = tmp_path / "java.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
            file_path TEXT NOT NULL, line_start INTEGER NOT NULL, language TEXT NOT NULL
        );
        INSERT INTO symbols(name, kind, file_path, line_start, language)
            VALUES ('Main', 'class', 'Main.java', 1, 'java');
        """
    )
    conn.commit()
    conn.close()

    result = core.export_wiki_from_index("demo", str(db))
    assert result["fallback"] is True
    assert result["reason"] == "unsupported_language"


def test_existing_path_management_is_unchanged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    wiki_root = tmp_path / "wiki"
    monkeypatch.setattr(core, "get_wiki_path", lambda: wiki_root)
    monkeypatch.setattr(core, "project_root", tmp_path)

    created = core.init_repo_wiki("demo")
    assert created["errors"] == []
    assert core.repo_wiki_exists("demo")
    assert core.get_repo_wiki_path("demo") == wiki_root / "demo"
    assert core.get_modules_dir("demo") == wiki_root / "demo" / "modules"
    assert core.get_diagrams_dir("demo") == wiki_root / "demo" / "diagrams"
    path = core.write_doc("demo", "modules/core.md", "# Core\n")
    assert path.read_text(encoding="utf-8") == "# Core\n"
    assert core.read_doc("demo", "modules/core.md") == "# Core\n"
    assert core.list_modules("demo")[0]["name"] == "core"


def test_module_names_use_the_deepest_half_covering_directory():
    by_id = {
        1: {"name": "a", "file_path": "agent/tools/subagent/x.py"},
        2: {"name": "b", "file_path": "agent/tools/subagent/y.py"},
        3: {"name": "c", "file_path": "agent/tools/subagent/z.py"},
        4: {"name": "d", "file_path": "agent/other.py"},
    }

    name = core._module_name([1, 2, 3], by_id, set())

    assert name == "agent-tools-subagent"


def _capped_index_db(tmp_path: Path) -> Path:
    db = tmp_path / "capped.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
            file_path TEXT NOT NULL, line_start INTEGER NOT NULL, language TEXT NOT NULL
        );
        """
    )
    rows = []
    for top in ("alpha", "beta", "gamma"):
        for index in range(3):
            rows.append((f"{top}{index}", "function", f"{top}/mod{index}.py", 1, "python"))
    conn.executemany(
        "INSERT INTO symbols(name, kind, file_path, line_start, language) VALUES (?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db


def test_a_capped_index_samples_every_top_level_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db = _capped_index_db(tmp_path)
    monkeypatch.setitem(core.SKILLS_TOOLING, "code_wiki_export_max_symbols", 3)

    index = core._load_index(db)

    assert index is not None
    assert index["total_symbols"] == 9
    assert index["symbols_capped"] is True
    assert len(index["symbols"]) == 3
    assert {symbol["file_path"].split("/", 1)[0] for symbol in index["symbols"]} == {
        "alpha",
        "beta",
        "gamma",
    }

    monkeypatch.setattr(core, "get_wiki_path", lambda: tmp_path / "wiki")
    result = core.export_wiki_from_index("demo", str(db))

    assert result["symbols"] == 3
    assert result["total_symbols"] == 9
    assert result["symbols_capped"] is True
    readme = (tmp_path / "wiki" / "demo" / "README.md").read_text(encoding="utf-8")
    assert "3 of 9" in readme
    assert "sampled across top-level directories" in readme


def test_a_truncated_index_build_is_labelled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = _capped_index_db(tmp_path)
    monkeypatch.setattr(core, "get_wiki_path", lambda: tmp_path / "wiki")

    result = core.export_wiki_from_index("demo", str(db), index_truncated=True)

    assert result["index_truncated"] is True
    readme = (tmp_path / "wiki" / "demo" / "README.md").read_text(encoding="utf-8")
    assert "TRUNCATED by the indexer" in readme
    state = json.loads(
        (tmp_path / "wiki" / "demo" / ".codewiki-state.json").read_text(encoding="utf-8")
    )
    assert state["index_truncated"] is True
    assert state["total_symbols"] == 9


def test_the_wiki_scope_excludes_tests_and_vendored_copies(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    db = tmp_path / "scope.db"
    conn = sqlite3.connect(str(db))
    conn.executescript(
        """
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY, name TEXT NOT NULL, kind TEXT NOT NULL,
            file_path TEXT NOT NULL, line_start INTEGER NOT NULL, language TEXT NOT NULL
        );
        INSERT INTO symbols(name, kind, file_path, line_start, language) VALUES
            ('main', 'function', 'app/main.py', 1, 'python'),
            ('helper', 'function', 'pkg/helper.py', 1, 'python'),
            ('test_main', 'function', 'tests/test_main.py', 1, 'python'),
            ('test_helper', 'function', 'pkg/helper_test.py', 1, 'python'),
            ('lib', 'function', 'vendor/lib.py', 1, 'python');
        """
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(core, "get_wiki_path", lambda: tmp_path / "wiki")

    result = core.export_wiki_from_index("demo", str(db))

    assert result["total_symbols"] == 2
    assert result["excluded_symbols"] == 3
    assert result["symbols"] == 2
    readme = (tmp_path / "wiki" / "demo" / "README.md").read_text(encoding="utf-8")
    assert "Scope: tests, vendor and test files excluded (3 symbols)" in readme
    module_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (tmp_path / "wiki" / "demo" / "modules").glob("*.md")
    )
    assert "tests/test_main.py" not in module_text
    assert "vendor/lib.py" not in module_text
    assert "pkg/helper_test.py" not in module_text
    assert "app/main.py" in module_text
