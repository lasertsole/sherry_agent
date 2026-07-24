import pytest
import time
from unittest.mock import AsyncMock, MagicMock, patch
from future_subagent.announce.delivery import (
    deliver_subagent_announcement,
    _is_already_delivered,
    _mark_delivered,
    _check_delivery_mirror,
    _build_delivery_context,
    _deliver_internal_injection,
    _deliver_completion_message,
    _delivered_keys,
    _delivery_mirror,
)
from future_subagent.announce.idempotency import build_idempotency_key
from future_subagent.types.registry import (
    SubagentRunRecord,
    ExecutionState,
    ExecutionStatus,
    CompletionState,
    CompletionDeliveryState,
    DeliveryStatus,
    RunOutcome,
    RunOutcomeStatus,
)
from future_subagent.types.delivery import DeliveryContext


@pytest.fixture(autouse=True)
def _clean():
    _delivered_keys.clear()
    _delivery_mirror.clear()
    from future_subagent.registry.memory import clear
    clear()
    yield
    _delivered_keys.clear()
    _delivery_mirror.clear()
    clear()


def _make_run(**overrides) -> SubagentRunRecord:
    defaults = dict(
        run_id="r1",
        child_session_key="agent:main:future_subagent:abc",
        requester_session_key="agent:main:session:p1",
        task="test task",
        label="worker-1",
        execution=ExecutionState(
            status=ExecutionStatus.TERMINAL,
            outcome=RunOutcome(status=RunOutcomeStatus.OK),
        ),
        completion=CompletionState(result_text="All done"),
        delivery=CompletionDeliveryState(status=DeliveryStatus.PENDING),
    )
    defaults.update(overrides)
    return SubagentRunRecord(**defaults)


class TestIsAlreadyDelivered:
    def test_not_delivered(self):
        run = _make_run()
        assert _is_already_delivered(run) is False

    def test_delivered(self):
        run = _make_run()
        _mark_delivered(run)
        assert _is_already_delivered(run) is True


class TestMarkDelivered:
    def test_marks_key(self):
        run = _make_run()
        _mark_delivered(run)
        key = build_idempotency_key(run.run_id, run.generation)
        assert key in _delivered_keys


class TestCheckDeliveryMirror:
    def test_first_delivery_not_mirror(self):
        run = _make_run()
        assert _check_delivery_mirror(run) is False

    def test_duplicate_detected(self):
        run = _make_run()
        _check_delivery_mirror(run)
        assert _check_delivery_mirror(run) is True

    def test_different_run_not_mirror(self):
        run1 = _make_run(run_id="r1")
        run2 = _make_run(run_id="r2", child_session_key="agent:main:future_subagent:different")
        _check_delivery_mirror(run1)
        assert _check_delivery_mirror(run2) is False


class TestBuildDeliveryContext:
    def test_subagent_to_user(self):
        run = _make_run()
        ctx = _build_delivery_context(run)
        assert ctx.requester_session_key == "agent:main:session:p1"
        assert ctx.child_session_key == "agent:main:future_subagent:abc"
        assert ctx.task == "test task"
        assert ctx.result_text == "All done"
        assert ctx.run_id == "r1"

    def test_subagent_to_subagent(self):
        run = _make_run()
        with patch("future_subagent.announce.origin.resolve_announce_origin") as mock_origin:
            mock_origin.return_value = MagicMock(is_requester_subagent=True)
            ctx = _build_delivery_context(run)
            assert ctx.is_requester_subagent is True


