"""Integrated smoke: functional-role spawn chain + synthesize aggregation.

One hermetic file (stub lane, no LLM, no network) ties the two increments
together end to end:

* ``test_role_driven_spawn_chain_smoke`` walks the REAL spawn entry point with a
  ``researcher`` hint -- role parsing -> allow-list -> prompt sections -- and
  then feeds the captured role/tier/allow-list into the REAL
  ``_build_child_agent`` with stub LLMs, proving the role also drives LLM
  selection and tool filtering.
* ``test_synthesize_reaches_real_spawn_smoke`` runs the REAL ``taskflow_run_task``
  with ``aggregate_deps=True`` while only the SUBAGENT lane is stubbed, so the
  dependency result is aggregated by Phase 2 and then travels through the real
  spawn pipeline.
* ``test_synthesize_task_with_role_through_dispatch_smoke`` strings the two
  phases directly: ``build_task_with_dep_results`` output is handed to the real
  ``_dispatch.dispatch_child(functional_role=...)`` seam, so the aggregated task
  is spawned under an ``executor`` role with its whitelist applied.
"""

from __future__ import annotations

import asyncio
import sys
from types import SimpleNamespace

import pytest

from agent.tools.subagent.config import get_config
from agent.tools.subagent.registry import clear as clear_registry
from agent.tools.subagent.spawn import core as spawn_core
from agent.tools.subagent.spawn.core import (
    _build_child_agent,
    spawn_subagent_direct,
)
from agent.tools.subagent.types import FunctionalRole, SubagentSessionRole
from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow.config import StepStatus
from agent.tools.taskflow.registry import store_sqlite

pytestmark = [pytest.mark.integration, pytest.mark.timeout(120)]

_SESSION = "sess-smoke"
_OWNER_KEY = "agent:main:session:sess-smoke"
_HEADER = "## Upstream Results"
_FINDINGS = "FINDINGS-XYZ"

dispatch_module = sys.modules["agent.tools.taskflow.tools._dispatch"]


class _StubTool:
    def __init__(self, name: str, metadata: dict | None = None) -> None:
        self.name = name
        self.metadata = metadata or {}


class _StubCheckpointer:
    async def setup(self) -> None:
        return None


@pytest.fixture()
def _captured_lane(monkeypatch):
    """Stub the SUBAGENT lane so spawn resolves everything without an LLM."""
    captured: list[dict] = []

    async def _fake_lane(**kwargs):
        captured.append(kwargs)

    monkeypatch.setattr(spawn_core, "_execute_subagent_with_lane", _fake_lane)
    clear_registry()
    yield captured
    clear_registry()


@pytest.fixture()
def _wiring(monkeypatch):
    """Stub every heavy dependency of ``_build_child_agent`` and capture model/tools."""
    captured: dict = {}

    def _fake_create_agent(**kwargs):
        captured["model"] = kwargs.get("model")
        captured["tools"] = kwargs.get("tools", [])
        return SimpleNamespace(name="child-graph")

    async def _fake_checkpointer(*args, **kwargs):
        return _StubCheckpointer()

    monkeypatch.setattr("langchain.agents.create_agent", _fake_create_agent)
    monkeypatch.setattr("agent.checkpointer.build_async_sqlite_checkpointer", _fake_checkpointer)

    main_llm = SimpleNamespace(kind="main")
    aux_llm = SimpleNamespace(kind="aux")
    monkeypatch.setenv("MAIN_LLM_MAX_TOKEN", "131072")
    monkeypatch.setenv("AUXILIARY_LLM_MAX_TOKEN", "131072")
    monkeypatch.setattr("models.LLMs.main_llm.max_tokens", 65_536)
    monkeypatch.setattr("models.build_main_llm", lambda *a, **k: main_llm)
    monkeypatch.setattr("models.build_auxiliary_llm", lambda *a, **k: aux_llm)
    captured["main_llm"] = main_llm
    captured["aux_llm"] = aux_llm
    return captured


def _tools() -> dict:
    return {t.name: t for t in build_taskflow_tools()}


async def _seed_done_dependency(flow_id: str) -> None:
    dep = {
        "step_id": "step-1",
        "task": "task-step-1",
        "depends_on": [],
        "status": str(StepStatus.DONE),
        "child_session_key": "child-1",
        "retry_count": 0,
    }
    record = {
        "child_session_key": "child-1",
        "result": _FINDINGS,
        "result_hash": "hash::child-1",
    }
    out = await _tools()["taskflow_create"].coroutine(
        session_id=_SESSION,
        flow_id=flow_id,
        description="smoke probe",
        initial_state={"steps": [dep], "results": [record]},
    )
    assert "Error" not in out, out


