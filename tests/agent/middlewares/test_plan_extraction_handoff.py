"""Plan extraction consumes the structured subagent handoff chain.

The child emits four EXPERIENCE fields about its own layer; completion parses
them into ``run.completion.handoff``; plan extraction reads that dict, filters
the runs against the CURRENT plan snapshot (cancelled/superseded branches out,
step→position re-resolved), assembles the parent→child tree, aggregates it, and
writes the RAW tree to ``handoff-tree.json`` BEFORE the extraction LLM runs.
"""

from __future__ import annotations

import json

import pytest

from agent.middlewares.summarization import nudges
from agent.tools.subagent.types.registry import (
    CompletionState,
    ExecutionState,
    ExecutionStatus,
    FunctionalRole,
    RunOutcome,
    RunOutcomeStatus,
    SubagentRunRecord,
)

pytestmark = [pytest.mark.unit]

SESSION = "s-handoff-plan"
MAIN = f"agent:main:session:{SESSION}"


def _run(
    child: str,
    requester: str,
    *,
    handoff: dict | None = None,
    step_id: str | None = None,
    flow_id: str | None = None,
    depth: int = 1,
    label: str | None = None,
    role: FunctionalRole = FunctionalRole.GENERAL,
    outcome: RunOutcomeStatus = RunOutcomeStatus.OK,
) -> SubagentRunRecord:
    return SubagentRunRecord(
        run_id=f"run-{child}",
        child_session_key=child,
        requester_session_key=requester,
        task=f"task of {child}",
        depth=depth,
        label=label,
        functional_role=role,
        step_id=step_id,
        flow_id=flow_id,
        execution=ExecutionState(
            status=ExecutionStatus.TERMINAL, outcome=RunOutcome(status=outcome)
        ),
        completion=CompletionState(result_text="free text result", handoff=handoff),
    )


def _handoff(**overrides) -> dict:
    data = {
        "task_understanding": "restated",
        "discovered_hard_constraints": [],
        "locked_decisions": [],
        "failed_branches": [],
        "success_steps": [],
    }
    data.update(overrides)
    return data


@pytest.fixture
def runs(monkeypatch):
    """Patch the registry read used by ``_read_subagent_runs``."""
    holder: list[SubagentRunRecord] = []

    def _install() -> None:
        monkeypatch.setattr(
            "agent.tools.subagent.registry.list_descendant_runs", lambda _key: list(holder)
        )

    _install()
    return holder


# ── reading the runs ─────────────────────────────────────────────────────


def test_read_subagent_runs_carries_the_handoff_and_coordinates(runs):
    runs.append(
        _run(
            "agent:main:subagent:a",
            MAIN,
            handoff=_handoff(task_understanding="mine"),
            step_id="step-1",
            flow_id="flow-1",
            depth=1,
            label="researcher",
        )
    )

    out = nudges._read_subagent_runs(SESSION)

    assert len(out) == 1
    entry = out[0]
    assert entry["handoff"] == _handoff(task_understanding="mine")
    assert entry["step_id"] == "step-1"
    assert entry["flow_id"] == "flow-1"
    assert entry["spawn_depth"] == 1
    assert entry["label"] == "researcher"
    assert entry["child_session_key"] == "agent:main:subagent:a"


def test_a_run_without_a_handoff_keeps_its_free_text(runs):
    runs.append(_run("agent:main:subagent:b", MAIN))

    entry = nudges._read_subagent_runs(SESSION)[0]

    assert entry["handoff"] is None
    assert entry["result_text"] == "free text result"


# ── snapshot filter ──────────────────────────────────────────────────────


def _snapshot(todos: list[dict], flows: dict[str, dict] | None = None) -> dict:
    by_step = {str(t.get("step_id")): t for t in todos if t.get("step_id")}
    return {"todos": todos, "todo_by_step": by_step, "flows": flows or {}}


def test_a_cancelled_todo_is_filtered_out():
    run = {"step_id": "step-1", "flow_id": None}
    snapshot = _snapshot([{"step_id": "step-1", "status": "cancelled", "position": 1}])

    assert nudges._filter_runs_by_snapshot([run], snapshot) == []


def test_a_superseded_flow_is_filtered_out():
    run = {"step_id": "step-1", "flow_id": "flow-1"}
    snapshot = _snapshot(
        [{"step_id": "step-1", "status": "completed", "position": 1}],
        {"flow-1": {"status": "running", "superseded_by": "flow-2", "steps": {}}},
    )

    assert nudges._filter_runs_by_snapshot([run], snapshot) == []


