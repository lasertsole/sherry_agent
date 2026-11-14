"""Three-layer LSP config: validation, merge precedence and mtime reload."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

import agent.tools.code_intel.lsp.config_merge as merge

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _skill_tree(tmp_path: Path, monkeypatch) -> Path:
    skills = tmp_path / "skills"
    skill_dir = skills / "plugins" / "elixir-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text("---\nname: elixir\n---\n", encoding="utf-8")
    (skill_dir / "lsp.json").write_text(
        json.dumps(
            {
                "servers": {
                    "elixir": {
                        "command": ["elixir-ls"],
                        "extensions": [".ex", ".exs"],
                        "language_id": "elixir",
                    }
                },
                "enable": ["elixir"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(merge, "SKILLS_DIR", skills)
    monkeypatch.setattr(merge, "SKILL_DISCOVERY_ROOTS", ("plugins",))
    return skills


def _project(root: Path, payload: dict) -> None:
    path = root / ".sherry" / "lsp.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_validation_refuses_a_path_or_shell_command():
    bad_path, errors = merge.validate_server_spec(
        "elixir", {"command": ["/usr/bin/elixir-ls"], "extensions": [".ex"]}
    )
    assert bad_path is None and errors

    bad_shell, errors = merge.validate_server_spec(
        "elixir", {"command": ["elixir-ls; rm -rf /"], "extensions": [".ex"]}
    )
    assert bad_shell is None and errors

    bad_arg, errors = merge.validate_server_spec(
        "elixir", {"command": ["elixir-ls", "--lsp; rm"], "extensions": [".ex"]}
    )
    assert bad_arg is None and errors

    bad_ext, errors = merge.validate_server_spec(
        "elixir", {"command": ["elixir-ls"], "extensions": ["ex"]}
    )
    assert bad_ext is None and errors


def test_a_valid_spec_is_normalized():
    spec, errors = merge.validate_server_spec(
        "elixir",
        {
            "command": ["elixir-ls", "--lsp"],
            "extensions": [".ex", ".exs"],
            "language_id": "elixir",
            "install_hint": "install elixir-ls",
            "unknown": "ignored",
        },
    )
    assert errors == []
    assert spec is not None
    assert spec["command"] == ["elixir-ls", "--lsp"]
    assert spec["extensions"] == [".ex", ".exs"]
    assert spec["install_hint"] == "install elixir-ls"
    assert "unknown" not in spec


def test_skills_and_project_merge_over_builtin(tmp_path: Path, monkeypatch):
    _skill_tree(tmp_path, monkeypatch)
    _project(
        tmp_path,
        {
            "servers": {
                "elixir": {
                    "command": ["elixir-ls-project"],
                    "extensions": [".ex"],
                    "language_id": "elixir",
                },
                "haskell": {
                    "command": ["haskell-language-server", "--lsp"],
                    "extensions": [".hs"],
                },
            },
            "enable": ["haskell"],
        },
    )

    view = merge.effective_lsp_servers(tmp_path)

    assert view.servers["elixir"]["command"] == ["elixir-ls-project"]
    assert view.source_of("elixir") == "project"
    assert view.source_of("haskell") == "project"
    assert view.source_of("python") == "builtin"
    assert "haskell" in view.enabled
    assert view.errors == []


def test_a_skill_declaration_is_visible_and_enabled(tmp_path: Path, monkeypatch):
    _skill_tree(tmp_path, monkeypatch)

    view = merge.effective_lsp_servers(tmp_path)

    assert view.servers["elixir"]["command"] == ["elixir-ls"]
    assert view.source_of("elixir") == "skill:elixir-skill"
    assert "elixir" in view.enabled


def test_write_project_config_is_atomic_and_validated(tmp_path: Path):
    payload = {
        "servers": {
            "elixir": {"command": ["elixir-ls"], "extensions": [".ex"], "language_id": "elixir"}
        },
        "enable": ["elixir"],
    }

    view, errors = merge.write_project_config(tmp_path, payload)

    assert errors == [] and view is not None
    written = json.loads((tmp_path / ".sherry" / "lsp.json").read_text(encoding="utf-8"))
    assert written["servers"]["elixir"]["command"] == ["elixir-ls"]
    assert merge.project_payload(tmp_path)["enable"] == ["elixir"]


def test_an_invalid_write_leaves_the_previous_file(tmp_path: Path):
    good = {
        "servers": {
            "elixir": {"command": ["elixir-ls"], "extensions": [".ex"], "language_id": "elixir"}
        }
    }
    merge.write_project_config(tmp_path, good)
    before = (tmp_path / ".sherry" / "lsp.json").read_text(encoding="utf-8")

    view, errors = merge.write_project_config(
        tmp_path,
        {"servers": {"bad": {"command": ["/usr/bin/bad; rm"], "extensions": ["nope"]}}},
    )

    assert view is None and errors
    assert (tmp_path / ".sherry" / "lsp.json").read_text(encoding="utf-8") == before


def test_the_project_file_reloads_when_its_mtime_changes(tmp_path: Path):
    merge.write_project_config(
        tmp_path,
        {
            "servers": {
                "elixir": {"command": ["elixir-ls"], "extensions": [".ex"], "language_id": "elixir"}
            }
        },
    )
    assert "elixir" in merge.effective_lsp_servers(tmp_path).servers

    path = tmp_path / ".sherry" / "lsp.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    del payload["servers"]["elixir"]
    path.write_text(json.dumps(payload), encoding="utf-8")
    stat = path.stat()
    os.utime(path, ns=(stat.st_atime_ns + 1_000_000, stat.st_mtime_ns + 1_000_000))

    assert "elixir" not in merge.effective_lsp_servers(tmp_path).servers


def test_a_bad_project_file_reports_errors_without_dropping_builtins(tmp_path: Path):
    (tmp_path / ".sherry").mkdir()
    (tmp_path / ".sherry" / "lsp.json").write_text("{not json", encoding="utf-8")

    view = merge.effective_lsp_servers(tmp_path)

    assert "python" in view.servers
    assert view.errors
