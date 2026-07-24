import pytest
from future_subagent.control.controller import resolve_controller, list_controlled_runs, is_run_controllable_by
from future_subagent.control.list import build_subagent_list
from future_subagent.registry.memory import set_run, clear
from future_subagent.types.registry import SubagentRunRecord, ExecutionState, RunOutcome, RunOutcomeStatus


@pytest.fixture(autouse=True)
def _clean():
    clear()
    yield
    clear()


def _make_run(run_id, requester, child_key=None, depth=1, label=None):
    child_key = child_key or f"agent:main:future_subagent:{run_id}"
    r = SubagentRunRecord(
        run_id=run_id,
        child_session_key=child_key,
        requester_session_key=requester,
        task=f"task-{run_id}",
        depth=depth,
        label=label,
    )
    set_run(r)
    return r


class TestResolveController:
    def test_found(self):
        _make_run("r1", "parent1", "agent:main:future_subagent:child1")
        controller = resolve_controller("agent:main:future_subagent:child1")
        assert controller == "parent1"

    def test_not_found(self):
        assert resolve_controller("nonexistent") is None


class TestListControlledRuns:
    def test_list_by_requester(self):
        _make_run("r1", "parent1")
        _make_run("r2", "parent1")
        _make_run("r3", "parent2")
        runs = list_controlled_runs("parent1")
        assert len(runs) == 2


class TestIsRunControllableBy:
    def test_same_requester(self):
        run = _make_run("r1", "parent1")
        assert is_run_controllable_by(run, "parent1")

    def test_different_requester(self):
        run = _make_run("r1", "parent1")
        assert not is_run_controllable_by(run, "parent2")


class TestBuildSubagentList:
    def test_empty(self):
        result = build_subagent_list("nonexistent")
        assert result["total"] == 0
        assert result["active_count"] == 0

    def test_with_runs(self):
        _make_run("r1", "parent1", label="worker-1")
        _make_run("r2", "parent1", label="worker-2")
        result = build_subagent_list("parent1")
        assert result["total"] == 2
        assert result["active_count"] == 2
        assert len(result["active"]) == 2

    def test_with_completed_runs(self):
        from future_subagent.registry.run_manager import complete_run
        run = _make_run("r1", "parent1")
        complete_run(run.run_id, RunOutcome(status=RunOutcomeStatus.OK), "done")
        result = build_subagent_list("parent1")
        assert result["active_count"] == 0
        assert result["recent_count"] == 1
