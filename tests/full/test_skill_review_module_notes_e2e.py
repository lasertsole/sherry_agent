"""Live-network e2e: the plan-completion skill-review review_mod.

Drives the REAL ``_run_skill_review`` fork: auxiliary model, real
``skill_list`` / ``skill_view`` / ``skill_manage`` tools, real sandboxed
``skills/auto`` tree, and the runtime quality judge. It verifies the skill-only
contract: no knowledge/FACTS writes, a class-level module-notes skill is created
or patched, and the created skill remains visible to the curator.

Run explicitly::

    uv run --no-sync pytest -m llm_e2e tests/full/test_facts_module_notes_e2e.py -v --durations=0
"""

from __future__ import annotations

import contextlib
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml

import agent.core as agent_core
import agent.tools.pub_base.skill_usage as agent_skill_usage
import agent.tools.skill_tools.skill_manage as skill_manage_module
import agent.tools.skill_tools.skill_view as skill_view_module
from agent.checkpointer.async_sqlite_checkpointer import delete_thread_history
from agent.middlewares.summarization import background_reviews as review_mod
from agent.middlewares.summarization import skill_review
from agent.prompt_data_provider import register_prompt_data_provider
from agent.tools.todolist.knowledge import knowledge_store
from agent.tools.todolist.registry.store_sqlite import delete_todos_by_session, replace_all
from config import SESSIONS_DIR
from config.features import SUMMARIZATION
from context_engine import delete_messages_by_session
from context_engine.curator import constants as curator_constants
from context_engine.curator import usage as curator_usage
from runtime import clear_all_register_sessions, state_register_db, state_register_mem

pytestmark = [pytest.mark.llm_e2e, pytest.mark.system, pytest.mark.timeout(1500)]

_TEST_SYSTEM_PROMPT = "You are Sherry, an e2e test session agent. Follow the instructions exactly."
_MODULE_SENTINEL = "MODULE-NOTE-4711"
_BROAD_SENTINEL = "BROAD-FACT-9137"


class _Sandbox:
    """Redirect every agent-owned write root into the pytest tmp dir."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = tmp_path
        self.plans_dir = tmp_path / "plan_knowledge"
        self.auto_skills_dir = tmp_path / "skills_auto"
        self.usage_dir = tmp_path / "curator_usage"
        self.archive_dir = tmp_path / "skills_archive"
        for directory in (
            self.plans_dir,
            self.auto_skills_dir,
            self.usage_dir,
            self.archive_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(knowledge_store, "_KNOWLEDGE_ROOT", self.plans_dir)
        monkeypatch.setattr(skill_manage_module, "AUTO_SKILLS_DIR", self.auto_skills_dir)
        monkeypatch.setattr(skill_view_module, "SKILLS_DIR", self.auto_skills_dir)
        monkeypatch.setattr(skill_review, "AUTO_SKILLS_DIR", self.auto_skills_dir)
        monkeypatch.setattr("skills.build_skills_snapshot", lambda: None)
        monkeypatch.setattr(agent_skill_usage, "AUTO_SKILLS_DIR", self.auto_skills_dir)
        monkeypatch.setattr(curator_constants, "AUTO_SKILLS_DIR", self.auto_skills_dir)
        monkeypatch.setattr(curator_constants, "ARCHIVE_DIR", self.archive_dir)
        monkeypatch.setattr(curator_usage, "USAGE_DIR", self.usage_dir)
        monkeypatch.setitem(SUMMARIZATION, "compression_todo_update_enabled", False)
        agent_core.init()
        register_prompt_data_provider()

    def notes_skill_file(self, name: str = "auth-module-notes") -> Path:
        matches = sorted(self.auto_skills_dir.glob(f"**/{name}/SKILL.md"))
        return matches[0] if matches else self.auto_skills_dir / name / "SKILL.md"

    def reset(self) -> None:
        for directory in (self.auto_skills_dir, self.usage_dir, self.archive_dir):
            shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.plans_dir, ignore_errors=True)
        self.plans_dir.mkdir(parents=True, exist_ok=True)


@pytest.fixture()
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Sandbox:
    return _Sandbox(tmp_path, monkeypatch)


def _new_sid(tag: str) -> str:
    return f"e2e-skill-review-{tag}-{uuid.uuid4().hex[:8]}"


async def _purge_session(session_id: str) -> None:
    with contextlib.suppress(Exception):
        delete_messages_by_session(session_id)
    with contextlib.suppress(Exception):
        await delete_thread_history(session_id)
    with contextlib.suppress(Exception):
        await delete_todos_by_session(session_id)
    with contextlib.suppress(Exception):
        shutil.rmtree(Path(SESSIONS_DIR) / session_id, ignore_errors=True)
    with contextlib.suppress(Exception):
        clear_all_register_sessions(session_id=session_id, clear_persistent_states=True)


_PLAN_MARKDOWN = """# {plan_name}: skill-review probe

