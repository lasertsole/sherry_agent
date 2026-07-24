import pytest
from future_subagent.session.metrics import get_subagent_session_runtime_ms, resolve_subagent_session_status
from future_subagent.types.registry import SubagentRunRecord, ExecutionState, ExecutionStatus


class TestMetrics:
    def test_runtime_no_start(self):
        run = SubagentRunRecord(
            run_id="r1", child_session_key="c", requester_session_key="p", task="t",
        )
        assert get_subagent_session_runtime_ms(run) is None

    def test_runtime_running(self):
        import time
        run = SubagentRunRecord(
            run_id="r1", child_session_key="c", requester_session_key="p", task="t",
            execution=ExecutionState(status=ExecutionStatus.RUNNING, started_at=time.monotonic() - 5.0),
        )
        ms = get_subagent_session_runtime_ms(run)
        assert ms is not None
        assert ms >= 4000

    def test_runtime_completed(self):
        run = SubagentRunRecord(
            run_id="r1", child_session_key="c", requester_session_key="p", task="t",
            execution=ExecutionState(status=ExecutionStatus.TERMINAL, started_at=100.0, ended_at=105.0),
        )
        ms = get_subagent_session_runtime_ms(run)
        assert ms == pytest.approx(5000.0)

    def test_session_status(self):
        run = SubagentRunRecord(
            run_id="r1", child_session_key="c", requester_session_key="p", task="t",
            execution=ExecutionState(status=ExecutionStatus.RUNNING),
        )
        assert resolve_subagent_session_status(run) == "running"
