"""Regression: the orchestration gate enforces PER TASK inside one run.

Measured on the DeepSWE L1 run ``20261125_222727``: task #1 planned, and the
process-wide gate counters then stayed satisfied for the rest of the exit —
task #2 (``taskflow_calls=0``) executed `terminal` freely and the run recorded a
single ``blocked`` in total. This pins the measured sequence: after task A
plans and writes, task B must still be refused until it plans itself.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.safe_tool_node import SafeToolNode
from evals import external_bench
from evals.external_bench import _ORCHESTRATION_UNLOCK

pytestmark = [pytest.mark.regression, pytest.mark.timeout(30)]

_TASK_A = "evals-deepswe-actionlint-aaaaaa"
_TASK_B = "evals-deepswe-bandit-bbbbbb"


class _Runtime:
    def __init__(self, session_id: str) -> None:
        self.context: dict[str, Any] = {"session_id": session_id}


class _Result:
    content = ""


@pytest.fixture()
def gate(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    executed: list[tuple[str, str]] = []

    async def _fake_arun_one(self: Any, call: Any, input_type: Any, tool_runtime: Any) -> _Result:
        executed.append((str(call.get("name")), str(tool_runtime.context["session_id"])))
        return _Result()

    monkeypatch.setattr(SafeToolNode, "_arun_one", _fake_arun_one)
    monkeypatch.setattr(external_bench, "_GATE_INSTALLED", False)
    monkeypatch.setattr(external_bench, "_GATE_STATE", {})
    external_bench.install_orchestration_gate()
    return executed


async def _call(session_id: str, name: str, **args: Any) -> Any:
    return await SafeToolNode._arun_one(  # type: ignore[misc] — the installed hook
        object(),
        {"name": name, "args": args, "id": f"{session_id}-{name}"},
        "list",
        _Runtime(session_id),
    )


@pytest.mark.asyncio
async def test_a_later_task_is_still_gated_after_an_earlier_task_planned(
    gate: list[tuple[str, str]],
):
    # Task A: plans, then works.
    await _call(_TASK_A, "todowrite")
    await _call(_TASK_A, "taskflow_flow", action="plan")
    first = await _call(_TASK_A, "terminal", command="ls")
    assert first.content == ""

    # Task B: never planned — the measured shape (47 tool calls, no taskflow).
    refused = await _call(_TASK_B, "terminal", command="python -m pytest -q")
    assert refused.content == _ORCHESTRATION_UNLOCK
    terminals = [entry for entry in gate if entry[0] == "terminal"]
    assert terminals == [("terminal", _TASK_A)], "task B never reached the executor"

    # The refusal is attributed to B, not to the run's earlier task.
    assert external_bench.orchestration_state(_TASK_A)["blocked"] == 0
    assert external_bench.orchestration_state(_TASK_B)["blocked"] == 1
    assert external_bench.orchestration_state()["blocked"] == 1

    # Once B plans, the same call goes through.
    await _call(_TASK_B, "todowrite")
    await _call(_TASK_B, "taskflow_flow", action="plan")
    retried = await _call(_TASK_B, "terminal", command="python -m pytest -q")
    assert retried.content == ""
    assert gate[-1] == ("terminal", _TASK_B)


@pytest.mark.asyncio
async def test_the_gate_survives_a_second_install(gate: list[tuple[str, str]]):
    """A second suite in one interpreter must keep using the same live mapping."""
    again = external_bench.install_orchestration_gate()

    assert again is external_bench._GATE_STATE
    refused = await _call(_TASK_B, "write_file", path="b.py")
    assert refused.content == _ORCHESTRATION_UNLOCK
