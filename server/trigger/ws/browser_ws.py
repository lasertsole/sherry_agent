"""The browser panel's live channel: screencast frames out, input events in.

One WebSocket per panel instance (``/browser/ws?session_id=…``), behind the
same two handshake gates as every other socket (gateway token + the login
ticket). The panel never talks to the CDP port — it watches JPEG frames and
sends intent; the manager performs every CDP call in-process (C3).

Wire frames (JSON):

server -> client
    ``{"event": "ready", "enabled", "running", "page"|null, "pages", "can_back",
        "can_forward"}``  the opening snapshot
    ``{"event": "frame", "data", "width", "height"}``  one screencast JPEG
        (base64); frames are DROPPABLE — only the newest awaiting one is sent,
        so a slow client loses pictures, never correctness
    ``{"event": "page", …page info…, "can_back", "can_forward"}``  after a
        navigation / history move / watch switch
    ``{"event": "error", "message"}``  one refused command (never fatal)
    ``{"event": "pong"}``

client -> server
    ``{"event": "open"|"nav", "url"}``   navigate (creates the page on first use)
    ``{"event": "reload"}``
    ``{"event": "history", "step": -1|1}``
    ``{"event": "viewport", "width", "height", "scale", "mobile"}``
    ``{"event": "input", "kind": "mouse"|"wheel"|"key"|"text", "payload": {…}}``
    ``{"event": "watch", "page": "pN"|null}``  switch the watched page
        (``null`` = the session's default page) — the devtools toggle rides this
    ``{"event": "devtools", "on": true|false}``  open/reuse the devtools target
        and watch it, or switch back to the page
    ``{"event": "ping"}``
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from typing import Any

from loguru import logger
from robyn import WebSocketAdapter

from config.features import BROWSER_AGENT
from server.service.browser_manager import BrowserManager, BrowserPage, get_browser_manager
from server.trigger import auth, auth_user
from server.trigger.core import app

__all__ = ["browser_ws_handler"]


class BrowserWSSession:
    """One panel connection: the page it watches, its frames and its commands."""

    def __init__(self, websocket: Any, manager: BrowserManager, session_id: str) -> None:
        self.ws = websocket
        self.manager = manager
        self.session_id = session_id
        self.page: BrowserPage | None = None
        self._latest_frame: dict[str, Any] | None = None
        self._wake = asyncio.Event()
        self._sender: asyncio.Task | None = None
        self._closed = False
        self._page_frame_task: asyncio.Task | None = None

    # ----------------------------------------------------------------- outbound

    async def send(self, payload: dict[str, Any]) -> None:
        """Send one JSON frame to the panel (failures end the connection)."""
        await self.ws.send_text(json.dumps(payload, ensure_ascii=False))

    def on_frame(self, page: BrowserPage, params: dict[str, Any]) -> None:
        """Screencast callback (loop thread): keep only the newest frame."""
        if self._closed or self.page is None or page.page_id != self.page.page_id:
            return
        metadata = params.get("metadata") or {}
        self._latest_frame = {
            "event": "frame",
            "data": params.get("data", ""),
            "width": metadata.get("deviceWidth") or metadata.get("width"),
            "height": metadata.get("deviceHeight") or metadata.get("height"),
        }
        self._wake.set()

    def on_page_event(self, page: BrowserPage, method: str, params: dict[str, Any]) -> None:
        """Raw CDP page event: a user-driven navigation updates the address bar.

        The panel's commands answer with a ``page`` frame, but a link the USER
        clicked (or a redirect) has no command behind it — ``Page.frameNavigated``
        is the only signal, so the frame is coalesced and sent from here.
        """
        if self._closed or self.page is None or page.page_id != self.page.page_id:
            return
        if method != "Page.frameNavigated":
            return
        frame = params.get("frame") or {}
        if frame.get("parentId"):
            return  # a subframe does not move the address bar
        url = str(frame.get("url") or "")
        if not url or url == page.url:
            return
        if url.startswith("chrome-error://"):
            # A failed load reports the error page's URL; Chrome's own omnibox
            # keeps what the user asked for, and so does the panel (the picture
            # still shows Chrome's error page).
            return
        page.url = url
        self._schedule_page_frame()

    def _schedule_page_frame(self) -> None:
        """Send one ``page`` frame shortly after the last navigation event."""
        if self._page_frame_task is not None and not self._page_frame_task.done():
            self._page_frame_task.cancel()

        async def send_later() -> None:
            await asyncio.sleep(0.12)  # let a redirect chain settle
            if self._closed or self.page is None:
                return
            with contextlib.suppress(Exception):
                await self.send({"event": "page", **await self._page_payload(self.page)})

        self._page_frame_task = asyncio.create_task(send_later())

    async def _flush(self) -> None:
        """Send the newest pending frame; drop older ones (a slow panel cannot stall)."""
        while not self._closed:
            await self._wake.wait()
            self._wake.clear()
            frame, self._latest_frame = self._latest_frame, None
            if frame is None:
                continue
            try:
                await self.send(frame)
            except Exception:  # noqa: BLE001 - a dead socket ends the loop
                return

    # --------------------------------------------------------------- page wiring

    async def _page_payload(self, page: BrowserPage | None) -> dict[str, Any]:
        """Page info plus its back/forward state (the ``event`` key is the caller's)."""
        if page is None:
            return {"page": None, "can_back": False, "can_forward": False}
        return dict(await self.manager.page_history(self.session_id, page.page_id))

    async def _watch(self, page: BrowserPage) -> dict[str, Any]:
        """Point this connection's frames and inputs at ``page``."""
        if self.page is not None and self.page.page_id != page.page_id:
            self.manager.unsubscribe_frames(self.page, self.on_frame)
            self.manager.unsubscribe_events(self.page, self.on_page_event)
            await self._stop_if_unwatched(self.page)
        self.page = page
        self._latest_frame = None
        self.manager.subscribe_frames(page, self.on_frame)
        self.manager.subscribe_events(page, self.on_page_event)
        await self.manager.start_screencast(self.session_id, page.page_id)
        return {"event": "page", **await self._page_payload(page)}

    async def _stop_if_unwatched(self, page: BrowserPage) -> None:
        """Stop a page's screencast once nobody watches it (best effort)."""
        if page.framers:
            return
        try:
            await self.manager.stop_screencast(self.session_id, page.page_id)
        except (KeyError, RuntimeError):
            pass

    def _default_page(self) -> BrowserPage | None:
        """The session's current page (``None`` until the first navigation)."""
        try:
            return self.manager.page_for(self.session_id)
        except KeyError:
            return None

    # ---------------------------------------------------------------- commands

    async def handle(self, message: dict[str, Any]) -> None:
        """Run one client command; expected failures answer ``error``, never raise."""
        event = str(message.get("event") or "")
        try:
            if event == "ping":
                await self.send({"event": "pong"})
            elif event in ("open", "nav"):
                url = str(message.get("url") or "").strip()
                info = await self.manager.navigate(self.session_id, url)
                page = self.manager.page_for(self.session_id, info["page"])
                await self.send(await self._watch(page))
            elif event == "reload":
                await self.manager.reload(self.session_id)
                await self.send(
                    {"event": "page", **await self._page_payload(self.page or self._default_page())}
                )
            elif event in ("history", "back", "forward"):
                if event == "back":
                    step = -1
                elif event == "forward":
                    step = 1
                else:
                    step = int(message.get("step") or 0)
                await self.manager.history_step(self.session_id, step)
                await self.send(
                    {"event": "page", **await self._page_payload(self.page or self._default_page())}
                )
            elif event == "viewport":
                await self.manager.set_viewport(
                    self.session_id,
                    width=int(message.get("width") or 800),
                    height=int(message.get("height") or 600),
                    device_scale_factor=float(message.get("scale") or 1),
                    mobile=bool(message.get("mobile")),
                )
            elif event == "input":
                await self.manager.send_input(
                    self.session_id,
                    str(message.get("kind") or ""),
                    message.get("payload") or {},
                )
            elif event == "watch":
                page_id = str(message.get("page") or "").strip() or None
                page = self.manager.page_for(self.session_id, page_id)
                await self.send(await self._watch(page))
            elif event == "devtools":
                if message.get("on", True):
                    info = await self.manager.open_devtools(self.session_id)
                    page = self.manager.page_for(self.session_id, info["page"])
                    await self.send(await self._watch(page))
                else:
                    page = self._default_page()
                    if page is not None:
                        await self.send(await self._watch(page))
            else:
                await self.send({"event": "error", "message": f"unknown event {event!r}"})
        except KeyError as error:
            await self.send(
                {"event": "error", "message": error.args[0] if error.args else str(error)}
            )
        except (ValueError, PermissionError) as error:
            await self.send({"event": "error", "message": str(error)})
        except RuntimeError as error:
            # Also covers the server's CdpError (a RuntimeError subclass).
            await self.send({"event": "error", "message": f"browser error: {error}"})

    # ------------------------------------------------------------------ lifecycle

    async def run(self) -> None:
        """Serve until the socket closes: welcome, commands, frame flush."""
        self._sender = asyncio.create_task(self._flush())
        try:
            page = self._default_page()
            ready: dict[str, Any] = {
                "event": "ready",
                "enabled": bool(BROWSER_AGENT["enabled"]),
                "running": self.manager.running,
                "pages": self.manager.list_pages(self.session_id),
            }
            ready.update(await self._page_payload(page))
            await self.send(ready)
            if page is not None:
                await self.send(await self._watch(page))
            while True:
                raw = await self.ws.receive_text()
                if not raw:
                    continue
                try:
                    message = json.loads(raw)
                except (TypeError, ValueError):
                    await self.send({"event": "error", "message": "malformed frame"})
                    continue
                if isinstance(message, dict):
                    await self.handle(message)
        except Exception as error:  # noqa: BLE001 - disconnect / socket errors end the loop
            logger.debug("Browser WS ended: {}", error)
        finally:
            self._closed = True
            self._wake.set()
            if self._sender is not None:
                self._sender.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._sender
            if self._page_frame_task is not None:
                self._page_frame_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._page_frame_task
            if self.page is not None:
                self.manager.unsubscribe_frames(self.page, self.on_frame)
                self.manager.unsubscribe_events(self.page, self.on_page_event)
                await self._stop_if_unwatched(self.page)
            logger.debug("Browser WS closed: session={}", self.session_id)


@app.websocket("/browser/ws")
async def browser_ws_handler(websocket: WebSocketAdapter):
    """The panel's live channel (gateway token + login ticket + session id)."""
    query = getattr(websocket, "query_params", {}) or {}
    refusal = auth.check_ws(query.get(auth.TOKEN_QUERY_PARAM, None))
    if refusal is not None:
        logger.warning("Browser WS connection rejected: {}", refusal)
        await websocket.close()
        return
    user_refusal = await auth_user.ws_user_check(query)
    if user_refusal is not None:
        logger.warning("Browser WS connection rejected: {}", user_refusal)
        await websocket.close()
        return
    session_id = str(query.get("session_id") or "").strip()
    if not session_id:
        logger.warning("Browser WS connection rejected: missing session_id")
        await websocket.close()
        return
    if not BROWSER_AGENT["enabled"]:
        # The feature ships off: the panel falls back to its iframe mode and must
        # be able to learn that over the socket (C4 leaves no browser routes live).
        with contextlib.suppress(Exception):
            await websocket.send_text(
                json.dumps({"event": "ready", "enabled": False, "running": False, "page": None})
            )
            await websocket.close()
        return
    logger.info("Browser WS handler started: websocket_id={}", websocket.id)
    await BrowserWSSession(websocket, get_browser_manager(), session_id).run()
    logger.info("Browser WS client {} unregistered", websocket.id)
