"""The eval orchestration gate counts PER SESSION (one gate, many tasks).

The gate is process-level but its policy is per task: every suite drives all of
its tasks in one process, so a single global counter dict let the first task's
planning calls unlock every later task. These tests drive the installed hook
over a stubbed executor and assert the bucket boundaries.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent.safe_tool_node import SafeToolNode
from evals import external_bench
from evals.external_bench import _ORCHESTRATION_UNLOCK

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


class _Runtime:
    """The slice of the tool runtime the gate reads (context.session_id)."""

    def __init__(self, session_id: str | None) -> None:
        self.context: dict[str, Any] = {"session_id": session_id}


class _Result:
    def __init__(self, content: str = "") -> None:
        self.content = content


class _Executor:
    """Stands in for the real tool executor behind the gate."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, str]] = []
        self.content = ""


@pytest.fixture()
def executor(monkeypatch: pytest.MonkeyPatch) -> _Executor:
    """Install the gate over a stub executor; restore class + globals after."""
    stub = _Executor()

    async def _fake_arun_one(self: Any, call: Any, input_type: Any, tool_runtime: Any) -> _Result:
        stub.executed.append((str(call.get("name")), str(tool_runtime.context["session_id"])))
        return _Result(stub.content)

    monkeypatch.setattr(SafeToolNode, "_arun_one", _fake_arun_one)
    monkeypatch.setattr(external_bench, "_GATE_INSTALLED", False)
    monkeypatch.setattr(external_bench, "_GATE_STATE", {})
    external_bench.install_orchestration_gate()
    return stub


async def _call(name: str, session_id: str | None, **args: Any) -> Any:
    runtime = _Runtime(session_id)
    return await SafeToolNode._arun_one(  # type: ignore[misc] — the installed hook
        object(), {"name": name, "args": args, "id": f"call-{name}"}, "list", runtime
    )


@pytest.mark.asyncio
async def test_planning_in_one_session_does_not_unlock_another(executor: _Executor):
    await _call("todowrite", "sess-a")
    await _call("taskflow_flow", "sess-a", action="plan")

    allowed = await _call("write_file", "sess-a", path="a.py")
    blocked = await _call("write_file", "sess-b", path="b.py")

    assert allowed.content == "", "the planning task may write"
    assert blocked.content == _ORCHESTRATION_UNLOCK
    assert ("write_file", "sess-a") in executor.executed
    assert ("write_file", "sess-b") not in executor.executed, (
        "the refused call must not reach the executor"
    )
    assert external_bench.orchestration_state("sess-a")["blocked"] == 0
    assert external_bench.orchestration_state("sess-b")["blocked"] == 1


@pytest.mark.asyncio
async def test_todoread_is_not_planning(executor: _Executor):
    await _call("todoread", "sess-a")
    await _call("taskflow_flow", "sess-a", action="plan")

    blocked = await _call("write_file", "sess-a", path="a.py")

    assert blocked.content == _ORCHESTRATION_UNLOCK
    assert ("write_file", "sess-a") not in executor.executed, (
        "a read-only todo call must not unlock writes"
    )
    assert external_bench.orchestration_state("sess-a")["todo"] == 0


@pytest.mark.asyncio
async def test_todowrite_and_one_plan_call_unlock(executor: _Executor):
    await _call("todowrite", "sess-a")
    await _call("taskflow_flow", "sess-a", action="plan")

    allowed = await _call("patch_file", "sess-a", path="a.py")

    assert allowed.content == ""
    assert executor.executed[-1] == ("patch_file", "sess-a")
    assert external_bench.orchestration_state("sess-a")["plan_calls"] == 1


@pytest.mark.asyncio
async def test_a_child_agent_is_never_gated(executor: _Executor):
    """Children have no todowrite/taskflow: the mandate is the MAIN agent's."""
    child = "agent:main:subagent:0d5f2b1e"

    allowed = await _call("write_file", child, path="a.py")

    assert allowed.content == ""
    assert executor.executed == [("write_file", child)]
    assert external_bench.orchestration_state(child)["blocked"] == 0


@pytest.mark.asyncio
async def test_a_path_denial_is_counted_in_the_calling_session(executor: _Executor):
    await _call("todowrite", "sess-a")
    await _call("taskflow_flow", "sess-a", action="plan")
    executor.content = "access denied by PathGuard: rejected"

    await _call("write_file", "sess-a", path="a.py")

    assert external_bench.orchestration_state("sess-a")["path_denials"] == 1
    assert external_bench.orchestration_state("sess-a")["blocked_writes"] == 1


def test_run_totals_sum_the_buckets_and_reads_do_not_create_them(
    executor: _Executor,
):
    external_bench._gate_bucket("sess-a")["blocked"] = 2
    external_bench._gate_bucket("sess-b")["blocked"] = 3

    totals = external_bench.orchestration_state()
    assert totals["blocked"] == 5
    assert external_bench.orchestration_state("sess-missing")["blocked"] == 0
    assert "sess-missing" not in external_bench._GATE_STATE


def test_the_second_install_returns_the_live_mapping(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(external_bench, "_GATE_INSTALLED", False)
    monkeypatch.setattr(external_bench, "_GATE_STATE", {})
    first = external_bench.install_orchestration_gate()
    second = external_bench.install_orchestration_gate()

    assert first is second is external_bench._GATE_STATE
