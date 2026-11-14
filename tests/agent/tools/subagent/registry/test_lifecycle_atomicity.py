"""Completion atomicity and lifecycle bookkeeping bounds.

The completion transition used to expose a check-then-act fast path (``if
lock.locked(): return``), so two callbacks could both pass the check and one
would complete a run the other had already finalized. The lock now guards the
whole transition and a waiter re-checks the terminal state inside it. The
per-run bookkeeping dicts are pruned by the registry sweeper, including the
generation tracker and the steer rate-limit marks.
"""

from __future__ import annotations

import asyncio

import pytest

import agent.tools.subagent.registry.lifecycle as lifecycle
from agent.tools.subagent.control import steer
from agent.tools.subagent.registry.terminal_gen import TerminalGenerationTracker
from agent.tools.subagent.types.registry import (
    ExecutionState,
    ExecutionStatus,
    RunOutcome,
    RunOutcomeStatus,
    SubagentRunRecord,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _record(run_id: str, status: ExecutionStatus = ExecutionStatus.RUNNING) -> SubagentRunRecord:
    return SubagentRunRecord(
        run_id=run_id,
        child_session_key=f"agent:main:subagent:{run_id}",
        requester_session_key="agent:main:session:s",
        task="t",
        execution=ExecutionState(status=status),
    )


class _Tracker(TerminalGenerationTracker):
    """A tracker whose callbacks are always current (the test targets the lock)."""

    def is_callback_current(self, _run_id: str, _generation: int) -> bool:
        return True

    def is_older_equivalent(self, _run_id: str, _generation: int) -> bool:
        return False


class TestCompletionAtomicity:
    @pytest.fixture()
    def board(self, monkeypatch: pytest.MonkeyPatch):
        runs = {"r1": _record("r1")}
        announced: list[str] = []
        monkeypatch.setattr(lifecycle, "_terminal_locks", {})
        monkeypatch.setattr(lifecycle, "get_run", lambda run_id: runs.get(run_id))

        def _set_run(run: SubagentRunRecord) -> None:
            runs[run.run_id] = run

        def _complete(run_id, _outcome, _result_text, _handoff):
            updated = runs[run_id].model_copy(
                update={"execution": ExecutionState(status=ExecutionStatus.TERMINAL)}
            )
            runs[run_id] = updated
            return updated

        async def _announce(run):
            announced.append(run.run_id)
            await asyncio.sleep(0.05)  # hold the lock so the second caller waits

        monkeypatch.setattr(lifecycle, "set_run", _set_run)
        monkeypatch.setattr(lifecycle, "_complete_run", _complete)
        monkeypatch.setattr(lifecycle, "_mark_terminal_owner", lambda run, _owner: run)
        monkeypatch.setattr(lifecycle, "_arbitrate_kill_vs_completion", lambda run, _o: run)
        monkeypatch.setattr(lifecycle, "is_superseded_run", lambda _run: False)
        monkeypatch.setattr(lifecycle, "get_terminal_gen_tracker", lambda: _Tracker())
        monkeypatch.setattr(lifecycle, "_start_announce_cleanup_flow", _announce)
        return runs, announced

    def test_two_concurrent_completions_announce_once(self, board):
        runs, announced = board
        outcome = RunOutcome(status=RunOutcomeStatus.OK)

        async def _both():
            return await asyncio.gather(
                lifecycle.complete_subagent_run("r1", outcome),
                lifecycle.complete_subagent_run("r1", outcome),
            )

        first, second = asyncio.run(_both())

        assert announced == ["r1"], "the terminal transition must run exactly once"
        assert runs["r1"].execution.status == ExecutionStatus.TERMINAL
        assert first is not None and first.execution.status == ExecutionStatus.TERMINAL
        assert second is not None and second.execution.status == ExecutionStatus.TERMINAL

    def test_a_missing_run_is_not_completed(self, board):
        _runs, announced = board

        result = asyncio.run(
            lifecycle.complete_subagent_run("absent", RunOutcome(status=RunOutcomeStatus.OK))
        )

        assert result is None
        assert announced == []


class _Timer:
    def __init__(self) -> None:
        self.cancelled = False

    def done(self) -> bool:
        return False

    def cancel(self) -> None:
        self.cancelled = True


class TestLifecycleSweep:
    def test_absent_runs_lose_every_bookkeeping_entry(self, monkeypatch):
        live = _record("live")
        runs = {"live": live}
        monkeypatch.setattr(lifecycle, "get_run", lambda run_id: runs.get(run_id))
        dead_lock = asyncio.Lock()
        live_lock = asyncio.Lock()
        timer = _Timer()
        tracker = TerminalGenerationTracker()
        tracker.register_expected("live", 1)
        tracker.register_expected("dead", 1)
        monkeypatch.setattr(
            lifecycle,
            "_terminal_locks",
            {"dead": dead_lock, "live": live_lock},
        )
        monkeypatch.setattr(lifecycle, "_cleanup_generations", {"dead": 1, "live": 1})
        monkeypatch.setattr(
            lifecycle, "_deferred_cleanup_timers", {"dead": timer, "live": _Timer()}
        )
        monkeypatch.setattr(lifecycle, "get_terminal_gen_tracker", lambda: tracker)

        pruned = lifecycle.sweep_stale_lifecycle_state()

        assert pruned == 4  # lock + generation + timer + generation-tracker entry
        assert "dead" not in lifecycle._terminal_locks
        assert "dead" not in lifecycle._cleanup_generations
        assert "dead" not in lifecycle._deferred_cleanup_timers
        assert timer.cancelled is True
        assert tracker.is_callback_current("dead", 1) is True  # entry gone
        assert tracker.is_callback_current("live", 1) is True
        assert "live" in lifecycle._terminal_locks

    def test_stale_steer_marks_are_pruned_by_age(self, monkeypatch):
        now = 1000.0
        monkeypatch.setattr(steer.time, "monotonic", lambda: now)
        monkeypatch.setattr(
            steer,
            "_last_steer_at",
            {"old:child": now - 7200.0, "fresh:child": now - 10.0},
        )

        pruned = steer.prune_stale_steer_marks(max_age_s=3600.0)

        assert pruned == 1
        assert set(steer._last_steer_at) == {"fresh:child"}
