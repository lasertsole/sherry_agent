"""Shared bounded-deque WS push channel (audit 2.1.4).

``server/trigger/ws/logs.py`` and ``server/trigger/ws/subagent_ws.py``
previously carried the same push machinery: a lock-guarded subscriber set, a
per-websocket bounded deque of pre-serialized frames (drop-oldest at 2000), a
per-connection sender task draining the deque on the event loop, and the same
handler skeleton (register → sender task → ``ready`` frame → receive loop →
unregister + sender cancel).

Send mechanism (why the deque indirection exists): producers fire on
non-event-loop threads (loguru's enqueue=True sink thread; sub-agent hook
contexts), so they must never ``await`` a send. Producers only append to the
deque under a lock; the sender task performs the actual
``await websocket.send_text`` on the loop thread.

Subclasses/endpoints choose what to broadcast (frame serialization stays at
the call site) and provide their idempotent registration hook (loguru sink /
sub-agent lifecycle hooks) plus their log wording.
"""

import asyncio
import json
import threading
from collections import deque
from typing import Any, Callable

from loguru import logger
from robyn import WebSocketDisconnect


class WSPushChannel:
    max_pending: int = 2000

    def __init__(self) -> None:
        # Shared set of connected websockets + the lock guarding it
        # (producers and the handler run on different threads).
        self._subscribers: set[Any] = set()
        self._subscribers_lock = threading.Lock()
        # Per-websocket bounded deque of serialized JSON frames.
        self._pending: dict[Any, deque[str]] = {}

    def enqueue_frame(self, frame: str) -> None:
        """Enqueue a pre-serialized frame to every connected websocket.

        Safe to call from any thread/async context: only touches the guarded
        subscriber set and the per-websocket deque; never awaits asyncio.
        """
        with self._subscribers_lock:
            for ws in list(self._subscribers):
                queue = self._pending.get(ws)
                if queue is None:
                    continue
                # Bounded queue: drop the oldest frame when full so a slow
                # consumer cannot cause unbounded memory growth.
                if len(queue) >= self.max_pending:
                    queue.popleft()
                queue.append(frame)

    async def _sender(self, websocket: Any, queue: deque[str]) -> None:
        """Drain the per-websocket deque and send frames on the event loop."""
        while True:
            # Wait for at least one frame to be available.
            while not queue:
                await asyncio.sleep(0.05)
            while queue:
                frame = queue.popleft()
                try:
                    await websocket.send_text(frame)
                except Exception:
                    return

    async def serve(
        self,
        websocket: Any,
        *,
        ensure_registered: Callable[[], None],
        handler_label: str,
        client_label: str,
    ) -> None:
        """Handler skeleton: subscribe, start the sender, serve until disconnect.

        ``ensure_registered`` is the endpoint's idempotent registration hook
        (called once per connection, after the started log line).
        """
        logger.info(f"{handler_label} handler started: websocket_id={websocket.id}")
        ensure_registered()

        queue: deque[str] = deque()
        with self._subscribers_lock:
            self._subscribers.add(websocket)
            self._pending[websocket] = queue

        sender_task = asyncio.create_task(self._sender(websocket, queue))

        try:
            # Welcome frame.
            await websocket.send_text(json.dumps({"event": "ready"}, ensure_ascii=False))

            # Receive loop: keeps the connection alive and detects disconnect.
            while True:
                try:
                    await websocket.receive_text()
                except Exception:
                    break
        except (WebSocketDisconnect, ConnectionResetError, Exception) as e:
            logger.warning(f"{client_label} client {websocket.id} disconnected: {e}")
        finally:
            sender_task.cancel()
            with self._subscribers_lock:
                self._subscribers.discard(websocket)
                self._pending.pop(websocket, None)
            logger.info(f"{client_label} client {websocket.id} unregistered")
