"""Real-component e2e: session isolation for TaskFlow, TodoList/Knowledge, subagents.

These tests drive the REAL implementations against isolated real resources — a
real SQLite TaskFlow store (tmp file), the real taskflow/knowledge/todowrite
tool coroutines, the real todos SQLite store, the real ownership resolver over
a real ``boulder.json`` sandbox, and the real ``spawn_subagent_direct`` pipeline
(real child-agent build + real ``apply_tool_policy``). No fake replaces the
code under test; only file locations / storage seams are redirected so each
test cleans up after itself and can be re-run independently.

Run explicitly::

    uv run --no-sync pytest -m llm_e2e tests/full/test_session_isolation_e2e.py -v --durations=0

Marker policy: tagged ``llm_e2e`` because case 4 drives a real
``spawn_subagent_direct`` run against the configured LLM (the other cases are
hermetic). A bare ``pytest`` run therefore deselects the file via the
``pyproject.toml`` addopts, and ``tests/run_tests_split.py`` additionally
``--ignore``s ``tests/full/`` outright.

Case map (one test each):

1. ``test_taskflow_two_session_isolation`` — A creates a real flow; B using A's
   ``flow_id`` gets "not found" from ``taskflow_summary`` / ``taskflow_finish``,
   A's row stays ``running``, B's ``taskflow_list`` never shows A's flow.
2. ``test_knowledge_two_session_isolation_and_no_overwrite`` — B (associated
   with another plan) is denied A's plan on read/write and cannot even see it in
   ``list``; a third non-associated session cannot overwrite A's document either.
   Two sessions that hold same-named plans in different files each write a
   different knowledge document under their own ``<plan_key>`` — neither write
   overwrites the other, and each reads its own back. Session-derived fallback
   names stay per-session.
3. ``test_knowledge_multi_session_boulder_collaboration`` — a work in a real
   ``boulder.json`` lists ``session_ids=[A, B]``: both may read/write the plan's
   knowledge; an unlisted session is denied.
4. ``test_subagent_toolset_excludes_planning_families`` — a real
   ``spawn_subagent_direct`` run captures the child's filtered tool list from
   the real ``apply_tool_policy`` call; taskflow/todowrite/todoread/knowledge
   are absent while the main-agent toolset does contain them.
5. ``test_taskflow_summary_context_is_session_scoped`` — with A and B each
   owning a flow, ``_get_taskflow_context_sync(session)`` (the compression
   prompt's TaskFlow block) renders only the calling session's flows.

The LLM-dependent case (4) retries once on a failed run; no web search is used.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import uuid
from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from agent.middlewares.summarization.core import _get_taskflow_context_sync
from agent.tools.taskflow import build_taskflow_tools
from agent.tools.todolist.knowledge import build_knowledge_tools
from agent.tools.todolist.knowledge import knowledge_store as knowledge_store_mod
from agent.tools.todolist.knowledge import ownership as ownership_mod
from agent.tools.todolist.knowledge.identity import fallback_plan_name, resolve_plan_identity
from agent.tools.todolist.registry import store_sqlite as todo_store
from agent.tools.taskflow.registry import store_sqlite as flow_store
from config import SESSIONS_DIR
from runtime import state_register_db

pytestmark = [pytest.mark.llm_e2e, pytest.mark.timeout(900)]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _new_sid(tag: str) -> str:
    """Unique-per-test session id."""
    return f"{tag[:3]}-{uuid.uuid4().hex[:8]}"


def _flow_tool(name: str):
    return {t.name: t for t in build_taskflow_tools()}[name]


def _knowledge_tool():
    return {t.name: t for t in build_knowledge_tools()}["knowledge"]


def _purge_state_rows(*session_ids: str) -> None:
    for session_id in session_ids:
        with contextlib.suppress(Exception):
            state_register_db.delete_state(session_id, "plan_ref")


async def _purge_child_session(child_session_key: str) -> None:
    """Best-effort removal of a spawned child's checkpoint + session folder."""
    with contextlib.suppress(Exception):
        from agent.checkpointer.async_sqlite_checkpointer import delete_thread_history

        await delete_thread_history(child_session_key)
    with contextlib.suppress(Exception):
        shutil.rmtree(Path(SESSIONS_DIR) / child_session_key, ignore_errors=True)


