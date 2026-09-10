"""Behavior tests for taskflow_wait_all (todo 5, taskflow-dag-phase1).

The tool polls ONLY the child sessions recorded on the flow's dispatched steps.
Every test monkeypatches the tool module's registry seam
(``get_run_by_child_session_key`` / ``is_live_unended_run``) with in-memory
fakes -- the real subagent registry is never touched -- and passes a tiny poll
interval, so no test sleeps for real wall-clock time.

Coverage:
(a) no dispatched steps -> immediate "no dispatched" result
(b) a dispatched step whose run is None -> settled, no AttributeError
(c) all runs not-live -> immediate settled report
(d) one live run -> loop polls again and returns settled after it flips
(e) timeout path returns a timeout message with a partial report
(f) an unrelated live child key is never queried and does not block the return
"""

import asyncio
from pathlib import Path

import pytest

from agent.tools.taskflow.config import StepStatus
from agent.tools.taskflow.registry import store_sqlite
from agent.tools.taskflow.tools import taskflow_wait_all as wait_mod

pytestmark = [pytest.mark.unit]

FLOW = "flow-wait"


class _Run:
    """Minimal stand-in for a SubagentRunRecord (only liveness matters here)."""

    def __init__(self, key: str, live: bool | object = True) -> None:
        self.child_session_key = key
        self.live = live


def _step(step_id: str, status: StepStatus | str, child: str | None = None) -> dict:
    step: dict = {
        "step_id": step_id,
        "task": f"task-{step_id}",
        "status": status.value if isinstance(status, StepStatus) else status,
    }
    if child is not None:
        step["child_session_key"] = child
    return step


def _patch_registry(
    monkeypatch: pytest.MonkeyPatch,
    runs: dict[str, _Run],
) -> tuple[list[str], list[str]]:
    """Replace the registry seam; return (queried keys, liveness checks)."""
    queried: list[str] = []
    live_checks: list[str] = []

    def fake_get_run(child_session_key: str) -> _Run | None:
        queried.append(child_session_key)
        return runs.get(child_session_key)

    def fake_is_live(run: _Run) -> bool:
        live_checks.append(run.child_session_key)
        live = run.live
        return bool(live()) if callable(live) else bool(live)

    monkeypatch.setattr(wait_mod, "get_run_by_child_session_key", fake_get_run)
    monkeypatch.setattr(wait_mod, "is_live_unended_run", fake_is_live)
    return queried, live_checks


async def _seed(steps: list[dict]) -> None:
    await store_sqlite.create_flow(
        FLOW, {"description": "wait probe", "steps": steps, "results": []}
    )


async def _wait(timeout_seconds: float = 5.0) -> str:
    return await wait_mod.taskflow_wait_all.coroutine(
        FLOW, timeout_seconds=timeout_seconds, poll_interval_seconds=0.01
    )


# ---------------------------------------------------------------------------
# (a) no dispatched steps -> immediate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_dispatched_steps_returns_immediately(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given a flow whose steps are done/ready/blocked but none dispatched
    queried, live_checks = _patch_registry(monkeypatch, {})
    await _seed(
        [
            _step("step-1", StepStatus.DONE, child="c-done"),
            _step("step-2", StepStatus.READY),
            _step("step-3", StepStatus.BLOCKED),
        ]
    )

    # When wait_all is called
    out = await _wait()

    # Then it returns immediately without touching the registry
    assert "no dispatched" in out.lower()
    assert "Error" not in out
    assert queried == []
    assert live_checks == []


# ---------------------------------------------------------------------------
# (b) dispatched run is None -> settled (never dereference None)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dispatched_step_with_none_run_is_settled(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given a dispatched step whose registry run cannot be found (None)
    queried, live_checks = _patch_registry(monkeypatch, {})
    await _seed([_step("step-1", StepStatus.DISPATCHED, child="c-none")])

    # When wait_all is called
    out = await _wait()

    # Then the unknown run is treated as settled, and is_live is never called
    assert "Error" not in out
    assert "settled=True" in out
    assert queried == ["c-none"]
    assert live_checks == []


# ---------------------------------------------------------------------------
# (c) all runs not-live -> immediate settled report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_all_runs_not_live_returns_settled_report(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given two dispatched steps whose runs are both ended
    runs = {"c1": _Run("c1", live=False), "c2": _Run("c2", live=False)}
    queried, live_checks = _patch_registry(monkeypatch, runs)
    await _seed(
        [
            _step("step-1", StepStatus.DISPATCHED, child="c1"),
            _step("step-2", StepStatus.DISPATCHED, child="c2"),
        ]
    )

    # When wait_all is called
    out = await _wait()

    # Then both are reported settled with no timeout
    assert "Error" not in out
    assert out.count("settled=True") == 2
    assert "timeout" not in out.lower()
    assert sorted(queried) == ["c1", "c2"]
    assert sorted(live_checks) == ["c1", "c2"]


# ---------------------------------------------------------------------------
# (d) a live run is polled until it settles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_run_is_polled_until_settled(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given a run that is live on the first check then ends
    state = {"checks": 0}

    def _live_once() -> bool:
        state["checks"] += 1
        return state["checks"] == 1

    runs = {"c1": _Run("c1", live=_live_once)}
    queried, live_checks = _patch_registry(monkeypatch, runs)
    await _seed([_step("step-1", StepStatus.DISPATCHED, child="c1")])

    # When wait_all is called
    out = await _wait()

    # Then it slept once and re-checked, returning settled
    assert "Error" not in out
    assert "settled=True" in out
    assert queried == ["c1", "c1"]
    assert live_checks == ["c1", "c1"]


# ---------------------------------------------------------------------------
# (e) timeout path returns a partial report
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeout_returns_partial_report(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given a dispatched run that stays live forever
    runs = {"c1": _Run("c1", live=True)}
    queried, live_checks = _patch_registry(monkeypatch, runs)
    await _seed([_step("step-1", StepStatus.DISPATCHED, child="c1")])

    # When wait_all is called with a tiny deadline
    out = await _wait(timeout_seconds=0.01)

    # Then it reports the timeout and that the step is still unsettled
    assert "timeout" in out.lower()
    assert "settled=False" in out
    assert "c1" in out
    assert len(queried) >= 1


# ---------------------------------------------------------------------------
# (f) an unrelated live child key never blocks the return
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unrelated_live_child_does_not_block(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given the flow's target has no run, but an unrelated child is live
    runs = {"other-child": _Run("other-child", live=True)}
    queried, _ = _patch_registry(monkeypatch, runs)
    await _seed([_step("step-1", StepStatus.DISPATCHED, child="flow-child")])

    # When wait_all is called
    out = await _wait()

    # Then only the flow's own key was queried and it returned settled
    assert "Error" not in out
    assert "settled=True" in out
    assert queried == ["flow-child"]
    assert "other-child" not in queried


# ---------------------------------------------------------------------------
# unknown flow -> Error, never raise
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_unknown_flow_returns_error(
    isolated_db: Path, monkeypatch: pytest.MonkeyPatch
):
    # Given no flow exists
    _patch_registry(monkeypatch, {})

    # When wait_all is called on a missing id
    out = await wait_mod.taskflow_wait_all.coroutine(
        "ghost-flow", timeout_seconds=0.01, poll_interval_seconds=0.01
    )

    # Then it returns an Error string
    assert out.startswith("Error:")
    assert "ghost-flow" in out
