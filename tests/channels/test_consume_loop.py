"""TDD tests for ``ChannelManager`` bus consumers (DESIGN_PATTERN #1, §3.3.2).

Regression target: the outbound queue must have EXACTLY ONE consumer,
``_dispatch_outbound``. The removed ``_outbound_consume_loop`` raced it on
``bus.consume_outbound()`` and silently dropped every message it won (its
consumer only registered the session, it never called ``channel.send``).

Also pins the routing contract shared by both directions: a message is handled
by the channel named in ``msg.channel`` — never broadcast to every configured
channel.
"""

import asyncio
import contextlib

import pytest
from pub.types.bus import InboundMessage, OutboundMessage

from channels.manager import ChannelManager

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _FakeBus:
    def __init__(self, inbound=None, outbound=None):
        self._inbound = list(inbound or [])
        self._outbound = list(outbound or [])

    async def consume_inbound(self):
        if not self._inbound:
            await asyncio.sleep(3600)
        return self._inbound.pop(0)

    async def consume_outbound(self):
        if not self._outbound:
            await asyncio.sleep(3600)
        return self._outbound.pop(0)


class _FakeChannel:
    def __init__(self, name):
        self.name = name
        self.sent: list[object] = []

    async def send(self, msg):
        self.sent.append(msg)


def _make_manager(bus, *, inbound_consumer=None, outbound_consumer=None, channels=("qq",)):
    manager = ChannelManager.__new__(ChannelManager)
    manager._bus = bus
    manager._channels = {name: _FakeChannel(name) for name in channels}
    manager._config = {name: {} for name in channels}
    manager._inbound_consumer = inbound_consumer
    manager._outbound_consumer = outbound_consumer
    return manager


def _inbound_msg(channel="qq"):
    return InboundMessage(channel=channel, chat_id="c1", content="hi", sender_id="u1")


def _outbound_msg(channel="qq"):
    return OutboundMessage(channel=channel, chat_id="c1", content="reply")


async def _run_until(task, predicate, *, timeout=2.0):
    try:
        for _ in range(int(timeout / 0.02)):
            await asyncio.sleep(0.02)
            if predicate():
                return
        raise AssertionError("predicate never became true")
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TestSingleConsumer:
    def test_outbound_has_no_second_consumer(self):
        """The racing second outbound consumer must not exist (regression #1).

        This is the deterministic red proof: before the fix both
        ``_outbound_consume_loop`` and ``_consume_loop`` existed and the
        lifecycle scheduled the former alongside ``_dispatch_outbound``.
        """
        assert not hasattr(ChannelManager, "_outbound_consume_loop")
        assert not hasattr(ChannelManager, "_consume_loop")


class TestInboundRouting:
    def test_inbound_routes_to_msg_channel_only(self):
        calls: list[tuple[object, object]] = []

        async def _consumer(msg, channel):
            calls.append((msg, channel))

        bus = _FakeBus(inbound=[_inbound_msg("qq")])
        manager = _make_manager(bus, inbound_consumer=_consumer, channels=("qq", "other"))

        async def _scenario():
            await _run_until(
                asyncio.create_task(manager._inbound_consume_loop()), lambda: len(calls) >= 1
            )

        asyncio.run(_scenario())

        assert len(calls) == 1
        msg, channel = calls[0]
        assert msg.content == "hi"
        assert channel.name == "qq"  # not broadcast to "other"

    def test_inbound_unknown_channel_is_dropped(self):
        calls: list[tuple[object, object]] = []

        async def _consumer(msg, channel):
            calls.append((msg, channel))

        bus = _FakeBus(inbound=[_inbound_msg("ghost")])
        manager = _make_manager(bus, inbound_consumer=_consumer, channels=("qq",))

        async def _scenario():
            await _run_until(
                asyncio.create_task(manager._inbound_consume_loop()),
                lambda: False,
                timeout=0.3,
            )

        with pytest.raises(AssertionError):
            asyncio.run(_scenario())
        assert calls == []

    def test_no_registered_inbound_consumer_means_no_calls(self):
        bus = _FakeBus(inbound=[_inbound_msg()])
        manager = _make_manager(bus)  # consumer left None

        async def _scenario():
            task = asyncio.create_task(manager._inbound_consume_loop())
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(_scenario())  # no crash; message consumed and dropped


class TestOutboundDispatch:
    def test_send_called_exactly_once_and_consumer_only_for_target(self):
        consumer_calls: list[tuple[object, object]] = []

        async def _consumer(msg, channel):
            consumer_calls.append((msg, channel))

        bus = _FakeBus(outbound=[_outbound_msg("qq")])
        manager = _make_manager(bus, outbound_consumer=_consumer, channels=("qq", "other"))
        qq = manager._channels["qq"]
        other = manager._channels["other"]

        async def _scenario():
            await _run_until(
                asyncio.create_task(manager._dispatch_outbound()),
                lambda: len(qq.sent) >= 1,
            )

        asyncio.run(_scenario())

        assert len(qq.sent) == 1
        assert qq.sent[0].content == "reply"
        assert other.sent == []
        assert len(consumer_calls) == 1
        assert consumer_calls[0][1].name == "qq"

    def test_consumer_exception_is_fail_open_and_delivery_still_happens(self):
        consumer_calls: list[str] = []

        async def _consumer(msg, channel):
            consumer_calls.append(channel.name)
            raise RuntimeError("consumer boom")

        bus = _FakeBus(outbound=[_outbound_msg("qq")])
        manager = _make_manager(bus, outbound_consumer=_consumer)
        qq = manager._channels["qq"]

        async def _scenario():
            await _run_until(
                asyncio.create_task(manager._dispatch_outbound()),
                lambda: len(qq.sent) >= 1,
            )

        asyncio.run(_scenario())

        assert consumer_calls == ["qq"]
        assert len(qq.sent) == 1  # delivery was not blocked by the failing consumer

    def test_unknown_channel_is_dropped_without_send(self):
        bus = _FakeBus(outbound=[_outbound_msg("ghost")])
        manager = _make_manager(bus)

        async def _scenario():
            await _run_until(
                asyncio.create_task(manager._dispatch_outbound()),
                lambda: False,
                timeout=0.3,
            )

        with pytest.raises(AssertionError):
            asyncio.run(_scenario())
        assert manager._channels["qq"].sent == []
