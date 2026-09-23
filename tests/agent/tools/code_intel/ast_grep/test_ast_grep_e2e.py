"""Role isolation + hermetic end-to-end for ast-grep (fixture project, fake download)."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import io
import json
import sys
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.tools.code_intel.ast_grep import provisioner, resolver, runner
from config.features import AST_GREP

pytestmark = [pytest.mark.module, pytest.mark.timeout(120)]

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


class _StubTool:
    def __init__(self, name: str, metadata: dict | None = None) -> None:
        self.name = name
        self.metadata = metadata or {}


class _StubCheckpointer:
    async def setup(self) -> None:
        return None


@pytest.fixture()
def _wiring(monkeypatch: pytest.MonkeyPatch) -> dict:
    captured: dict = {}

    def _fake_create_agent(**kwargs):
        captured["tools"] = kwargs.get("tools", [])
        return SimpleNamespace(name="child-graph")

    async def _fake_checkpointer(*args, **kwargs):
        return _StubCheckpointer()

    monkeypatch.setattr("langchain.agents.create_agent", _fake_create_agent)
    monkeypatch.setattr("agent.checkpointer.build_async_sqlite_checkpointer", _fake_checkpointer)
    monkeypatch.setenv("MAIN_LLM_MAX_TOKEN", "131072")
    monkeypatch.setenv("AUXILIARY_LLM_MAX_TOKEN", "131072")
    monkeypatch.setattr("models.LLMs.main_llm.max_tokens", 65_536)
    monkeypatch.setattr("models.build_main_llm", lambda *a, **k: SimpleNamespace(kind="main"))
    monkeypatch.setattr("models.build_auxiliary_llm", lambda *a, **k: SimpleNamespace(kind="aux"))
    return captured


def _build(**kwargs) -> None:
    from agent.tools.subagent.spawn.core import _build_child_agent

    asyncio.run(_build_child_agent(system_prompt="child", **kwargs))


def _tool_names(captured: dict) -> set[str]:
    return {tool.name for tool in captured["tools"]}


def _role(value: str):
    from agent.tools.subagent.types import FunctionalRole

    return FunctionalRole(value)


class TestEveryRoleGetsAstGrep:
    @pytest.mark.parametrize("role_name", ["general", "researcher", "executor", "reviewer"])
    def test_all_four_functional_roles_get_ast_grep(self, _wiring: dict, role_name: str) -> None:
        _build(
            tools=[_StubTool("read_file")],
            tool_allow=[],
            tool_deny=[],
            functional_role=_role(role_name),
        )
        assert {"ast_grep_search", "ast_grep_rewrite"} <= _tool_names(_wiring)


class TestMainAgentHasNoAstGrep:
    def test_main_builders_reference_no_ast_grep(self) -> None:
        from agent.tools import _MAIN_TOOLS_BUILDERS

        for builder in _MAIN_TOOLS_BUILDERS:
            module = getattr(builder, "__module__", "")
            qualname = getattr(builder, "__qualname__", "")
            assert "ast_grep" not in module
            assert "ast_grep" not in qualname

    def test_agent_tools_package_does_not_expose_ast_grep(self) -> None:
        import agent.tools as agent_tools

        assert not hasattr(agent_tools, "build_ast_grep_tools")
        source = Path(agent_tools.__file__).read_text(encoding="utf-8")
        assert "ast_grep" not in source


class TestToolSurface:
    def test_builder_returns_two_named_tools(self) -> None:
        from agent.tools.code_intel.ast_grep import build_ast_grep_tools

        tools = build_ast_grep_tools("sess")
        assert [tool.name for tool in tools] == ["ast_grep_search", "ast_grep_rewrite"]
        assert tools[0].metadata["idempotent"] is True
        assert tools[1].metadata["idempotent"] is False


class TestHermeticE2E:
    def _provision_fake(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        payload = FAKE_SG.encode("utf-8")
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w") as zf:
            zf.writestr("sg", b"launcher")
            zf.writestr("ast-grep", payload)
        zip_bytes = archive.getvalue()
        sha = hashlib.sha256(zip_bytes).hexdigest()

        config = copy.deepcopy(AST_GREP)
        config["ast_grep_release_assets"] = {
            "test-slug": {"url": "https://example.invalid/app.zip", "sha256": sha}
        }
        monkeypatch.setattr(provisioner, "AST_GREP", config)
        monkeypatch.setattr(provisioner, "_runtime_slug", lambda: "test-slug")
        monkeypatch.setattr(provisioner, "runtime_dir", lambda: tmp_path / "runtime")
        monkeypatch.setattr(provisioner, "_download", lambda url, timeout: zip_bytes)
        return Path(provisioner.provision_sg_binary())

    def test_provision_then_search_and_rewrite(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        project = tmp_path / "project"
        project.mkdir()
        (project / "a.py").write_text('print("hi")\nprint("x")\n', encoding="utf-8")

        binary = self._provision_fake(tmp_path, monkeypatch)
        assert binary.exists()
        if sys.platform != "win32":
            assert binary.stat().st_mode & 0o777 == 0o755

        monkeypatch.setenv("SHERRY_SG_PATH", str(binary))
        monkeypatch.setenv("SHERRY_SG_ROOT", str(project))
        resolver._clear_cache_for_tests()

        search = json.loads(
            runner.AstGrepSearchTool()._run(pattern="print($A)", language="python", paths=["."])
        )
        assert search["count"] == 2

        preview = json.loads(
            runner.AstGrepRewriteTool()._run(
                pattern="print($A)", rewrite="logger.info($A)", language="python", paths=["."]
            )
        )
        assert preview["applied"] is False
        assert "print(" in (project / "a.py").read_text(encoding="utf-8")

        applied = json.loads(
            runner.AstGrepRewriteTool()._run(
                pattern="print($A)",
                rewrite="logger.info($A)",
                language="python",
                paths=["."],
                dry_run=False,
            )
        )
        assert applied["applied"] is True
        assert applied["count"] >= 1
        assert (project / "a.py").read_text(encoding="utf-8").count("logger.info(") == 2

    def test_probe_accepts_provisioned_binary(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        binary = self._provision_fake(tmp_path, monkeypatch)
        monkeypatch.setenv("SHERRY_SG_PATH", str(binary))
        resolver._clear_cache_for_tests()
        assert resolver.resolve_sg_binary() is not None
