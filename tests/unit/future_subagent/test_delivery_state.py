import pytest
from future_subagent.registry.delivery_state import (
    is_delivery_pending,
    is_delivery_delivered,
    is_delivery_failed,
    is_delivery_suspended,
    is_delivery_terminal,
    should_retry_delivery,
    mark_delivery_pending,
    mark_delivery_in_progress,
    mark_delivery_delivered,
    mark_delivery_failed,
    mark_delivery_suspended,
    mark_delivery_discarded,
    get_delivery_attempt_count,
)
from future_subagent.registry.memory import set_run, get, clear
from future_subagent.types.registry import SubagentRunRecord, DeliveryStatus


@pytest.fixture(autouse=True)
def _clean():
    clear()
    yield
    clear()


def _make_run(run_id="r1"):
    r = SubagentRunRecord(
        run_id=run_id,
        child_session_key="agent:main:subagent:abc",
        requester_session_key="agent:main:session:p1",
        task="test",
    )
    set_run(r)
    return r


class TestDeliveryState:
    def test_initial_not_required(self):
        run = _make_run()
        assert run.delivery.status == DeliveryStatus.NOT_REQUIRED

    def test_mark_pending(self):
        run = _make_run()
        updated = mark_delivery_pending(run)
        assert updated.delivery.status == DeliveryStatus.PENDING
        set_run(updated)
        assert is_delivery_pending(get("r1"))

    def test_mark_in_progress(self):
        run = _make_run()
        updated = mark_delivery_pending(run)
        updated = mark_delivery_in_progress(updated)
        assert updated.delivery.status == DeliveryStatus.IN_PROGRESS

    def test_mark_delivered(self):
        run = _make_run()
        updated = mark_delivery_pending(run)
        updated = mark_delivery_delivered(updated)
        assert is_delivery_delivered(updated)
        assert is_delivery_terminal(updated)

    def test_mark_failed(self):
        run = _make_run()
        updated = mark_delivery_pending(run)
        updated = mark_delivery_in_progress(updated)
        updated = mark_delivery_failed(updated, "connection error")
        assert is_delivery_failed(updated)
        assert get_delivery_attempt_count(updated) == 1
        assert updated.delivery.last_error == "connection error"

    def test_should_retry(self):
        run = _make_run()
        updated = mark_delivery_pending(run)
        updated = mark_delivery_failed(updated, "err1")
        assert should_retry_delivery(updated, max_attempts=3)

        updated = mark_delivery_failed(updated, "err2")
        assert should_retry_delivery(updated, max_attempts=3)

        updated = mark_delivery_failed(updated, "err3")
        assert not should_retry_delivery(updated, max_attempts=3)

    def test_mark_suspended(self):
        run = _make_run()
        updated = mark_delivery_pending(run)
        updated = mark_delivery_suspended(updated)
        assert is_delivery_suspended(updated)
        assert updated.delivery.suspended_at is not None

    def test_mark_discarded(self):
        run = _make_run()
        updated = mark_delivery_pending(run)
        updated = mark_delivery_discarded(updated)
        assert updated.delivery.status == DeliveryStatus.DISCARDED
        assert is_delivery_terminal(updated)

    def test_full_lifecycle(self):
        run = _make_run()
        updated = mark_delivery_pending(run)
        set_run(updated)
        updated = get("r1")
        updated = mark_delivery_in_progress(updated)
        set_run(updated)
        updated = get("r1")
        updated = mark_delivery_delivered(updated)
        set_run(updated)
        assert is_delivery_delivered(get("r1"))
