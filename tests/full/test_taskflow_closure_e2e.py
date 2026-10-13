"""Live-network e2e: the expectation→actual closure on a real agent + real TaskFlow.

LIVE-NETWORK, RUN EXPLICITLY. These tests drive the production graph built by
``agent.core.built_agent()`` against the real configured main LLM, and the real
TaskFlow tools against a real per-test SQLite store. Marker policy: tagged
``llm_e2e``, so a bare ``pytest`` run deselects it via the ``pyproject.toml``
addopts and the hermetic CI gate never collects this file;
``tests/run_tests_split.py`` additionally ``--ignore``s ``tests/full/`` outright.
Run it explicitly with::

    uv run --no-sync pytest -m llm_e2e tests/full/test_taskflow_closure_e2e.py -v --durations=0

Coverage (the live layer for the closure — ``response_schema``, ``step_outcome``,
``input_bindings``; the hermetic layer is ``tests/agent/tools/taskflow/
test_expectation_closure.py``):

1. ``test_live_model_creates_expectation_step_then_real_child`` — the live model
   is instructed to call ``taskflow_run_task`` once with a ``response_schema``
   and a ``functional_role``; the stored step carries both expectations and a
   REAL child run is spawned on that task.
2. ``test_live_model_declares_step_failure`` — the live model is instructed to
   call ``taskflow_resume`` with ``step_outcome="failure"``; the step lands
   ``failed``, its dependent stays blocked, and the model receives the verdict
   text (the model-view path for a declared outcome).
3. ``test_schema_gate_then_binding_reaches_real_child`` — a violating
   ``structured_result`` fails the Tier 1 gate with no judge call (step
   ``failed``, dependent blocked); a valid one validates, records the structure
   and lets a downstream step created with ``input_bindings`` carry the upstream
   field into its REAL child task.

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
import uuid
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from loguru import logger

import agent.core as agent_core
from agent.checkpointer.async_sqlite_checkpointer import delete_thread_history
from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.registry import store_sqlite as flow_store
from config import SESSIONS_DIR
from context_engine import delete_messages_by_session
from pub.func.build_agent_config import build_agent_config
from runtime import clear_all_register_sessions

pytestmark = [pytest.mark.llm_e2e, pytest.mark.timeout(900)]


# The expectation a step can carry: the child must answer with a JSON object
# holding one string field.
_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"token": {"type": "string"}},
    "required": ["token"],
}


# ---------------------------------------------------------------------------
# Session / store helpers
# ---------------------------------------------------------------------------


def _new_sid(tag: str) -> str:
    return f"e2e-clo-{tag}-{uuid.uuid4().hex[:8]}"


def _step(
    step_id: str,
    task: str,
    *,
    status: str = "ready",
    depends_on: list[str] | None = None,
    child: str | None = None,
    **extra: Any,
) -> dict:
    step: dict[str, Any] = {
        "step_id": step_id,
        "task": task,
        "depends_on": list(depends_on or []),
        "status": status,
    }
    if child is not None:
        step["child_session_key"] = child
    step.update(extra)
    return step


async def _seed(sid: str, fid: str, steps: list[dict], status: str = "running") -> None:
    await flow_store.create_flow(
        fid,
        {"description": f"{fid} probe", "steps": list(steps), "results": []},
        session_id=sid,
        status=status,
    )


async def _flow(sid: str, fid: str) -> dict:
    flow = await flow_store.get_flow(fid, sid)
    assert flow is not None, f"flow {fid!r} vanished for session {sid!r}"
    return flow


async def _stored(sid: str, fid: str) -> list[dict]:
    return list((await _flow(sid, fid))["state"]["steps"])


def _by_id(steps: list[dict], step_id: str) -> dict:
    return next(s for s in steps if s.get("step_id") == step_id)


def _result_for(flow: dict, child_key: str) -> dict:
    return next(
        r for r in flow["state"].get("results") or [] if r.get("child_session_key") == child_key
    )


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


async def _drive_llm_tool(
    graph: Any,
    *,
    tag: str,
    sid: str,
    prompt: str,
    tool: str,
    expect: str,
    attempts: int = 2,
) -> dict:
    """Invoke the live model until its `tool` call result contains `expect`.

    Retries a non-compliant model (<= attempts) and, on final failure, reports
    exactly what the model did instead of skipping.
    """
    last_note = f"model never called {tool}"
    for attempt in range(1, attempts + 1):
        out = await _invoke_with_retry(
            graph,
            {
                "messages": [HumanMessage(content=prompt, id=f"h-{sid}-{attempt}")],
                "session_id": sid,
            },
            build_agent_config(sid),
        )
        messages = _tool_messages(out, tool)
        if messages and expect in str(messages[-1].content):
            return out
        last_note = _describe(out, tool, messages)
        logger.warning("[{}] attempt {}/{}: {}", tag, attempt, attempts, last_note)
    pytest.fail(
        f"[{tag}] live model did not call {tool} with {expect!r} after {attempts} attempts: "
        f"{last_note}"
    )


# ---------------------------------------------------------------------------
# Case 1 — the live model expresses the expectation, a real child runs it
# ---------------------------------------------------------------------------


def _run_task_prompt(sid: str, fid: str, task: str) -> str:
    """An explicit, low-ambiguity instruction to call run_task with a schema."""
    return (
        "You must call the tool `taskflow_run_task` exactly once with these exact "
        "arguments and nothing else:\n"
        f"  session_id = {sid!r}\n"
        f"  flow_id = {fid!r}\n"
        f"  task = {task!r}\n"
        f"  response_schema = {json.dumps(_SCHEMA, ensure_ascii=False, separators=(',', ':'))}\n"
        "  functional_role = 'researcher'\n"
        "Use ONLY the values above; do not invent different values and do not call any "
        "other tool. After the tool returns, reply with the single word DONE."
    )


@pytest.mark.asyncio
async def test_live_model_creates_expectation_step_then_real_child(
    flow_env: Path, clean_subagent_registry
) -> None:
    graph = await _real_graph()
    sid = _new_sid("schema")
    fid = "flow-clo-schema"
    marker = f"mk-{uuid.uuid4().hex[:8]}"
    task = f"Reply with exactly this token and nothing else: {marker}"
    await _seed(sid, fid, [])

    try:
        await _drive_llm_tool(
            graph,
            tag="schema-step",
            sid=sid,
            prompt=_run_task_prompt(sid, fid, task),
            tool="taskflow_run_task",
            expect="TaskFlow step dispatched",
        )

        steps = await _stored(sid, fid)
        assert len(steps) == 1, steps
        step = steps[0]
        # The expectation the model expressed survives into the stored step.
        assert step["response_schema"] == _SCHEMA, step
        assert step["functional_role"] == "researcher", step
        assert step["status"] == "dispatched", step
        child_key = step["child_session_key"]
        assert child_key, step

        from agent.tools.subagent.registry import get_run, get_run_by_child_session_key

        run = get_run_by_child_session_key(child_key)
        assert run is not None, "run_task did not spawn a real child run"
        assert run.task == task, f"child ran a different task: {run.task!r}"

        done = await _poll_run(run.run_id)
        from agent.tools.subagent.types.registry import RunOutcomeStatus

        assert done.execution.outcome is not None, done.execution
        assert done.execution.outcome.status == RunOutcomeStatus.OK, done.execution.outcome
        logger.info("[clo e2e] real child result: {!r}", (done.completion.result_text or "")[:160])
        assert marker in (done.completion.result_text or ""), "the real child did not run the task"
        assert get_run(run.run_id) is not None
    finally:
        await _purge_session(sid)


# ---------------------------------------------------------------------------
# Case 2 — the live model declares the outcome, the flow records it
# ---------------------------------------------------------------------------


def _resume_prompt(sid: str, fid: str, child_key: str) -> str:
    return (
        "You must call the tool `taskflow_resume` exactly once with these exact "
        "arguments and nothing else:\n"
        f"  session_id = {sid!r}\n"
        f"  flow_id = {fid!r}\n"
        f"  child_session_key = {child_key!r}\n"
        "  result = 'the child crashed before producing anything'\n"
        "  step_outcome = 'failure'\n"
        "Use ONLY the values above; do not invent different values and do not call any "
        "other tool. After the tool returns, reply with the single word DONE."
    )


@pytest.mark.asyncio
async def test_live_model_declares_step_failure(flow_env: Path) -> None:
    graph = await _real_graph()
    sid = _new_sid("declared")
    fid = "flow-clo-declared"
    await _seed(
        sid,
        fid,
        [
            _step("step-1", "collect findings", status="dispatched", child="child-1"),
            _step("step-2", "use the findings", depends_on=["step-1"], status="blocked"),
        ],
    )

    try:
        out = await _drive_llm_tool(
            graph,
            tag="declared-failure",
            sid=sid,
            prompt=_resume_prompt(sid, fid, "child-1"),
            tool="taskflow_resume",
            expect="outcome: failed (declared by the caller)",
        )

        steps = await _stored(sid, fid)
        assert _by_id(steps, "step-1")["status"] == "failed", steps
        # A declared failure never unlocks the dependent.
        assert _by_id(steps, "step-2")["status"] == "blocked", steps
        flow = await _flow(sid, fid)
        record = _result_for(flow, "child-1")
        assert record["step_id"] == "step-1", record
        assert record["step_outcome"] == "failure", record
        logger.info(
            "[clo e2e] declared outcome tool result: {!r}",
            str(_tool_messages(out, "taskflow_resume")[-1].content)[:200],
        )
    finally:
        await _purge_session(sid)


# ---------------------------------------------------------------------------
# Case 3 — the Tier 1 gate, then the bound value in a real child's task
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_schema_gate_then_binding_reaches_real_child(
    flow_env: Path, clean_subagent_registry
) -> None:
    await _real_graph()  # real spawn needs the same assembly as case 1
    tools = {t.name: t for t in build_taskflow_tools()}
    resume = tools["taskflow_resume"]
    run_task = tools["taskflow_run_task"]
    marker = f"mk-{uuid.uuid4().hex[:8]}"

    # (a) A payload that violates the schema fails the gate without a judge.
    sid_a = _new_sid("invalid")
    fid_a = "flow-clo-invalid"
    await _seed(
        sid_a,
        fid_a,
        [
            _step(
                "step-1",
                "collect",
                status="dispatched",
                child="child-a",
                response_schema=_SCHEMA,
            ),
            _step("step-2", "after", depends_on=["step-1"], status="blocked"),
        ],
    )
    try:
        out = await resume.coroutine(
            session_id=sid_a,
            flow_id=fid_a,
            child_session_key="child-a",
            result="the child answered in prose",
            structured_result={"token": 12345},  # required: string
        )
        assert "schema: invalid" in out, out
        assert "outcome: failed (schema, no retry left:" in out, out
        assert "judge:" not in out, out
        steps = await _stored(sid_a, fid_a)
        assert _by_id(steps, "step-1")["status"] == "failed", steps
        assert "response_schema not satisfied" in _by_id(steps, "step-1")["fail_reason"], steps
        # The gate failure keeps the dependent blocked.
        assert _by_id(steps, "step-2")["status"] == "blocked", steps
        record = _result_for(await _flow(sid_a, fid_a), "child-a")
        assert record["schema_validated"] is False, record
    finally:
        await _purge_session(sid_a)

    # (b) A valid payload validates, records the structure, and the next step's
    #     run_task binds the upstream field into a REAL child task.
    sid_b = _new_sid("bound")
    fid_b = "flow-clo-bound"
    await _seed(
        sid_b,
        fid_b,
        [
            _step(
                "step-1",
                "collect",
                status="dispatched",
                child="child-b",
                response_schema=_SCHEMA,
            )
        ],
    )
    try:
        out = await resume.coroutine(
            session_id=sid_b,
            flow_id=fid_b,
            child_session_key="child-b",
            result="reported",
            structured_result={"token": marker},
        )
        assert "schema: validated" in out, out
        steps = await _stored(sid_b, fid_b)
        assert _by_id(steps, "step-1")["status"] == "done", steps
        record = _result_for(await _flow(sid_b, fid_b), "child-b")
        assert record["schema_validated"] is True, record
        assert record["structured_result"] == {"token": marker}, record

        bound_task = "Use the collected token and answer with a short acknowledgement."
        dispatched = await run_task.coroutine(
            session_id=sid_b,
            flow_id=fid_b,
            task=bound_task,
            depends_on=["step-1"],
            input_bindings={"upstream_token": "step-1.structured_result.token"},
        )
        assert "TaskFlow step dispatched" in dispatched, dispatched

        steps = await _stored(sid_b, fid_b)
        downstream = _by_id(steps, "step-2")
        assert downstream["status"] == "dispatched", steps
        assert downstream["input_bindings"] == {
            "upstream_token": "step-1.structured_result.token"
        }, downstream

        from agent.tools.subagent.registry import get_run_by_child_session_key

        run = get_run_by_child_session_key(downstream["child_session_key"])
        assert run is not None, "the bound step did not spawn a real child run"
        # The closure: the upstream structured field reached the child's task.
        assert "## Input parameters" in run.task, run.task
        assert marker in run.task, run.task
        assert bound_task in run.task, run.task
        logger.info("[clo e2e] bound child task: {!r}", run.task[:240])

        done = await _poll_run(run.run_id)
        assert done.completion.result_text, done.execution
    finally:
        await _purge_session(sid_b)
