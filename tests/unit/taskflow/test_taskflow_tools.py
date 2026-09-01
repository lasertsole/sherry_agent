"""Behavior tests for the taskflow tool family, its skill file, and wiring.

Covers the six acceptance checks:
1. build_main_tools() exposes all 8 taskflow_* tools
2. create -> run_task -> resume -> finish full chain, readable back across a
   simulated restart (fresh connections / new event loop)
3. concurrent double-write on one flow: exactly one expectedRevision conflict
   and the error carries the latest revision
4. skills/taskflow/SKILL.md discovered via scan_skills(use_cache=False) with
   scope=main_only (main visible, subagent not)
5. resume injects a child result idempotently (same result never injected twice)
6. the tool family loads with no P0-1 guard layer wired

The run_task dispatcher is a module-level injectable reference
(taskflow_run_task._dispatch_child) and is monkeypatched in every test here:
the real spawn pipeline is never invoked.
"""

import asyncio
import sys
from pathlib import Path

import pytest

from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.config import INITIAL_REVISION
from agent.tools.taskflow.registry import store_sqlite

# The real module, NOT the tool object: the family package re-exports the tool
# under the same name, so package-attribute traversal (from-imports, pytest
# string targets) resolves to the StructuredTool. sys.modules keys are exact
# strings and immune to that shadowing.
taskflow_run_task_module = sys.modules["agent.tools.taskflow.tools.taskflow_run_task"]

EXPECTED_TOOL_NAMES = [
    "taskflow_create",
    "taskflow_run_task",
    "taskflow_set_waiting",
    "taskflow_resume",
    "taskflow_finish",
    "taskflow_fail",
    "taskflow_cancel",
    "taskflow_summary",
]


def _tool_map() -> dict:
    tools = build_taskflow_tools()
    assert len(tools) == 8
    return {t.name: t for t in tools}


def _fake_dispatch(child_key: str, calls: list | None = None):
    async def _dispatch(task: str, requester_session_key: str, label: str | None = None) -> str:
        if calls is not None:
            calls.append((task, requester_session_key, label))
        return child_key

    return _dispatch


# ---------------------------------------------------------------------------
# Acceptance 1: main toolset wiring
# ---------------------------------------------------------------------------


def test_builder_returns_full_family():
    tools = build_taskflow_tools()
    assert [t.name for t in tools] == EXPECTED_TOOL_NAMES
    assert all(t.handle_tool_error for t in tools)


def test_build_main_tools_contains_taskflow_family(build_main_tools_real):
    names = [t.name for t in build_main_tools_real()]
    for prefix in EXPECTED_TOOL_NAMES:
        assert any(n.startswith(prefix) for n in names), f"missing {prefix} in {names}"


# ---------------------------------------------------------------------------
# Acceptance 2: full chain across a simulated restart
# ---------------------------------------------------------------------------