async def _poll_run(run_id: str, timeout: float = 180.0, interval: float = 1.0) -> Any:
    """Poll the subagent registry until the run reaches TERMINAL."""
    from agent.tools.subagent.registry import get_run
    from agent.tools.subagent.types.registry import ExecutionStatus

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    run = None
    while loop.time() < deadline:
        run = get_run(run_id)
        if run is not None and run.execution.status == ExecutionStatus.TERMINAL:
            return run
        await asyncio.sleep(interval)
    raise TimeoutError(f"Run {run_id} did not complete within {timeout}s")


# ---------------------------------------------------------------------------
# Fixtures (real stores, redirected to tmp paths; init state reset per test)
# ---------------------------------------------------------------------------


@pytest.fixture()
def flow_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Real TaskFlow SQLite store pointed at a per-test tmp file."""
    db_dir = tmp_path / "taskflow"
    monkeypatch.setattr(flow_store, "_DB_DIR", db_dir)
    monkeypatch.setattr(flow_store, "_DB_PATH", db_dir / "taskflow_registry.db")
    monkeypatch.setattr(flow_store, "_initialized", False)
    monkeypatch.setattr(flow_store, "_init_loop", None)
    monkeypatch.setattr(flow_store, "_init_lock", asyncio.Lock())
    monkeypatch.setattr(flow_store, "_sync_tables_ready", False)
    return db_dir


@pytest.fixture()
def knowledge_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Real knowledge store + real todos store + real ownership, tmp-rooted.

    ``ownership.state_register_db`` stays the REAL module; every ``plan_ref``
    row written through ``associate_state`` is deleted in teardown.
    ``resolve_boulder_path`` is redirected to a real boulder.json file created
    by the test on demand.
    """
    root = tmp_path / "plans"
    boulder = tmp_path / "boulder.json"
    monkeypatch.setattr(knowledge_store_mod, "_KNOWLEDGE_ROOT", root)
    monkeypatch.setattr(ownership_mod, "resolve_boulder_path", lambda: boulder)

    todos_dir = tmp_path / "todos"
    monkeypatch.setattr(todo_store, "_DB_DIR", todos_dir)
    monkeypatch.setattr(todo_store, "_DB_PATH", todos_dir / "todos.db")
    monkeypatch.setattr(todo_store, "_initialized", False)
    monkeypatch.setattr(todo_store, "_init_loop", None)
    monkeypatch.setattr(todo_store, "_init_lock", asyncio.Lock())
    monkeypatch.setattr(todo_store, "_sync_tables_ready", False)

    recorded: list[str] = []

    def associate_state(session_id: str, plan_name: str) -> None:
        recorded.append(session_id)
        state_register_db.set_state(
            session_id, "plan_ref", f"workspace/sessions/{session_id}/plans/{plan_name}.md"
        )

    yield {"root": root, "boulder": boulder, "associate_state": associate_state}
    _purge_state_rows(*recorded)


@pytest.fixture()
def clean_subagent_registry():
    """Empty the subagent registry before and after the test."""
    from agent.tools.subagent.registry import clear as clear_registry

    clear_registry()
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Case 1 — TaskFlow two-session isolation (real tools + real SQLite)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_taskflow_two_session_isolation(flow_env: Path) -> None:
    sid_a, sid_b = _new_sid("flow-a"), _new_sid("flow-b")

    created = await _flow_tool("taskflow_create").coroutine(
        session_id=sid_a, flow_id="flow-A", description="A's work"
    )
    assert "flow-A" in created, created

    summary_b = await _flow_tool("taskflow_summary").coroutine(session_id=sid_b, flow_id="flow-A")
    finish_b = await _flow_tool("taskflow_finish").coroutine(session_id=sid_b, flow_id="flow-A")
    assert "not found" in summary_b, summary_b
    assert "not found" in finish_b, finish_b

    row = await flow_store.get_flow("flow-A", sid_a)
    assert row is not None and row["status"] == "running", row

    list_a = await _flow_tool("taskflow_list").coroutine(session_id=sid_a)
    list_b = await _flow_tool("taskflow_list").coroutine(session_id=sid_b)
    assert "flow-A" in list_a, list_a
    assert "flow-A" not in list_b, list_b

    summary_a = await _flow_tool("taskflow_summary").coroutine(session_id=sid_a, flow_id="flow-A")
    assert "flow-A" in summary_a and "A's work" in summary_a, summary_a
    logger.info("[iso e2e] B's foreign read: {!r}", summary_b.strip())


