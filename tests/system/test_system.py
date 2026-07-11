"""System tests for agent lifecycle and message bus E2E."""

import asyncio
import pytest
from unittest.mock import MagicMock, patch, AsyncMock
from type.bus import InboundMessage, OutboundMessage
from bus.core import MessageBus


@pytest.mark.asyncio
class TestMessageBusE2E:
    """End-to-end message bus flow: channel → agent → channel."""

    @pytest.fixture
    def bus(self):
        return MessageBus()

    async def test_full_inbound_outbound_flow(self, bus):
        """Simulate: channel publishes inbound → agent consumes → publishes outbound → channel consumes."""
        # Channel publishes a user message
        user_msg = InboundMessage(
            channel="telegram",
            sender_id="user123",
            chat_id="chat456",
            content="What's the weather?",
            metadata={"message_id": "12345"},
        )
        await bus.publish_inbound(user_msg)

        # Agent consumes the inbound message
        consumed = await bus.consume_inbound()
        assert consumed.content == "What's the weather?"
        assert consumed.channel == "telegram"
        assert consumed.sender_id == "user123"

        # Agent processes and publishes a response
        response = OutboundMessage(
            channel="telegram",
            chat_id="chat456",
            content="It's 25°C and sunny!",
            reply_to="12345",
        )
        await bus.publish_outbound(response)

        # Channel consumes the outbound response
        out = await bus.consume_outbound()
        assert out.content == "It's 25°C and sunny!"
        assert out.reply_to == "12345"

    async def test_multi_channel_interleaving(self, bus):
        """Multiple channels publish messages — each gets its own response."""
        telegram_msg = InboundMessage(
            channel="telegram", sender_id="u1", chat_id="c1", content="Hello from TG"
        )
        discord_msg = InboundMessage(
            channel="discord", sender_id="u2", chat_id="c2", content="Hello from DC"
        )

        await bus.publish_inbound(telegram_msg)
        await bus.publish_inbound(discord_msg)

        # Agent processes in order
        first = await bus.consume_inbound()
        second = await bus.consume_inbound()
        assert first.channel == "telegram"
        assert second.channel == "discord"

        # Responses go to respective channels
        await bus.publish_outbound(
            OutboundMessage(channel="telegram", chat_id="c1", content="TG reply")
        )
        await bus.publish_outbound(
            OutboundMessage(channel="discord", chat_id="c2", content="DC reply")
        )

        out1 = await bus.consume_outbound()
        out2 = await bus.consume_outbound()
        assert out1.content == "TG reply"
        assert out2.content == "DC reply"

    async def test_subagent_publish_flow(self, bus):
        """Simulate subagent publishing result via inbound bus."""
        subagent_result = InboundMessage(
            channel="system",
            sender_id="subagent",
            chat_id="direct",
            content="Task completed: story written",
            session_id="main",
            metadata={
                "injected_event": "subagent_result",
                "subagent_task_id": "12345",
            },
        )
        await bus.publish_inbound(subagent_result)

        consumed = await bus.consume_inbound()
        assert consumed.sender_id == "subagent"
        assert consumed.metadata["injected_event"] == "subagent_result"
        assert consumed.session_id == "main"

    async def test_concurrent_publish_consume(self, bus):
        """Multiple concurrent producers and consumers."""
        received = []

        async def producer(n):
            await bus.publish_inbound(
                InboundMessage(
                    channel="test", sender_id="u", chat_id="c", content=f"msg-{n}"
                )
            )

        async def consumer(count):
            for _ in range(count):
                msg = await bus.consume_inbound()
                received.append(msg.content)

        # Launch 5 producers and 1 consumer
        producers = [asyncio.create_task(producer(i)) for i in range(5)]
        consumer_task = asyncio.create_task(consumer(5))

        await asyncio.gather(*producers)
        await asyncio.wait_for(consumer_task, timeout=2.0)

        assert len(received) == 5
        assert set(received) == {f"msg-{i}" for i in range(5)}


class TestRegisterIntegration:
    """Integration test: register lifecycle across multiple components."""

    def test_clear_all_registers(self):
        """Verify clear_all_register_sessions cascades to all register types."""
        from runtime.core import Register
        from runtime.state_register import StateRegisterMeM
        from runtime.count_call_register import CountCallRegister

        # Ensure singletons exist
        sm = StateRegisterMeM()
        cc = CountCallRegister()

        # Set some state
        sm.set_state("test-session", "key", "value")
        cc.register("test-session", "counter", lambda: None)

        # Clear
        from runtime import clear_all_register_sessions
        clear_all_register_sessions("test-session")

        # Verify state register is cleared
        assert sm.get_state("test-session", "key") is None

    def test_multi_session_isolation(self):
        """Verify sessions are properly isolated."""
        from runtime.state_register import StateRegisterMeM
        from runtime.core import Register

        if StateRegisterMeM in Register._instances:
            del Register._instances[StateRegisterMeM]
        sm = StateRegisterMeM()

        sm.set_state("session-1", "data", "for-1")
        sm.set_state("session-2", "data", "for-2")

        assert sm.get_state("session-1", "data") == "for-1"
        assert sm.get_state("session-2", "data") == "for-2"

        sm.clear_session("session-1")
        assert sm.get_state("session-1", "data") is None
        assert sm.get_state("session-2", "data") == "for-2"


class TestConfigIntegration:
    """Integration test: config loading and access."""

    def test_config_construction(self):
        """Verify Config can be constructed with defaults."""
        from config.schema import Config
        c = Config()
        assert c.agents.defaults.model is not None
        assert c.gateway.port > 0

    def test_nested_config_access(self):
        """Verify deep nested config access."""
        from config.schema import Config
        c = Config()
        # Access deeply nested values
        assert isinstance(c.tools.web.search.provider, str)
        assert isinstance(c.tools.exec.timeout, int)
        assert isinstance(c.gateway.heartbeat.interval_s, int)
