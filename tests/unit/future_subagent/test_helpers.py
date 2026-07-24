import pytest
from future_subagent.registry.helpers import (
    cap_frozen_result_text,
    resolve_announce_retry_delay_seconds,
    is_live_unended_run,
    has_run_ended,
    reconcile_orphaned_run,
)
from future_subagent.types.registry import SubagentRunRecord, ExecutionState, ExecutionStatus, RunOutcome, RunOutcomeStatus
from future_subagent.registry.memory import clear


@pytest.fixture(autouse=True)
def _clean():
    clear()
    yield
    clear()


def _make_run(**overrides):
    defaults = dict(
        run_id="r1",
        child_session_key="agent:main:future_subagent:abc",
        requester_session_key="agent:main:session:p1",
        task="test",
    )
    defaults.update(overrides)
    return SubagentRunRecord(**defaults)


class TestCapFrozenResultText:
    def test_none(self):
        assert cap_frozen_result_text(None) is None

    def test_short_text(self):
        assert cap_frozen_result_text("hello") == "hello"

    def test_long_text_truncated(self):
        text = "x" * 30000
        result = cap_frozen_result_text(text, max_bytes=24000)
        assert result is not None
        assert len(result) < len(text)
        assert "truncated" in result

    def test_exact_limit(self):
        text = "x" * 24000
        assert cap_frozen_result_text(text, max_bytes=24000) == text


class TestRetryDelay:
    def test_base_delay(self):
        assert resolve_announce_retry_delay_seconds(0, 1000) == 1.0

    def test_exponential_backoff(self):
        assert resolve_announce_retry_delay_seconds(1, 1000) == 2.0
        assert resolve_announce_retry_delay_seconds(2, 1000) == 4.0


class TestRunLiveness:
    def test_live_running(self):
        run = _make_run()
        assert is_live_unended_run(run)

    def test_live_interrupted(self):
        run = _make_run(execution=ExecutionState(status=ExecutionStatus.INTERRUPTED))
        assert is_live_unended_run(run)

    def test_not_live_terminal(self):
        run = _make_run(execution=ExecutionState(status=ExecutionStatus.TERMINAL))
        assert not is_live_unended_run(run)

    def test_has_ended(self):
        run = _make_run(execution=ExecutionState(status=ExecutionStatus.TERMINAL))
        assert has_run_ended(run)


class TestReconcileOrphanedRun:
    def test_not_orphaned(self):
        run = _make_run()
        result = reconcile_orphaned_run(run)
        assert result is None

    def test_terminal_not_orphaned(self):
        run = _make_run(execution=ExecutionState(status=ExecutionStatus.TERMINAL))
        result = reconcile_orphaned_run(run)
        assert result is None

    def test_no_started_at(self):
        run = _make_run(execution=ExecutionState(status=ExecutionStatus.RUNNING, started_at=None))
        result = reconcile_orphaned_run(run)
        assert result is None