# ---------------------------------------------------------------------------
# Case 2 — Knowledge two-session isolation + no-overwrite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_knowledge_two_session_isolation_and_no_overwrite(
    knowledge_env: dict[str, Any],
) -> None:
    root: Path = knowledge_env["root"]
    tool = _knowledge_tool()

    sid_a, sid_b = _new_sid("know-a"), _new_sid("know-b")
    # A's association: real state-register plan_ref. B's: real todos plan_ref.
    knowledge_env["associate_state"](sid_a, "alpha")
    await todo_store.replace_all(
        sid_b,
        [
            {
                "content": "b work",
                "status": "pending",
                "plan_ref": f"workspace/sessions/{sid_b}/plans/beta.md",
            }
        ],
    )

    written_a = await tool.coroutine(
        action="write",
        plan_name="alpha",
        layer="plan",
        data={"method": "alpha-method"},
        session_id=sid_a,
    )
    assert written_a.startswith("Knowledge written to "), written_a

    # B (associated with beta) cannot read / write / list A's plan.
    read_b = await tool.coroutine(action="read", plan_name="alpha", session_id=sid_b)
    write_b = await tool.coroutine(
        action="write", plan_name="alpha", layer="plan", data={"method": "stolen"}, session_id=sid_b
    )
    list_b = await tool.coroutine(action="list", session_id=sid_b)
    assert read_b.startswith("Error: knowledge read denied for plan 'alpha'"), read_b
    assert write_b.startswith("Error: knowledge write denied for plan 'alpha'"), write_b
    assert "alpha" not in list_b, list_b
    alpha_identity = resolve_plan_identity(sid_a, "alpha")
    assert alpha_identity is not None
    assert (root / alpha_identity.key / "plan-summary.json").is_file()

    # Same name, different plan files: each session resolves its own <plan_key>.
    knowledge_env["associate_state"](sid_a, "dup-plan")
    await todo_store.replace_all(
        sid_b,
        [
            {
                "content": "b dup",
                "status": "pending",
                "plan_ref": f"workspace/sessions/{sid_b}/plans/dup-plan.md",
            }
        ],
    )
    dup_a = await tool.coroutine(
        action="write",
        plan_name="dup-plan",
        layer="plan",
        data={"method": "from-A"},
        session_id=sid_a,
    )
    dup_b = await tool.coroutine(
        action="write",
        plan_name="dup-plan",
        layer="task",
        position=0,
        data={"method": "from-B"},
        session_id=sid_b,
    )
    assert dup_a.startswith("Knowledge written to "), dup_a
    assert dup_b.startswith("Knowledge written to "), dup_b
    dup_a_identity = resolve_plan_identity(sid_a, "dup-plan")
    dup_b_identity = resolve_plan_identity(sid_b, "dup-plan")
    assert dup_a_identity is not None and dup_b_identity is not None
    assert dup_a_identity.key != dup_b_identity.key
    assert (
        json.loads((root / dup_a_identity.key / "plan-summary.json").read_text())["method"]
        == "from-A"
    )
    assert json.loads((root / dup_b_identity.key / "task-0.json").read_text())["method"] == "from-B"

    read_a_own = await tool.coroutine(
        action="read", plan_name="dup-plan", layer="plan", session_id=sid_a
    )
    read_b_own = await tool.coroutine(
        action="read", plan_name="dup-plan", layer="task", position=0, session_id=sid_b
    )
    assert "from-A" in read_a_own, read_a_own
    assert "from-B" in read_b_own, read_b_own

    # A third, non-associated session cannot overwrite either document.
    sid_c = _new_sid("know-c")
    write_c = await tool.coroutine(
        action="write",
        plan_name="dup-plan",
        layer="plan",
        data={"method": "stolen"},
        session_id=sid_c,
    )
    assert write_c.startswith("Error: knowledge write denied for plan 'dup-plan'"), write_c
    assert (
        json.loads((root / dup_a_identity.key / "plan-summary.json").read_text())["method"]
        == "from-A"
    )


