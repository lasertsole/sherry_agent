import pytest
from future_subagent.control.list import is_subagent_run_visible_to_session, build_subagent_list
from future_subagent.registry.memory import set_run, clear
from future_subagent.types.registry import SubagentRunRecord, ExecutionState, RunOutcome, RunOutcomeStatus


@pytest.fixture(autouse=True)
def _clean():
    clear()
    yield
    clear()


def _make_run(**overrides) -> SubagentRunRecord:
    defaults = dict(
        run_id="r1",
        child_session_key="agent:main:future_subagent:abc",
        requester_session_key="agent:main:session:p1",
        task="test",
    )
    defaults.update(overrides)
    return SubagentRunRecord(**defaults)


class TestIsSubagentRunVisibleToSession:
    def test_visible_to_requester(self):
        run = _make_run(requester_session_key="parent1")
        assert is_subagent_run_visible_to_session(run, "parent1") is True

    def test_visible_to_controller(self):
        run = _make_run(
            requester_session_key="parent1",
            controller_session_key="controller1",
        )
        assert is_subagent_run_visible_to_session(run, "controller1") is True

    def test_visible_via_requester_fallback(self):
        run = _make_run(requester_session_key="parent1", controller_session_key=None)
        assert is_subagent_run_visible_to_session(run, "parent1") is True

    def test_not_visible_to_other(self):
        run = _make_run(requester_session_key="parent1")
        assert is_subagent_run_visible_to_session(run, "other_session") is False

    def test_controller_takes_precedence(self):
        run = _make_run(
            requester_session_key="parent1",
            controller_session_key="controller1",
        )
        assert is_subagent_run_visible_to_session(run, "controller1") is True
        assert is_subagent_run_visible_to_session(run, "parent1") is True


class TestBuildSubagentListWithVisibility:
    def test_filters_by_visibility(self):
        r1 = _make_run(run_id="r1", requester_session_key="parent1",
                        child_session_key="agent:main:future_subagent:c1", label="w1")
        r2 = _make_run(run_id="r2", requester_session_key="parent2",
                        child_session_key="agent:main:future_subagent:c2", label="w2")
        set_run(r1)
        set_run(r2)

        result = build_subagent_list("parent1")
        assert result["total"] >= 1
        active_labels = [a["label"] for a in result["active"]]
        assert "w1" in active_labels

    def test_empty_list(self):
        result = build_subagent_list("nonexistent")
        assert result["total"] == 0
