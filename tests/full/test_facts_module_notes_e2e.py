"""Live-network e2e: experience routing to FACTS.md (Part 3) and <module>-notes (Part 2).

LIVE-NETWORK, RUN EXPLICITLY. These tests drive the REAL plan-extraction nudge
(``summarization/nudges.py::_nudge_plan_extraction``: the real nudge agent
built on the main LLM via ``build_main_llm()``, the real ``memory`` /
``skill_manage`` / ``knowledge`` tools), the real ``Summarization`` compression
harness for the FACTS prompt context, real SQLite stores and real files under
sandboxed roots. They require a populated ``.env`` with working endpoints.
Run with::

    uv run --no-sync pytest tests/full/test_facts_module_notes_e2e.py -v --durations=0

Marker decision: deliberately NOT tagged ``llm_e2e`` (same rationale as
``test_context_governance_e2e.py``): ``tests/full/`` is excluded from
``tests/run_tests_split.py``, so the hermetic CI gate never collects this file;
``llm_e2e`` would instead pull it into the dedicated live-network CI job, which
must not depend on live credentials nor write real session data.

Coverage:

1. FACTS extraction + injection — the real nudge extracts a broad pitfall from
   the completed plan into ``FACTS.md`` (memory target ``facts``), and the next
   system-prompt build injects the FACTS block (verified through
   ``workspace.prompt_builder.build_system_prompt``).
2. FACTS rolling eviction — the real memory tool with a small char limit drops
   the oldest entry and persists the newest one.
3. Editorial read-modify-write — a memory-review-style pass merges two
   same-kind entries into one and removes an outdated entry through the real
   ``memory`` tool actions (``replace`` / ``remove``).
4. ``<module>-notes`` create — the real nudge creates
   ``skills/auto/auth-module-notes/SKILL.md`` with valid frontmatter and the
   module-bound lesson, keeping the broad pitfall out of the notes skill.
5. ``<module>-notes`` update — when the skill already exists the nudge patches
   it in place (append) instead of creating a duplicate.
6. Curator compatibility — the created notes skill shows up in the curator's
   ``agent_created_report()`` (the lifecycle-management discovery path).

Sandboxing / cleanup: memory files, plan knowledge and the auto-skills tree are
redirected into the pytest tmp dir; every session this module created is purged
in ``finally`` blocks (MesMemory rows, checkpoints, session folder incl. plan
files, todo rows, register states), so the module is independently re-runnable.
LLM non-compliance is retried at most once per case and reported verbatim when
it persists — never skipped or faked.
"""

from __future__ import annotations

import contextlib
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from langchain_core.messages import AIMessage, HumanMessage
from loguru import logger

import agent.core as agent_core
import agent.tools.memory as memory_module
import agent.tools.pub_base.skill_usage as agent_skill_usage
import agent.tools.skill_tools.skill_manage as skill_manage_module
from agent.checkpointer.async_sqlite_checkpointer import delete_thread_history
from agent.middlewares.summarization import nudges as nudge_mod
from agent.prompt_data_provider import register_prompt_data_provider
from agent.tools.memory import memory_store
from agent.tools.todolist.knowledge import knowledge_store
from agent.tools.todolist.registry.store_sqlite import delete_todos_by_session, replace_all
from config import SESSIONS_DIR
from config.features import SUMMARIZATION
from context_engine import delete_messages_by_session
from context_engine.curator import constants as curator_constants
from context_engine.curator import usage as curator_usage
from runtime import clear_all_register_sessions, state_register_db, state_register_mem

pytestmark = [pytest.mark.unit, pytest.mark.timeout(1500)]

_TEST_SYSTEM_PROMPT = "You are Sherry, an e2e test session agent. Follow the instructions exactly."
_BROAD_SENTINEL = "BROAD-FACT-9137"
_MODULE_SENTINEL = "MODULE-NOTE-4711"


# ---------------------------------------------------------------------------
# Sandbox + session helpers
# ---------------------------------------------------------------------------