class TestDeliverInternalInjection:
    @pytest.mark.asyncio
    async def test_internal_injection_format(self):
        ctx = DeliveryContext(
            requester_session_key="agent:main:future_subagent:parent",
            child_session_key="agent:main:future_subagent:child",
            task="test",
            result_text="summary",
            outcome=RunOutcome(status=RunOutcomeStatus.OK),
            run_id="r1",
            is_requester_subagent=True,
        )
        mock_bus_instance = AsyncMock()
        import bus
        old = getattr(bus, "MessageBus", None)
        bus.MessageBus = MagicMock(return_value=mock_bus_instance)
        try:
            await _deliver_internal_injection(ctx)
            mock_bus_instance.publish_inbound.assert_called_once()
            msg = mock_bus_instance.publish_inbound.call_args[0][0]
            assert msg.metadata["internal"] is True
            assert "Subagent Internal" in msg.content
        finally:
            if old is not None:
                bus.MessageBus = old
            else:
                delattr(bus, "MessageBus")


class TestDeliverCompletionMessage:
    @pytest.mark.asyncio
    async def test_completion_message_format(self):
        ctx = DeliveryContext(
            requester_session_key="agent:main:session:user1",
            child_session_key="agent:main:future_subagent:child",
            child_label="worker-1",
            task="test",
            result_text="All done",
            outcome=RunOutcome(status=RunOutcomeStatus.OK),
            run_id="r1",
            is_requester_subagent=False,
        )
        mock_bus_instance = AsyncMock()
        import bus
        old = getattr(bus, "MessageBus", None)
        bus.MessageBus = MagicMock(return_value=mock_bus_instance)
        try:
            await _deliver_completion_message(ctx)
            msg = mock_bus_instance.publish_inbound.call_args[0][0]
            assert "Subagent Task" in msg.content
            assert "review" in msg.content
            assert "internal" not in msg.metadata
        finally:
            if old is not None:
                bus.MessageBus = old
            else:
                delattr(bus, "MessageBus")

    @pytest.mark.asyncio
    async def test_completion_message_killed(self):
        ctx = DeliveryContext(
            requester_session_key="agent:main:session:user1",
            child_session_key="agent:main:future_subagent:child",
            task="test",
            outcome=RunOutcome(status=RunOutcomeStatus.KILLED),
            run_id="r1",
            is_requester_subagent=False,
        )
        mock_bus_instance = AsyncMock()
        import bus
        old = getattr(bus, "MessageBus", None)
        bus.MessageBus = MagicMock(return_value=mock_bus_instance)
        try:
            await _deliver_completion_message(ctx)
            msg = mock_bus_instance.publish_inbound.call_args[0][0]
            assert "killed" in msg.content
        finally:
            if old is not None:
                bus.MessageBus = old
            else:
                delattr(bus, "MessageBus")

    @pytest.mark.asyncio
    async def test_completion_message_error(self):
        ctx = DeliveryContext(
            requester_session_key="agent:main:session:user1",
            child_session_key="agent:main:future_subagent:child",
            task="test",
            outcome=RunOutcome(status=RunOutcomeStatus.ERROR, error="crash"),
            run_id="r1",
            is_requester_subagent=False,
        )
        mock_bus_instance = AsyncMock()
        import bus
        old = getattr(bus, "MessageBus", None)
        bus.MessageBus = MagicMock(return_value=mock_bus_instance)
        try:
            await _deliver_completion_message(ctx)
            msg = mock_bus_instance.publish_inbound.call_args[0][0]
            assert "crash" in msg.content
        finally:
            if old is not None:
                bus.MessageBus = old
            else:
                delattr(bus, "MessageBus")


class TestDeliverSubagentAnnouncement:
    @pytest.mark.asyncio
    async def test_skips_already_delivered(self):
        run = _make_run()
        _mark_delivered(run)
        result = await deliver_subagent_announcement(run)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_skips_mirror_hit(self):
        run = _make_run()
        _check_delivery_mirror(run)
        result = await deliver_subagent_announcement(run)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_skips_non_required(self):
        run = _make_run(
            completion=CompletionState(required=False),
            delivery=CompletionDeliveryState(status=DeliveryStatus.NOT_REQUIRED),
        )
        from future_subagent.registry.memory import set_run
        set_run(run)
        result = await deliver_subagent_announcement(run)
        assert result.success is True