def test_full_chain_create_run_resume_finish_across_restart(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    dispatched: list = []

    async def fake_dispatch(task: str, requester_session_key: str, label: str | None = None) -> str:
        dispatched.append((task, requester_session_key, label))
        return "agent:main:subagent:child-1"

    monkeypatch.setattr(taskflow_run_task_module, "_dispatch_child", fake_dispatch)

    tools = _tool_map()

    async def phase1() -> None:
        out = await tools["taskflow_create"].coroutine(
            flow_id="flow-1", description="demo chain"
        )
        assert "flow-1" in out
        assert "revision=1" in out

        out = await tools["taskflow_run_task"].coroutine(
            flow_id="flow-1", task="collect data", session_id="sess-1"
        )
        assert "child-1" in out
        assert "step" in out

        out = await tools["taskflow_resume"].coroutine(
            flow_id="flow-1",
            child_session_key="agent:main:subagent:child-1",
            result="all data collected",
        )
        assert "TaskFlow resumed" in out

        out = await tools["taskflow_finish"].coroutine(flow_id="flow-1", summary="chain done")
        assert "status=done" in out

    asyncio.run(phase1())  # event loop 1 (first "process")

    # Simulated restart: fresh once-per-process init state; same db file.
    monkeypatch.setattr(store_sqlite, "_initialized", False)
    monkeypatch.setattr(store_sqlite, "_init_loop", None)
    monkeypatch.setattr(store_sqlite, "_init_lock", asyncio.Lock())
    monkeypatch.setattr(store_sqlite, "_sync_tables_ready", False)

    async def phase2() -> None:
        flow = await store_sqlite.get_flow("flow-1")
        assert flow is not None
        assert flow["status"] == "done"
        assert flow["child_session_key"] == "agent:main:subagent:child-1"
        assert flow["expected_revision"] == 4  # create + run_task + resume + finish
        assert flow["state"]["results"][0]["result"] == "all data collected"
        assert flow["state"]["steps"][0]["child_session_key"] == "agent:main:subagent:child-1"

        summary = await tools["taskflow_summary"].coroutine(flow_id="flow-1")
        assert "flow-1" in summary
        assert "done" in summary
        assert "child-1" in summary

    asyncio.run(phase2())  # event loop 2 (second "process")

    assert dispatched == [("collect data", "agent:main:session:sess-1", None)]


# ---------------------------------------------------------------------------
# Acceptance 3: optimistic-lock conflict carries the latest revision
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_conflict_exactly_one_wins_with_latest_revision(isolated_db: Path):
    tools = _tool_map()
    await tools["taskflow_create"].coroutine(flow_id="flow-1", description="conflict probe")

    results = await asyncio.gather(
        tools["taskflow_set_waiting"].coroutine(
            flow_id="flow-1", wait_reason="writer A", expected_revision=INITIAL_REVISION
        ),
        tools["taskflow_set_waiting"].coroutine(
            flow_id="flow-1", wait_reason="writer B", expected_revision=INITIAL_REVISION
        ),
        return_exceptions=True,
    )

    # Business errors are returned as text, not raised: no exception may escape.
    assert not any(isinstance(r, BaseException) for r in results), results
    texts = [str(r) for r in results]

    winners = [t for t in texts if "status=waiting" in t]
    conflicts = [t for t in texts if "conflict" in t.lower()]
    assert len(winners) == 1, texts
    assert len(conflicts) == 1, texts
    assert "latest revision=2" in conflicts[0]
    assert "expected_revision=1" in conflicts[0]

    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["status"] == "waiting"
    assert flow["expected_revision"] == 2


# ---------------------------------------------------------------------------
# Acceptance 4: skill file discovery and scope visibility
# ---------------------------------------------------------------------------


def test_taskflow_skill_discovered_and_scoped(scan_skills_real, skill_visible_to_real):
    skills = scan_skills_real()
    entry = next((s for s in skills if s["name"] == "taskflow"), None)
    assert entry is not None, "skills/taskflow/SKILL.md must be discovered by scan_skills"
    assert entry["scope"] == "main_only"
    assert entry["active"] is True
    assert entry["description"].strip(), "description must be a non-empty one-liner"

    # Visibility contract (skills/loader.py:63-79 semantics):
    assert skill_visible_to_real(entry, "main") is True
    assert skill_visible_to_real(entry, "subagent") is False


# ---------------------------------------------------------------------------
# Acceptance 5: resume is idempotent
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_resume_idempotent_no_double_injection(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(taskflow_run_task_module, "_dispatch_child", _fake_dispatch("agent:main:subagent:child-1"))
    tools = _tool_map()

    await tools["taskflow_create"].coroutine(flow_id="flow-1", description="resume probe")
    await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1", task="step one", session_id="sess-1"
    )

    first = await tools["taskflow_resume"].coroutine(
        flow_id="flow-1", child_session_key="agent:main:subagent:child-1", result="R1"
    )
    assert "TaskFlow resumed" in first
    flow_after_first = await store_sqlite.get_flow("flow-1")
    assert flow_after_first is not None
    assert flow_after_first["expected_revision"] == 3  # create + run_task + resume
    assert len(flow_after_first["state"]["results"]) == 1

    # Same (child_session_key, result) again: no second injection, no revision bump.
    second = await tools["taskflow_resume"].coroutine(
        flow_id="flow-1", child_session_key="agent:main:subagent:child-1", result="R1"
    )
    assert "already resumed" in second
    flow_after_second = await store_sqlite.get_flow("flow-1")
    assert flow_after_second is not None
    assert flow_after_second["expected_revision"] == 3
    assert len(flow_after_second["state"]["results"]) == 1

    # A DIFFERENT result is a new injection.
    third = await tools["taskflow_resume"].coroutine(
        flow_id="flow-1", child_session_key="agent:main:subagent:child-1", result="R2"
    )
    assert "TaskFlow resumed" in third
    flow_after_third = await store_sqlite.get_flow("flow-1")
    assert flow_after_third is not None
    assert len(flow_after_third["state"]["results"]) == 2


# ---------------------------------------------------------------------------
# Acceptance 6: the family loads with no P0-1 guard layer wired
# ---------------------------------------------------------------------------


def test_family_loads_without_p0_1_wiring():
    """The family is importable and buildable standalone: no guard layer, no
    server runtime, no spawn-pipeline import at module scope (the dispatch
    entry is a lazy module-level injectable reference)."""
    tools = build_taskflow_tools()
    assert len(tools) == 8
    assert all(t.name.startswith("taskflow_") for t in tools)
    # The injection seam exists and can be replaced (used by every dispatch test).
    assert hasattr(taskflow_run_task_module, "_dispatch_child")


# ---------------------------------------------------------------------------
# Tool-level state machine details
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_create_rejects_duplicate(isolated_db: Path):
    tools = _tool_map()
    await tools["taskflow_create"].coroutine(flow_id="flow-1", description="first")
    out = await tools["taskflow_create"].coroutine(flow_id="flow-1", description="second")
    assert "already exists" in out


@pytest.mark.asyncio
async def test_set_waiting_then_resume_cycle(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(taskflow_run_task_module, "_dispatch_child", _fake_dispatch("agent:main:subagent:child-2"))
    tools = _tool_map()
    await tools["taskflow_create"].coroutine(flow_id="flow-1")

    out = await tools["taskflow_set_waiting"].coroutine(
        flow_id="flow-1", wait_reason="awaiting child result"
    )
    assert "status=waiting" in out
    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["status"] == "waiting"
    assert flow["wait"] is not None
    assert flow["wait"]["reason"] == "awaiting child result"
    assert flow["expected_revision"] == 2

    out = await tools["taskflow_resume"].coroutine(
        flow_id="flow-1", child_session_key="agent:main:subagent:child-2", result="done"
    )
    assert "status=running" in out
    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["status"] == "running"
    assert flow["wait"] is None  # wait payload cleared on resume
    assert flow["expected_revision"] == 3


@pytest.mark.asyncio
async def test_run_task_registers_step_and_dispatch_args(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    dispatched: list = []

    async def fake_dispatch(task: str, requester_session_key: str, label: str | None = None) -> str:
        dispatched.append((task, requester_session_key, label))
        return "agent:main:subagent:child-7"

    monkeypatch.setattr(taskflow_run_task_module, "_dispatch_child", fake_dispatch)
    tools = _tool_map()
    await tools["taskflow_create"].coroutine(flow_id="flow-1", description="dispatch probe")

    out = await tools["taskflow_run_task"].coroutine(
        flow_id="flow-1", task="write report", label="report", session_id="sess-9"
    )
    assert "child-7" in out
    assert dispatched == [("write report", "agent:main:session:sess-9", "report")]

    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["child_session_key"] == "agent:main:subagent:child-7"
    assert flow["state"]["steps"][0]["task"] == "write report"
    assert flow["state"]["steps"][0]["child_session_key"] == "agent:main:subagent:child-7"
    assert flow["expected_revision"] == 2


@pytest.mark.asyncio
async def test_run_task_rejects_terminal_flow(isolated_db: Path):
    tools = _tool_map()
    await tools["taskflow_create"].coroutine(flow_id="flow-1")
    await tools["taskflow_cancel"].coroutine(flow_id="flow-1", reason="obsolete")

    out = await tools["taskflow_run_task"].coroutine(flow_id="flow-1", task="zombie step")
    assert "terminal" in out
    flow = await store_sqlite.get_flow("flow-1")
    assert flow is not None
    assert flow["expected_revision"] == 2  # unchanged by the rejected call


@pytest.mark.asyncio
async def test_finish_fail_cancel_transitions(isolated_db: Path):
    tools = _tool_map()
    for fid in ("flow-done", "flow-fail", "flow-cancel"):
        await tools["taskflow_create"].coroutine(flow_id=fid)

    out = await tools["taskflow_finish"].coroutine(flow_id="flow-done", summary="ok")
    assert "status=done" in out
    out = await tools["taskflow_fail"].coroutine(flow_id="flow-fail", reason="bad input")
    assert "status=failed" in out
    out = await tools["taskflow_cancel"].coroutine(flow_id="flow-cancel", reason="user aborted")
    assert "status=cancelled" in out

    done = await store_sqlite.get_flow("flow-done")
    failed = await store_sqlite.get_flow("flow-fail")
    cancelled = await store_sqlite.get_flow("flow-cancel")
    assert done is not None and done["state"]["summary"] == "ok"
    assert failed is not None and failed["state"]["failure_reason"] == "bad input"
    assert cancelled is not None and cancelled["state"]["cancel_reason"] == "user aborted"

    # Terminal flows are immutable.
    for fid in ("flow-done", "flow-fail", "flow-cancel"):
        out = await tools["taskflow_set_waiting"].coroutine(flow_id=fid, wait_reason="x")
        assert "terminal" in out


@pytest.mark.asyncio
async def test_unknown_flow_errors(isolated_db: Path):
    tools = _tool_map()
    out = await tools["taskflow_summary"].coroutine(flow_id="ghost")
    assert "not found" in out
    out = await tools["taskflow_resume"].coroutine(flow_id="ghost", result="x")
    assert "not found" in out
