"""timeout=0 semantics: run_timeout_seconds = 0 means "no timeout" for all consumers.

Covers the two consumers besides spawn (see test_spawn for spawn's direct-call branch):
followup's periodic timeout sweep must skip entirely, and steer's restarted child
must run without asyncio.wait_for. test_positive_timeout_still_times_out pins the
existing positive-timeout behavior (must pass before AND after the guard lands).
"""

import asyncio
import pytest
from agent.tools.subagent.config import get_config, set_config
from agent.tools.subagent.registry import (
    register_run,
    clear as clear_registry,
    get_run,
    set_run,
)
from agent.tools.subagent.types.registry import ExecutionStatus, SubagentRunRecord


def _minimal_run() -> SubagentRunRecord:
    return SubagentRunRecord(
        run_id="run-timeout-zero",
        child_session_key="agent:main:subagent:tz",
        requester_session_key="agent:main:session:tz",
        task="t",
    )


class TestFollowupZeroTimeout:
    @pytest.mark.asyncio
    async def test_zero_timeout_disables_check(self, monkeypatch):
        called = {"n": 0}

        async def _spy():
            called["n"] += 1

        import agent.tools.subagent.followup.core as fc

        monkeypatch.setattr(fc, "recover_orphaned_runs", _spy)
        orig = get_config()
        set_config(orig.model_copy(update={"run_timeout_seconds": 0.0}))
        clear_registry()
        try:
            run = register_run(
                child_session_key="agent:main:subagent:fu",
                requester_session_key="agent:main:session:fu",
                task="t",
                depth=1,
            )
            run.execution.status = ExecutionStatus.RUNNING
            await fc._check_timeouts()
            assert called["n"] == 0  # 0 超时：不得触发批量 recovery
        finally:
            set_config(orig)
            clear_registry()


class TestSteerZeroTimeout:
    @pytest.mark.asyncio
    async def test_zero_timeout_runs_without_wait_for(self):
        from agent.tools.subagent.control.steer import _execute_steered_subagent

        class _FakeAgent:
            async def ainvoke(self, input, config):
                return {"messages": [{"content": "done"}]}

        orig = get_config()
        set_config(orig.model_copy(update={"run_timeout_seconds": 0.0}))
        clear_registry()
        try:
            run = _minimal_run()
            set_run(run)  # complete_subagent_run resolves the run from registry memory
            await _execute_steered_subagent(run, _FakeAgent(), "[STEER] go", 0.0)
            got = get_run(run.run_id)
            assert got is not None
            assert got.execution.outcome is not None
            assert str(
                getattr(got.execution.outcome.status, "value", got.execution.outcome.status)
            ) != "timeout"
        finally:
            set_config(orig)
            clear_registry()

    @pytest.mark.asyncio
    async def test_positive_timeout_still_times_out(self):
        from agent.tools.subagent.control.steer import _execute_steered_subagent

        class _SlowAgent:
            async def ainvoke(self, input, config):
                await asyncio.sleep(0.3)
                return {"messages": [{"content": "late"}]}

        orig = get_config()
        set_config(orig.model_copy(update={"run_timeout_seconds": 300.0}))
        clear_registry()
        try:
            run = _minimal_run()
            set_run(run)  # complete_subagent_run resolves the run from registry memory
            await _execute_steered_subagent(run, _SlowAgent(), "[STEER] go", 0.05)
            got = get_run(run.run_id)
            assert str(
                getattr(got.execution.outcome.status, "value", got.execution.outcome.status)
            ) == "timeout"
        finally:
            set_config(orig)
            clear_registry()