@pytest.mark.asyncio
async def test_knowledge_session_fallback_names_are_per_session(
    knowledge_env: dict[str, Any],
) -> None:
    """Same naming scheme, no shared association: per-session copies, no overwrite."""
    root: Path = knowledge_env["root"]
    tool = _knowledge_tool()
    sid_a, sid_b = _new_sid("fb-a"), _new_sid("fb-b")
    name_a, name_b = fallback_plan_name(sid_a), fallback_plan_name(sid_b)

    write_a = await tool.coroutine(
        action="write",
        plan_name=name_a,
        layer="plan",
        data={"method": "fallback-A"},
        session_id=sid_a,
    )
    write_b = await tool.coroutine(
        action="write",
        plan_name=name_b,
        layer="plan",
        data={"method": "fallback-B"},
        session_id=sid_b,
    )
    assert write_a.startswith("Knowledge written to ") and write_b.startswith(
        "Knowledge written to "
    )

    read_a = await tool.coroutine(action="read", plan_name=name_a, layer="plan", session_id=sid_a)
    read_b = await tool.coroutine(action="read", plan_name=name_b, layer="plan", session_id=sid_b)
    cross = await tool.coroutine(action="read", plan_name=name_b, layer="plan", session_id=sid_a)
    assert "fallback-A" in read_a and "fallback-B" not in read_a, read_a
    assert "fallback-B" in read_b and "fallback-A" not in read_b, read_b
    assert cross.startswith("Error: knowledge read denied"), cross
    fallback_a = resolve_plan_identity(sid_a, name_a)
    fallback_b = resolve_plan_identity(sid_b, name_b)
    assert fallback_a is not None and fallback_a.is_session_fallback is True
    assert fallback_b is not None and fallback_b.is_session_fallback is True
    assert fallback_a.key != fallback_b.key
    assert (root / fallback_a.key / "plan-summary.json").is_file()
    assert (root / fallback_b.key / "plan-summary.json").is_file()


