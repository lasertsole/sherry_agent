"""Structural pins for the lazy skills imports at the agent assembly seam.

Importing ``agent.core`` must stay side-effect free (AGENTS.md Known Pitfalls),
so the skills snapshot is built inside ``init()`` through a call-time import;
``agent.tools.subagent.delegate`` must not bind ``skills.loader`` at module
scope either. These pins are static (AST) so they cannot flake on import order.
"""

import ast
from pathlib import Path

import pytest

_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())

pytestmark = [pytest.mark.unit]


def _module_tree(rel_path: str) -> ast.Module:
    return ast.parse((_ROOT / rel_path).read_text(encoding="utf-8"))


def _module_level_skills_imports(tree: ast.Module) -> list[str]:
    found: list[str] = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            found.extend(alias.name for alias in node.names if alias.name.split(".")[0] == "skills")
        elif isinstance(node, ast.ImportFrom) and (node.module or "").split(".")[0] == "skills":
            found.append(node.module or "")
    return found


class TestAgentCoreSkillsImport:
    def test_no_module_level_skills_import(self):
        assert _module_level_skills_imports(_module_tree("agent/core.py")) == []

    def test_init_imports_skills_and_builds_snapshot(self):
        tree = _module_tree("agent/core.py")
        init_fn = next(
            node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "init"
        )

        imported = {
            alias.name
            for node in ast.walk(init_fn)
            if isinstance(node, ast.ImportFrom) and node.module == "skills"
            for alias in node.names
        }
        called = {
            node.func.id
            for node in ast.walk(init_fn)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        }

        assert "build_skills_snapshot" in imported
        assert "build_skills_snapshot" in called


class TestDelegateSkillsImport:
    def test_no_module_level_skills_import(self):
        assert _module_level_skills_imports(_module_tree("agent/tools/subagent/delegate.py")) == []
