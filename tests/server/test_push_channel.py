"""TDD tests for audit 2.1.4 — shared ``WSPushChannel`` (server/trigger/ws/push_channel.py).

Pins the push machinery logs.py and subagent_ws.py used to duplicate:
lock-guarded subscriber set, bounded per-websocket deque with drop-oldest,
sender task draining on the loop, and the handler skeleton
(register → ready frame → receive loop → unregister + sender cancel).
"""

import asyncio
import json
from collections import deque

import pytest

from server.trigger.ws.push_channel import WSPushChannel

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _FakeSocket:
    def __init__(self, *, receive_side_effect=None):
        self.id = f"ws-{id(self)}"
        self.sent: list[str] = []
        self._receive_side_effect = receive_side_effect or "wait"

    async def send_text(self, frame: str) -> None:
        self.sent.append(frame)

    async def receive_text(self) -> str:
        if self._receive_side_effect == "wait":
            await asyncio.sleep(10)
            return ""
        if isinstance(self._receive_side_effect, Exception):
            raise self._receive_side_effect
        return str(self._receive_side_effect)


def _run(coro):
    return asyncio.run(coro)


class TestEnqueueFrame:
    def test_no_subscribers_is_noop(self):
        channel = WSPushChannel()
        channel.enqueue_frame("frame-1")  # must not raise

    def test_frame_reaches_all_subscribers(self):
        channel = WSPushChannel()
        q1, q2 = deque(), deque()
        s1, s2 = object(), object()
        channel._subscribers.update({s1, s2})
        channel._pending = {s1: q1, s2: q2}

        channel.enqueue_frame("frame-1")
        channel.enqueue_frame("frame-2")

        assert list(q1) == ["frame-1", "frame-2"]
        assert list(q2) == ["frame-1", "frame-2"]

    def test_subscriber_without_queue_is_skipped(self):
        channel = WSPushChannel()
        s1 = object()
        channel._subscribers.add(s1)  # registered but deque not yet attached

        channel.enqueue_frame("frame-1")  # must not raise

        assert channel._pending.get(s1) is None

    def test_bounded_deque_drops_oldest(self):
        class _SmallChannel(WSPushChannel):
            max_pending = 3

        channel = _SmallChannel()
        queue = deque()
        s = object()
        channel._subscribers.add(s)
        channel._pending[s] = queue

        for i in range(5):
            channel.enqueue_frame(f"f{i}")

        assert list(queue) == ["f2", "f3", "f4"]


class TestSenderDrain:
    def test_sender_waits_for_frames_then_drains(self):
        async def scenario():
            socket = _FakeSocket()
            queue = deque()
            channel = WSPushChannel()
            task = asyncio.create_task(channel._sender(socket, queue))
            await asyncio.sleep(0.1)
            assert socket.sent == []  # idle while empty
            queue.append("x")
            await asyncio.sleep(0.1)
            assert socket.sent == ["x"]
            task.cancel()

        _run(scenario())

    def test_sender_returns_quietly_on_send_failure(self):
        async def scenario():
            class _DyingSocket(_FakeSocket):
                async def send_text(self, frame):
                    raise ConnectionResetError("gone")

            channel = WSPushChannel()
            queue = deque(["a", "b"])
            task = asyncio.create_task(channel._sender(_DyingSocket(), queue))
            await asyncio.sleep(0.2)
            assert task.done()  # returned instead of raising/hanging
            assert task.exception() is None

        _run(scenario())


class TestServeSkeleton:
    def test_serve_registers_sends_ready_and_unregisters_on_disconnect(self):
        async def scenario():
            channel = WSPushChannel()
            socket = _FakeSocket(receive_side_effect=ConnectionResetError("client gone"))
            registered: list[bool] = []

            def _ensure():
                registered.append(True)

            await channel.serve(
                socket,
                ensure_registered=_ensure,
                handler_label="Logs WebSocket",
                client_label="Logs WS",
            )

            assert registered == [True]
            assert socket.sent, "ready frame must be sent"
            assert json.loads(socket.sent[0]) == {"event": "ready"}
            # Unregistered after disconnect.
            assert len(channel._subscribers) == 0
            assert channel._pending == {}

    def test_registration_hook_runs_before_ready_frame(self):
        async def scenario():
            channel = WSPushChannel()
            socket = _FakeSocket(receive_side_effect=RuntimeError("end"))
            order: list[str] = []

            original_send = socket.send_text

            async def _send(text):
                if not order or order[-1] != "ready":
                    order.append("ready")
                await original_send(text)

            socket.send_text = _send

            def _ensure():
                order.append("register")

            await channel.serve(
                socket, ensure_registered=_ensure, handler_label="X", client_label="X"
            )

            assert order == ["register", "ready"]

        _run(scenario())
