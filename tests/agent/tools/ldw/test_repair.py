"""LDW repair merge semantics."""

from __future__ import annotations

import pytest

from agent.tools.ldw.repair import (
    extract_failure_experience,
    extract_react_repair_experience,
    merge_knowledge,
)

pytestmark = [pytest.mark.unit]


def test_merge_replaces_success_steps_and_unions_failures() -> None:
    original = {
        "steps": [{"step_id": "a", "task": "old"}],
        "success_steps": [{"step": "old"}],
        "failed_branches": [{"branch": "old"}],
        "hard_constraints": ["h1"],
        "locked_decisions": [{"decision": "d1"}],
        "replay_count": 2,
    }
    repaired = {
        "steps": [{"step_id": "a", "task": "new"}],
        "success_steps": [{"step": "new"}],
        "failed_branches": [{"branch": "new"}, {"branch": "old"}],
        "hard_constraints": ["h1", "h2"],
        "locked_decisions": [{"decision": "d2"}],
        "replay_count": 4,
    }

    merged = merge_knowledge(original, repaired)

    assert merged["steps"] == [{"step_id": "a", "task": "new"}]
    assert merged["success_steps"] == [{"step": "new"}]
    assert [item["branch"] for item in merged["failed_branches"]] == ["old", "new"]
    assert merged["hard_constraints"] == ["h1", "h2"]
    assert merged["replay_count"] == 4


def test_react_repair_collects_handoffs_and_variant() -> None:
    merged = extract_react_repair_experience(
        {"failed_branches": [], "hard_constraints": []},
        [
            {
                "success_steps": [{"step": "react"}],
                "failed_branches": [{"branch": "ptc", "failure_reason": "params"}],
                "discovered_hard_constraints": ["b"],
                "locked_decisions": [{"decision": "use env"}],
            }
        ],
        variant_input="project X",
    )

    assert merged["success_steps"] == [{"step": "react"}]
    assert merged["failed_branches"] == [{"branch": "ptc", "failure_reason": "params"}]
    assert merged["hard_constraints"] == ["b"]
    assert merged["variant_input"] == "project X"


def test_failure_experience_records_affected_steps() -> None:
    merged = extract_failure_experience(
        {"failed_branches": []},
        ["step-2"],
        [{"failed_branches": [{"branch": "x", "failure_reason": "broken"}]}],
    )

    assert merged["failed_steps"] == ["step-2"]
    assert merged["failed_branches"] == [{"branch": "x", "failure_reason": "broken"}]


@pytest.mark.asyncio
async def test_repair_extraction_persists_and_recompiles(monkeypatch) -> None:
    from agent.tools.ldw import repair as repair_mod

    written: list[tuple] = []
    recompiled: list[tuple] = []

    async def _write(identity, *, layer, data):
        written.append((identity.key, layer, data))
        return "ok"

    async def _recompile(plan_key, knowledge):
        recompiled.append((plan_key, knowledge))
        return "script"

    monkeypatch.setattr(
        "agent.tools.todolist.knowledge.knowledge_store.KnowledgeStore.write",
        _write,
    )
    monkeypatch.setattr(repair_mod, "recompile_ldw_workflow", _recompile)

    merged = await repair_mod.repair_extraction(
        "k1",
        {"failed_branches": []},
        {"success_steps": [{"step": "new"}]},
        variant_input="v",
        plan_name="p",
    )

    assert merged["success_steps"] == [{"step": "new"}]
    assert written[0][0:2] == ("k1", "success")
    assert recompiled[0][0] == "k1"
