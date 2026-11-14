"""Skill-review → PTC compile handoff (best-effort, never rolls back the skill)."""

from __future__ import annotations

import pytest

import agent.middlewares.summarization.background_reviews as reviews
from agent.middlewares.summarization.skill_review import SkillTreeSnapshot

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_PLAN_KEY = "0123456789ab"


def _snapshots() -> tuple[SkillTreeSnapshot, SkillTreeSnapshot]:
    before = SkillTreeSnapshot()
    after = SkillTreeSnapshot(
        files={"backend/my-skill/SKILL.md": b"# My skill\nUse read_file.\n"},
        dirs=frozenset({"backend", "backend/my-skill"}),
    )
    return before, after


def _context() -> dict:
    return {
        "plan_name": "demo",
        "plan_key": _PLAN_KEY,
        "todos": [{"step_id": "step-1", "content": "read the config"}],
        "subagent_runs": [],
        "aggregated_handoff": {
            "success_steps": [
                {
                    "step": "read config",
                    "tool": "read_file",
                    "params": "file_path='config.json'",
                    "outcome": "ok",
                    "step_id": "step-1",
                    "flow_id": "flow-1",
                }
            ],
            "hard_constraints": ["use the relative path"],
        },
    }


@pytest.mark.asyncio
async def test_compiles_one_script_into_the_owning_skill(monkeypatch) -> None:
    before, after = _snapshots()
    writes: list[tuple[str, str, str]] = []
    compiled: list[dict] = []

    async def _compile(**kwargs):
        compiled.append(kwargs)
        return "from sherry_tools import read_file\nprint(read_file(file_path='config.json'))\n"

    def _write(name: str, file_path: str, content: str) -> dict:
        writes.append((name, file_path, content))
        return {"success": True}

    monkeypatch.setitem(reviews.PTC, "ptc_prefill_enabled", True)
    monkeypatch.setattr("agent.tools.ptc.compiler.compile_step_script", _compile)
    monkeypatch.setattr(
        "agent.tools.skill_tools.skill_manage._write_file",
        _write,
    )
    monkeypatch.setattr(reviews, "_ptc_tool_signatures", lambda _allowed: ["read_file(file_path)"])

    await reviews._compile_ptc_scripts(
        _context(),
        before,
        after,
        '{"status":"updated","skills":["my-skill"],"reason":"ok"}',
    )

    assert len(compiled) == 1
    assert compiled[0]["plan_key"] == _PLAN_KEY
    assert compiled[0]["step_id"] == "step-1"
    assert compiled[0]["flow_id"] == "flow-1"
    assert compiled[0]["task"] == "read the config"
    assert compiled[0]["allowed_tools"] == {"read_file"}
    assert writes == [
        (
            "my-skill",
            f"scripts/ptc__{_PLAN_KEY}__step-1.py",
            "from sherry_tools import read_file\nprint(read_file(file_path='config.json'))\n",
        )
    ]


@pytest.mark.asyncio
async def test_disabled_flag_writes_nothing(monkeypatch) -> None:
    before, after = _snapshots()
    writes: list[tuple] = []

    async def _compile(**_kwargs):
        raise AssertionError("compiler must not run when the flag is off")

    monkeypatch.setitem(reviews.PTC, "ptc_prefill_enabled", False)
    monkeypatch.setattr("agent.tools.ptc.compiler.compile_step_script", _compile)
    monkeypatch.setattr(
        "agent.tools.skill_tools.skill_manage._write_file",
        lambda *args: writes.append(args) or {"success": True},
    )

    await reviews._compile_ptc_scripts(_context(), before, after, "{}")

    assert writes == []


@pytest.mark.asyncio
async def test_flow_extraction_off_writes_nothing(monkeypatch) -> None:
    """The 高级 flow switch disables compilation even with PTC prefill enabled.

    PTC compilation is flow-extraction work, so the user-facing switch gates it
    BEFORE any owning-skill scan or compiler call. The gate is read from the
    session's effective settings (fail-open when the read raises).
    """
    before, after = _snapshots()
    writes: list[tuple] = []
    compiled: list[dict] = []

    async def _compile(**kwargs):
        compiled.append(kwargs)
        return "print('should not compile')\n"

    monkeypatch.setitem(reviews.PTC, "ptc_prefill_enabled", True)
    monkeypatch.setattr("runtime.session.extraction.flow_extraction_enabled", lambda _sid="": False)
    monkeypatch.setattr("agent.tools.ptc.compiler.compile_step_script", _compile)
    monkeypatch.setattr(
        "agent.tools.skill_tools.skill_manage._write_file",
        lambda *args: writes.append(args) or {"success": True},
    )

    await reviews._compile_ptc_scripts(_context(), before, after, "{}", session_id="sess-1")

    assert compiled == []
    assert writes == []


@pytest.mark.asyncio
async def test_no_changed_skill_md_writes_nothing(monkeypatch) -> None:
    before = SkillTreeSnapshot()
    after = SkillTreeSnapshot(files={"other/references/x.md": b"# Other\n"})
    writes: list[tuple] = []

    async def _compile(**_kwargs):
        raise AssertionError("no owning skill derived")

    monkeypatch.setitem(reviews.PTC, "ptc_prefill_enabled", True)
    monkeypatch.setattr("agent.tools.ptc.compiler.compile_step_script", _compile)
    monkeypatch.setattr(
        "agent.tools.skill_tools.skill_manage._write_file",
        lambda *args: writes.append(args) or {"success": True},
    )

    await reviews._compile_ptc_scripts(_context(), before, after, "{}")

    assert writes == []


@pytest.mark.asyncio
async def test_a_skip_from_the_compiler_writes_nothing(monkeypatch) -> None:
    before, after = _snapshots()
    writes: list[tuple] = []

    async def _compile(**_kwargs):
        return None

    monkeypatch.setitem(reviews.PTC, "ptc_prefill_enabled", True)
    monkeypatch.setattr("agent.tools.ptc.compiler.compile_step_script", _compile)
    monkeypatch.setattr(
        "agent.tools.skill_tools.skill_manage._write_file",
        lambda *args: writes.append(args) or {"success": True},
    )

    await reviews._compile_ptc_scripts(_context(), before, after, "{}")

    assert writes == []