def test_role_driven_spawn_chain_smoke(_captured_lane, _wiring, monkeypatch):
    # Given a LEAF-compatible depth cap so the executor whitelist is observable
    monkeypatch.setattr(get_config(), "max_spawn_depth", 1)

    # When a researcher spawn is requested with one extra tool
    async def _go():
        result = await spawn_subagent_direct(
            task="research the codebase",
            requester_session_key="agent:main:session:smoke",
            functional_role_hint="researcher",
            extra_tools=["python_repl"],
        )
        await asyncio.sleep(0.01)
        return result

    result = asyncio.run(_go())

    # Then the role was parsed, the tier resolved, the whitelist applied, and the
    # prompt carries the role sections
    assert result.status == "accepted"
    lane = _captured_lane[-1]
    run = lane["run"]
    assert run.functional_role is FunctionalRole.RESEARCHER
    assert lane["model_tier"] == "auxiliary"
    assert run.inherited_tool_allow == ["read_file", "terminal", "web_search", "python_repl"]
    assert "RESEARCHER specialization" in lane["system_prompt"]
    assert "## Role Instructions" in lane["system_prompt"]

    # ...and the captured role drives the real child-agent construction
    candidates = [
        _StubTool("read_file"),
        _StubTool("terminal"),
        _StubTool("web_search"),
        _StubTool("python_repl"),
        _StubTool("write_file"),
        _StubTool("sessions_spawn"),
        _StubTool("memory", {"scope": "main_only"}),
    ]
    asyncio.run(
        _build_child_agent(
            system_prompt=lane["system_prompt"],
            tools=candidates,
            tool_allow=run.inherited_tool_allow,
            tool_deny=run.inherited_tool_deny,
            role=SubagentSessionRole.LEAF,
            functional_role=run.functional_role,
            model_tier=lane["model_tier"],
            extra_tools=lane["extra_tools"],
        )
    )
    assert _wiring["model"] is _wiring["aux_llm"]
    assert [t.name for t in _wiring["tools"]] == [
        "read_file",
        "terminal",
        "web_search",
        "python_repl",
        "explore",
        "callers",
        "callees",
        "impact",
    ]


@pytest.mark.asyncio
async def test_synthesize_reaches_real_spawn_smoke(isolated_db, _captured_lane):
    # Given a done dependency with a recorded result
    await _seed_done_dependency("flow-smoke")

    # When an opted-in synthesis step dispatches through the real spawn pipeline
    out = await _tools()["taskflow_run_task"].coroutine(
        flow_id="flow-smoke",
        task="Synthesize the findings.",
        depends_on=["step-1"],
        aggregate_deps=True,
        session_id=_SESSION,
    )

    # Then the spawn was accepted and received the aggregated task text
    assert "dispatched" in out, out
    assert len(_captured_lane) == 1
    spawned_task = _captured_lane[-1]["run"].task
    assert _HEADER in spawned_task
    assert _FINDINGS in spawned_task

    # ...while the stored step keeps the original task text
    flow = await store_sqlite.get_flow("flow-smoke", _SESSION)
    assert flow is not None
    step = next(s for s in flow["state"]["steps"] if s["step_id"] == "step-2")
    assert step["task"] == "Synthesize the findings."


@pytest.mark.asyncio
async def test_synthesize_task_with_role_through_dispatch_smoke(
    isolated_db, _captured_lane, monkeypatch
):
    # Given a dependency result aggregated into a dispatch task (Phase 2) and a
    # LEAF depth cap (Phase 1 whitelist)
    monkeypatch.setattr(get_config(), "max_spawn_depth", 1)
    steps = [
        {
            "step_id": "step-1",
            "task": "gather",
            "depends_on": [],
            "status": "done",
            "child_session_key": "child-1",
        }
    ]
    results = [{"child_session_key": "child-1", "result": _FINDINGS, "result_hash": "h"}]
    synthesis = {
        "step_id": "step-2",
        "task": "Synthesize the findings.",
        "depends_on": ["step-1"],
        "aggregate_deps": True,
    }
    from agent.tools.taskflow.tools._shared import build_task_with_dep_results

    aggregated = build_task_with_dep_results(synthesis, steps, results)
    assert _HEADER in aggregated and _FINDINGS in aggregated

    # When the aggregated task is dispatched under the executor role
    child_key = await dispatch_module.dispatch_child(
        task=aggregated,
        requester_session_key=_OWNER_KEY,
        functional_role="executor",
    )
    assert child_key.startswith("agent:main:subagent:")
    # Let the background lane task run on this loop before inspecting the capture.
    await asyncio.sleep(0.01)

    # Then the real spawn resolved the role, applied its whitelist, and delivered
    # the aggregated text
    lane = _captured_lane[-1]
    run = lane["run"]
    assert run.functional_role is FunctionalRole.EXECUTOR
    assert "write_file" in run.inherited_tool_allow
    assert "sessions_spawn" not in run.inherited_tool_allow
    assert _HEADER in run.task and _FINDINGS in run.task
