"""Unit tests for type/bus.py — InboundMessage and OutboundMessage dataclasses."""

import pytest
from datetime import datetime
from type.bus import InboundMessage, OutboundMessage


class TestInboundMessage:
    """Test InboundMessage dataclass."""

    def test_basic_construction(self):
        msg = InboundMessage(
            channel="telegram",
            sender_id="user123",
            chat_id="chat456",
            content="Hello!"
        )
        assert msg.channel == "telegram"
        assert msg.sender_id == "user123"
        assert msg.chat_id == "chat456"
        assert msg.content == "Hello!"

    def test_timestamp_auto_populated(self):
        msg = InboundMessage(
            channel="telegram",
            sender_id="u1",
            chat_id="c1",
            content="hi"
        )
        assert isinstance(msg.timestamp, datetime)

    def test_default_empty_media(self):
        msg = InboundMessage(
            channel="telegram",
            sender_id="u1",
            chat_id="c1",
            content="hi"
        )
        assert msg.media == []

    def test_default_empty_metadata(self):
        msg = InboundMessage(
            channel="telegram",
            sender_id="u1",
            chat_id="c1",
            content="hi"
        )
        assert msg.metadata == {}

    def test_session_id_none_by_default(self):
        msg = InboundMessage(
            channel="telegram",
            sender_id="u1",
            chat_id="c1",
            content="hi"
        )
        assert msg.session_id is None

    def test_unique_id_with_session_id(self):
        msg = InboundMessage(
            channel="telegram",
            sender_id="u1",
            chat_id="c1",
            content="hi",
            session_id="session-abc"
        )
        assert msg.unique_id == "session-abc"

    def test_unique_id_without_session_id(self):
        msg = InboundMessage(
            channel="telegram",
            sender_id="u1",
            chat_id="c1",
            content="hi"
        )
        assert msg.unique_id == "telegram:c1"

    def test_media_list(self):
        msg = InboundMessage(
            channel="telegram",
            sender_id="u1",
            chat_id="c1",
            content="hi",
            media=["http://example.com/img.png"]
        )
        assert len(msg.media) == 1

    def test_metadata_dict(self):
        msg = InboundMessage(
            channel="telegram",
            sender_id="u1",
            chat_id="c1",
            content="hi",
            metadata={"key": "value"}
        )
        assert msg.metadata["key"] == "value"


class TestOutboundMessage:
    """Test OutboundMessage dataclass."""

    def test_basic_construction(self):
        msg = OutboundMessage(
            channel="telegram",
            chat_id="chat456",
            content="Reply!"
        )
        assert msg.channel == "telegram"
        assert msg.chat_id == "chat456"
        assert msg.content == "Reply!"

    def test_reply_to_none_by_default(self):
        msg = OutboundMessage(
            channel="telegram",
            chat_id="c1",
            content="hi"
        )
        assert msg.reply_to is None

    def test_reply_to_set(self):
        msg = OutboundMessage(
            channel="telegram",
            chat_id="c1",
            content="hi",
            reply_to="msg-123"
        )
        assert msg.reply_to == "msg-123"

    def test_default_empty_media(self):
        msg = OutboundMessage(
            channel="telegram",
            chat_id="c1",
            content="hi"
        )
        assert msg.media == []

    def test_default_empty_metadata(self):
        msg = OutboundMessage(
            channel="telegram",
            chat_id="c1",
            content="hi"
        )
        assert msg.metadata == {}

    def test_media_list(self):
        msg = OutboundMessage(
            channel="telegram",
            chat_id="c1",
            content="hi",
            media=["http://example.com/img.png"]
        )
        assert len(msg.media) == 1
