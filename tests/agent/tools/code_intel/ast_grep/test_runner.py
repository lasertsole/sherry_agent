"""Unit tests for the ast-grep runner tools (subprocess, JSON, rewrite safety)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agent.tools.code_intel.ast_grep import resolver, runner

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

FAKE_SG = r"""#!/usr/bin/env python3
import json
import pathlib
import sys

args = sys.argv[1:]
if "--version" in args or "-V" in args:
    print("ast-grep 0.43.0")
    sys.exit(0)

update_all = "-U" in args or "--update-all" in args
roots = [a for a in args if a.startswith("/")]
files = []
for root in roots:
    p = pathlib.Path(root)
    if p.is_file():
        files.append(p)
    elif p.is_dir():
        files.extend(sorted(p.rglob("*.py")))

if update_all:
    count = 0
    for f in files:
        text = f.read_text()
        if "print(" in text:
            f.write_text(text.replace("print(", "logger.info("))
            count += 1
    print(f"Applied {count} changes")
    sys.exit(0)

for f in files:
    for i, line in enumerate(f.read_text().splitlines()):
        if "print(" in line:
            rec = {
                "text": "print(",
                "range": {"start": {"line": i, "column": 0}, "end": {"line": i, "column": 6}},
                "file": str(f),
                "lines": line,
                "language": "Python",
            }
            if "-r" in args:
                rec["replacement"] = line.replace("print(", "logger.info(")
            print(json.dumps(rec))
