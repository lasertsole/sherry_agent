"""Unit tests for the pure step-DAG helpers and the shared dispatch seam.

TDD for todo 1 of taskflow-dag-phase1: these were written BEFORE the
implementation and pin the dependency / status / unlock semantics without the
LLM or the real spawn pipeline. Every test follows Given / When / Then and
asserts only the observable contract.

Coverage:
(a) deps_satisfied True only when every referenced step is done
(b) unknown dep id -> False
(c) missing depends_on -> True
(d) mark_step_done flips the matched step, returns its id, is idempotent
(e) unlock_dependents moves exactly the newly-satisfied blocked steps to ready
(f) step_status derives dispatched/ready for legacy steps
(g) _dispatch.dispatch_child is monkeypatchable and returns the child key
(h) a self-dependency is never satisfied and causes no infinite unlock
"""

import asyncio

import pytest

from agent.tools.taskflow.config import StepStatus
from agent.tools.taskflow.tools import _dispatch, _shared

pytestmark = [pytest.mark.unit]


def _step(
    step_id: str,
    status: StepStatus | str | None = None,
    child: str | None = None,
    depends_on: list[str] | None = None,
) -> dict:
    """Build a step dict; omitted keys model a pre-DAG legacy step."""
    step: dict = {"step_id": step_id, "task": f"task-{step_id}"}
    if status is not None:
        step["status"] = status
    if child is not None:
        step["child_session_key"] = child
    if depends_on is not None:
        step["depends_on"] = depends_on
    return step


# ---------------------------------------------------------------------------
# (a) deps_satisfied: all referenced steps done
# ---------------------------------------------------------------------------


def test_deps_satisfied_true_when_every_referenced_step_done():
    # Given two dependency steps that are done
    steps = [_step("step-1", StepStatus.DONE), _step("step-2", StepStatus.DONE)]
    dependent = _step("step-3", StepStatus.BLOCKED, depends_on=["step-1", "step-2"])

    # When deps are checked
    result = _shared.deps_satisfied(dependent, steps)

    # Then the dependency contract is met
    assert result is True


def test_deps_satisfied_false_when_one_referenced_step_not_done():
    # Given one done dependency and one still dispatched
    steps = [_step("step-1", StepStatus.DONE), _step("step-2", StepStatus.DISPATCHED)]
    dependent = _step("step-3", StepStatus.BLOCKED, depends_on=["step-1", "step-2"])

    # When deps are checked
    result = _shared.deps_satisfied(dependent, steps)

    # Then the unmet dependency blocks satisfaction
    assert result is False


# ---------------------------------------------------------------------------
# (b) unknown dep id -> False
# ---------------------------------------------------------------------------


def test_deps_satisfied_false_for_unknown_dep_id():
    # Given a step referencing an id that does not exist in the flow
    steps = [_step("step-1", StepStatus.BLOCKED, depends_on=["ghost"])]

    # When deps are checked
    result = _shared.deps_satisfied(steps[0], steps)

    # Then the unknown id can never be satisfied
    assert result is False


# ---------------------------------------------------------------------------
# (c) missing depends_on -> True
# ---------------------------------------------------------------------------


def test_deps_satisfied_true_when_depends_on_missing():
    # Given a legacy step with no depends_on key
    step = _step("step-1", StepStatus.BLOCKED)

    # When deps are checked
    result = _shared.deps_satisfied(step, [step])

    # Then there are no dependencies to wait on
    assert result is True


def test_deps_satisfied_true_when_depends_on_empty():
    # Given an explicit empty dependency list
    step = _step("step-1", StepStatus.BLOCKED, depends_on=[])

    # When deps are checked
    result = _shared.deps_satisfied(step, [step])

    # Then it is trivially satisfied
    assert result is True


# ---------------------------------------------------------------------------
# (d) mark_step_done
# ---------------------------------------------------------------------------


def test_mark_step_done_flips_matched_step_and_returns_id():
    # Given two dispatched steps with distinct child keys
    steps = [
        _step("step-1", StepStatus.DISPATCHED, child="child-1"),
        _step("step-2", StepStatus.DISPATCHED, child="child-2"),
    ]

    # When the second child's result is marked done
    returned = _shared.mark_step_done(steps, "child-2")

    # Then only that step flips and its id is returned
    assert returned == "step-2"
    assert steps[1]["status"] == StepStatus.DONE
    assert steps[0]["status"] == StepStatus.DISPATCHED


def test_mark_step_done_is_idempotent():
    # Given a step already marked done
    steps = [_step("step-1", StepStatus.DISPATCHED, child="child-1")]
    first = _shared.mark_step_done(steps, "child-1")

    # When marked done again
    second = _shared.mark_step_done(steps, "child-1")

    # Then it stays done and the same id is returned
    assert first == "step-1"
    assert second == "step-1"
    assert steps[0]["status"] == StepStatus.DONE


def test_mark_step_done_returns_none_for_unknown_child_key():
    # Given a step keyed by child-1
    steps = [_step("step-1", StepStatus.DISPATCHED, child="child-1")]

    # When an unknown child key is marked done
    returned = _shared.mark_step_done(steps, "child-nope")

    # Then nothing changes and None is returned
    assert returned is None
    assert steps[0]["status"] == StepStatus.DISPATCHED


# ---------------------------------------------------------------------------
# (e) unlock_dependents
# ---------------------------------------------------------------------------


