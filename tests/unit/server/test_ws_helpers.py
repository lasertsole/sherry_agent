"""TDD tests for audit 2.1.2 — shared ``send_ws_json`` (server/utils/ws_helpers.py).

Pins the exact contract the three former private ``_send_ws`` copies had:
- ``None`` socket → no-op (frames are skippable);
- payload serialized via ``json.dumps`` and awaited on ``websocket.send_text``;
- send failures swallowed and logged with the site's own warning prefix;
- the auto-turn path's ``ensure_ascii=False`` serialization.
"""

import asyncio
import json

import pytest
from loguru import logger

from server.utils.ws_helpers import send_ws_json

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _RecordingSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.fail_with: Exception | None = None

    async def send_text(self, text: str) -> None:
        if self.fail_with is not None:
            raise self.fail_with
        self.sent.append(text)


class _LogSink:
    def __init__(self) -> None:
        self.messages: list[str] = []
        self._handler_id = None

    def __enter__(self):
        self._handler_id = logger.add(self._record, level="DEBUG")
        return self

    def __exit__(self, *exc):
        logger.remove(self._handler_id)

    def _record(self, message):
        self.messages.append(str(message))


def _run(coro):
    return asyncio.run(coro)


class TestSendWsJson:
    def test_sends_json_serialized_payload(self):
        socket = _RecordingSocket()
        payload = {"event": "chunk", "session_id": "s1", "content": "hello"}

        _run(send_ws_json(socket, payload))

        assert socket.sent == [json.dumps(payload)]

    def test_none_socket_is_noop(self):
        _run(send_ws_json(None, {"event": "chunk"}))  # must not raise

    def test_send_failure_swallowed_and_logged_with_prefix(self):
        socket = _RecordingSocket()
        socket.fail_with = RuntimeError("socket closed")

        with _LogSink() as sink:
            _run(send_ws_json(socket, {"event": "chunk"}, warn_prefix="Agent WS send failed"))

        assert socket.sent == []
        assert any("Agent WS send failed: socket closed" in m for m in sink.messages)

    def test_ensure_ascii_false_keeps_cjk_unescaped(self):
        socket = _RecordingSocket()
        payload = {"content": "小兰"}

        _run(send_ws_json(socket, payload, ensure_ascii=False))

        assert socket.sent == [json.dumps(payload, ensure_ascii=False)]
        assert "小兰" in socket.sent[0]

    def test_ensure_ascii_default_escapes_cjk(self):
        socket = _RecordingSocket()
        payload = {"content": "小兰"}

        _run(send_ws_json(socket, payload))

        assert "\\u" in socket.sent[0]

    def test_site_prefixes_reproduce_original_log_wording(self):
        """The three wired sites keep their original warning wording."""
        expected = {
            "Agent WS send failed",
            "TurnRunner: ws send failed",
            "auto_turn: websocket send failed",
        }
        for prefix in expected:
            socket = _RecordingSocket()
            socket.fail_with = OSError("boom")
            with _LogSink() as sink:
                _run(send_ws_json(socket, {"e": 1}, warn_prefix=prefix))
            assert any(f"{prefix}: boom" in m for m in sink.messages), prefix
