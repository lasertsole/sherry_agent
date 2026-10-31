"""The load gate's integration with the SUBAGENT lane wrapper.

Four behaviors matter at the seam (`spawn/core.py::_execute_subagent_with_lane`):

* a stop verdict defers the dispatch WITHOUT taking a lane slot;
* the deferral is bounded — past the budget the run is TERMINALIZED, not left
  behind for the sweeper to re-dispatch (that would be an infinite loop);
* a kill during the wait ends it at once;
* the warning frame fires once per run, not once per retry.
"""

from __future__ import annotations

import asyncio
import time

import pytest

import agent.tools.subagent.spawn.core as core
from agent.tools.pub_base.load_gate import LoadGateVerdict
from agent.tools.subagent.registry import memory
from agent.tools.subagent.types.registry import ExecutionStatus, SubagentRunRecord

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_STOP = LoadGateVerdict(ok=False, stop=True, cpu=97.0, ram=96.0)
_OK = LoadGateVerdict(ok=True, stop=False, cpu=10.0, ram=20.0)


@pytest.fixture()
def run() -> SubagentRunRecord:
    """A PENDING run record, as the dispatcher would have registered it."""
    record = SubagentRunRecord(
        run_id="run-load-1",
        child_session_key="agent:main:subagent:child-1",
        requester_session_key="agent:main:session:owner",
        task="do the thing",
    )
    memory.set_run(record)
    yield record
    memory.delete(record.run_id)


@pytest.fixture(autouse=True)
def _gate_config(monkeypatch: pytest.MonkeyPatch):
    from config.features import LANE_SYSTEM

    monkeypatch.setitem(LANE_SYSTEM, "load_gate_enabled", True)
    monkeypatch.setitem(LANE_SYSTEM, "load_gate_retry_s", 0.0)
    monkeypatch.setitem(LANE_SYSTEM, "load_gate_max_defer_s", 600.0)
    core._load_warned_runs.clear()
    yield
    core._load_warned_runs.clear()


def _patch_gate(monkeypatch: pytest.MonkeyPatch, *verdicts: LoadGateVerdict) -> list[int]:
    """Serve the given verdicts in order, repeating the last one."""
    calls: list[int] = []

    async def _gate() -> LoadGateVerdict:
        calls.append(1)
        return verdicts[min(len(calls) - 1, len(verdicts) - 1)]

    import agent.tools.pub_base.load_gate as load_gate

    monkeypatch.setattr(load_gate, "get_load_gate", _gate)
    return calls


