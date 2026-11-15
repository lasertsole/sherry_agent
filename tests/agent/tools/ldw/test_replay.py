"""LDW replay threshold, journal hit, and compile-on-miss."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from langchain_core.messages import AIMessage
from langchain_core.runnables import RunnableLambda

from agent.tools.ldw import replay as replay_mod
from agent.tools.ldw.compiler import compile_plan_to_workflow
from agent.tools.subagent.ldw_shim import Roster

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


def _meta() -> dict:
    return compile_plan_to_workflow({"plan_key": "k1", "steps": [{"step_id": "a", "task": "do a"}]})


@pytest.mark.asyncio
async def test_threshold_routes_first_replays_to_taskflow(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(replay_mod, "PLAN_KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setitem(replay_mod.LDW, "min_replays", 3)
    monkeypatch.setitem(replay_mod.LDW, "journal_path", "")
    monkeypatch.setitem(replay_mod.LDW, "enabled", True)
    monkeypatch.setattr(
        replay_mod,
        "resolve_session_plan_identity",
        lambda _sid: type("I", (), {"key": "k1", "plan_name": "p"})(),
    )
    monkeypatch.setattr(replay_mod, "load_ldw_script", lambda _key: _meta())

    for expected in (1, 2):
        result = await replay_mod.replay_plan("sess", plan_key="k1", variant_input="v")
        assert result["status"] == "taskflow"
        assert result["replay_count"] == expected


@pytest.mark.asyncio
async def test_third_replay_runs_and_second_run_hits_journal(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(replay_mod, "PLAN_KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setitem(replay_mod.LDW, "min_replays", 3)
    monkeypatch.setitem(replay_mod.LDW, "journal_path", str(tmp_path / "journal.db"))
    monkeypatch.setattr(
        replay_mod,
        "resolve_session_plan_identity",
        lambda _sid: type("I", (), {"key": "k1", "plan_name": "p"})(),
    )
    monkeypatch.setattr(replay_mod, "load_ldw_script", lambda _key: _meta())
    calls: list[str] = []

    def _roster(_meta, _session_id, **_kwargs):
        async def _leaf(input, config=None):
            calls.append(input["messages"][-1].content)
            return {"messages": [AIMessage(content="ok")]}

        return Roster().register("general@a", runnable=RunnableLambda(_leaf))

    monkeypatch.setattr(replay_mod, "register_roster", _roster)

    for _ in range(2):
        early = await replay_mod.replay_plan("sess", plan_key="k1", variant_input="v")
        assert early["status"] == "taskflow", early

    first = await replay_mod.replay_plan("sess", plan_key="k1", variant_input="v")
    assert first["status"] == "ok", first
    assert len(calls) == 1

    second = await replay_mod.replay_plan("sess", plan_key="k1", variant_input="v")
    assert second["status"] == "ok", second
    assert len(calls) == 1, "the SQLite journal must serve the second run"
    assert second["journal_hits"] == 1
    assert second["leaf_calls"] == 0


@pytest.mark.asyncio
async def test_compile_on_miss_writes_skill_script(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(replay_mod, "PLAN_KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setitem(replay_mod.LDW, "min_replays", 1)
    monkeypatch.setitem(replay_mod.LDW, "journal_path", "")
    monkeypatch.setattr(
        replay_mod,
        "resolve_session_plan_identity",
        lambda _sid: type("I", (), {"key": "k1", "plan_name": "p"})(),
    )
    monkeypatch.setattr(replay_mod, "load_ldw_script", lambda _key: None)
    monkeypatch.setattr(
        replay_mod,
        "plan_knowledge_from_flow",
        lambda _sid, **_kwargs: {"plan_key": "k1", "steps": [{"step_id": "a", "task": "do a"}]},
    )
    writes: list[dict] = []
    monkeypatch.setattr(
        replay_mod,
        "write_ldw_script",
        lambda _key, meta: writes.append(meta) or tmp_path / "ldw.py",
    )

    async def _ok(input, config=None):
        return {"messages": [AIMessage(content="ok")]}

    monkeypatch.setattr(
        replay_mod,
        "register_roster",
        lambda _meta, _sid, **_kwargs: Roster().register("general@a", runnable=RunnableLambda(_ok)),
    )

    result = await replay_mod.replay_plan("sess", plan_key="k1", variant_input="v")
    assert result["status"] == "ok", result
    assert writes and writes[0]["plan_key"] == "k1"


@pytest.mark.asyncio
async def test_compile_on_miss_is_skipped_when_flow_extraction_is_off(
    tmp_path, monkeypatch
) -> None:
    """A missing script is not compiled while the global flow switch is off.

    An EXISTING script still replays — the switch governs compilation only — so
    this path returns a distinct status instead of falling through to a compile.
    """
    monkeypatch.setattr(replay_mod, "PLAN_KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setitem(replay_mod.LDW, "min_replays", 1)
    monkeypatch.setitem(replay_mod.LDW, "journal_path", "")
    monkeypatch.setattr(
        replay_mod,
        "resolve_session_plan_identity",
        lambda _sid: type("I", (), {"key": "k1", "plan_name": "p"})(),
    )
    monkeypatch.setattr(replay_mod, "load_ldw_script", lambda _key: None)
    monkeypatch.setattr("runtime.session.extraction.flow_extraction_enabled", lambda _sid="": False)

    def _knowledge(*_args, **_kwargs):
        raise AssertionError("the DAG must not be read for a refused compile")

    monkeypatch.setattr(replay_mod, "plan_knowledge_from_flow", _knowledge)

    result = await replay_mod.replay_plan("sess", plan_key="k1", variant_input="v")

    assert result["status"] == "flow_extraction_disabled"
    assert result["plan_key"] == "k1"


def test_plan_knowledge_from_flow_reads_taskflow_steps(monkeypatch) -> None:
    flow = {
        "state": {
            "steps": [
                {
                    "step_id": "a",
                    "task": "do a",
                    "functional_role": "executor",
                    "depends_on": [],
                    "response_schema": {"type": "object"},
                    "retry_policy": {"max_retries": 2},
                    "step_timeout_seconds": 30,
                    "judge_criteria": "must pass",
                },
                {"step_id": "b", "task": "do b", "depends_on": ["a"]},
            ]
        }
    }
    monkeypatch.setattr(
        "agent.tools.taskflow.registry.store_sqlite.get_flow_sync",
        lambda _flow_id, _sid: flow,
    )
    monkeypatch.setattr(
        replay_mod,
        "resolve_session_plan_identity",
        lambda _sid: type("I", (), {"key": "k1", "plan_name": "p"})(),
    )

    knowledge = replay_mod.plan_knowledge_from_flow("sess", flow_id="flow-1")

    assert knowledge is not None
    assert knowledge["plan_key"] == "k1"
    assert knowledge["flow_id"] == "flow-1"
    assert knowledge["steps"][0]["functional_role"] == "executor"
    assert knowledge["steps"][1]["depends_on"] == ["a"]
    step = knowledge["steps"][0]
    assert step["response_schema"] == {"type": "object"}
    assert step["retry_policy"] == {"max_retries": 2}
    assert step["step_timeout_seconds"] == 30
    assert step["judge_criteria"] == "must pass"


def test_plan_knowledge_from_flow_keys_by_goal(monkeypatch) -> None:
    """Two tasks sharing one plan file must not share the LDW replay key."""
    flows = {
        "flow-a": {
            "state": {
                "description": "Fix login",
                "steps": [{"step_id": "a", "task": "do a"}],
            }
        },
        "flow-b": {
            "state": {
                "description": "Fix logout",
                "steps": [{"step_id": "a", "task": "do a"}],
            }
        },
        "flow-c": {
            "state": {
                "description": "  fix LOGIN  ",
                "steps": [{"step_id": "a", "task": "do a"}],
            }
        },
        "flow-none": {"state": {"steps": [{"step_id": "a", "task": "do a"}]}},
    }
    monkeypatch.setattr(
        "agent.tools.taskflow.registry.store_sqlite.get_flow_sync",
        lambda flow_id, _sid: flows[flow_id],
    )
    monkeypatch.setattr(
        replay_mod,
        "resolve_session_plan_identity",
        lambda _sid: SimpleNamespace(key="k1", plan_name="p"),
    )

    login = replay_mod.plan_knowledge_from_flow("sess", flow_id="flow-a")
    logout = replay_mod.plan_knowledge_from_flow("sess", flow_id="flow-b")
    same_login = replay_mod.plan_knowledge_from_flow("sess", flow_id="flow-c")
    no_goal = replay_mod.plan_knowledge_from_flow("sess", flow_id="flow-none")

    assert login is not None and logout is not None
    assert login["plan_key"] != logout["plan_key"]
    assert login["plan_key"] == same_login["plan_key"]
    assert login["plan_key"] != "k1"
    # An empty description keeps the historical path-based key.
    assert no_goal["plan_key"] == "k1"


@pytest.mark.asyncio
async def test_goal_keys_use_separate_replay_counters(tmp_path, monkeypatch) -> None:
    """Separate keys mean separate counter files; one task cannot advance another."""
    monkeypatch.setattr(replay_mod, "PLAN_KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setitem(replay_mod.LDW, "min_replays", 99)
    monkeypatch.setitem(replay_mod.LDW, "journal_path", "")
    monkeypatch.setitem(replay_mod.LDW, "enabled", True)
    monkeypatch.setattr(
        replay_mod,
        "resolve_session_plan_identity",
        lambda _sid: SimpleNamespace(key="k1", plan_name="p"),
    )

    first_a = await replay_mod.replay_plan("sess", plan_key="key-a", variant_input="v")
    first_b = await replay_mod.replay_plan("sess", plan_key="key-b", variant_input="v")
    second_a = await replay_mod.replay_plan("sess", plan_key="key-a", variant_input="v")

    assert first_a["replay_count"] == 1
    assert first_b["replay_count"] == 1
    assert second_a["replay_count"] == 2


@pytest.mark.asyncio
async def test_ldw_exception_auto_degrades_to_taskflow(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(replay_mod, "PLAN_KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setitem(replay_mod.LDW, "min_replays", 1)
    monkeypatch.setitem(replay_mod.LDW, "journal_path", "")
    monkeypatch.setitem(replay_mod.LDW, "auto_degrade_enabled", True)
    monkeypatch.setitem(replay_mod.LDW, "max_failed_steps", 5)
    monkeypatch.setattr(
        replay_mod,
        "resolve_session_plan_identity",
        lambda _sid: type("I", (), {"key": "k1", "plan_name": "p"})(),
    )
    monkeypatch.setattr(replay_mod, "load_ldw_script", lambda _key: _meta())
    monkeypatch.setattr(
        replay_mod,
        "register_roster",
        lambda _meta, _sid, **_kwargs: Roster(),
    )

    async def _boom(*_a, **_k):
        raise RuntimeError("leaf exploded")

    monkeypatch.setattr(replay_mod, "run_workflow", _boom)
    from agent.tools.ldw import taskflow_adapter as adapter_mod

    async def _fallback(*_a, **_k):
        return {"status": "ok", "flow_id": "flow-1", "results": {"a": "A"}}

    monkeypatch.setattr(adapter_mod, "run_taskflow_fallback", _fallback)

    result = await replay_mod.replay_plan("sess", plan_key="k1", variant_input="v")

    assert result["status"] == "degraded", result
    assert result["degraded"]["flow_id"] == "flow-1"
    assert "leaf exploded" in result["error"]


@pytest.mark.asyncio
async def test_failed_step_count_over_cap_skips_degrade(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(replay_mod, "PLAN_KNOWLEDGE_DIR", tmp_path)
    monkeypatch.setitem(replay_mod.LDW, "min_replays", 1)
    monkeypatch.setitem(replay_mod.LDW, "journal_path", "")
    monkeypatch.setitem(replay_mod.LDW, "auto_degrade_enabled", True)
    monkeypatch.setitem(replay_mod.LDW, "max_failed_steps", 0)
    monkeypatch.setattr(
        replay_mod,
        "resolve_session_plan_identity",
        lambda _sid: type("I", (), {"key": "k1", "plan_name": "p"})(),
    )
    monkeypatch.setattr(replay_mod, "load_ldw_script", lambda _key: _meta())
    monkeypatch.setattr(
        replay_mod,
        "register_roster",
        lambda _meta, _sid, **_kwargs: Roster(),
    )

    async def _empty(*_a, **_k):
        return {}

    monkeypatch.setattr(replay_mod, "run_workflow", _empty)
    from agent.tools.ldw import taskflow_adapter as adapter_mod

    async def _fallback(*_a, **_k):
        raise AssertionError("over-cap failures must not degrade")

    monkeypatch.setattr(adapter_mod, "run_taskflow_fallback", _fallback)

    result = await replay_mod.replay_plan("sess", plan_key="k1", variant_input="v")

    assert result["status"] == "partial", result
    assert "degrade_skipped" in result


@pytest.mark.asyncio
async def test_disabled_master_switch_never_replays(monkeypatch) -> None:
    monkeypatch.setitem(replay_mod.LDW, "enabled", False)
    result = await replay_mod.replay_plan("sess", plan_key="k1", force=True)
    assert result["status"] == "disabled"
