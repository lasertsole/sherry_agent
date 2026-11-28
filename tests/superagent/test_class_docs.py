"""Every public superagent class carries a docstring (prototype parity)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2] / "superagent"


def _classes(path: Path) -> list[ast.ClassDef]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return [node for node in ast.walk(tree) if isinstance(node, ast.ClassDef)]


@pytest.mark.parametrize("path", sorted(ROOT.rglob("*.py")))
def test_every_class_has_a_docstring(path):
    classes = _classes(path)
    missing = [node.name for node in classes if ast.get_docstring(node) is None]
    assert not missing, f"{path.relative_to(ROOT)}: classes without docstrings: {missing}"


def test_class_inventory_is_non_trivial():
    total = sum(len(_classes(path)) for path in ROOT.rglob("*.py"))
    assert total >= 40
