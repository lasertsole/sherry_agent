"""Boundary guards for the split summarization package.

Verifies each responsibility module imports standalone, the package's
package-relative dependency graph is acyclic, the split modules never pull in
the composition root ``core``, and ``core`` still re-exports the moved surface.
"""

import ast
import importlib
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

_PKG_DIR = Path(__file__).resolve().parents[3] / "agent" / "middlewares" / "summarization"
_SPLIT_MODULES = ("compression", "overflow", "summary_generation", "thrash", "state_aliases")


def _package_relative_imports(path: Path) -> set[str]:
    """All ``from .x import ...`` targets (lazy imports included)."""
    deps: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.level == 1 and node.module:
            deps.add(node.module.split(".")[0])
    return deps


def _dependency_graph() -> dict[str, set[str]]:
    return {p.stem: _package_relative_imports(p) for p in _PKG_DIR.glob("*.py")}


def test_split_modules_import_standalone():
    for name in _SPLIT_MODULES:
        module = importlib.import_module(f"agent.middlewares.summarization.{name}")
        assert module.__name__.endswith(name)


def test_split_modules_do_not_import_the_composition_root():
    graph = _dependency_graph()
    for name in _SPLIT_MODULES:
        assert "core" not in graph.get(name, set()), name


def test_package_dependency_graph_is_acyclic():
    graph = _dependency_graph()
    visiting: set[str] = set()
    visited: set[str] = set()

    def _walk(node: str) -> None:
        assert node not in visiting, f"cycle through {node}"
        if node in visited:
            return
        visiting.add(node)
        for dep in graph.get(node, set()):
            if dep in graph:
                _walk(dep)
        visiting.discard(node)
        visited.add(node)

    for module in graph:
        _walk(module)


def test_composition_root_reexports_moved_surface():
    core = importlib.import_module("agent.middlewares.summarization.core")
    for name in (
        "Summarization",
        "_filter_summary_messages",
        "_serialize_for_summary",
        "_build_static_fallback_summary",
        "_extract_file_operations",
        "_format_file_ops",
        "extract_reported_input_tokens",
        "_get_taskflow_context_sync",
        "_get_plan_context_sync",
        "_RESTORED_COOLDOWN_SESSIONS",
    ):
        assert hasattr(core, name), name