"""


def _fake_binary(tmp_path: Path, content: str = FAKE_SG) -> Path:
    path = tmp_path / "fake-sg"
    path.write_text(content, encoding="utf-8")
    path.chmod(0o755)
    return path


def _fixture_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir()
    (project / "a.py").write_text('def f():\n    print("hi")\nprint("x")\n', encoding="utf-8")
    return project


@pytest.fixture(autouse=True)
def _isolate(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SHERRY_SG_PATH", raising=False)
    monkeypatch.delenv("SHERRY_SG_ROOT", raising=False)
    resolver._clear_cache_for_tests()
    yield
    resolver._clear_cache_for_tests()


class TestRunSg:
    def test_parses_json_and_ignores_garbage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        binary = _fake_binary(
            tmp_path,
            "#!/bin/sh\n"
            'echo \'{"file":"/x/a.py","range":{"start":{"line":0,"column":0},'
            '"end":{"line":0,"column":1}},"text":"p","language":"Python"}\'\n'
            "echo 'not json'\n",
        )
        result = runner._run_sg(str(binary), ["run"], 5000, str(tmp_path))
        assert result["ok"] is True
        assert len(result["records"]) == 1

    def test_nonzero_exit_without_output_is_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        binary = _fake_binary(tmp_path, "#!/bin/sh\necho boom >&2\nexit 2\n")
        result = runner._run_sg(str(binary), ["run"], 5000, str(tmp_path))
        assert result["ok"] is False
        assert "boom" in result["error"]

    def test_timeout_is_error(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        binary = _fake_binary(tmp_path, "#!/bin/sh\nsleep 5\n")
        result = runner._run_sg(str(binary), ["run"], 200, str(tmp_path))
        assert result["ok"] is False
        assert "timed out" in result["error"]

    def test_unexecutable_binary_is_error(self, tmp_path: Path) -> None:
        result = runner._run_sg(str(tmp_path / "missing"), ["run"], 5000, str(tmp_path))
        assert result["ok"] is False
        assert "could not be executed" in result["error"]


class TestSearchTool:
    def test_structured_search(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        project = _fixture_project(tmp_path)
        binary = _fake_binary(tmp_path)
        monkeypatch.setenv("SHERRY_SG_ROOT", str(project))
        monkeypatch.setattr(runner, "_ensure_binary", lambda: str(binary))

        payload = json.loads(
            runner.AstGrepSearchTool()._run(pattern="print($A)", language="python", paths=["."])
        )
        assert payload["count"] >= 2
        first = payload["matches"][0]
        assert first["file"].endswith("a.py")
        assert first["line"] >= 1
        assert first["text"]

    def test_search_truncates_to_max_matches(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _fixture_project(tmp_path)
        binary = _fake_binary(tmp_path)
        monkeypatch.setenv("SHERRY_SG_ROOT", str(project))
        monkeypatch.setattr(runner, "_ensure_binary", lambda: str(binary))

        payload = json.loads(
            runner.AstGrepSearchTool()._run(
                pattern="print($A)", language="python", paths=["."], max_matches=1
            )
        )
        assert payload["count"] == 1
        assert payload["truncated"] is True

    def test_oversized_pattern_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setitem(runner.AST_GREP, "ast_grep_max_pattern_bytes", 4)
        payload = json.loads(
            runner.AstGrepSearchTool()._run(
                pattern="print($A)", language="python", paths=[str(tmp_path)]
            )
        )
        assert "error" in payload
        assert payload["count"] == 0


class TestRewriteTool:
    def test_dry_run_does_not_modify_files(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _fixture_project(tmp_path)
        binary = _fake_binary(tmp_path)
        monkeypatch.setenv("SHERRY_SG_ROOT", str(project))
        monkeypatch.setattr(runner, "_ensure_binary", lambda: str(binary))

        payload = json.loads(
            runner.AstGrepRewriteTool()._run(
                pattern="print($A)",
                rewrite="logger.info($A)",
                language="python",
                paths=["."],
            )
        )
        assert payload["applied"] is False
        assert payload["count"] >= 1
        assert "replacement" in payload["changes"][0]
        assert "print(" in (project / "a.py").read_text(encoding="utf-8")

    def test_apply_writes_inside_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _fixture_project(tmp_path)
        binary = _fake_binary(tmp_path)
        monkeypatch.setenv("SHERRY_SG_ROOT", str(project))
        monkeypatch.setattr(runner, "_ensure_binary", lambda: str(binary))

        payload = json.loads(
            runner.AstGrepRewriteTool()._run(
                pattern="print($A)",
                rewrite="logger.info($A)",
                language="python",
                paths=["."],
                dry_run=False,
            )
        )
        assert payload["applied"] is True
        assert payload["count"] >= 1
        assert "logger.info(" in (project / "a.py").read_text(encoding="utf-8")

    def test_apply_count_reads_stderr(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = _fixture_project(tmp_path)
        binary = _fake_binary(tmp_path, "#!/bin/sh\necho 'Applied 3 changes' >&2\n")
        monkeypatch.setenv("SHERRY_SG_ROOT", str(project))
        monkeypatch.setattr(runner, "_ensure_binary", lambda: str(binary))

        payload = json.loads(
            runner.AstGrepRewriteTool()._run(
                pattern="print($A)",
                rewrite="logger.info($A)",
                language="python",
                paths=["."],
                dry_run=False,
            )
        )
        assert payload["applied"] is True
        assert payload["count"] == 3


class TestPathSafety:
    def test_traversal_path_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SHERRY_SG_ROOT", raising=False)
        payload = json.loads(
            runner.AstGrepRewriteTool()._run(
                pattern="print($A)", rewrite="x", language="python", paths=["../outside"]
            )
        )
        assert "error" in payload
        assert payload["applied"] is False

    def test_out_of_root_absolute_path_rejected(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("SHERRY_SG_ROOT", raising=False)
        payload = json.loads(
            runner.AstGrepSearchTool()._run(pattern="x", language="python", paths=["/etc/passwd"])
        )
        assert "error" in payload

    def test_override_root_rejects_path_outside_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SHERRY_SG_ROOT", str(tmp_path / "project"))
        outside = tmp_path / "outside"
        outside.mkdir()
        payload = json.loads(
            runner.AstGrepRewriteTool()._run(
                pattern="print($A)", rewrite="x", language="python", paths=[str(outside)]
            )
        )
        assert "error" in payload

    def test_too_many_paths_rejected(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("SHERRY_SG_ROOT", str(tmp_path))
        payload = json.loads(
            runner.AstGrepSearchTool()._run(pattern="x", language="python", paths=["."] * 65)
        )
        assert "error" in payload


class TestEnsureBinary:
    def test_shpath_override_is_used_without_provisioning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        binary = _fake_binary(tmp_path)
        monkeypatch.setenv("SHERRY_SG_PATH", str(binary))
        monkeypatch.setattr(resolver, "_path_candidates", list)
        monkeypatch.setattr(resolver, "_homebrew_candidates", list)
        monkeypatch.setattr(
            runner, "provision_sg_binary", lambda: pytest.fail("provision must not run")
        )
        assert runner._ensure_binary() == str(binary)

    def test_missing_binary_payload_lists_hints(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("SHERRY_SG_ROOT", str(tmp_path))
        monkeypatch.setattr(runner, "_ensure_binary", lambda: None)
        payload = json.loads(
            runner.AstGrepSearchTool()._run(pattern="x", language="python", paths=["."])
        )
        assert "error" in payload
        assert payload["install_hints"]
