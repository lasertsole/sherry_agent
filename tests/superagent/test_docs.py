"""Numbered doc chain and module/test inventory completeness."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PLAN = ROOT / "TODO" / "super-agent"
WIKI = ROOT / "wiki" / "super-agent"


def test_numbered_plan_documents_are_present():
    numbers = list(range(0, 16))
    for number in numbers:
        matches = list(PLAN.glob(f"{number:02d}-*.md"))
        assert matches, f"missing numbered design doc {number:02d}"


def test_plan_readme_and_revisions_exist():
    assert (PLAN / "README.md").is_file()
    assert (PLAN / "REVISIONS.md").is_file()


def test_module_table_maps_every_key_module():
    modules = [
        "core/model.py",
        "core/engine.py",
        "app/store_read.py",
        "app/store.py",
        "app/effects.py",
        "app/outbox.py",
        "app/passes.py",
        "sources/registry.py",
        "sources/github.py",
        "sources/feishu.py",
        "executor/sherry.py",
        "panel/api.py",
        "mcp/server.py",
        "index.py",
        "__main__.py",
    ]
    for module in modules:
        assert (ROOT / "superagent" / module).is_file(), module


def test_test_table_maps_every_key_suite():
    suites = [
        "core/test_engine.py",
        "core/test_model.py",
        "core/test_memory.py",
        "sources/test_github.py",
        "sources/test_feishu.py",
        "executor/test_sherry.py",
        "panel/test_http.py",
        "test_runtime.py",
        "test_service_script.py",
        "test_deployment.py",
        "test_index.py",
        "test_class_docs.py",
        "test_docs.py",
        "test_e2e.py",
    ]
    for suite in suites:
        assert (ROOT / "tests" / "superagent" / suite).is_file(), suite


def test_four_language_wiki_group_is_complete():
    for name in ("README.md", "README.zh.md", "README.ja.md", "README.ko.md"):
        assert (WIKI / name).is_file()
    for name in ("roadmap.md", "integration.md", "deployment.md"):
        assert (WIKI / name).is_file()


def test_service_script_and_config_templates_exist():
    assert (ROOT / "scripts" / "superagent").is_file()
    assert (ROOT / "superagent.jsonc").is_file()
    assert (ROOT / "superagent" / "HOURLY_CHECK.md").is_file()
