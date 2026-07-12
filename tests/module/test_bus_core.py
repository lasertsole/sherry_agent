"""Module tests for bus/TODOManager.py — async MessageBus."""

import pytest
import asyncio
from type.bus import InboundMessage, OutboundMessage
from bus.core import MessageBus


@pytest.mark.asyncio
class TestMessageBus:
    """Test the async MessageBus pub/sub pattern."""

    @pytest.fixture
    def bus(self):
        return MessageBus()

    # --- Inbound ---

    async def test_publish_consume_inbound(self, bus):
        msg = InboundMessage(
            channel="telegram",
            sender_id="u1",
            chat_id="c1",
            content="hello",
        )
        await bus.publish_inbound(msg)
        consumed = await bus.consume_inbound()
        assert consumed is msg
        assert consumed.content == "hello"

    async def test_inbound_fifo_order(self, bus):
        m1 = InboundMessage(channel="t", sender_id="u", chat_id="c", content="first")
        m2 = InboundMessage(channel="t", sender_id="u", chat_id="c", content="second")
        await bus.publish_inbound(m1)
        await bus.publish_inbound(m2)
        assert (await bus.consume_inbound()).content == "first"
        assert (await bus.consume_inbound()).content == "second"

    async def test_inbound_size(self, bus):
        assert bus.inbound_size == 0
        await bus.publish_inbound(
            InboundMessage(channel="t", sender_id="u", chat_id="c", content="x")
        )
        assert bus.inbound_size == 1
        await bus.consume_inbound()
        assert bus.inbound_size == 0

    # --- Outbound ---

    async def test_publish_consume_outbound(self, bus):
        msg = OutboundMessage(
            channel="discord",
            chat_id="c1",
            content="reply",
        )
        await bus.publish_outbound(msg)
        consumed = await bus.consume_outbound()
        assert consumed is msg
        assert consumed.content == "reply"

    async def test_outbound_fifo_order(self, bus):
        m1 = OutboundMessage(channel="d", chat_id="c", content="a")
        m2 = OutboundMessage(channel="d", chat_id="c", content="b")
        await bus.publish_outbound(m1)
        await bus.publish_outbound(m2)
        assert (await bus.consume_outbound()).content == "a"
        assert (await bus.consume_outbound()).content == "b"

    async def test_outbound_size(self, bus):
        assert bus.outbound_size == 0
        await bus.publish_outbound(
            OutboundMessage(channel="d", chat_id="c", content="x")
        )
        assert bus.outbound_size == 1
        await bus.consume_outbound()
        assert bus.outbound_size == 0

    # --- Isolation ---

    async def test_independent_queues(self, bus):
        """Inbound and outbound are independent."""
        in_msg = InboundMessage(channel="t", sender_id="u", chat_id="c", content="in")
        out_msg = OutboundMessage(channel="d", chat_id="c", content="out")
        await bus.publish_inbound(in_msg)
        await bus.publish_outbound(out_msg)

        consumed_in = await bus.consume_inbound()
        consumed_out = await bus.consume_outbound()
        assert consumed_in.content == "in"
        assert consumed_out.content == "out"

    async def test_consume_blocks_until_published(self, bus):
        """consume_inbound should wait until a message is published."""
        results = []

        async def consumer():
            msg = await bus.consume_inbound()
            results.append(msg.content)

        # Start consumer in background
        task = asyncio.create_task(consumer())
        await asyncio.sleep(0.05)  # Let consumer start waiting
        assert results == []  # Nothing consumed yet

        await bus.publish_inbound(
            InboundMessage(channel="t", sender_id="u", chat_id="c", content="arrived")
        )
        await asyncio.wait_for(task, timeout=1.0)
        assert results == ["arrived"]