def _lane_spy(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Record lane-slot entry WITHOUT blocking on the real semaphore."""
    entered: list[str] = []

    import contextlib

    import runtime.lane as lane

    @contextlib.asynccontextmanager
    async def _slot(_lane_type):
        entered.append("slot")
        yield

    monkeypatch.setattr(lane, "lane_slot", _slot)

    async def _no_execute(**_kwargs) -> None:
        entered.append("execute")

    monkeypatch.setattr(core, "_execute_subagent", _no_execute)
    return entered


class TestTheGateInFrontOfTheLane:
    @pytest.mark.asyncio
    async def test_a_quiet_host_reaches_the_lane(self, run, monkeypatch):
        _patch_gate(monkeypatch, _OK)
        entered = _lane_spy(monkeypatch)

        await core._execute_subagent_with_lane(
            run=run, system_prompt="", user_message="", tools=None, timeout_seconds=60
        )

        assert entered == ["slot", "execute"]
        # The stub executor does not finalize; reaching it proves the promote ran.
        assert memory.get(run.run_id).execution.status == ExecutionStatus.RUNNING

    @pytest.mark.asyncio
    async def test_a_stop_verdict_never_enters_the_lane(self, run, monkeypatch):
        from config.features import LANE_SYSTEM

        monkeypatch.setitem(LANE_SYSTEM, "load_gate_max_defer_s", 0.05)
        monkeypatch.setitem(LANE_SYSTEM, "load_gate_retry_s", 0.02)
        _patch_gate(monkeypatch, _STOP)
        entered = _lane_spy(monkeypatch)

        await core._execute_subagent_with_lane(
            run=run, system_prompt="", user_message="", tools=None, timeout_seconds=60
        )

        assert entered == [], "a deferred dispatch must hold no lane slot"

    @pytest.mark.asyncio
    async def test_the_budget_terminalizes_instead_of_abandoning(self, run, monkeypatch):
        """Leaving it PENDING would be an orphan the sweeper re-dispatches forever."""
        from config.features import LANE_SYSTEM

        monkeypatch.setitem(LANE_SYSTEM, "load_gate_max_defer_s", 0.05)
        monkeypatch.setitem(LANE_SYSTEM, "load_gate_retry_s", 0.02)
        _patch_gate(monkeypatch, _STOP)
        _lane_spy(monkeypatch)

        started = time.monotonic()
        await core._execute_subagent_with_lane(
            run=run, system_prompt="", user_message="", tools=None, timeout_seconds=60
        )

        assert time.monotonic() - started < 5, "the budget must be honoured, not slept past"
        current = memory.get(run.run_id)
        assert current.execution.status == ExecutionStatus.TERMINAL, "must be terminalized"
        assert current.ended_reason == "timeout"
        assert "load_timeout" in (current.execution.outcome.error or "")

    @pytest.mark.asyncio
    async def test_a_kill_during_the_wait_ends_it_immediately(self, run, monkeypatch):
        from config.features import LANE_SYSTEM
        from agent.tools.subagent.registry.run_manager import save_kill_reconciliation

        # A long budget with a short retry: only the kill can end this quickly.
        monkeypatch.setitem(LANE_SYSTEM, "load_gate_max_defer_s", 600.0)
        monkeypatch.setitem(LANE_SYSTEM, "load_gate_retry_s", 0.02)
        _patch_gate(monkeypatch, _STOP)
        _lane_spy(monkeypatch)

        async def _kill_soon() -> None:
            await asyncio.sleep(0.05)
            save_kill_reconciliation(run.run_id)

        started = time.monotonic()
        await asyncio.gather(
            core._execute_subagent_with_lane(
                run=run, system_prompt="", user_message="", tools=None, timeout_seconds=60
            ),
            _kill_soon(),
        )

        assert time.monotonic() - started < 10, "a kill must not wait out the budget"
        assert memory.get(run.run_id).kill_reconciliation is not None
        assert memory.get(run.run_id).execution.status != ExecutionStatus.TERMINAL

    @pytest.mark.asyncio
    async def test_the_warning_fires_once_per_run(self, run, monkeypatch):
        from config.features import LANE_SYSTEM

        monkeypatch.setitem(LANE_SYSTEM, "load_gate_max_defer_s", 0.08)
        monkeypatch.setitem(LANE_SYSTEM, "load_gate_retry_s", 0.02)
        _patch_gate(monkeypatch, _STOP)
        _lane_spy(monkeypatch)

        pushed: list[dict] = []

        async def _fake_broadcast(session_id, frame, **_kwargs):
            pushed.append(frame)

        import runtime.session.ws_push as ws_push

        monkeypatch.setattr(ws_push, "broadcast_to_session", _fake_broadcast)

        await core._execute_subagent_with_lane(
            run=run, system_prompt="", user_message="", tools=None, timeout_seconds=60
        )

        assert len(pushed) == 1, "the retries must not spam the socket"
        assert pushed[0]["event"] == "load_warning"
        assert pushed[0]["content"]["run_id"] == run.run_id
        assert run.run_id not in core._load_warned_runs, "the throttle entry is cleaned up"

    @pytest.mark.asyncio
    async def test_disabled_is_a_pure_pass_through(self, run, monkeypatch):
        """The shipped default must not sample at all."""
        from config.features import LANE_SYSTEM

        monkeypatch.setitem(LANE_SYSTEM, "load_gate_enabled", False)
        called = _patch_gate(monkeypatch, _STOP)
        entered = _lane_spy(monkeypatch)

        await core._execute_subagent_with_lane(
            run=run, system_prompt="", user_message="", tools=None, timeout_seconds=60
        )

        assert called == [], "disabled means no gate read at all"
        assert entered == ["slot", "execute"]
