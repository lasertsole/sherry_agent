"""The release-artifact guard (packaging/check_release.py).

The desktop release is audited before upload: the frozen backend tree and the
generated SPA must not carry the repository's test suites, evals, documentation
or the generated wiki. These tests pin the leak classes and the deliberate
exemptions (third-party tests under ``_internal``; the skill's documented manual
``rag_import*_test.py`` utilities).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = [pytest.mark.unit]

ROOT = Path(__file__).resolve().parents[2]


def _load_check_release():
    """Import ``packaging/check_release.py`` by path (it is a script)."""
    path = ROOT / "packaging" / "check_release.py"
    spec = importlib.util.spec_from_file_location("packaging_check_release", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _touch(path: Path, text: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_a_clean_backend_tree_passes_and_skips_internal_dependency_tests(tmp_path: Path):
    module = _load_check_release()
    tree = tmp_path / "sherry-backend"
    _touch(tree / "sherry-backend.exe")
    _touch(tree / "VERSION", "0.1.1\n")
    _touch(tree / "README.md", "# packaging\n")
    _touch(tree / "skills" / "demo" / "SKILL.md", "# demo\n")
    # A wheel's own tests may live under PyInstaller's dependency payload.
    _touch(tree / "_internal" / "site-packages" / "lib" / "tests" / "test_vendor.py")

    assert module.audit_backend(tree) == []


def test_backend_leaks_name_every_shipped_test_eval_doc_and_wiki_tree(tmp_path: Path):
    module = _load_check_release()
    tree = tmp_path / "sherry-backend"
    _touch(tree / "sherry-backend.exe")
    _touch(tree / "tests" / "test_suite.py")
    _touch(tree / "evals" / "run.py")
    _touch(tree / "agent" / "evals" / "nested.py")
    _touch(tree / "docs" / "index.md")
    _touch(tree / "wiki" / "index.md")
    _touch(tree / "client" / "app.test.ts")
    _touch(tree / "agent" / "test_inner.py")
    _touch(tree / "skills" / "demo" / "app.spec.ts")

    leaks = module.audit_backend(tree)

    assert set(leaks) == {
        "tests",
        "evals",
        "agent/evals",
        "docs",
        "wiki",
        "client",
        "client/app.test.ts",
        "agent/test_inner.py",
        "skills/demo/app.spec.ts",
    }


def test_documented_skill_utilities_are_not_mistaken_for_test_suites(tmp_path: Path):
    module = _load_check_release()
    tree = tmp_path / "sherry-backend"
    _touch(tree / "sherry-backend.exe")
    _touch(tree / "skills" / "multimodal_rag" / "rag_import_test.py")
    _touch(tree / "skills" / "multimodal_rag" / "rag_import_pdf_test.py")

    assert module.audit_backend(tree) == []


def test_a_clean_frontend_bundle_passes(tmp_path: Path):
    module = _load_check_release()
    dist = tmp_path / "public"
    _touch(dist / "index.html")
    _touch(dist / "robots.txt")
    _touch(dist / "_nuxt" / "app.js")

    assert module.audit_frontend(dist) == []


def test_frontend_leaks_cover_tests_evals_docs_and_markdown(tmp_path: Path):
    module = _load_check_release()
    dist = tmp_path / "public"
    _touch(dist / "index.html")
    _touch(dist / "__tests__" / "page.ts")
    _touch(dist / "evals" / "run.ts")
    _touch(dist / "home" / "page.test.ts")
    _touch(dist / "home" / "page.spec.tsx")
    _touch(dist / "docs" / "guide.html")
    _touch(dist / "README.md")

    leaks = module.audit_frontend(dist)

    assert set(leaks) == {
        "__tests__",
        "evals",
        "docs",
        "home/page.test.ts",
        "home/page.spec.tsx",
        "README.md",
    }


def test_cli_exit_codes(tmp_path: Path, capsys: pytest.CaptureFixture[str]):
    module = _load_check_release()
    clean = tmp_path / "public"
    _touch(clean / "index.html")
    assert module.main(["frontend", str(clean)]) == 0
    assert "ok:" in capsys.readouterr().out

    _touch(clean / "app.test.ts")
    assert module.main(["frontend", str(clean)]) == 1
    assert "release leak" in capsys.readouterr().err

    assert module.main(["backend", str(tmp_path / "missing")]) == 2
