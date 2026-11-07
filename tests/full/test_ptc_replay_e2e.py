"""Hermetic e2e: a compiled skill script actually runs through the prefill path.

Real PTC child process and RPC bridge, but a fake tool instead of a live model.
This is the integration point between the skill-owned script and the spawn-side
prefill: no LLM is involved in either the script choice or its execution.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import BaseModel

from agent.tools.subagent.spawn import core
from agent.tools.subagent.types.functional_role import FunctionalRole
from agent.tools.subagent.types.registry import SubagentRunRecord
from config.features import PTC

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

_PLAN = "0123456789ab"
_STEP = "step-1"


class _ReadArgs(BaseModel):
    file_path: str


class _ReadTool:
    name = "read_file"
    tool_call_schema = _ReadArgs

    async def ainvoke(self, args, config=None):
        return f"content:{args['file_path']}"


class _BoomTool(_ReadTool):
    async def ainvoke(self, args, config=None):
        raise ValueError("denied")


def _run() -> SubagentRunRecord:
    return SubagentRunRecord(
        run_id="run-e2e",
        child_session_key="agent:executor:subagent:e2e",
        requester_session_key="agent:main:session:parent",
        controller_session_key="agent:main:session:parent",
        task="read a file",
        functional_role=FunctionalRole.EXECUTOR,
        step_id=_STEP,
        spawned_cwd="/tmp",
    )


def _install_script(tmp_path: Path, code: str) -> None:
    script = tmp_path / "cat" / "owning-skill" / "scripts" / f"ptc__{_PLAN}__{_STEP}.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(code, encoding="utf-8")


@pytest.fixture(autouse=True)
def _patch_lookup(monkeypatch, tmp_path: Path):
    monkeypatch.setitem(PTC, "ptc_prefill_enabled", True)
    monkeypatch.setattr(
        "agent.tools.ptc.scripts.AUTO_SKILLS_DIR",
        tmp_path,
    )
    monkeypatch.setattr(
        "agent.tools.todolist.knowledge.identity.resolve_session_plan_identity",
        lambda _sid: SimpleNamespace(key=_PLAN),
    )


@pytest.mark.asyncio
async def test_prefill_runs_the_owned_script_and_returns_stdout(monkeypatch, tmp_path) -> None:
    _install_script(
        tmp_path,
        "from sherry_tools import read_file\nprint(read_file(file_path='x.txt'))\n",
    )
    monkeypatch.setattr(core, "_ptc_tools_map", lambda *_args: {"read_file": _ReadTool()})

    result, progress = await core._try_ptc_prefill(_run(), [], [], None)

    assert progress is None
    assert result is not None
    assert result["output"].strip() == "content:x.txt"
    assert result["calls_log"] == [
        {"tool": "read_file", "args": {"file_path": "x.txt"}, "result": "content:x.txt", "ok": True}
    ]


@pytest.mark.asyncio
async def test_prefill_tool_error_becomes_react_progress(monkeypatch, tmp_path) -> None:
    _install_script(
        tmp_path,
        "from sherry_tools import read_file\nprint(read_file(file_path='x.txt'))\n",
    )
    monkeypatch.setattr(core, "_ptc_tools_map", lambda *_args: {"read_file": _BoomTool()})

    result, progress = await core._try_ptc_prefill(_run(), [], [], None)

    assert result is None
    assert progress is not None
    assert "[PTC Pre-run Progress]" in progress
    assert "denied" in progress
    assert "Failed at step 1" in progress
