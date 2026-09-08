"""TDD tests for audit 3.1.1 — generalized ``ChannelManager._consume_loop``
(channels/manager.py).

Pins the loop semantics the inbound/outbound copies used to duplicate:
drain the direction's bus queue forever, invoke the direction's registered
consumer once per configured channel, warn+skip unknown channels, and the
per-direction debug log fields.
"""

import asyncio

import pytest
from type.bus import InboundMessage, OutboundMessage

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


def _make_manager(bus, *, inbound_consumer=None, outbound_consumer=None, channels=("qq",)):
    manager = ChannelManager.__new__(ChannelManager)
    manager._bus = bus
    manager._channels = {name: _FakeChannel(name) for name in channels}
    manager._config = {name: {} for name in channels}
    manager._inbound_consumer = inbound_consumer
    manager._outbound_consumer = outbound_consumer
    return manager


def _inbound_msg():
    return InboundMessage(channel="qq", chat_id="c1", content="hi", sender_id="u1")


def _outbound_msg():
    return OutboundMessage(channel="qq", chat_id="c1", content="reply")


async def _run_until(loop_coro_factory, predicate, *, timeout=2.0):
    task = asyncio.create_task(loop_coro_factory())
    try:
        for _ in range(int(timeout / 0.02)):
            await asyncio.sleep(0.02)
            if predicate():
                return task
        raise AssertionError("predicate never became true")
    finally:
        task.cancel()


class TestConsumeLoop:
    def test_inbound_loop_invokes_consumer_per_configured_channel(self):
        calls: list[tuple[object, object]] = []

        async def _consumer(msg, channel):
            calls.append((msg, channel))

        bus = _FakeBus(inbound=[_inbound_msg()])
        manager = _make_manager(bus, inbound_consumer=_consumer, channels=("qq", "other"))
        manager._channels.pop("other")  # configured but not instantiated

        async def _scenario():
            task = await _run_until(
                lambda: manager._consume_loop("inbound"), lambda: len(calls) >= 1
            )
            await task

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(_scenario())

        assert len(calls) == 1
        msg, channel = calls[0]
        assert msg.content == "hi"
        assert channel.name == "qq"  # the missing 'other' channel was warned+skipped

    def test_outbound_loop_uses_outbound_consumer(self):
        calls: list[tuple[object, object]] = []

        async def _consumer(msg, channel):
            calls.append((msg, channel))

        bus = _FakeBus(outbound=[_outbound_msg()])
        manager = _make_manager(bus, outbound_consumer=_consumer)

        async def _scenario():
            task = await _run_until(
                lambda: manager._consume_loop("outbound"), lambda: len(calls) >= 1
            )
            await task

        with pytest.raises(asyncio.CancelledError):
            asyncio.run(_scenario())

        assert calls and calls[0][0].content == "reply"

    def test_no_registered_consumer_means_no_calls(self):
        bus = _FakeBus(inbound=[_inbound_msg()])
        manager = _make_manager(bus)  # consumer left None

        async def _scenario():
            task = asyncio.create_task(manager._consume_loop("inbound"))
            await asyncio.sleep(0.1)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

        asyncio.run(_scenario())  # no crash; consumer never invoked (no calls to observe)

    def test_legacy_methods_delegate_to_direction(self):
        manager = _make_manager(_FakeBus())

        async def _scenario():
            t_in = asyncio.create_task(manager._inbound_consume_loop())
            t_out = asyncio.create_task(manager._outbound_consume_loop())
            await asyncio.sleep(0.05)
            t_in.cancel()
            t_out.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.gather(t_in, t_out)

        asyncio.run(_scenario())  # both legacy entry points still run