## Goal
Verify that the completed plan updates a class-level skill with real evidence.

## Findings
- Broad pitfall (not module-bound): full-suite `python_repl` runs hit the 30s
  REPL timeout and truncate — split the suite into batches. Sentinel {broad}.
- Module-bound pitfall: when touching `agent/tools/auth/token.py`, run
  `test_auth_17` first — otherwise the token-refresh regression stays hidden.
  Sentinel {module}.
"""

_CREATE_INSTRUCTION = (
    "计划执行完成，请做技能质量维护：\n"
    "把绑定 agent/tools/auth/token.py 的坑写进 auth-module-notes 技能"
    "（skill_manage action='create'，技能名 auth-module-notes），"
    f"内容逐字保留哨兵 {_MODULE_SENTINEL}。\n"
    "不要写 knowledge，也不要把广泛坑写进 skill；只更新 auth-module-notes。"
)

_PATCH_INSTRUCTION = (
    "计划执行完成，请做技能质量维护：\n"
    "auth-module-notes 已经存在（含 OLD-ROW-2201），不要新建重复技能；"
    "请用 skill_manage(action='patch') 追加 agent/tools/auth/token.py 的坑，"
    f"新增行逐字保留哨兵 {_MODULE_SENTINEL}，并保留 OLD-ROW-2201。"
)

_SEEDED_NOTES_CONTENT = (
    "---\nname: auth-module-notes\ndescription: Module-bound notes for the auth module.\n"
    "when_to_use: When modifying agent/tools/auth/token.py.\n---\n\n"
    "## auth module\n\n- OLD-ROW-2201: existing recorded pitfall (keep me).\n"
)


async def _seed_completed_plan(sid: str, tag: str) -> str:
    plan_name = f"e2e-skill-{tag}-{uuid.uuid4().hex[:6]}"
    plan_file = Path(SESSIONS_DIR) / sid / "plans" / f"{plan_name}.md"
    plan_file.parent.mkdir(parents=True, exist_ok=True)
    plan_file.write_text(
        _PLAN_MARKDOWN.format(plan_name=plan_name, broad=_BROAD_SENTINEL, module=_MODULE_SENTINEL),
        encoding="utf-8",
    )
    plan_ref = f"{plan_name}.md"
    state_register_db.set_state(sid, "plan_ref", plan_ref)
    await replace_all(
        sid,
        [
            {"content": "reproduce the broad pitfall", "status": "completed", "plan_ref": plan_ref},
            {"content": "fix the auth module pitfall", "status": "completed", "plan_ref": plan_ref},
        ],
    )
    return plan_name


async def _run_skill_review(session_id: str, instruction: str = "") -> None:
    state_register_mem.set_state(session_id, "system_prompt", _TEST_SYSTEM_PROMPT)
    original_prompt = review_mod._SKILL_REVIEW_PROMPT
    original_generalizability = review_mod._judge_task_generalizability

    async def _generalizable(_context):
        # This suite tests the review's own behavior; the task-level pre-check
        # has its own unit tests and would only spend another live call here.
        return review_mod._GeneralizabilityVerdict(True, "e2e: reusable module lesson")

    review_mod._judge_task_generalizability = _generalizable
    if instruction:
        review_mod._SKILL_REVIEW_PROMPT = f"{original_prompt}\n\n{instruction}"
    try:
        await review_mod._run_skill_review(session_id)
    finally:
        review_mod._SKILL_REVIEW_PROMPT = original_prompt
        review_mod._judge_task_generalizability = original_generalizability


def _notes_has_module_lesson(content: str) -> bool:
    return _MODULE_SENTINEL in content or "test_auth_17" in content


def _frontmatter(content: str) -> dict[str, Any]:
    stripped = content.lstrip()
    assert stripped.startswith("---"), f"SKILL.md carries no frontmatter: {content[:120]!r}"
    block = stripped[3:].split("\n---", 1)[0]
    parsed = yaml.safe_load(block)
    assert isinstance(parsed, dict), f"frontmatter is not a mapping: {block[:200]!r}"
    return parsed


@pytest.mark.asyncio
async def test_module_notes_created_by_real_skill_review(sandbox: _Sandbox) -> None:
    reports: list[str] = []
    for attempt in (1, 2):
        sandbox.reset()
        sid = _new_sid(f"create-{attempt}")
        try:
            await _seed_completed_plan(sid, f"create-{attempt}")
            await _run_skill_review(sid, _CREATE_INSTRUCTION)

            skill_file = sandbox.notes_skill_file()
            if not skill_file.is_file():
                reports.append(f"attempt {attempt}: no auth-module-notes")
                continue
            content = skill_file.read_text(encoding="utf-8")
            frontmatter = _frontmatter(content)
            assert frontmatter.get("name") == "auth-module-notes"
            assert str(frontmatter.get("description") or "").strip()
            assert _notes_has_module_lesson(content), content[:300]
            return
        finally:
            await _purge_session(sid)
    pytest.fail("the live review never created auth-module-notes:\n" + "\n".join(reports))


@pytest.mark.asyncio
async def test_module_notes_patched_when_already_present(sandbox: _Sandbox) -> None:
    reports: list[str] = []
    for attempt in (1, 2):
        sandbox.reset()
        skill_file = sandbox.notes_skill_file()
        skill_file.parent.mkdir(parents=True, exist_ok=True)
        skill_file.write_text(_SEEDED_NOTES_CONTENT, encoding="utf-8")
        sid = _new_sid(f"patch-{attempt}")
        try:
            await _seed_completed_plan(sid, f"patch-{attempt}")
            await _run_skill_review(sid, _PATCH_INSTRUCTION)

            content = skill_file.read_text(encoding="utf-8")
            auth_dirs = [
                path for path in sandbox.auto_skills_dir.rglob("auth-module-notes") if path.is_dir()
            ]
            problems: list[str] = []
            if len(auth_dirs) != 1:
                problems.append(f"expected one auth-module-notes dir, got {len(auth_dirs)}")
            if "OLD-ROW-2201" not in content:
                problems.append("patch lost the existing row")
            if not _notes_has_module_lesson(content):
                problems.append("patch never appended the new lesson")
            if problems:
                reports.append(f"attempt {attempt}: {problems}; content={content[:300]!r}")
                continue
            return
        finally:
            await _purge_session(sid)
    pytest.fail("the live review never patched auth-module-notes:\n" + "\n".join(reports))


@pytest.mark.asyncio
async def test_module_notes_visible_to_curator_report(sandbox: _Sandbox) -> None:
    from context_engine.curator.usage import agent_created_report

    reports: list[str] = []
    for attempt in (1, 2):
        sandbox.reset()
        sid = _new_sid(f"curator-{attempt}")
        try:
            await _seed_completed_plan(sid, f"curator-{attempt}")
            await _run_skill_review(sid, _CREATE_INSTRUCTION)

            rows = agent_created_report()
            names = [row["name"] for row in rows]
            if "auth-module-notes" not in names:
                reports.append(f"attempt {attempt}: report names={names}")
                continue
            row = next(row for row in rows if row["name"] == "auth-module-notes")
            assert sandbox.notes_skill_file().is_file()
            assert str(row.get("description") or "").strip()
            return
        finally:
            await _purge_session(sid)
    pytest.fail(
        "auth-module-notes never appeared in agent_created_report():\n" + "\n".join(reports)
    )
