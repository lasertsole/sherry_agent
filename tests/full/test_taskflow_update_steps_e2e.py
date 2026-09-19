"""Live-network e2e: ``taskflow_update_steps`` on a real agent + real TaskFlow SQLite.

LIVE-NETWORK, RUN EXPLICITLY. These tests drive the production graph built by
``agent.core.built_agent()`` against the real configured main LLM, and the real
TaskFlow tools against a real per-test SQLite store. ``tests/full/`` is excluded
from ``tests/run_tests_split.py`` by construction, so the hermetic CI gate never
collects this file; it is exercised only by the explicit command::

    uv run --no-sync pytest tests/full/test_taskflow_update_steps_e2e.py -v --durations=0

Coverage (the Phase 7 e2e layer for ``taskflow_update_steps``):

1. ``test_llm_rewrite_unrun_step_then_real_dispatch`` — a real session seeds a
   2-step flow, the live model is instructed to call ``taskflow_update_steps``
   and rewrite an un-dispatched step's task, then the real ``taskflow_dispatch``
   spawns a real child on the NEW description; the child's run record task and
   its completed result prove dispatch consumed the rewritten list.
2. ``test_add_remove_reorder_then_dispatch_new_structure`` — a full replacement
   adds/removes/reorders steps; the removed id is no longer dispatchable, a
   step whose new dependency is unmet stays blocked, and a dispatch over the new
   list walks the new structure (recorded through the documented
   ``_dispatch.dispatch_child`` seam).
3. ``test_safety_rules_real_chain`` — dispatched step task rewrite keeps its
   child key; done-step task/status edits are rejected; fabricated dispatch
   statuses on new steps are rejected; removing a dispatched step whose child
   is still live emits a ``Warning:``.
4. ``test_rejection_is_visible_to_live_model`` — the live model attempts to edit
   a done step and receives the ``Error:`` tool result (model-view path).
5. ``test_cas_real_chain`` — a stale ``expected_revision`` returns the latest
   revision in the conflict text and leaves the stored list untouched.
6. ``test_cross_session_isolation_real_chain`` — session B's update against
   session A's flow is rejected as not found; A's flow is unchanged.

Discipline: real LLM (network errors retry once, non-compliance retries <=2 and
is reported verbatim, never a silent skip), real SQLite (tmp-redirected), real
subagent spawn (default ``cleanup="delete"``), self-cleaning and independently
re-runnable, no web search.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from loguru import logger

import agent.core as agent_core
import agent.tools.taskflow.tools._dispatch as dispatch_mod
from agent.checkpointer.async_sqlite_checkpointer import delete_thread_history
from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.registry import store_sqlite as flow_store
from config import SESSIONS_DIR
from context_engine import delete_messages_by_session
from pub.func.build_agent_config import build_agent_config
from runtime import clear_all_register_sessions

pytestmark = [pytest.mark.unit, pytest.mark.timeout(900)]


# ---------------------------------------------------------------------------
# Session / store helpers
# ---------------------------------------------------------------------------


def _new_sid(tag: str) -> str:
    return f"e2e-upd-{tag}-{uuid.uuid4().hex[:8]}"


def _step(
    step_id: str,
    task: str,
    *,
    status: str = "ready",
    depends_on: list[str] | None = None,
    child: str | None = None,
) -> dict:
    step: dict[str, Any] = {
        "step_id": step_id,
        "task": task,
        "depends_on": list(depends_on or []),
        "status": status,
    }
    if child is not None:
        step["child_session_key"] = child
    return step


async def _seed(sid: str, fid: str, steps: list[dict], status: str = "running") -> None:
    await flow_store.create_flow(
        fid,
        {"description": f"{fid} probe", "steps": list(steps), "results": []},
        session_id=sid,
        status=status,
    )


async def _stored(sid: str, fid: str) -> list[dict]:
    flow = await flow_store.get_flow(fid, sid)
    assert flow is not None, f"flow {fid!r} vanished for session {sid!r}"
    return list(flow["state"]["steps"])


async def _stored_revision(sid: str, fid: str) -> int:
    flow = await flow_store.get_flow(fid, sid)
    assert flow is not None
    return int(flow["expected_revision"])


async def _purge_session(session_id: str) -> None:
    """Delete every trace of a session this module created (best effort)."""
    with contextlib.suppress(Exception):
        delete_messages_by_session(session_id)
    with contextlib.suppress(Exception):
        await delete_thread_history(session_id)
    with contextlib.suppress(Exception):
        shutil.rmtree(Path(SESSIONS_DIR) / session_id, ignore_errors=True)
    with contextlib.suppress(Exception):
        clear_all_register_sessions(session_id=session_id, clear_persistent_states=True)


async def _poll_run(run_id: str, timeout: float = 240.0, interval: float = 1.0) -> Any:
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
# Fixtures
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
def clean_subagent_registry():
    """Empty the subagent registry before and after a test."""
    from agent.tools.subagent.registry import clear as clear_registry

    clear_registry()
    yield
    clear_registry()


# ---------------------------------------------------------------------------
# Real-graph helpers (live LLM)
# ---------------------------------------------------------------------------


async def _real_graph() -> Any:
    agent_core.init()
    return await agent_core.built_agent(temperature=0.0)


async def _invoke_with_retry(graph: Any, payload: dict, config: dict, attempts: int = 2) -> dict:
    """One network-error retry, per the e2e discipline (no silent skips)."""
    for attempt in range(1, attempts + 1):
        try:
            return await graph.ainvoke(payload, config)
        except Exception as exc:  # noqa: BLE001 - live network boundary
            if attempt >= attempts:
                raise
            logger.warning("graph.ainvoke attempt {} failed ({}); retrying once", attempt, exc)
            await asyncio.sleep(5)
    raise AssertionError("unreachable")


def _tool_messages(out: dict, name: str) -> list[ToolMessage]:
    return [
        m
        for m in out.get("messages", [])
        if isinstance(m, ToolMessage) and getattr(m, "name", None) == name
    ]


def _describe(out: dict, tool_name: str, tool_messages: list[ToolMessage]) -> str:
    names = [f"{type(m).__name__}:{getattr(m, 'name', None)}" for m in out.get("messages", [])]
    last_ai = next(
        (m for m in reversed(out.get("messages", [])) if isinstance(m, AIMessage)),
        None,
    )
    return (
        f"tool_messages={[m.content for m in tool_messages]!r}; "
        f"message_names={names}; last_ai={getattr(last_ai, 'content', None)!r}"
    )


def _update_prompt(fid: str, steps: list[dict]) -> str:
    """An explicit, low-ambiguity instruction to call the tool once."""
    payload = json.dumps(steps, ensure_ascii=False, separators=(",", ":"))
    return (
        "You must call the tool `taskflow_update_steps` exactly once with these exact "
        "arguments and nothing else:\n"
        f"  flow_id = {fid!r}\n"
        f"  steps = {payload}\n"
        "Use ONLY the values above; do not invent different step ids and do not call any "
        "other tool. After the tool returns, reply with the single word DONE."
    )


async def _drive_llm_update(
    graph: Any,
    *,
    tag: str,
    initial_steps: list[dict],
    new_steps: list[dict],
    attempts: int = 2,
) -> tuple[str, str, dict]:
    """Seed a flow, let the live model call update_steps; retry non-compliance.

    Returns ``(sid, fid, out)`` on success. On final failure reports exactly what
    the model did — no fake success, no skip.
    """
    last_note = "model never called taskflow_update_steps"
    for attempt in range(1, attempts + 1):
        sid = _new_sid(f"{tag}-{attempt}")
        fid = f"flow-{tag}-{attempt}"
        await _seed(sid, fid, initial_steps)
        out = await _invoke_with_retry(
            graph,
            {
                "messages": [HumanMessage(content=_update_prompt(fid, new_steps), id=f"h-{sid}")],
                "session_id": sid,
            },
            build_agent_config(sid),
        )
        tool_messages = _tool_messages(out, "taskflow_update_steps")
        if tool_messages and "TaskFlow steps updated" in str(tool_messages[-1].content):
            return sid, fid, out
        last_note = _describe(out, "taskflow_update_steps", tool_messages)
        logger.warning("[{}] attempt {}/{}: {}", tag, attempt, attempts, last_note)
        await _purge_session(sid)
    pytest.fail(
        f"[{tag}] live model did not call taskflow_update_steps successfully after "
        f"{attempts} attempts: {last_note}"
    )


# ---------------------------------------------------------------------------
# Case 1 — live model rewrites an un-run step, then a real dispatch runs it
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_llm_rewrite_unrun_step_then_real_dispatch(
    flow_env: Path, clean_subagent_registry
) -> None:
    graph = await _real_graph()
    marker = f"mk-{uuid.uuid4().hex[:8]}"
    new_task = f"Reply with exactly this token and nothing else: {marker}"

    initial = [
        _step("step-1", "old description one"),
        _step("step-2", "old description two"),
    ]
    new_steps = [
        _step("step-1", new_task),
        _step("step-2", "old description two"),
    ]

    t0 = time.perf_counter()
    sid, fid, _out = await _drive_llm_update(
        graph, tag="llm-rewrite", initial_steps=initial, new_steps=new_steps
    )
    try:
        steps = await _stored(sid, fid)
        assert [s["step_id"] for s in steps] == ["step-1", "step-2"], steps
        assert steps[0]["task"] == new_task, steps[0]
        assert steps[0]["status"] == "ready", steps[0]
        logger.info("[upd e2e] LLM rewrote step-1 in {:.1f}s", time.perf_counter() - t0)

        tools = {t.name: t for t in build_taskflow_tools()}
        dispatched = await tools["taskflow_dispatch"].coroutine(
            flow_id=fid, step_ids=["step-1"], session_id=sid
        )
        assert "TaskFlow steps dispatched" in dispatched, dispatched

        steps = await _stored(sid, fid)
        assert steps[0]["status"] == "dispatched", steps[0]
        child_key = steps[0]["child_session_key"]
        assert child_key, steps[0]

        from agent.tools.subagent.registry import get_run, get_run_by_child_session_key

        run = get_run_by_child_session_key(child_key)
        assert run is not None, "dispatch did not register a real child run"
        assert run.task == new_task, f"child ran the OLD task: {run.task!r}"

        done = await _poll_run(run.run_id)
        from agent.tools.subagent.types.registry import RunOutcomeStatus

        assert done.execution.outcome is not None, done.execution
        assert done.execution.outcome.status == RunOutcomeStatus.OK, done.execution.outcome
        result = (done.completion.result_text or "").strip()
        logger.info("[upd e2e] real child result: {!r}", result[:160])
        assert marker in result, f"child did not execute the NEW description: {result!r}"
        assert get_run(run.run_id) is not None
    finally:
        await _purge_session(sid)


# ---------------------------------------------------------------------------
# Case 2 — add / remove / reorder, then dispatch walks the new structure
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_add_remove_reorder_then_dispatch_new_structure(
    flow_env: Path, monkeypatch: pytest.MonkeyPatch, clean_subagent_registry
) -> None:
    sid = _new_sid("struct")
    fid = "flow-struct"
    await _seed(
        sid,
        fid,
        [
            _step("step-a", "task-a"),
            _step("step-b", "task-b"),
            _step("step-c", "task-c"),
        ],
    )
    tools = {t.name: t for t in build_taskflow_tools()}

    new_steps = [
        _step("step-c", "task-c"),
        _step("step-a", "task-a"),
        _step("step-d", "task-d", status="blocked", depends_on=["step-a"]),
    ]
    out = await tools["taskflow_update_steps"].coroutine(
        flow_id=fid, steps=new_steps, session_id=sid
    )
    assert "TaskFlow steps updated" in out, out
    assert "added=[step-d]" in out, out
    assert "removed=[step-b]" in out, out

    steps = await _stored(sid, fid)
    assert [s["step_id"] for s in steps] == ["step-c", "step-a", "step-d"], steps

    # The removed id is no longer dispatchable (new list is authoritative).
    gone = await tools["taskflow_dispatch"].coroutine(
        flow_id=fid, step_ids=["step-b"], session_id=sid
    )
    assert "unknown step_id" in gone, gone

    # The new step's dependency is unmet, so it stays blocked and un-dispatchable.
    blocked = await tools["taskflow_dispatch"].coroutine(
        flow_id=fid, step_ids=["step-d"], session_id=sid
    )
    assert "not dispatchable" in blocked, blocked

    # Dispatch over the new list records the NEW structure, in order.
    calls: list[str] = []

    async def fake_dispatch(task: str, requester_session_key: str, label: str | None = None) -> str:
        calls.append(task)
        return f"child-{len(calls)}"

    monkeypatch.setattr(dispatch_mod, "dispatch_child", fake_dispatch)
    dispatched = await tools["taskflow_dispatch"].coroutine(
        flow_id=fid, step_ids=["step-c", "step-a"], session_id=sid
    )
    assert "TaskFlow steps dispatched" in dispatched, dispatched
    assert calls == ["task-c", "task-a"], calls

    steps = await _stored(sid, fid)
    assert [s["status"] for s in steps] == ["dispatched", "dispatched", "blocked"], steps


# ---------------------------------------------------------------------------
# Case 3 — safety rules on the real store (dispatch/done/fabrication/orphan)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_safety_rules_real_chain(flow_env: Path, clean_subagent_registry) -> None:
    sid = _new_sid("safety")
    fid = "flow-safety"
    await _seed(
        sid,
        fid,
        [
            _step("s-done", "done task", status="done", child="child-done"),
            _step("s-disp", "running task", status="dispatched", child="child-disp"),
            _step("s-ready", "ready task"),
        ],
    )
    tools = {t.name: t for t in build_taskflow_tools()}
    update = tools["taskflow_update_steps"]

    # (a) A dispatched step may have its task rewritten, but keeps its child key.
    out = await update.coroutine(
        flow_id=fid,
        steps=[
            _step("s-done", "done task", status="done", child="child-done"),
            _step("s-disp", "rewritten while running", status="dispatched", child="child-disp"),
            _step("s-ready", "ready task"),
        ],
        session_id=sid,
    )
    assert "TaskFlow steps updated" in out, out
    steps = await _stored(sid, fid)
    disp = next(s for s in steps if s["step_id"] == "s-disp")
    assert disp["task"] == "rewritten while running", disp
    assert disp["child_session_key"] == "child-disp", disp

    # (b) A done step's task cannot change.
    err = await update.coroutine(
        flow_id=fid,
        steps=[
            _step("s-done", "CHANGED", status="done", child="child-done"),
            _step("s-disp", "rewritten while running", status="dispatched", child="child-disp"),
            _step("s-ready", "ready task"),
        ],
        session_id=sid,
    )
    assert "Error" in err and "done" in err and "cannot be changed" in err, err

    # (c) A done step's status cannot change.
    err = await update.coroutine(
        flow_id=fid,
        steps=[
            _step("s-done", "done task", status="ready"),
            _step("s-disp", "rewritten while running", status="dispatched", child="child-disp"),
            _step("s-ready", "ready task"),
        ],
        session_id=sid,
    )
    assert "Error" in err and "done" in err, err

    # (d) A brand-new step cannot claim a fabricated execution status.
    err = await update.coroutine(
        flow_id=fid,
        steps=[
            _step("s-done", "done task", status="done", child="child-done"),
            _step("s-disp", "rewritten while running", status="dispatched", child="child-disp"),
            _step("s-ready", "ready task"),
            _step("s-new", "fabricated", status="dispatched"),
        ],
        session_id=sid,
    )
    assert "Error" in err and "s-new" in err and "must be ready or blocked" in err, err

    # The stored list is still exactly the (a) result.
    steps = await _stored(sid, fid)
    assert [s["step_id"] for s in steps] == ["s-done", "s-disp", "s-ready"], steps


# ---------------------------------------------------------------------------
# Case 4 — a live model sees the safety rejection (model-view path)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rejection_is_visible_to_live_model(flow_env: Path) -> None:
    graph = await _real_graph()
    sid = _new_sid("reject")
    fid = "flow-reject"
    await _seed(
        sid,
        fid,
        [
            _step("s-done", "finished task", status="done", child="child-done"),
            _step("s-ready", "ready task"),
        ],
    )
    forbidden = [
        _step("s-done", "attempted edit", status="done", child="child-done"),
        _step("s-ready", "ready task"),
    ]
    last_note = "model never called taskflow_update_steps"
    for attempt in range(1, 3):
        if attempt > 1:
            fid = f"flow-reject-{attempt}"
            await _seed(
                sid,
                fid,
                [
                    _step("s-done", "finished task", status="done", child="child-done"),
                    _step("s-ready", "ready task"),
                ],
            )
        out = await _invoke_with_retry(
            graph,
            {
                "messages": [
                    HumanMessage(content=_update_prompt(fid, forbidden), id=f"h-{sid}-{attempt}")
                ],
                "session_id": sid,
            },
            build_agent_config(sid),
        )
        tool_messages = _tool_messages(out, "taskflow_update_steps")
        if tool_messages:
            text = str(tool_messages[-1].content)
            logger.info("[upd e2e] model-visible rejection: {!r}", text[:200])
            assert "Error" in text and "done" in text and "cannot be changed" in text, text
            steps = await _stored(sid, fid)
            assert next(s for s in steps if s["step_id"] == "s-done")["task"] == "finished task"
            await _purge_session(sid)
            return
        last_note = _describe(out, "taskflow_update_steps", tool_messages)
        logger.warning("[reject] attempt {}/2: {}", attempt, last_note)
    await _purge_session(sid)
    pytest.fail(f"[reject] live model never called taskflow_update_steps: {last_note}")


# ---------------------------------------------------------------------------
# Case 5 — CAS on the real store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cas_real_chain(flow_env: Path) -> None:
    sid = _new_sid("cas")
    fid = "flow-cas"
    await _seed(sid, fid, [_step("s1", "task one"), _step("s2", "task two")])
    tools = {t.name: t for t in build_taskflow_tools()}
    update = tools["taskflow_update_steps"]

    assert await _stored_revision(sid, fid) == 1  # INITIAL_REVISION
    r0 = await _stored_revision(sid, fid)
    good = await update.coroutine(
        flow_id=fid,
        steps=[_step("s1", "task one v2"), _step("s2", "task two")],
        expected_revision=r0,
        session_id=sid,
    )
    assert "TaskFlow steps updated" in good, good
    assert await _stored_revision(sid, fid) == r0 + 1
    after_good = await _stored(sid, fid)

    stale = await update.coroutine(
        flow_id=fid,
        steps=[_step("s1", "STALE"), _step("s2", "STALE")],
        expected_revision=r0,
        session_id=sid,
    )
    assert "revision conflict" in stale, stale
    assert f"latest revision={r0 + 1}" in stale, stale
    assert f"expected_revision={r0 + 1}" in stale, stale

    assert await _stored(sid, fid) == after_good
    assert await _stored_revision(sid, fid) == r0 + 1


# ---------------------------------------------------------------------------
# Case 6 — cross-session isolation on the real store
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_cross_session_isolation_real_chain(flow_env: Path) -> None:
    sid_a = _new_sid("iso-a")
    sid_b = _new_sid("iso-b")
    fid = "flow-iso"
    await _seed(sid_a, fid, [_step("s1", "A's task")])
    tools = {t.name: t for t in build_taskflow_tools()}

    rejected = await tools["taskflow_update_steps"].coroutine(
        flow_id=fid,
        steps=[_step("s1", "B's rewrite")],
        session_id=sid_b,
    )
    assert "not found" in rejected, rejected

    steps = await _stored(sid_a, fid)
    assert steps[0]["task"] == "A's task", steps
    assert await flow_store.get_flow(fid, sid_b) is None
