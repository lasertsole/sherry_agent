"""Channel identifiers are pseudonymised at the log boundary.

The threat is a log file that doubles as a record of who talked to the bot: a
chat platform's ``chat_id``/``user_id`` is an account identifier, and the
delivery lines are exactly where it used to be printed.

Two properties matter together, so both are tested:

* the pseudonym is **stable** — the same identifier yields the same string in a
  later process, which is what keeps "received" correlatable with "sent
  successfully" once the raw value is gone (the cross-process case runs in a
  subprocess, because an in-process repeat would pass with a salted or
  per-run value too);
* the raw identifier does **not** appear in what the logger emits — asserted on
  real log records from the real channel code, not on the helper.
"""

from __future__ import annotations

import re
import subprocess
import sys

import pytest
from loguru import logger

from agent.security.pii import hash_identifier, pseudonym
from pub.types.bus import OutboundMessage

pytestmark = [pytest.mark.unit]

_QQ_GROUP_ID = "A1B2C3D4E5F60718293A4B5C6D7E8F90"


@pytest.fixture
def captured_records(monkeypatch):
    """Capture loguru records the way the production pipeline formats them.

    The log-redaction patcher is installed here because it is part of what keeps
    the identifier out of the record: it rewrites the exception, whose traceback
    loguru would otherwise render itself — including its ``diagnose`` dump of the
    frame values — straight into the log. ``monkeypatch`` restores the previous
    hook afterwards, so the global logger is left as it was found.
    """
    from agent.security.redact_formatter import redact_record

    messages: list[str] = []
    sink_id = logger.add(lambda message: messages.append(message), level="DEBUG")
    monkeypatch.setattr(logger._core, "patcher", redact_record)
    try:
        yield messages
    finally:
        logger.remove(sink_id)


# ---------------------------------------------------------------------------
# The pseudonym itself
# ---------------------------------------------------------------------------


def test_pseudonym_shape():
    result = pseudonym("chat-1")

    assert re.fullmatch(r"«pii:[0-9a-f]{12}»", result), result


def test_pseudonym_is_stable_across_calls():
    assert pseudonym(_QQ_GROUP_ID) == pseudonym(_QQ_GROUP_ID)


def test_pseudonym_is_stable_across_processes():
    """The correlation property: a later process must derive the same pseudonym."""
    script = f"from agent.security.pii import pseudonym; print(pseudonym({_QQ_GROUP_ID!r}))"
    out = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=True,
        cwd=None,
    )

    assert out.stdout.strip() == pseudonym(_QQ_GROUP_ID)


def test_distinct_identifiers_do_not_collide():
    values = [_QQ_GROUP_ID, _QQ_GROUP_ID.lower(), "12345", "12346", "chat-1", "chat-2"]
    pseudonyms = [pseudonym(v) for v in values]

    assert len(set(pseudonyms)) == len(values)


def test_the_pseudonym_carries_only_a_truncated_digest():
    result = pseudonym(_QQ_GROUP_ID)

    assert hash_identifier(_QQ_GROUP_ID) in result
    assert len(re.sub(r"[^0-9a-f]", "", result)) == 12


def test_absent_identifiers_stay_absent():
    """An empty id must not turn into a hash of nothing and hide that it was missing."""
    assert pseudonym(None) == ""
    assert pseudonym("") == ""


def test_non_string_identifiers_are_handled():
    """Telegram-style numeric ids arrive as ints."""
    assert pseudonym(12345) == pseudonym("12345")


# ---------------------------------------------------------------------------
# The boundary: real channel code, real log records
# ---------------------------------------------------------------------------


class _StubClient:
    """Minimal botpy client stand-in: records the outgoing payload."""

    def __init__(self, fail: bool = False):
        self.fail = fail
        self.calls: list[dict] = []

        class _Api:
            def __init__(self, outer):
                self._outer = outer

            async def post_c2c_message(self, **kwargs):
                self._outer.calls.append({"c2c": kwargs})
                if self._outer.fail:
                    raise RuntimeError("platform refused")

            async def post_group_message(self, **kwargs):
                self._outer.calls.append({"group": kwargs})
                if self._outer.fail:
                    raise RuntimeError("platform refused")

        self.api = _Api(self)


def _qq_channel(client: _StubClient):
    from plugins.channels.qq.core import QQChannel

    channel = QQChannel({"enabled": True, "app_id": "x", "secret": "y"}, bus=None)
    channel._client = client
    return channel


def test_the_send_path_logs_a_pseudonym_not_the_chat_id(captured_records):
    client = _StubClient()
    channel = _qq_channel(client)

    import asyncio

    asyncio.run(channel.send(OutboundMessage(channel="qq", chat_id=_QQ_GROUP_ID, content="hi")))

    logged = "\n".join(captured_records)
    assert _QQ_GROUP_ID not in logged
    assert pseudonym(_QQ_GROUP_ID) in logged
    # The functional path is untouched: the platform still receives the real id.
    assert client.calls[0]["c2c"]["openid"] == _QQ_GROUP_ID


def test_the_send_failure_path_logs_a_pseudonym_too(captured_records):
    channel = _qq_channel(_StubClient(fail=True))

    import asyncio

    asyncio.run(channel.send(OutboundMessage(channel="qq", chat_id=_QQ_GROUP_ID, content="hi")))

    logged = "\n".join(captured_records)
    assert _QQ_GROUP_ID not in logged
    assert pseudonym(_QQ_GROUP_ID) in logged


def test_the_channel_core_failure_line_logs_a_pseudonym(captured_records, monkeypatch):
    """``_send_reply``'s except branch: the frame could not be delivered."""
    import asyncio

    from server.trigger.channels import core as channels_core

    class _FailingChannel:
        name = "qq"

        async def send(self, msg):
            raise RuntimeError("platform refused")

    monkeypatch.setattr(
        channels_core.channel_manager, "get_channel", lambda name: _FailingChannel()
    )

    asyncio.run(channels_core._send_reply({"channel": "qq", "chat_id": _QQ_GROUP_ID}, "content"))

    logged = "\n".join(captured_records)
    assert _QQ_GROUP_ID not in logged
    assert pseudonym(_QQ_GROUP_ID) in logged
