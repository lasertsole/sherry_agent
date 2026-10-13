"""Fixtures for code_intel tests: a tiny 4-language fixture repo + isolated DB.

Every test points ``SHERRY_CODE_INTEL_ROOT`` / ``SHERRY_CODE_INTEL_DB`` at a
tmp location (autouse), so a test never walks or writes the real repository.
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from collections.abc import Callable

import pytest

from agent.tools.code_intel import CodeIndexer, CodeQuery
from config.features.agent_side import CODE_INTEL, CodeIntelConfig

FIXTURE_FILES: dict[str, str] = {
    "pkg/main.py": '''
        def helper(x):
            return x + 1

        def top_func(x):
            """Top docstring."""
            y = helper(x)
            return y

        class MyClass:
            def method_one(self, y):
                return self.method_two(y) + top_func(y)

            def method_two(self, z):
                return z
    ''',
    "pkg/calls_top.py": """
        from pkg.main import top_func

        def uses_top():
            return top_func(3)
    """,
    "web/app.ts": """
        import { foo } from "./foo";

        export function topFunc(x: number): string {
          const y = foo(x);
          return helper(y);
        }

        export class Widget {
          render(y: number): number {
            return this.layout(y);
          }
          layout(z: number): number {
            return z;
          }
        }

        const arrow = (a: number) => a + 1;
    """,
    "rs/lib.rs": """
        pub fn top_func(x: i32) -> String {
            let y = helper(x);
            format!("{}", y)
        }

        pub struct Thing { field: i32 }

        impl Thing {
            pub fn method_one(&self, y: i32) -> i32 {
                self.method_two(y) + top_func(y)
            }
            pub fn method_two(&self, z: i32) -> i32 { z }
        }
    """,
    "go/sample.go": """
        package sample

        import "fmt"

        func TopFunc(x int) string {
        \ty := helper(x)
        \treturn fmt.Sprintf("%d", y)
        }

        type Widget struct { Field int }

        func (m *Widget) MethodOne(y int) int {
        \treturn m.MethodTwo(y) + TopFunc(y)
        }

        func (m *Widget) MethodTwo(z int) int { return z }
    """,
    "chain/base.py": """
        def base_fn():
            return 1
    """,
    "chain/level1.py": """
        from chain.base import base_fn

        def level1_fn():
            return base_fn()
    """,
    "chain/level2.py": """
        from chain.level1 import level1_fn

        def level2_fn():
            return level1_fn()
    """,
    "chain/level3.py": """
        from chain.level2 import level2_fn

        def level3_fn():
            return level2_fn()
    """,
    "pkg/broken.py": "def oops(:\n    return\n",
    "notes.txt": "not code",
    "node_modules/ignored.py": "def nope():\n    pass\n",
}


@pytest.fixture(autouse=True)
def _isolated_index_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep the default tool root/db inside tmp_path for every test."""
    monkeypatch.setenv("SHERRY_CODE_INTEL_ROOT", str(tmp_path))
    monkeypatch.setenv("SHERRY_CODE_INTEL_DB", str(tmp_path / "codeintel" / "index.db"))


@pytest.fixture()
def sample_repo(tmp_path: Path) -> Path:
    """Write the multi-language fixture repo and return its root."""
    root = tmp_path / "repo"
    for rel, content in FIXTURE_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content).lstrip("\n"), encoding="utf-8")
    return root


@pytest.fixture()
def intel_config() -> CodeIntelConfig:
    """A COPY of the production config so tests can mutate it freely."""
    return CodeIntelConfig(**CODE_INTEL)


@pytest.fixture()
def index_db(tmp_path: Path) -> Path:
    """Isolated SQLite index path (created lazily by the indexer)."""
    return tmp_path / "codeintel" / "index.db"


@pytest.fixture()
def indexer(index_db: Path, intel_config: CodeIntelConfig) -> CodeIndexer:
    return CodeIndexer(str(index_db), intel_config)


@pytest.fixture()
def query(index_db: Path, intel_config: CodeIntelConfig) -> CodeQuery:
    return CodeQuery(str(index_db), intel_config)


@pytest.fixture()
def db_rows(index_db: Path) -> Callable[..., list]:
    """Open one read connection and return a ``query(sql, params)`` callable."""
    import sqlite3

    index_db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(index_db))
    conn.row_factory = sqlite3.Row

    def _query(sql: str, params: tuple = ()) -> list:
        return conn.execute(sql, params).fetchall()

    yield _query
    conn.close()
