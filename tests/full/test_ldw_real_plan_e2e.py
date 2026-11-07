"""Real LDW replay: a verified TaskFlow DAG, real leaves, fallback, repair.

Run explicitly::

    uv run --no-sync pytest -m llm_e2e tests/full/test_ldw_real_plan_e2e.py -v

Coverage:

1. A real TaskFlow flow is compiled into an LDW script; the first replay runs
   real auxiliary-model leaves, the second same-variant replay is a zero-call
   journal hit, and a new variant misses.
2. The live main agent calls ``workflow_replay(action="replay")`` and gets a
   real LDW result (not just a status payload).
3. An LDW leaf failure auto-creates a real TaskFlow replay flow: the succeeded
   step is seeded (no second child), only the failed step spawns a real child,
   the TaskFlow result is written back to the journal, and the next LDW replay
   is zero-call.
4. A TaskFlow child timeout leaves the flow partial and repair extraction
   persists knowledge + recompiles the LDW script.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
from langchain.agents import create_agent
from langchain.agents.middleware import AgentState
from langchain_core.messages import AIMessage, HumanMessage
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import MemorySaver

import agent.core as agent_core
import agent.tools.ldw.replay as replay_mod
import agent.tools.ldw.scripts as scripts_mod
from agent.tools.ldw.compiler import compile_plan_to_workflow
from agent.tools.ldw.tool import build_workflow_replay_tool
from agent.tools.subagent.ldw_shim import Roster
from agent.tools.taskflow.registry import store_sqlite as flow_store
from agent.tools.todolist.knowledge import knowledge_store as knowledge_mod
from agent.tools.todolist.knowledge.identity import resolve_session_plan_identity
from models import build_main_llm

pytestmark = [pytest.mark.llm_e2e, pytest.mark.system, pytest.mark.timeout(900)]


class _E2EState(AgentState):
    session_id: str


_ANSWER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}


def _new_sid(tag: str) -> str:
    return f"e2e-ldw-real-{tag}-{uuid.uuid4().hex[:8]}"


@pytest.fixture()
def flow_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_dir = tmp_path / "taskflow"
    monkeypatch.setattr(flow_store, "_DB_DIR", db_dir)
    monkeypatch.setattr(flow_store, "_DB_PATH", db_dir / "taskflow_registry.db")
    monkeypatch.setattr(flow_store, "_initialized", False)
    monkeypatch.setattr(flow_store, "_init_loop", None)
    monkeypatch.setattr(flow_store, "_init_lock", asyncio.Lock())
    monkeypatch.setattr(flow_store, "_sync_tables_ready", False)
    return db_dir


@pytest.fixture()
def ldw_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    knowledge = tmp_path / "knowledge"
    monkeypatch.setattr(replay_mod, "PLAN_KNOWLEDGE_DIR", knowledge)
    monkeypatch.setattr(scripts_mod, "AUTO_SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(knowledge_mod, "_KNOWLEDGE_ROOT", knowledge)
    monkeypatch.setitem(replay_mod.LDW, "min_replays", 1)
    monkeypatch.setitem(replay_mod.LDW, "journal_path", str(tmp_path / "journal.db"))
    monkeypatch.setitem(replay_mod.LDW, "auto_degrade_enabled", True)
    monkeypatch.setitem(replay_mod.LDW, "repair_enabled", True)
    monkeypatch.setitem(replay_mod.LDW, "real_replay_timeout_s", 300.0)
    monkeypatch.setitem(replay_mod.LDW, "max_failed_steps", 5)
    return tmp_path


@pytest.fixture()
def clean_registry():
    from agent.tools.subagent.registry import clear as clear_registry

    clear_registry()
    yield
    clear_registry()


async def _seed_verified_flow(session_id: str, flow_id: str, variant: str) -> None:
    steps = [
        {
            "step_id": "a",
            "task": "Reply with exactly the token from [Current task input] and nothing else.",
            "depends_on": [],
            "status": "done",
        },
        {
            "step_id": "b",
            "task": (
                "Put the token from [Current task input] in the answer field and nothing else."
            ),
            "depends_on": ["a"],
            "status": "done",
            "response_schema": _ANSWER_SCHEMA,
        },
    ]
    await flow_store.create_flow(
        flow_id,
        {"description": f"verified plan {variant}", "steps": steps, "results": []},
        session_id=session_id,
        status="done",
    )


async def _real_graph():
    agent_core.init()
    return await agent_core.built_agent(temperature=0.0)


@pytest.mark.asyncio
async def test_real_plan_replay_and_zero_call_second_run(flow_env: Path, ldw_env: Path) -> None:
    sid = _new_sid("plan")
    fid = f"flow-ldw-{uuid.uuid4().hex[:8]}"
    variant = f"TOKEN-{uuid.uuid4().hex[:6]}"
    await _seed_verified_flow(sid, fid, variant)
    key = resolve_session_plan_identity(sid).key

    knowledge = replay_mod.plan_knowledge_from_flow(sid, flow_id=fid)
    assert knowledge is not None
    meta = compile_plan_to_workflow(knowledge)
    assert scripts_mod.write_ldw_script(key, meta) is not None

    first = await replay_mod.replay_plan(
        sid, plan_key=key, variant_input=variant, flow_id=fid, force=True
    )
    assert first["status"] == "ok", first
    assert first["leaf_calls"] == 2, first
    assert first["journal_hits"] == 0, first
    assert variant in first["results"]["a"]
    assert variant in json.loads(first["results"]["b"])["answer"]

    second = await replay_mod.replay_plan(
        sid, plan_key=key, variant_input=variant, flow_id=fid, force=True
    )
    assert second["status"] == "ok", second
    assert second["leaf_calls"] == 0, second
    assert second["journal_hits"] == 2, second

    other = f"OTHER-{uuid.uuid4().hex[:6]}"
    third = await replay_mod.replay_plan(
        sid, plan_key=key, variant_input=other, flow_id=fid, force=True
    )
    assert third["status"] == "ok", third
    assert third["leaf_calls"] == 2, third
    assert other in third["results"]["a"]


def _fake_roster(meta: dict[str, Any], *, fail_step: str = "", result: str = "A-OK"):
    """A roster whose named step raises (a forced LDW leaf failure)."""

    def _ok(_input, config=None):
        return {"messages": [AIMessage(content=result)]}

    def _fail(_input, config=None):
        raise RuntimeError("injected LDW leaf failure")

    roster = Roster()
    for step in meta["steps"]:
        agent_type = step["agent_type"]
        fn = _fail if step["step_id"] == fail_step else _ok
        roster.register(agent_type, runnable=RunnableLambda(fn))
    return roster


@pytest.mark.asyncio
async def test_ldw_failure_degrades_to_real_taskflow_and_writes_journal(
    flow_env: Path, ldw_env: Path, clean_registry
) -> None:
    agent_core.init()
    sid = _new_sid("degrade")
    variant = f"FB-{uuid.uuid4().hex[:6]}"
    key = resolve_session_plan_identity(sid).key
    meta = compile_plan_to_workflow(
        {
            "plan_key": key,
            "steps": [
                {"step_id": "a", "task": "Reply with exactly A-OK and nothing else."},
                {
                    "step_id": "b",
                    "task": "Reply with exactly TASKFLOW-B and nothing else.",
                    "depends_on": ["a"],
                },
            ],
        }
    )
    assert scripts_mod.write_ldw_script(key, meta) is not None
    real_register = replay_mod.register_roster
    replay_mod.register_roster = lambda m, s, **kw: _fake_roster(m, fail_step="b")
    try:
        outcome = await replay_mod.replay_plan(sid, plan_key=key, variant_input=variant, force=True)
    finally:
        replay_mod.register_roster = real_register

    assert outcome["status"] == "degraded", outcome
    degraded = outcome["degraded"]
    assert degraded["status"] == "ok", degraded
    assert "TASKFLOW-B" in degraded["results"]["b"], degraded
    flow = await flow_store.get_flow(degraded["flow_id"], sid)
    assert flow is not None and flow["status"] == "done", flow
    steps = flow["state"]["steps"]
    a_step = next(step for step in steps if step["step_id"] == "a")
    b_step = next(step for step in steps if step["step_id"] == "b")
    assert a_step["child_session_key"].startswith("ldw-journal:"), a_step
    assert not b_step["child_session_key"].startswith("ldw-journal:"), b_step

    replayed = await replay_mod.replay_plan(sid, plan_key=key, variant_input=variant, force=True)
    assert replayed["status"] == "ok", replayed
    assert replayed["leaf_calls"] == 0, replayed
    assert replayed["journal_hits"] == 2, replayed


@pytest.mark.asyncio
async def test_taskflow_failure_triggers_repair_and_recompile(
    flow_env: Path, ldw_env: Path, clean_registry
) -> None:
    agent_core.init()
    sid = _new_sid("repair")
    variant = f"RP-{uuid.uuid4().hex[:6]}"
    key = resolve_session_plan_identity(sid).key
    meta = compile_plan_to_workflow(
        {
            "plan_key": key,
            "steps": [
                {"step_id": "a", "task": "Reply with exactly A-OK and nothing else."},
                {
                    "step_id": "b",
                    "task": (
                        "Call the terminal tool with the exact command `sleep 30` and wait for "
                        "it to finish, then reply SLEEP-DONE."
                    ),
                    "depends_on": ["a"],
                    "functional_role": "executor",
                    "step_timeout_seconds": 5,
                },
            ],
        }
    )
    assert scripts_mod.write_ldw_script(key, meta) is not None
    real_register = replay_mod.register_roster
    replay_mod.register_roster = lambda m, s, **kw: _fake_roster(m, fail_step="b")
    try:
        outcome = await replay_mod.replay_plan(sid, plan_key=key, variant_input=variant, force=True)
    finally:
        replay_mod.register_roster = real_register

    assert outcome["status"] == "partial", outcome
    degraded = outcome["degraded"]
    assert degraded["repaired"] is True, degraded
    assert degraded["failed_steps"], degraded
    replay_flow = await flow_store.get_flow(degraded["flow_id"], sid)
    assert replay_flow is not None
    assert replay_flow["status"] in ("failed", "cancelled"), replay_flow
    success_file = Path(ldw_env, "knowledge", key, "success-steps.json")
    assert success_file.is_file(), success_file
    payload = json.loads(success_file.read_text(encoding="utf-8"))
    assert "failed_steps" in payload
    script = scripts_mod.find_ldw_script(key)
    assert script is not None and "WORKFLOW_META" in script.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_live_main_agent_calls_workflow_replay(flow_env: Path, ldw_env: Path) -> None:
    sid = _new_sid("agent")
    fid = f"flow-agent-{uuid.uuid4().hex[:8]}"
    variant = f"AGENT-{uuid.uuid4().hex[:6]}"
    await _seed_verified_flow(sid, fid, variant)
    key = resolve_session_plan_identity(sid).key
    knowledge = replay_mod.plan_knowledge_from_flow(sid, flow_id=fid)
    assert knowledge is not None
    meta = compile_plan_to_workflow(knowledge)
    assert scripts_mod.write_ldw_script(key, meta) is not None

    graph = create_agent(
        model=build_main_llm(temperature=0),
        state_schema=_E2EState,
        checkpointer=MemorySaver(),
        tools=[build_workflow_replay_tool()],
    )
    prompt = (
        "Call the `workflow_replay` tool exactly once with these exact arguments: "
        f"action='replay', plan_key={key!r}, variant_input={variant!r}, force=True. "
        "Then report the exact JSON object it returned."
    )
    out = await graph.ainvoke(
        {"messages": [HumanMessage(content=prompt)], "session_id": sid},
        {"configurable": {"thread_id": sid}},
    )
    tool_texts = [
        str(message.content)
        for message in (out.get("messages") or [])
        if getattr(message, "type", None) == "tool"
    ]
    assert tool_texts, f"live agent did not call workflow_replay: {out!r}"
    payload = json.loads(tool_texts[-1])
    assert payload["ok"] is True, payload
    assert payload["status"] == "ok", payload
    assert variant in payload["results"]["a"]