def test_a_run_with_unknown_coordinates_is_kept():
    run = {"step_id": "step-gone", "flow_id": "flow-gone"}
    snapshot = _snapshot([])

    kept = nudges._filter_runs_by_snapshot([run], snapshot)

    assert kept == [{**run, "relevance": "unknown", "resolved_position": None}]


def test_a_free_form_spawn_is_kept():
    run = {"step_id": None, "flow_id": None}

    kept = nudges._filter_runs_by_snapshot([run], _snapshot([]))

    assert kept[0]["relevance"] == "unknown"
    assert kept[0]["resolved_position"] is None


def test_resolved_position_uses_the_current_todo_position():
    run = {"step_id": "step-1", "flow_id": None}
    snapshot = _snapshot([{"step_id": "step-1", "status": "completed", "position": 7}])

    kept = nudges._filter_runs_by_snapshot([run], snapshot)

    assert kept[0]["relevance"] == "active"
    assert kept[0]["resolved_position"] == 7


def test_no_snapshot_fails_open():
    run = {"step_id": "step-1", "flow_id": None}

    assert nudges._filter_runs_by_snapshot([run], None) == [run]


# ── tree assembly + aggregation ──────────────────────────────────────────


def test_the_tree_reparents_children_and_injects_metadata():
    parent = {
        "child_session_key": "child-a",
        "requester_session_key": MAIN,
        "handoff": _handoff(task_understanding="parent"),
        "spawn_depth": 1,
        "label": "researcher",
        "functional_role": "researcher",
        "outcome": "ok",
    }
    child = {
        "child_session_key": "child-b",
        "requester_session_key": "child-a",
        "handoff": _handoff(task_understanding="child"),
        "spawn_depth": 2,
        "label": "executor",
        "functional_role": "executor",
        "outcome": "ok",
    }

    roots = nudges._assemble_handoff_tree([parent, child])

    assert len(roots) == 1
    root = roots[0]
    assert root["task_understanding"] == "parent"
    assert root["depth"] == 1 and root["agent_label"] == "researcher"
    assert len(root["children"]) == 1
    assert root["children"][0]["task_understanding"] == "child"
    assert root["children"][0]["depth"] == 2


def test_an_orphan_handoff_is_promoted_to_a_root():
    # Parent was filtered (cancelled); the child's experience still matters.
    orphan = {
        "child_session_key": "child-b",
        "requester_session_key": "missing-parent",
        "handoff": _handoff(task_understanding="orphan"),
        "spawn_depth": 2,
        "label": None,
        "functional_role": "executor",
        "outcome": "ok",
    }

    roots = nudges._assemble_handoff_tree([orphan])

    assert len(roots) == 1
    assert roots[0]["task_understanding"] == "orphan"
    assert roots[0]["agent_label"].startswith("executor-")


def test_aggregation_flattens_depth_first_with_sources_and_dedupes():
    parent = {
        "child_session_key": "child-a",
        "requester_session_key": MAIN,
        "handoff": _handoff(
            discovered_hard_constraints=["no network"],
            failed_branches=[{"branch": "p1", "failure_reason": "r1"}],
            locked_decisions=[{"decision": "d1", "reason": "why"}],
        ),
        "spawn_depth": 1,
        "label": "researcher",
        "functional_role": "researcher",
        "outcome": "ok",
    }
    child = {
        "child_session_key": "child-b",
        "requester_session_key": "child-a",
        "handoff": _handoff(
            discovered_hard_constraints=["no network", "disk cap"],
            failed_branches=[{"branch": "p2", "failure_reason": "r2"}],
            success_steps=[
                {"step": "write", "tool": "write_file", "params": "a.py", "outcome": "ok"}
            ],
        ),
        "spawn_depth": 2,
        "label": "executor",
        "functional_role": "executor",
        "outcome": "ok",
    }

    aggregated = nudges._aggregate_handoffs(nudges._assemble_handoff_tree([parent, child]))

    assert [entry["branch"] for entry in aggregated["failed_branches"]] == ["p1", "p2"]
    assert aggregated["failed_branches"][1]["source"] == "depth-1:researcher > depth-2:executor"
    assert aggregated["success_steps"][0]["step"] == "write"
    assert aggregated["success_steps"][0]["source"] == "depth-1:researcher > depth-2:executor"
    assert aggregated["hard_constraints"] == ["no network", "disk cap"]
    assert aggregated["locked_decisions"][0]["source"] == "depth-1:researcher"
    assert len(aggregated["handoff_forest"]) == 1


