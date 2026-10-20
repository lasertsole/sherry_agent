"""Per-language import/inheritance/mixin extraction (graphify edge parity)."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = next(p for p in Path(__file__).resolve().parents if (p / "pyproject.toml").exists())
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from agent.tools.code_intel.extract import extract_symbols  # noqa: E402

pytestmark = [pytest.mark.unit]


def _names(sites) -> list[str]:
    return [site.imported_name for site in sites]


def test_python_imports_inherits_and_function_local_imports():
    result = extract_symbols(
        b"""import os.path as osp, sys
from a.b import c as d, e
from . import f

class Child(Base, mixin.M):
    def run(self):
        import local.mod
        return 1
""",
        "python",
    )
    assert result.error is None
    assert _names(result.imports) == ["os.path", "sys", "c", "e", "f", "local.mod"]
    assert result.imports[-1].importer_index == 1  # inside ``run``
    assert [(site.parent_name, site.inherit_type) for site in result.inherits] == [
        ("Base", "class"),
        ("M", "class"),
    ]


def test_typescript_named_default_namespace_and_side_effect_imports():
    result = extract_symbols(
        b"""import def, { a, b as c } from "./mod";
import * as ns from "./ns";
import "./side";
class App extends Base implements I1, I2 {}
interface Shape extends A, B {}
""",
        "typescript",
    )
    assert result.error is None
    assert _names(result.imports) == ["def", "a", "b", "ns", "./side"]
    assert [(site.parent_name, site.inherit_type) for site in result.inherits] == [
        ("Base", "class"),
        ("A", "interface"),
        ("B", "interface"),
    ]
    assert [site.mixin_name for site in result.mixins] == ["I1", "I2"]
    assert any(symbol.name == "Shape" and symbol.kind == "interface" for symbol in result.symbols)


def test_rust_use_trait_bounds_and_trait_impl():
    result = extract_symbols(
        b"""use std::collections::{HashMap, HashSet};
use foo::bar as baz;
pub trait Animal: Base + Other { fn x(&self); }
pub struct Dog;
impl Animal for Dog { fn x(&self) {} }
""",
        "rust",
    )
    assert result.error is None
    assert _names(result.imports) == ["HashMap", "HashSet", "foo::bar"]
    assert [(site.parent_name, site.inherit_type) for site in result.inherits] == [
        ("Base", "trait"),
        ("Other", "trait"),
    ]
    assert [(site.mixin_name) for site in result.mixins] == ["Animal"]


def test_go_imports_struct_embedding_and_interface_embedding():
    result = extract_symbols(
        b"""package p
import ( "fmt"; m "example.com/m"; _ "example.com/side" )
type A struct { B; *C; D int }
type I interface { J; M() }
""",
        "go",
    )
    assert result.error is None
    assert _names(result.imports) == ["fmt", "example.com/m", "example.com/side"]
    assert [site.mixin_name for site in result.mixins] == ["B", "C"]
    assert [(site.parent_name, site.inherit_type) for site in result.inherits] == [
        ("J", "interface")
    ]


def test_existing_call_extraction_is_unaffected():
    result = extract_symbols(
        b"""def helper(x):
    return x

def top(x):
    return helper(x)
""",
        "python",
    )
    assert [(call.callee_name, call.line) for call in result.calls] == [("helper", 5)]
    assert result.imports == []
    assert result.inherits == []
    assert result.mixins == []