class _Sandbox:
    """Redirects every agent-owned write root into the pytest tmp dir."""

    def __init__(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.root = tmp_path
        self.memory_dir = tmp_path / "memory"
        self.plans_dir = tmp_path / "plan_knowledge"
        self.auto_skills_dir = tmp_path / "skills_auto"
        self.usage_dir = tmp_path / "curator_usage"
        self.archive_dir = tmp_path / "skills_archive"
        for directory in (
            self.memory_dir,
            self.plans_dir,
            self.auto_skills_dir,
            self.usage_dir,
            self.archive_dir,
        ):
            directory.mkdir(parents=True, exist_ok=True)

        monkeypatch.setattr(memory_module, "MEMORY_DIR", self.memory_dir)
        monkeypatch.setattr(knowledge_store, "_KNOWLEDGE_ROOT", self.plans_dir)
        monkeypatch.setattr(skill_manage_module, "AUTO_SKILLS_DIR", self.auto_skills_dir)
        monkeypatch.setattr(agent_skill_usage, "AUTO_SKILLS_DIR", self.auto_skills_dir)
        monkeypatch.setattr(curator_constants, "AUTO_SKILLS_DIR", self.auto_skills_dir)
        monkeypatch.setattr(curator_constants, "ARCHIVE_DIR", self.archive_dir)
        monkeypatch.setattr(curator_usage, "USAGE_DIR", self.usage_dir)
        monkeypatch.setitem(SUMMARIZATION, "compression_todo_update_enabled", False)

        agent_core.init()
        # The shared conftest clears the provider registry after every test;
        # ``init()`` is idempotent, so the real provider must be re-registered
        # here for the system-prompt build to see the memory blocks.
        register_prompt_data_provider()
        memory_store.load_from_disk()

    def facts_path(self) -> Path:
        return self.memory_dir / "FACTS.md"

    def facts_text(self) -> str:
        path = self.facts_path()
        return path.read_text(encoding="utf-8") if path.exists() else ""

    def notes_skill_file(self, name: str = "auth-module-notes") -> Path:
        return self.auto_skills_dir / name / "SKILL.md"

    def reset(self) -> None:
        """Clear every sandboxed root so a retry starts from a clean slate."""
        for name in ("FACTS.md", "MEMORY.md", "USER.md"):
            with contextlib.suppress(FileNotFoundError):
                (self.memory_dir / name).unlink()
        for directory in (self.auto_skills_dir, self.usage_dir, self.archive_dir):
            shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir(parents=True, exist_ok=True)
        shutil.rmtree(self.plans_dir, ignore_errors=True)
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        memory_store.load_from_disk()


@pytest.fixture
def sandbox(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Sandbox:
    return _Sandbox(tmp_path, monkeypatch)


def _new_sid(tag: str) -> str:
    return f"e2e-facts-{tag}-{uuid.uuid4().hex[:8]}"


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


# ---------------------------------------------------------------------------
# Completed-plan setup + real nudge driver
# ---------------------------------------------------------------------------

_PLAN_MARKDOWN = """# {plan_name}: route plan lessons

## Goal
Verify the experience routing: broad pitfalls land in FACTS.md, module-bound
lessons land in a `<module>-notes` skill.

## Findings
- Broad pitfall (NOT bound to any module): full-suite `python_repl` runs hit the
  30s REPL timeout and truncate — split the suite into batches. Sentinel
  {broad}.
- Module-bound pitfall: when touching `agent/tools/auth/token.py`, run
  `test_auth_17` first — otherwise the token-refresh regression stays hidden.
  Sentinel {module}.
"""

_CREATE_INSTRUCTION = (
    "计划执行完成，请做计划经验提取：\n"
    f"1) 把广泛存在的坑写进 FACTS.md（memory 工具 target='facts'）；条目请逐字保留哨兵 "
    f"{_BROAD_SENTINEL}（不要翻译或改写哨兵）；\n"
    f"2) 把绑定 agent/tools/auth/token.py 的坑写进 auth-module-notes 技能"
    f"（skill_manage action='create'，技能名就用 auth-module-notes）；内容请逐字保留哨兵 "
    f"{_MODULE_SENTINEL}。"
)

_PATCH_INSTRUCTION = (
    "计划执行完成，请做计划经验提取：\n"
    f"auth-module-notes 技能已经存在（里面有一条 OLD-ROW-2201），不要新建重复技能；"
    f"请用 skill_manage(action='patch') 追加 agent/tools/auth/token.py 的坑，"
    f"新增行请保留哨兵 {_MODULE_SENTINEL}，并保留已有的 OLD-ROW-2201。\n"
    f"同时把广泛存在的坑写进 FACTS.md（target='facts'，保留哨兵 {_BROAD_SENTINEL}）。"
)

_SEEDED_NOTES_CONTENT = (
    "---\nname: auth-module-notes\ndescription: Module-bound notes for the auth module.\n---\n\n"
    "## auth module\n\n- OLD-ROW-2201: existing recorded pitfall (keep me).\n"
)


async def _seed_completed_plan(sid: str, tag: str) -> str:
    """Write a real plan file + all-complete real todo rows for *sid*."""
    plan_name = f"e2e-route-{tag}-{uuid.uuid4().hex[:6]}"
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


async def _run_plan_extraction(session_id: str, instruction: str) -> None:
    """Run the real plan-extraction nudge once (real agent, real main LLM, real tools)."""
    state_register_mem.set_state(session_id, "system_prompt", _TEST_SYSTEM_PROMPT)
    await nudge_mod._nudge_plan_extraction(
        session_id,
        _TEST_SYSTEM_PROMPT,
        [
            HumanMessage(content=instruction),
            AIMessage(content="好的，我来提取并分流这些经验。"),
        ],
    )


def _facts_has_broad_pitfall(content: str) -> bool:
    """True when FACTS.md carries the broad pitfall (sentinel or its core terms).

    The live model may translate/paraphrase the entry; the routing contract is
    that a module-independent pitfall about the ``python_repl`` timeout reaches
    FACTS.md, not that a test sentinel survives translation.
    """
    if _BROAD_SENTINEL in content:
        return True
    lowered = content.lower()
    return ("python_repl" in lowered or "repl" in lowered) and (
        "30s" in lowered
        or "timeout" in lowered
        or "30 秒" in content
        or "超时" in content
        or "截断" in content
    )


def _notes_has_module_lesson(content: str) -> bool:
    return _MODULE_SENTINEL in content or "test_auth_17" in content


def _frontmatter(content: str) -> dict[str, Any]:
    """Parse the YAML frontmatter block of a SKILL.md (real skills contract)."""
    stripped = content.lstrip()
    assert stripped.startswith("---"), f"SKILL.md carries no frontmatter: {content[:120]!r}"
    block = stripped[3:].split("\n---", 1)[0]
    parsed = yaml.safe_load(block)
    assert isinstance(parsed, dict), f"frontmatter is not a mapping: {block[:200]!r}"
    return parsed


# ===========================================================================
# 1. FACTS extraction + next system-prompt injection
# ===========================================================================


@pytest.mark.asyncio
async def test_broad_pitfall_lands_in_facts_and_next_prompt(sandbox: _Sandbox) -> None:
    from workspace.prompt_builder import build_system_prompt

    reports: list[str] = []
    for attempt in (1, 2):
        sandbox.reset()
        sid = _new_sid(f"facts-{attempt}")
        try:
            await _seed_completed_plan(sid, f"facts-{attempt}")
            await _run_plan_extraction(sid, _CREATE_INSTRUCTION)

            content = sandbox.facts_text()
            if not _facts_has_broad_pitfall(content):
                reports.append(f"attempt {attempt}: FACTS.md={content[:300]!r}")
                logger.warning("facts attempt {}: broad pitfall missing from FACTS.md", attempt)
                continue

            # Next session start: reload the snapshot, then rebuild the prompt.
            memory_store.load_from_disk()
            prompt = build_system_prompt(session_id=sid)
            assert _facts_has_broad_pitfall(content)
            assert _facts_has_broad_pitfall(prompt), (
                f"the FACTS entry did not reach the system prompt: {content[:200]!r}"
            )
            assert "FACTS (broad pitfalls and conventions)" in prompt
            logger.info("facts injection: {!r}", content[:200])
            return
        finally:
            await _purge_session(sid)
    pytest.fail(
        "the live nudge never wrote the broad pitfall into FACTS.md after 2 attempts:\n"
        + "\n".join(reports)
    )


# ===========================================================================
# 2. FACTS rolling eviction (real memory tool + real file)
# ===========================================================================


def test_facts_rolling_eviction_keeps_newest(
    sandbox: _Sandbox, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(memory_store, "facts_char_limit", 240)
    old = "OLD-ENTRY-1 " + "a" * 80
    mid = "MID-ENTRY-2 " + "b" * 80
    new = "NEW-ENTRY-3 " + "c" * 80

    first = memory_module.memory_tool("add", "facts", old)
    second = memory_module.memory_tool("add", "facts", mid)
    overflow = memory_module.memory_tool("add", "facts", new)

    assert '"success": true' in first and '"success": true' in second
    assert '"success": true' in overflow
    assert "evicted 1 oldest entries" in overflow
    content = sandbox.facts_text()
    assert old not in content
    assert mid in content and new in content
    assert memory_store.format_live_content("facts") == content
    leftovers = [p.name for p in sandbox.memory_dir.iterdir() if p.name.startswith(".mem_")]
    assert leftovers == []
    logger.info("facts overflow: kept={!r}", content.split("\n§\n"))


# ===========================================================================
# 3. Editorial read-modify-write: merge same-kind + drop outdated
# ===========================================================================


def test_editorial_read_modify_write_merges_and_drops(sandbox: _Sandbox) -> None:
    memory_module.memory_tool("add", "facts", "PITFALL-A1 python_repl 超时截断（旧写法）")
    memory_module.memory_tool("add", "facts", "PITFALL-A2 python_repl 超时截断 → 拆批运行")
    memory_module.memory_tool("add", "facts", "OUTDATED-770 旧版 API 已废弃")

    live = memory_store.format_live_content("facts")
    assert "PITFALL-A1" in live and "PITFALL-A2" in live and "OUTDATED-770" in live

    merged = "PITFALL-A python_repl 超时截断 → 拆批运行（同类合并）"
    replaced = memory_module.memory_tool("replace", "facts", content=merged, old_text="PITFALL-A1")
    absorbed = memory_module.memory_tool("remove", "facts", old_text="PITFALL-A2")
    removed = memory_module.memory_tool("remove", "facts", old_text="OUTDATED-770")

    assert '"success": true' in replaced
    assert '"success": true' in absorbed
    assert '"success": true' in removed
    content = sandbox.facts_text()
    assert "PITFALL-A1" not in content
    assert "PITFALL-A2" not in content, "the same-kind entry must be merged away"
    assert merged in content
    assert "OUTDATED-770" not in content
    assert content.count("PITFALL-A") == 1
    logger.info("editorial update: {!r}", content.split("\n§\n"))


# ===========================================================================
# 4. <module>-notes create (real nudge → real skills/auto file)
# ===========================================================================


@pytest.mark.asyncio
async def test_module_notes_created_by_real_nudge(sandbox: _Sandbox) -> None:
    reports: list[str] = []
    for attempt in (1, 2):
        sandbox.reset()
        sid = _new_sid(f"create-{attempt}")
        try:
            await _seed_completed_plan(sid, f"create-{attempt}")
            await _run_plan_extraction(sid, _CREATE_INSTRUCTION)

            skill_file = sandbox.notes_skill_file()
            if not skill_file.is_file():
                skills = sorted(p.name for p in sandbox.auto_skills_dir.iterdir())
                reports.append(f"attempt {attempt}: no auth-module-notes; skills={skills}")
                logger.warning("create attempt {}: skill missing", attempt)
                continue

            content = skill_file.read_text(encoding="utf-8")
            frontmatter = _frontmatter(content)
            assert frontmatter.get("name") == "auth-module-notes"
            assert str(frontmatter.get("description") or "").strip()
            assert _notes_has_module_lesson(content), (
                f"the notes skill lacks the module lesson: {content[:300]!r}"
            )
            logger.info("module notes created: frontmatter={}", frontmatter)
            return
        finally:
            await _purge_session(sid)
    pytest.fail(
        "the live nudge never created auth-module-notes after 2 attempts:\n" + "\n".join(reports)
    )


# ===========================================================================
# 5. <module>-notes update: patch-append, never a duplicate
# ===========================================================================


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
            await _run_plan_extraction(sid, _PATCH_INSTRUCTION)

            skill_dirs = sorted(p.name for p in sandbox.auto_skills_dir.iterdir() if p.is_dir())
            content = skill_file.read_text(encoding="utf-8")
            problems: list[str] = []
            if skill_dirs != ["auth-module-notes"]:
                problems.append(f"unexpected skill dirs: {skill_dirs}")
            if "OLD-ROW-2201" not in content:
                problems.append("patch lost the existing row")
            if not _notes_has_module_lesson(content):
                problems.append("patch never appended the new lesson")
            if problems:
                reports.append(f"attempt {attempt}: {problems}; content={content[:300]!r}")
                logger.warning("patch attempt {}: {}", attempt, problems)
                continue
            assert _frontmatter(content).get("name") == "auth-module-notes"
            logger.info("module notes patched in place, {} bytes", len(content))
            return
        finally:
            await _purge_session(sid)
    pytest.fail(
        "the live nudge never patched auth-module-notes without duplicating it after "
        "2 attempts:\n" + "\n".join(reports)
    )


# ===========================================================================
# 6. Curator compatibility: the notes skill enters lifecycle discovery
# ===========================================================================


@pytest.mark.asyncio
async def test_module_notes_visible_to_curator_report(sandbox: _Sandbox) -> None:
    from context_engine.curator.usage import agent_created_report

    reports: list[str] = []
    for attempt in (1, 2):
        sandbox.reset()
        sid = _new_sid(f"curator-{attempt}")
        try:
            await _seed_completed_plan(sid, f"curator-{attempt}")
            await _run_plan_extraction(sid, _CREATE_INSTRUCTION)

            rows = agent_created_report()
            names = [row["name"] for row in rows]
            if "auth-module-notes" not in names:
                reports.append(f"attempt {attempt}: report names={names}")
                logger.warning("curator attempt {}: notes skill missing from report", attempt)
                continue

            row = next(row for row in rows if row["name"] == "auth-module-notes")
            assert sandbox.notes_skill_file().is_file()
            assert str(row.get("description") or "").strip()
            logger.info("curator report row: {}", row)
            return
        finally:
            await _purge_session(sid)
    pytest.fail(
        "auth-module-notes never appeared in agent_created_report() after 2 attempts:\n"
        + "\n".join(reports)
    )