def test_unlock_dependents_moves_exactly_newly_satisfied_steps():
    # Given one satisfied dependent, one unsatisfied, and a non-blocked step
    steps = [
        _step("step-1", StepStatus.DONE),
        _step("step-2", StepStatus.BLOCKED, depends_on=["step-1"]),
        _step("step-3", StepStatus.BLOCKED, depends_on=["step-4"]),
        _step("step-4", StepStatus.DISPATCHED),
        _step("step-5", StepStatus.READY),
    ]

    # When dependents are unlocked
    newly_ready = _shared.unlock_dependents(steps)

    # Then only the satisfied blocked step moves, and only its id is returned
    assert newly_ready == ["step-2"]
    assert steps[1]["status"] == StepStatus.READY
    assert steps[2]["status"] == StepStatus.BLOCKED
    assert steps[4]["status"] == StepStatus.READY


def test_unlock_dependents_returns_empty_when_nothing_satisfied():
    # Given a blocked step whose dependency is still dispatched
    steps = [
        _step("step-1", StepStatus.DISPATCHED),
        _step("step-2", StepStatus.BLOCKED, depends_on=["step-1"]),
    ]

    # When dependents are unlocked
    newly_ready = _shared.unlock_dependents(steps)

    # Then nothing unlocks
    assert newly_ready == []
    assert steps[1]["status"] == StepStatus.BLOCKED


# ---------------------------------------------------------------------------
# (f) step_status legacy derivation
# ---------------------------------------------------------------------------


def test_step_status_derives_dispatched_for_legacy_step_with_child_key():
    # Given a legacy step that has a child key but no status field
    step = _step("step-1", child="child-1")

    # When status is read
    result = _shared.step_status(step)

    # Then it derives dispatched
    assert result == StepStatus.DISPATCHED


def test_step_status_derives_ready_for_legacy_step_without_child_key():
    # Given a legacy step with neither status nor child key
    step = _step("step-1")

    # When status is read
    result = _shared.step_status(step)

    # Then it derives ready
    assert result == StepStatus.READY


def test_step_status_prefers_explicit_status_over_derivation():
    # Given a blocked step that nonetheless carries a child key
    step = _step("step-1", StepStatus.BLOCKED, child="child-1")

    # When status is read
    result = _shared.step_status(step)

    # Then the explicit status wins
    assert result == StepStatus.BLOCKED


# ---------------------------------------------------------------------------
# new_step / steps_summary rounding out the pure helper surface
# ---------------------------------------------------------------------------


def test_new_step_defaults_to_ready_with_no_dependencies():
    # Given a minimal step construction
    step = _shared.new_step("step-1", "collect data")

    # Then it is ready with an empty dependency list
    assert step["step_id"] == "step-1"
    assert step["task"] == "collect data"
    assert step["status"] == StepStatus.READY
    assert step["depends_on"] == []


def test_new_step_records_blocked_with_dependencies():
    # Given a dependent step construction
    step = _shared.new_step("step-2", "analyze", depends_on=["step-1"], status=StepStatus.BLOCKED)

    # Then status and dependencies are recorded
    assert step["status"] == StepStatus.BLOCKED
    assert step["depends_on"] == ["step-1"]


def test_steps_summary_counts_every_status():
    # Given steps spanning all four statuses (done twice)
    steps = [
        _step("step-1", StepStatus.BLOCKED),
        _step("step-2", StepStatus.READY),
        _step("step-3", StepStatus.DISPATCHED),
        _step("step-4", StepStatus.DONE),
        _step("step-5", StepStatus.DONE),
    ]

    # When summarized
    counts = _shared.steps_summary(steps)

    # Then each status is counted, including zeros for absent ones
    assert counts == {"blocked": 1, "ready": 1, "dispatched": 1, "done": 2}


def test_steps_summary_derives_legacy_steps():
    # Given legacy steps without an explicit status
    steps = [_step("step-1", child="child-1"), _step("step-2")]

    # When summarized
    counts = _shared.steps_summary(steps)

    # Then the derived statuses are counted
    assert counts == {"blocked": 0, "ready": 1, "dispatched": 1, "done": 0}


# ---------------------------------------------------------------------------
# (g) shared dispatch seam
# ---------------------------------------------------------------------------


def test_dispatch_child_seam_is_monkeypatchable_and_returns_child_key(monkeypatch):
    # Given the real dispatch seam replaced by a fake
    async def fake_dispatch(task: str, requester_session_key: str, label: str | None = None) -> str:
        assert task == "do work"
        assert requester_session_key == "agent:main:session:sess-1"
        assert label == "lbl"
        return "agent:main:subagent:child-9"

    monkeypatch.setattr(_dispatch, "dispatch_child", fake_dispatch)

    # When the module-qualified seam is awaited
    child_key = asyncio.run(
        _dispatch.dispatch_child(
            task="do work",
            requester_session_key="agent:main:session:sess-1",
            label="lbl",
        )
    )

    # Then the fake's returned child key is observed
    assert child_key == "agent:main:subagent:child-9"


def test_dispatch_module_does_not_import_subagent_at_module_scope():
    # Given the imported dispatch module
    # Then the lazy-import contract holds: the subagent runtime is not bound
    assert not hasattr(_dispatch, "spawn_subagent_direct")


# ---------------------------------------------------------------------------
# (h) self-dependency is never satisfied and never loops
# ---------------------------------------------------------------------------


def test_self_dependency_never_satisfied_and_no_infinite_unlock():
    # Given a blocked step that depends on itself
    steps = [_step("step-1", StepStatus.BLOCKED, depends_on=["step-1"])]

    # When dependency satisfaction and unlocking are evaluated
    satisfied = _shared.deps_satisfied(steps[0], steps)
    newly_ready = _shared.unlock_dependents(steps)

    # Then it is never reported satisfied and nothing unlocks
    assert satisfied is False
    assert newly_ready == []
    assert steps[0]["status"] == StepStatus.BLOCKED
