import pytest
from future_subagent.registry.memory import get, set_run, delete, update, snapshot, size, clear, find_by_child_session_key
from future_subagent.types.registry import SubagentRunRecord, ExecutionStatus
from future_subagent.types.spawn import SpawnMode


@pytest.fixture(autouse=True)
def _clean_memory():
    clear()
    yield
    clear()


def _make_run(run_id="r1", child_key="agent:main:subagent:abc", requester="agent:main:session:p1", task="test"):
    return SubagentRunRecord(
        run_id=run_id,
        child_session_key=child_key,
        requester_session_key=requester,
        task=task,
    )


class TestMemoryStore:
    def test_set_and_get(self):
        run = _make_run()
        set_run(run)
        assert get("r1") is not None
        assert get("r1").task == "test"

    def test_get_missing(self):
        assert get("nonexistent") is None

    def test_delete(self):
        run = _make_run()
        set_run(run)
        deleted = delete("r1")
        assert deleted is not None
        assert get("r1") is None

    def test_delete_missing(self):
        assert delete("nonexistent") is None

    def test_update(self):
        run = _make_run()
        set_run(run)
        updated = update("r1", depth=5)
        assert updated.depth == 5
        assert get("r1").depth == 5

    def test_update_missing(self):
        assert update("nonexistent", depth=5) is None

    def test_snapshot(self):
        set_run(_make_run("r1"))
        set_run(_make_run("r2", child_key="agent:main:subagent:def"))
        snap = snapshot()
        assert len(snap) == 2
        assert "r1" in snap
        assert "r2" in snap

    def test_size(self):
        assert size() == 0
        set_run(_make_run())
        assert size() == 1

    def test_clear(self):
        set_run(_make_run())
        set_run(_make_run("r2"))
        clear()
        assert size() == 0

    def test_find_by_child_session_key(self):
        set_run(_make_run())
        found = find_by_child_session_key("agent:main:subagent:abc")
        assert found is not None
        assert found.run_id == "r1"

    def test_find_by_child_session_key_missing(self):
        assert find_by_child_session_key("nonexistent") is None