def test_compact_view_caps_lists_and_drops_the_forest():
    aggregate = {
        "failed_branches": [{"branch": str(i)} for i in range(15)],
        "success_steps": [{"step": str(i)} for i in range(15)],
        "hard_constraints": ["c"] * 15,
        "locked_decisions": [{"decision": "d"}] * 15,
        "handoff_forest": [{"big": "tree"}],
    }

    compact = nudges._compact_aggregate(aggregate)

    assert len(compact["failed_branches"]) == 10
    assert len(compact["success_steps"]) == 10
    assert compact["total_failed_branches"] == 15
    assert compact["total_success_steps"] == 15
    assert compact["total_hard_constraints"] == 15
    assert "handoff_forest" not in compact


# ── snapshot read + context assembly + pre-LLM write ─────────────────────


def test_read_plan_snapshot_indexes_steps_and_flows(monkeypatch):
    monkeypatch.setattr(
        "agent.tools.todolist.registry.store_sqlite.get_todos_sync",
        lambda _sid: [
            {"step_id": "step-1", "flow_id": "flow-1", "status": "completed", "position": 2}
        ],
    )
    monkeypatch.setattr(
        "agent.tools.taskflow.registry.store_sqlite.get_flow_sync",
        lambda flow_id, _sid: {
            "flow_id": flow_id,
            "status": "running",
            "superseded_by": None,
            "state": {"steps": [{"step_id": "step-1", "status": "done"}]},
        },
    )

    snapshot = nudges._read_plan_snapshot(SESSION)

    assert snapshot is not None
    assert snapshot["todo_by_step"]["step-1"]["position"] == 2
    assert snapshot["flows"]["flow-1"]["status"] == "running"
    assert snapshot["flows"]["flow-1"]["steps"]["step-1"] == "done"


def test_build_plan_context_runs_the_whole_chain(monkeypatch):
    run = _run(
        "agent:main:subagent:a",
        MAIN,
        handoff=_handoff(failed_branches=[{"branch": "p", "failure_reason": "r"}]),
        step_id="step-1",
        depth=1,
        label="researcher",
    )
    monkeypatch.setattr("agent.tools.subagent.registry.list_descendant_runs", lambda _key: [run])
    monkeypatch.setattr(
        "agent.tools.todolist.registry.store_sqlite.get_todos_sync",
        lambda _sid: [{"step_id": "step-1", "status": "completed", "position": 3, "content": "x"}],
    )
    monkeypatch.setattr(
        "agent.tools.taskflow.registry.store_sqlite.get_flow_sync", lambda *_a: None
    )

    context = nudges._build_plan_context(SESSION)

    assert context
    assert context["subagent_runs"][0]["resolved_position"] == 3
    assert context["aggregated_handoff"]["failed_branches"][0]["branch"] == "p"
    assert context["plan_snapshot"]["todo_by_step"]


def test_the_full_tree_is_written_before_the_llm(monkeypatch):
    written: list[tuple] = []

    class _Identity:
        key = "k"

    async def _write(identity, aggregated):
        written.append((identity.key, aggregated))
        return "path"

    monkeypatch.setattr(
        "agent.tools.todolist.knowledge.identity.resolve_plan_identity",
        lambda _sid, _name: _Identity(),
    )
    monkeypatch.setattr(
        "agent.tools.todolist.knowledge.knowledge_store.KnowledgeStore.write_handoff_tree",
        staticmethod(_write),
    )
    context = {
        "plan_name": "p",
        "aggregated_handoff": {
            "failed_branches": [],
            "hard_constraints": [],
            "locked_decisions": [],
            "handoff_forest": [{"task_understanding": "x"}],
        },
    }

    import asyncio

    asyncio.run(nudges._write_handoff_tree_safely(SESSION, context))

    assert written and written[0][1]["handoff_forest"]
    payload_text = json.dumps(written[0][1])
    assert "handoff_forest" in payload_text


def test_no_file_is_written_without_a_forest(monkeypatch):
    calls: list[int] = []
    monkeypatch.setattr(
        "agent.tools.todolist.knowledge.knowledge_store.KnowledgeStore.write_handoff_tree",
        staticmethod(lambda *_a: calls.append(1)),
    )

    import asyncio

    asyncio.run(nudges._write_handoff_tree_safely(SESSION, {"aggregated_handoff": {}}))

    assert calls == []


def test_the_plan_prompt_consumes_success_steps_and_branches():
    prompt = nudges._PLAN_EXTRACTION_PROMPT

    assert "success_steps" in prompt
    assert "failed_branches" in prompt
    assert "hard_constraints" in prompt
    assert "locked_decisions" in prompt
    assert "feedback.loaded_skills" in prompt
    assert "feedback.user_corrections" in prompt
    assert "aggregate_success_steps" not in prompt
    assert "failed_paths" not in prompt
