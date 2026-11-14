"""Subtree wall-clock budget and cascade kill on a failed parent run.

A descendant may never outlive its parent's remaining budget (the frozen
``run_timeout_seconds`` plus the monotonic ``started_at``), and a parent that
ends without OK must kill its descendants — a failed flow can never collect
their results, and each live grandchild holds a SUBAGENT lane slot.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from agent.tools.subagent.control import kill as kill_mod
from agent.tools.subagent.spawn import lifecycle
from agent.tools.subagent.spawn.plan import (
    remaining_parent_run_budget,
    resolve_run_timeout_for_requester,
)
from agent.tools.subagent.types.registry import (
    ExecutionState,
    ExecutionStatus,
    RunOutcome,
    RunOutcomeStatus,
    SubagentRunRecord,
)

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

PARENT_KEY = "agent:main:subagent:parent"


def _run(
    run_id: str,
    key: str,
    *,
    status: ExecutionStatus = ExecutionStatus.RUNNING,
    timeout: float | None = 600.0,
    started_offset: float = -50.0,
) -> SubagentRunRecord:
    return SubagentRunRecord(
        run_id=run_id,
        child_session_key=key,
        requester_session_key="agent:main:session:s1",
        task="probe",
        run_timeout_seconds=timeout,
        execution=ExecutionState(
            status=status,
            started_at=time.monotonic() + started_offset
            if status != ExecutionStatus.PENDING
            else None,
        ),
    )


def _bind_parent(monkeypatch: pytest.MonkeyPatch, parent: SubagentRunRecord | None) -> None:
    monkeypatch.setattr(
        "agent.tools.subagent.registry.queries.get_run_by_child_session_key",
        lambda _key: parent,
    )


class TestSubtreeBudget:
    def test_a_grandchild_is_capped_by_the_parents_remaining_time(self, monkeypatch):
        _bind_parent(monkeypatch, _run("r-parent", PARENT_KEY, timeout=600.0))

        remaining = remaining_parent_run_budget(PARENT_KEY)
        assert remaining is not None and 500 < remaining <= 550

        # Requesting more than the parent has left is capped; a smaller request
        # is kept as-is.
        assert resolve_run_timeout_for_requester(PARENT_KEY, 7200) == pytest.approx(
            remaining or 0, abs=1.0
        )
        assert resolve_run_timeout_for_requester(PARENT_KEY, 120) == 120.0

    def test_a_spent_budget_still_grants_the_fail_fast_floor(self, monkeypatch):
        _bind_parent(monkeypatch, _run("r-parent", PARENT_KEY, timeout=600.0, started_offset=-900))

        assert resolve_run_timeout_for_requester(PARENT_KEY, 7200) == 1.0

    def test_a_main_agent_requester_keeps_the_regular_timeout(self, monkeypatch):
        _bind_parent(monkeypatch, None)

        assert remaining_parent_run_budget("sess-main") is None
        assert resolve_run_timeout_for_requester("sess-main", 1800) == 1800.0

    def test_an_unknown_or_disabled_parent_timeout_is_not_capped(self, monkeypatch):
        _bind_parent(monkeypatch, _run("r-parent", PARENT_KEY, timeout=None))

        assert remaining_parent_run_budget(PARENT_KEY) is None
        assert resolve_run_timeout_for_requester(PARENT_KEY, 1800) == 1800.0


class TestCascadeKill:
    def _patch_registry(self, monkeypatch, *, parent, children, latest, allowed):
        monkeypatch.setattr(kill_mod, "get_run", lambda _run_id: parent)
        monkeypatch.setattr(kill_mod, "list_runs_for_requester", lambda _key: children)
        monkeypatch.setattr(
            kill_mod, "get_latest_run_by_child_session_key", lambda key: latest.get(key)
        )
        monkeypatch.setattr(kill_mod, "can_control_run", lambda run, _key: (allowed, "denied"))

    @pytest.mark.asyncio
    async def test_only_live_latest_authorized_descendants_are_killed(self, monkeypatch):
        parent = _run("r-parent", PARENT_KEY, status=ExecutionStatus.TERMINAL)
        live = _run("r-live", "agent:main:subagent:live")
        stale = _run("r-stale", "agent:main:subagent:stale")
        terminal = _run("r-term", "agent:main:subagent:term", status=ExecutionStatus.TERMINAL)
        denied = _run("r-denied", "agent:main:subagent:denied")
        self._patch_registry(
            monkeypatch,
            parent=parent,
            children=[live, stale, terminal, denied],
            latest={
                "agent:main:subagent:live": live,
                "agent:main:subagent:stale": _run("r-newer", "agent:main:subagent:stale"),
                "agent:main:subagent:term": terminal,
                "agent:main:subagent:denied": denied,
            },
            allowed=False,
        )
        calls: list[tuple[str, str]] = []

        async def _fake_kill(run_id, reason="killed", **_kwargs):
            calls.append((run_id, reason))
            return [live]

        monkeypatch.setattr(kill_mod, "kill_subagent_run_with_cascade", _fake_kill)

        killed = await kill_mod.kill_descendants_of_run("r-parent", "parent timeout")

        # live is the only latest, non-terminal child; can_control_run is False
        # for every child here, so even it is skipped.
        assert killed == []
        assert calls == []

    @pytest.mark.asyncio
    async def test_an_authorized_live_descendant_is_cascaded(self, monkeypatch):
        parent = _run("r-parent", PARENT_KEY, status=ExecutionStatus.TERMINAL)
        live = _run("r-live", "agent:main:subagent:live")
        self._patch_registry(
            monkeypatch,
            parent=parent,
            children=[live],
            latest={"agent:main:subagent:live": live},
            allowed=True,
        )
        calls: list[tuple[str, str]] = []

        async def _fake_kill(run_id, reason="killed", **_kwargs):
            calls.append((run_id, reason))
            return [live]

        monkeypatch.setattr(kill_mod, "kill_subagent_run_with_cascade", _fake_kill)

        killed = await kill_mod.kill_descendants_of_run("r-parent", "parent timeout")

        assert calls == [("r-live", "parent timeout")]
        assert killed == [live]

    @pytest.mark.asyncio
    async def test_the_lifecycle_kills_descendants_only_for_non_ok_outcomes(self, monkeypatch):
        run = _run("r-parent", PARENT_KEY, status=ExecutionStatus.TERMINAL)
        calls: list[tuple[str, str]] = []

        async def _fake_kill(run_id, reason):
            calls.append((run_id, reason))
            return []

        monkeypatch.setattr(kill_mod, "kill_descendants_of_run", _fake_kill)

        await lifecycle._kill_descendants_after_failure(run, RunOutcome(status=RunOutcomeStatus.OK))
        assert calls == []

        for status in (
            RunOutcomeStatus.TIMEOUT,
            RunOutcomeStatus.KILLED,
            RunOutcomeStatus.ERROR,
            RunOutcomeStatus.BUDGET_EXHAUSTED,
        ):
            await lifecycle._kill_descendants_after_failure(run, RunOutcome(status=status))
        assert calls == [
            ("r-parent", "parent timeout"),
            ("r-parent", "parent killed"),
            ("r-parent", "parent error"),
            ("r-parent", "parent budget_exhausted"),
        ]

    @pytest.mark.asyncio
    async def test_a_kill_failure_never_escapes(self, monkeypatch):
        run = _run("r-parent", PARENT_KEY, status=ExecutionStatus.TERMINAL)

        async def _boom(*_args: Any, **_kwargs: Any):
            raise RuntimeError("registry down")

        monkeypatch.setattr(kill_mod, "kill_descendants_of_run", _boom)

        await lifecycle._kill_descendants_after_failure(
            run, RunOutcome(status=RunOutcomeStatus.TIMEOUT)
        )