# ---------------------------------------------------------------------------
# Case 3 — Multi-session collaboration through a real boulder.json
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_knowledge_multi_session_boulder_collaboration(knowledge_env: dict[str, Any]) -> None:
    root: Path = knowledge_env["root"]
    boulder: Path = knowledge_env["boulder"]
    sid_a, sid_b, sid_c = _new_sid("bd-a"), _new_sid("bd-b"), _new_sid("bd-c")

    boulder.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "works": {
                    "work-0": {
                        "plan_name": "shared-plan",
                        "session_ids": [sid_a, sid_b],
                        "active_plan": f"workspace/sessions/{sid_a}/plans/shared-plan.md",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    tool = _knowledge_tool()
    write_a = await tool.coroutine(
        action="write",
        plan_name="shared-plan",
        layer="plan",
        data={"method": "from-A"},
        session_id=sid_a,
    )
    read_b = await tool.coroutine(
        action="read", plan_name="shared-plan", layer="plan", session_id=sid_b
    )
    write_b = await tool.coroutine(
        action="write",
        plan_name="shared-plan",
        layer="task",
        position=0,
        data={"method": "from-B"},
        session_id=sid_b,
    )
    read_a = await tool.coroutine(
        action="read", plan_name="shared-plan", layer="task", position=0, session_id=sid_a
    )
    list_b = await tool.coroutine(action="list", session_id=sid_b)
    denied_c = await tool.coroutine(action="read", plan_name="shared-plan", session_id=sid_c)

    assert write_a.startswith("Knowledge written to "), write_a
    assert "from-A" in read_b, read_b
    assert write_b.startswith("Knowledge written to "), write_b
    assert "from-B" in read_a, read_a
    assert "shared-plan" in list_b, list_b
    assert denied_c.startswith("Error: knowledge read denied"), denied_c
    shared_identity = resolve_plan_identity(sid_a, "shared-plan")
    assert shared_identity is not None
    assert (root / shared_identity.key / "plan-summary.json").is_file()
    shared_dirs = [entry for entry in root.iterdir() if entry.is_dir()]
    assert len(shared_dirs) == 1


# ---------------------------------------------------------------------------
# Case 4 — Real spawn chain: child toolset excludes the planning families
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_subagent_toolset_excludes_planning_families(clean_subagent_registry, monkeypatch):
    from agent.tools import build_main_tools
    from agent.tools.subagent.spawn import core as spawn_core
    from agent.tools.subagent.spawn.core import spawn_subagent_direct
    from agent.tools.subagent.types.registry import RunOutcomeStatus
    from agent.tools.subagent.types.spawn import SpawnMode

    # Positive control: the main toolset really does contain the families.
    main_names = {t.name for t in build_main_tools()}
    planners = {name for name in main_names if name.startswith("taskflow_")} | {
        "todowrite",
        "todoread",
        "knowledge",
    }
    assert {"taskflow_summary", "todowrite", "knowledge"} <= planners

    captured: list[list[str]] = []
    real_policy = spawn_core.apply_tool_policy

    def _spy_policy(tools, tool_allow, tool_deny, blocked_tools=None):
        filtered = real_policy(tools, tool_allow, tool_deny, blocked_tools)
        captured.append([getattr(t, "name", str(t)) for t in filtered])
        return filtered

    monkeypatch.setattr(spawn_core, "apply_tool_policy", _spy_policy)

    sid = _new_sid("spawn")
    requester = f"agent:main:session:{sid}"
    task = "Reply with exactly: iso-ok. Do not call any tools."

    result = await spawn_subagent_direct(
        task=task,
        requester_session_key=requester,
        agent_id="main",
        spawn_mode=SpawnMode.RUN,
        cleanup="delete",
        run_timeout_seconds=120.0,
    )
    assert result.status == "accepted", f"{result.status}: {result.error}"

    loop = asyncio.get_running_loop()
    deadline = loop.time() + 120.0
    while not captured and loop.time() < deadline:
        await asyncio.sleep(0.5)
    assert captured, "apply_tool_policy was never called for the child agent"
    child_names = set(captured[0])
    leaked = child_names & planners
    assert not leaked, f"planning tools leaked into the child toolset: {sorted(leaked)}"

    run = await _poll_run(result.run_id, timeout=180.0)
    if run.execution.outcome is None or run.execution.outcome.status != RunOutcomeStatus.OK:
        detail = None if run.execution.outcome is None else run.execution.outcome.error
        logger.warning("[iso e2e] spawn attempt 1 not OK ({}); retrying once", detail)
        retry = await spawn_subagent_direct(
            task=task,
            requester_session_key=requester,
            agent_id="main",
            spawn_mode=SpawnMode.RUN,
            cleanup="delete",
            run_timeout_seconds=120.0,
        )
        assert retry.status == "accepted", f"{retry.status}: {retry.error}"
        run = await _poll_run(retry.run_id, timeout=180.0)
        result = retry

    assert run.execution.outcome is not None
    assert run.execution.outcome.status == RunOutcomeStatus.OK, (
        f"child run outcome: {run.execution.outcome.status} — {run.execution.outcome.error}"
    )
    text = (run.completion.result_text or "").strip()
    logger.info("[iso e2e] child result: {!r}; child tools: {}", text[:120], sorted(child_names))
    assert result.child_session_key is not None
    await _purge_child_session(result.child_session_key)


# ---------------------------------------------------------------------------
# Case 5 — TaskFlow context (compression prompt block) is session-scoped
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_taskflow_summary_context_is_session_scoped(flow_env: Path) -> None:
    sid_a, sid_b = _new_sid("ctx-a"), _new_sid("ctx-b")
    state_a = {
        "description": "A-only flow",
        "steps": [{"step_id": "s1", "task": "do-A-work", "status": "ready", "depends_on": []}],
        "results": [],
        "creator_session_key": f"agent:main:session:{sid_a}",
    }
    state_b = {
        "description": "B-only flow",
        "steps": [{"step_id": "s1", "task": "do-B-work", "status": "ready", "depends_on": []}],
        "results": [],
        "creator_session_key": f"agent:main:session:{sid_b}",
    }
    await flow_store.create_flow("flow-ctx-A", state_a, session_id=sid_a)
    await flow_store.create_flow("flow-ctx-B", state_b, session_id=sid_b)

    ctx_a = _get_taskflow_context_sync(sid_a)
    ctx_b = _get_taskflow_context_sync(sid_b)

    assert "flow-ctx-A" in ctx_a and "do-A-work" in ctx_a, ctx_a
    assert "flow-ctx-B" not in ctx_a, ctx_a
    assert "flow-ctx-B" in ctx_b and "do-B-work" in ctx_b, ctx_b
    assert "flow-ctx-A" not in ctx_b, ctx_b
    logger.info("[iso e2e] session-scoped taskflow context:\n{}", ctx_a)
