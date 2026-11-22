"""Plan nudge (5 reads without planning) and TaskFlow-aware delegation pressure."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from agent.middlewares.delegation_pressure import core as gate_core
from agent.middlewares.delegation_pressure.core import DelegationPressureGate
from config.features import DELEGATION_PRESSURE

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_SESSION = "evals-swe-test-delegation-pressure-plan"

_DISPATCH_OK = "TaskFlow steps dispatched: flow_id=f1, step_ids=['s1'], revision=2"
_AUTO_DISPATCH_OK = "TaskFlow steps auto-dispatched: flow_id=f1, step_ids=['s1'], revision=2"
_PLAN_ONLY = (
    "TaskFlow planned: flow_id=f1, revision=2, steps=2; todo bound: do the thing\n"
    "  auto_dispatch: disabled by config; ready steps remain for a manual dispatch"
)
_DISPATCH_FAILED = "Error: step_id 's1' is not dispatchable (status=done)"


class _FakeRegister:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], Any] = {}

    def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        return self.data.get((session_id, key), default)

    def set_state(self, session_id: str, key: str, value: Any) -> bool:
        self.data[(session_id, key)] = value
        return True

    def delete_state(self, session_id: str, key: str) -> bool:
        self.data.pop((session_id, key), None)
        return True


@pytest.fixture()
def gate(monkeypatch: pytest.MonkeyPatch) -> DelegationPressureGate:
    monkeypatch.setenv("SHERRY_EVAL_DELEGATION_PRESSURE", "1")
    monkeypatch.setitem(DELEGATION_PRESSURE, "plan_nudge_after", 5)
    monkeypatch.setitem(DELEGATION_PRESSURE, "warn_after", 2)
    monkeypatch.setitem(DELEGATION_PRESSURE, "block_after", 4)
    monkeypatch.setattr(gate_core, "state_register_mem", _FakeRegister())
    return DelegationPressureGate()


def _request(name: str, **args: Any) -> SimpleNamespace:
    return SimpleNamespace(
        tool_call={"name": name, "args": args, "id": f"call-{name}"},
        state={"session_id": _SESSION},
    )


async def _run(
    gate: DelegationPressureGate,
    name: str,
    *,
    content: str = "ok",
    status: str = "success",
) -> None:
    async def handler(request):
        return ToolMessage(
            content=content,
            tool_call_id=str(request.tool_call["id"]),
            name=name,
            status=status,
        )

    await gate.awrap_tool_call(_request(name), handler)


async def _reads(gate: DelegationPressureGate, count: int) -> None:
    for _ in range(count):
        await _run(gate, "read_file")


async def _model(gate: DelegationPressureGate) -> str | None:
    out = await gate.abefore_model({"session_id": _SESSION})
    if not out:
        return None
    return str(out["messages"][0].content)


def _state(gate: DelegationPressureGate):
    return gate._get(_SESSION)


def _available(gate: DelegationPressureGate, *, todowrite: bool, taskflow: bool) -> None:
    gate._tool_availability = (todowrite, taskflow)


@pytest.mark.asyncio
async def test_plan_nudge_mentions_both_planning_tools(gate, monkeypatch):
    monkeypatch.setitem(DELEGATION_PRESSURE, "plan_nudge_after", 2)
    _available(gate, todowrite=True, taskflow=True)
    await _reads(gate, 2)

    text = await _model(gate)

    assert text is not None
    assert "PLANNING NUDGE" in text
    assert "todowrite" in text
    assert 'taskflow_flow(action="plan")' in text


@pytest.mark.asyncio
async def test_plan_nudge_only_names_the_available_tools(gate, monkeypatch):
    monkeypatch.setitem(DELEGATION_PRESSURE, "plan_nudge_after", 2)
    _available(gate, todowrite=True, taskflow=False)
    await _reads(gate, 2)
    only_todo = await _model(gate)
    assert only_todo is not None and "todowrite" in only_todo
    assert "taskflow_flow" not in only_todo

    gate._save(_SESSION, gate_core._PressureState())
    _available(gate, todowrite=False, taskflow=True)
    await _reads(gate, 2)
    only_flow = await _model(gate)
    assert only_flow is not None and "taskflow_flow" in only_flow
    assert "todowrite" not in only_flow


@pytest.mark.asyncio
async def test_plan_nudge_does_not_fire_without_a_planning_tool(gate, monkeypatch):
    monkeypatch.setitem(DELEGATION_PRESSURE, "plan_nudge_after", 2)
    _available(gate, todowrite=False, taskflow=False)
    await _reads(gate, 2)

    text = await _model(gate)
    # The ordinary warn may still fire (warn_after=2); the PLANNING NUDGE must not.
    assert text is None or "PLANNING NUDGE" not in text


@pytest.mark.asyncio
async def test_plan_nudge_does_not_fire_after_todowrite_or_a_flow(gate, monkeypatch):
    monkeypatch.setitem(DELEGATION_PRESSURE, "plan_nudge_after", 2)
    _available(gate, todowrite=True, taskflow=True)

    await _run(gate, "todowrite", content="ok")
    await _reads(gate, 3)
    after_todo = await _model(gate)
    assert after_todo is None or "PLANNING NUDGE" not in after_todo

    gate._save(_SESSION, gate_core._PressureState())
    await _run(gate, "taskflow_flow", content=_PLAN_ONLY)
    await _reads(gate, 3)
    after_flow = await _model(gate)
    assert after_flow is None or "PLANNING NUDGE" not in after_flow


@pytest.mark.asyncio
async def test_nudge_then_warn_on_the_next_threshold(gate, monkeypatch):
    monkeypatch.setitem(DELEGATION_PRESSURE, "plan_nudge_after", 1)
    _available(gate, todowrite=True, taskflow=True)
    await _reads(gate, 1)
    assert "PLANNING NUDGE" in (await _model(gate) or "")

    await _reads(gate, 1)  # total 2 = the fixture's warn_after
    text = await _model(gate)
    assert text is not None and "sessions_spawn" in text


@pytest.mark.asyncio
async def test_taskflow_dispatch_resets_the_pressure_count(gate):
    _available(gate, todowrite=True, taskflow=True)
    await _reads(gate, 3)

    await _run(gate, "taskflow_step", content=_DISPATCH_OK)

    state = _state(gate)
    assert state.read_calls == 0
    assert state.has_taskflow is True
    assert state.has_taskflow_dispatch is True


@pytest.mark.asyncio
async def test_auto_dispatch_from_plan_counts_as_delegation(gate):
    _available(gate, todowrite=True, taskflow=True)
    await _reads(gate, 3)

    await _run(gate, "taskflow_flow", content=_AUTO_DISPATCH_OK)

    state = _state(gate)
    assert state.read_calls == 0
    assert state.has_taskflow_dispatch is True


@pytest.mark.asyncio
async def test_a_failed_dispatch_neither_sets_the_flag_nor_resets(gate):
    _available(gate, todowrite=True, taskflow=True)
    await _reads(gate, 3)

    await _run(gate, "taskflow_step", content=_DISPATCH_FAILED)
    await _run(gate, "taskflow_step", content=_DISPATCH_FAILED, status="error")

    state = _state(gate)
    assert state.read_calls == 3
    assert state.has_taskflow_dispatch is False


@pytest.mark.asyncio
async def test_warn_and_escalate_use_the_taskflow_variant(gate):
    _available(gate, todowrite=True, taskflow=True)
    await _run(gate, "taskflow_step", content=_DISPATCH_OK)
    await _reads(gate, 2)  # warn_after in the fixture

    warn = await _model(gate)
    assert warn is not None
    assert "wait_all" in warn and "dispatch" in warn
    assert "sessions_spawn" not in warn

    await _reads(gate, 2)  # block_after in the fixture (total 4)
    escalated = await _model(gate)
    assert escalated is not None
    assert "STOP reading" in escalated and "wait_all" in escalated


@pytest.mark.asyncio
async def test_warn_and_escalate_keep_the_sessions_spawn_variant_without_dispatch(gate):
    _available(gate, todowrite=True, taskflow=True)
    await _reads(gate, 2)
    warn = await _model(gate)
    assert warn is not None and "sessions_spawn" in warn

    await _reads(gate, 2)
    escalated = await _model(gate)
    assert escalated is not None and "sessions_spawn" in escalated
    assert "ESCALATED" in escalated


@pytest.mark.asyncio
async def test_the_turn_boundary_resets_the_plan_tracking(gate):
    _available(gate, todowrite=True, taskflow=True)
    await _run(gate, "todowrite", content="ok")
    await _run(gate, "taskflow_flow", content=_AUTO_DISPATCH_OK)
    await _reads(gate, 2)

    gate._after_agent_impl({"session_id": _SESSION})

    state = _state(gate)
    assert state.read_calls == 0
    assert state.has_todowrite is False
    assert state.has_taskflow is False
    assert state.has_taskflow_dispatch is False
    assert state.plan_nudged is False


@pytest.mark.asyncio
async def test_a_new_turn_after_a_dispatch_turn_is_nudged_again(gate, monkeypatch):
    monkeypatch.setitem(DELEGATION_PRESSURE, "plan_nudge_after", 1)
    _available(gate, todowrite=True, taskflow=True)
    await _run(gate, "taskflow_flow", content=_AUTO_DISPATCH_OK)
    gate._after_agent_impl({"session_id": _SESSION})

    await _reads(gate, 1)

    assert "PLANNING NUDGE" in (await _model(gate) or "")


@pytest.mark.asyncio
async def test_an_accepted_spawn_still_resets_every_plan_flag(gate):
    _available(gate, todowrite=True, taskflow=True)
    await _run(gate, "todowrite", content="ok")
    await _reads(gate, 2)

    await _run(gate, "sessions_spawn", content="subagent spawned: run-1")

    state = _state(gate)
    assert state.read_calls == 0
    assert state.has_todowrite is False
