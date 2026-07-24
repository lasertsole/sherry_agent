import pytest
from future_subagent.registry.memory import set_run, clear
from future_subagent.registry.queries import (
    list_runs_for_requester,
    list_descendant_runs,
    count_active_runs_for_session,
    get_run_by_child_session_key,
)
from future_subagent.types.registry import SubagentRunRecord, ExecutionStatus


@pytest.fixture(autouse=True)
def _clean():
    clear()
    yield
    clear()


def _make_run(run_id, requester, child_key=None, task="test", task_name=None):
    child_key = child_key or f"agent:main:subagent:{run_id}"
    r = SubagentRunRecord(
        run_id=run_id,
        child_session_key=child_key,
        requester_session_key=requester,
        task=task,
        task_name=task_name,
    )
    set_run(r)
    return r


class TestQueries:
    def test_list_runs_for_requester(self):
        _make_run("r1", "parent1")
        _make_run("r2", "parent1")
        _make_run("r3", "parent2")
        result = list_runs_for_requester("parent1")
        assert len(result) == 2

    def test_list_runs_for_requester_empty(self):
        assert list_runs_for_requester("nonexistent") == []

    def test_list_descendant_runs(self):
        _make_run("r1", "root", child_key="child1")
        _make_run("r2", "child1", child_key="child2")
        _make_run("r3", "child2", child_key="child3")
        result = list_descendant_runs("root")
        assert len(result) == 3

    def test_list_descendant_no_infinite_loop(self):
        _make_run("r1", "A", child_key="B")
        _make_run("r2", "B", child_key="A")
        result = list_descendant_runs("A")
        assert len(result) >= 1

    def test_count_active_runs(self):
        _make_run("r1", "parent1")
        _make_run("r2", "parent1")
        count = count_active_runs_for_session("parent1")
        assert count == 2

    def test_get_run_by_child_session_key(self):
        _make_run("r1", "p1", child_key="agent:main:subagent:xyz")
        found = get_run_by_child_session_key("agent:main:subagent:xyz")
        assert found is not None
        assert found.run_id == "r1"

    def test_get_run_by_child_session_key_missing(self):
        assert get_run_by_child_session_key("nonexistent") is None
