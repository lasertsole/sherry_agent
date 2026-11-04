"""Shared bounded-deque WS push channel.

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
import contextlib
import inspect
import json
import threading
from collections import deque
from typing import Any
from collections.abc import Callable

from loguru import logger

from server.trigger import auth
from server.trigger import auth_user


class WSPushChannel:
    max_pending: int = 2000

    def __init__(self) -> None:
        # Connected websockets → their optional ROUTING KEY (None = a global
        # channel that receives every frame). The lock guards both maps
        # (producers and the handler run on different threads).
        self._subscribers: dict[Any, str | None] = {}
        self._subscribers_lock = threading.Lock()
        # Per-websocket bounded deque of serialized JSON frames.
        self._pending: dict[Any, deque[str]] = {}

    def enqueue_frame(self, frame: str, key: str | None = None) -> None:
        """Enqueue a pre-serialized frame to the matching websockets.

        ``key=None`` (every existing channel) broadcasts to ALL subscribers; a
        key delivers only to the sockets that subscribed under it (the
        trajectory channel routes by session id). Safe to call from any
        thread/async context: only touches the guarded maps and the deques;
        never awaits asyncio.
        """
        with self._subscribers_lock:
            for ws, ws_key in list(self._subscribers.items()):
                if key is not None and ws_key != key:
                    continue
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
                except Exception as e:
                    # The socket is gone: the serve() loop's finally unregisters
                    # it. Logged so a silently dead push channel is visible.
                    logger.debug(
                        "push sender stopped: websocket_id={} error={}",
                        getattr(websocket, "id", "?"),
                        e,
                    )
                    return

    async def serve(
        self,
        websocket: Any,
        *,
        ensure_registered: Callable[[], None],
        handler_label: str,
        client_label: str,
        channel_key: str | None = None,
        ready_factory: Callable[[], Any] | None = None,
    ) -> None:
        """Handler skeleton: subscribe, start the sender, serve until disconnect.

        ``ensure_registered`` is the endpoint's idempotent registration hook
        (called once per connection, after the started log line).

        The handshake must carry the gateway token (``?token=``): these sockets
        stream logs and run events to anyone who can reach ``ws://127.0.0.1``,
        so a token-less connection is closed before it is subscribed (see
        ``server/trigger/auth.py``).
        """
        logger.info(f"{handler_label} handler started: websocket_id={websocket.id}")
        query_params = getattr(websocket, "query_params", {}) or {}
        refusal = auth.check_ws(query_params.get(auth.TOKEN_QUERY_PARAM, None))
        if refusal is not None:
            logger.warning(f"{handler_label} connection rejected: {refusal}")
            await websocket.close()
            return
        # Second layer (login gate): while enforcement is active a single-use
        # ticket from /auth/ws-ticket is required — sockets carry no peer address.
        user_refusal = await auth_user.ws_user_check(query_params)
        if user_refusal is not None:
            logger.warning(f"{handler_label} connection rejected: {user_refusal}")
            await websocket.close()
            return
        ensure_registered()

        queue: deque[str] = deque()
        with self._subscribers_lock:
            self._subscribers[websocket] = channel_key
            self._pending[websocket] = queue

        sender_task = asyncio.create_task(self._sender(websocket, queue))

        try:
            # Welcome frame. A factory (the trajectory channel) may add a
            # gap-fill snapshot; it runs AFTER subscribing so no event can slip
            # between the snapshot and the subscription.
            ready: dict[str, Any] = {"event": "ready"}
            if ready_factory is not None:
                extra = ready_factory()
                if inspect.isawaitable(extra):
                    extra = await extra
                if isinstance(extra, dict):
                    ready.update(extra)
            await websocket.send_text(json.dumps(ready, ensure_ascii=False))

            # Receive loop: keeps the connection alive and detects disconnect.
            while True:
                try:
                    await websocket.receive_text()
                except Exception:
                    break
        except Exception as e:
            logger.warning(f"{client_label} client {websocket.id} disconnected: {e}")
        finally:
            # Await the cancelled task so its frame is finished before the
            # socket is unregistered (a bare cancel leaves it pending).
            sender_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await sender_task
            with self._subscribers_lock:
                self._subscribers.pop(websocket, None)
                self._pending.pop(websocket, None)
            logger.info(f"{client_label} client {websocket.id} unregistered")
