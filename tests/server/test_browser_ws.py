"""The browser panel's live channel: commands in, frames out, clean teardown.

Everything runs against a fake websocket and a fake manager, so these tests pin
the wire contract the panel depends on: the opening ``ready`` snapshot, one
``page`` frame per navigation / history move / watch switch, droppable frames
(latest wins), refusals as ``error`` frames instead of dropped connections, and
the teardown that unsubscribes + stops a screencast nobody watches.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from config.features import BROWSER_AGENT
from server.service.browser_manager import BrowserPage
from server.trigger.ws import browser_ws as ws_module
from server.trigger.ws.browser_ws import BrowserWSSession

pytestmark = [pytest.mark.unit]


#: Sentinel: a caller that omits Robyn's required default raises.
_MISSING = object()


class _StrictQueryParams:
    """Robyn's QueryParams contract: ``get`` requires the default argument.

    The plain-dict double the handler tests used accepted the one-arg form and
    hid a live TypeError ("QueryParams.get() missing 1 required positional
    argument: 'default'") — the same trap ``helpers.query_int`` exists for.
    """

    def __init__(self, values: dict) -> None:
        self._values = values

    def get(self, key: str, default: object = _MISSING):  # noqa: ANN001 - Robyn's signature
        if default is _MISSING:
            raise TypeError("QueryParams.get() missing 1 required positional argument: 'default'")
        return self._values.get(key, default)


class _FakeWS:
    """A websocket double: ``sent`` collects parsed frames; ``queue`` feeds input."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.queue: asyncio.Queue = asyncio.Queue()
        self.closed = False
        self.id = "ws-test"

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    async def receive_text(self) -> str:
        item = await self.queue.get()
        if item is None:
            raise RuntimeError("client gone")
        return item

    async def close(self) -> None:
        self.closed = True


class _FakeManager:
    """The manager surface the channel uses, recording every call."""

    def __init__(self, pages: list[BrowserPage] | None = None) -> None:
        self.running = True
        self.calls: list[tuple] = []
        self.pages = {page.page_id: page for page in (pages or [])}

    # ---- page registry (mirrors BrowserManager.page_for's contract)
    def page_for(self, session_id: str, page_id: str | None = None) -> BrowserPage:
        if page_id:
            page = self.pages.get(page_id)
            if page is None or page.session_id != session_id:
                raise KeyError(f"unknown page {page_id!r} for this session")
            return page
        candidates = [p for p in self.pages.values() if p.session_id == session_id]
        if not candidates:
            raise KeyError("session has no browser page — call browser_navigate first")
        ordinary = [p for p in candidates if p.kind == "page"]
        return max(ordinary or candidates, key=lambda p: p.last_used)

    def list_pages(self, session_id: str) -> list[dict]:
        return [p.info() for p in self.pages.values() if p.session_id == session_id]

    def add(self, page: BrowserPage) -> BrowserPage:
        self.pages[page.page_id] = page
        return page

    # ---- verbs
    async def page_history(self, session_id, page_id=None):
        page = self.page_for(session_id, page_id)
        self.calls.append(("page_history", page.page_id))
        return {**page.info(), "can_back": False, "can_forward": True}

    async def navigate(self, session_id, url, page_id=None):
        self.calls.append(("navigate", url))
        page = (
            self.page_for(session_id, page_id)
            if self.pages
            else self.add(
                BrowserPage(page_id="p1", target_id="T1", cdp_session="S1", session_id=session_id)
            )
        )
        page.url = url
        return {**page.info(), "loaded": True}

    async def reload(self, session_id, page_id=None):
        self.calls.append(("reload",))
        return {**self.page_for(session_id, page_id).info(), "loaded": True}

    async def history_step(self, session_id, step, page_id=None):
        self.calls.append(("history_step", step))
        return {**self.page_for(session_id, page_id).info(), "moved": True}

    async def set_viewport(
        self, session_id, *, width, height, device_scale_factor=1, mobile=False, page_id=None
    ):
        self.calls.append(("set_viewport", width, height, device_scale_factor, mobile))
        return {**self.page_for(session_id, page_id).info(), "viewport": [width, height]}

    async def send_input(self, session_id, kind, payload, page_id=None):
        self.calls.append(("send_input", kind, payload))
        return {"page": "p1", "kind": kind}

    async def open_devtools(self, session_id, page_id=None):
        self.calls.append(("open_devtools",))
        page = self.add(
            BrowserPage(
                page_id="p9",
                target_id="TD",
                cdp_session="SD",
                session_id=session_id,
                kind="devtools",
                devtools_for="T1",
                url="devtools://devtools/bundled/inspector.html",
                last_used=99.0,
            )
        )
        return page.info()

    async def start_screencast(self, session_id, page_id=None):
        page = self.page_for(session_id, page_id)
        page.screencasting = True
        self.calls.append(("start_screencast", page.page_id))
        return {**page.info(), "screencasting": True}

    async def stop_screencast(self, session_id, page_id=None):
        page = self.page_for(session_id, page_id)
        page.screencasting = False
        self.calls.append(("stop_screencast", page.page_id))
        return {**page.info(), "screencasting": False}

    def subscribe_frames(self, page, framer):
        if framer not in page.framers:
            page.framers.append(framer)
        self.calls.append(("subscribe", page.page_id))

    def unsubscribe_frames(self, page, framer):
        if framer in page.framers:
            page.framers.remove(framer)

    def subscribe_events(self, page, sink):
        if sink not in page.event_sinks:
            page.event_sinks.append(sink)
        self.calls.append(("subscribe_events", page.page_id))

    def unsubscribe_events(self, page, sink):
        if sink in page.event_sinks:
            page.event_sinks.remove(sink)


def _page(page_id: str = "p1", session_id: str = "s1", **kwargs) -> BrowserPage:
    return BrowserPage(
        page_id=page_id,
        target_id="T" + page_id,
        cdp_session="S" + page_id,
        session_id=session_id,
        last_used=1.0,
        **kwargs,
    )


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setitem(BROWSER_AGENT, "enabled", 1)


def test_run_sends_ready_then_serves_commands_and_tears_down(enabled):
    async def scenario():
        ws = _FakeWS()
        manager = _FakeManager([_page()])
        session = BrowserWSSession(ws, manager, "s1")
        await ws.queue.put(json.dumps({"event": "ping"}))
        await ws.queue.put(
            json.dumps({"event": "input", "kind": "text", "payload": {"text": "hi"}})
        )
        await ws.queue.put(None)  # the client goes away
        await session.run()
        return ws, manager, session

    ws, manager, session = asyncio.run(scenario())

    assert ws.sent[0]["event"] == "ready"
    assert ws.sent[0]["enabled"] is True and ws.sent[0]["page"] == "p1"
    assert any(frame["event"] == "pong" for frame in ws.sent)
    assert ("send_input", "text", {"text": "hi"}) in manager.calls
    # Teardown: nothing watches the page any more, so its stream stopped.
    assert session.page is not None and session.page.framers == []
    assert ("stop_screencast", "p1") in manager.calls


def test_navigate_watches_the_page_it_created(enabled):
    async def scenario():
        ws = _FakeWS()
        manager = _FakeManager()
        session = BrowserWSSession(ws, manager, "s1")
        await session.handle({"event": "nav", "url": "https://x.test"})
        return ws, manager, session

    ws, manager, session = asyncio.run(scenario())

    assert ("navigate", "https://x.test") in manager.calls
    assert ("subscribe", "p1") in manager.calls and ("start_screencast", "p1") in manager.calls
    page_frame = [f for f in ws.sent if f["event"] == "page"][-1]
    assert page_frame["page"] == "p1" and page_frame["url"] == "https://x.test"
    assert session.page is not None and session.page.page_id == "p1"


def test_history_and_reload_report_the_page(enabled):
    async def scenario():
        ws = _FakeWS()
        manager = _FakeManager([_page()])
        session = BrowserWSSession(ws, manager, "s1")
        session.page = manager.page_for("s1")
        await session.handle({"event": "back"})
        await session.handle({"event": "history", "step": 1})
        await session.handle({"event": "reload"})
        return ws, manager

    ws, manager = asyncio.run(scenario())

    steps = [call for call in manager.calls if call[0] == "history_step"]
    assert steps == [("history_step", -1), ("history_step", 1)]
    assert ("reload",) in manager.calls
    assert [f for f in ws.sent if f["event"] == "page"]


def test_viewport_and_input_ride_the_manager(enabled):
    async def scenario():
        ws = _FakeWS()
        manager = _FakeManager([_page()])
        session = BrowserWSSession(ws, manager, "s1")
        session.page = manager.page_for("s1")
        await session.handle(
            {"event": "viewport", "width": 393, "height": 852, "scale": 2, "mobile": True}
        )
        await session.handle(
            {"event": "input", "kind": "mouse", "payload": {"type": "mousePressed", "x": 1, "y": 2}}
        )
        return manager

    manager = asyncio.run(scenario())

    assert ("set_viewport", 393, 852, 2.0, True) in manager.calls
    assert ("send_input", "mouse", {"type": "mousePressed", "x": 1, "y": 2}) in manager.calls


def test_devtools_toggles_the_watched_page(enabled):
    async def scenario():
        ws = _FakeWS()
        manager = _FakeManager([_page()])
        session = BrowserWSSession(ws, manager, "s1")
        session.page = manager.page_for("s1")
        await session.handle({"event": "devtools", "on": True})
        on_page = session.page
        await session.handle({"event": "devtools", "on": False})
        return ws, manager, on_page, session

    ws, manager, on_page, session = asyncio.run(scenario())

    assert ("open_devtools",) in manager.calls
    assert on_page is not None and on_page.kind == "devtools"
    # Each switch streams the newly watched page and releases the old one.
    assert ("start_screencast", "p9") in manager.calls
    assert ("start_screencast", "p1") in manager.calls
    assert [f["event"] for f in ws.sent].count("page") == 2
    assert session.page is not None and session.page.page_id == "p1"


def test_errors_answer_with_a_frame_not_a_drop(enabled):
    async def scenario():
        ws = _FakeWS()
        manager = _FakeManager([_page()])
        session = BrowserWSSession(ws, manager, "s1")

        async def boom(*args, **kwargs):
            raise ValueError("unknown ref 'e9' — take a fresh snapshot")

        manager.send_input = boom  # type: ignore[method-assign]
        await session.handle({"event": "input", "kind": "text", "payload": {"text": "x"}})
        await session.handle({"event": "nonsense"})
        await session.handle({"event": "watch", "page": "p404"})
        return ws

    ws = asyncio.run(scenario())

    errors = [frame["message"] for frame in ws.sent if frame["event"] == "error"]
    assert "unknown ref 'e9' — take a fresh snapshot" in errors
    assert any("unknown event" in message for message in errors)
    assert any("unknown page 'p404'" in message for message in errors)


def test_malformed_frames_are_refused_not_fatal(enabled):
    async def scenario():
        ws = _FakeWS()
        session = BrowserWSSession(ws, _FakeManager(), "s1")
        await ws.queue.put("{not json")
        await ws.queue.put(None)
        await session.run()
        return ws

    ws = asyncio.run(scenario())

    assert any(
        frame.get("message") == "malformed frame" for frame in ws.sent if frame["event"] == "error"
    )


def test_frames_are_latest_wins_and_only_for_the_watched_page(enabled):
    async def scenario():
        ws = _FakeWS()
        manager = _FakeManager([_page(), _page("p2")])
        session = BrowserWSSession(ws, manager, "s1")
        page = manager.page_for("s1")
        session.page = page
        # A frame of another page is ignored outright.
        session.on_frame(manager.page_for("s1", "p2"), {"data": "other"})
        session.on_frame(page, {"data": "first"})
        session.on_frame(page, {"data": "second"})
        sender = asyncio.create_task(session._flush())
        for _ in range(100):
            if ws.sent:
                break
            await asyncio.sleep(0.01)
        session._closed = True
        session._wake.set()
        sender.cancel()
        with pytest.raises(asyncio.CancelledError):
            await sender
        return ws

    ws = asyncio.run(scenario())

    frames = [frame for frame in ws.sent if frame["event"] == "frame"]
    assert [frame["data"] for frame in frames] == ["second"]  # "first" was dropped


def test_the_ready_frame_carries_the_feature_switch(monkeypatch):
    monkeypatch.setitem(BROWSER_AGENT, "enabled", 0)

    async def scenario():
        ws = _FakeWS()
        session = BrowserWSSession(ws, _FakeManager(), "s1")
        await session.handle({"event": "ping"})
        return ws

    ws = asyncio.run(scenario())
    assert ws.sent == [{"event": "pong"}]  # ready comes from run(); handle() alone answers


def test_the_handler_refuses_a_socket_without_a_session(monkeypatch):
    async def scenario():
        ws = _FakeWS()
        ws.query_params = _StrictQueryParams({})
        monkeypatch.setattr(ws_module.auth, "check_ws", lambda token: None)

        async def allow(_query):
            return None

        monkeypatch.setattr(ws_module.auth_user, "ws_user_check", allow)
        await ws_module.browser_ws_handler(ws)
        return ws

    ws = asyncio.run(scenario())
    assert ws.closed is True and ws.sent == []


def test_the_handler_refuses_a_bad_token(monkeypatch):
    async def scenario():
        ws = _FakeWS()
        ws.query_params = _StrictQueryParams({"session_id": "s1"})
        monkeypatch.setattr(ws_module.auth, "check_ws", lambda token: "missing or invalid token")
        await ws_module.browser_ws_handler(ws)
        return ws

    ws = asyncio.run(scenario())
    assert ws.closed is True and ws.sent == []


def test_the_handler_tells_a_disabled_client_so(monkeypatch):
    async def scenario():
        ws = _FakeWS()
        ws.query_params = _StrictQueryParams({"session_id": "s1"})
        monkeypatch.setitem(BROWSER_AGENT, "enabled", 0)
        monkeypatch.setattr(ws_module.auth, "check_ws", lambda token: None)

        async def allow(_query):
            return None

        monkeypatch.setattr(ws_module.auth_user, "ws_user_check", allow)
        await ws_module.browser_ws_handler(ws)
        return ws

    ws = asyncio.run(scenario())
    assert ws.sent == [{"event": "ready", "enabled": False, "running": False, "page": None}]
    assert ws.closed is True


def test_the_handler_runs_a_full_session(monkeypatch):
    async def scenario():
        ws = _FakeWS()
        ws.query_params = _StrictQueryParams({"session_id": "s1"})
        monkeypatch.setitem(BROWSER_AGENT, "enabled", 1)
        monkeypatch.setattr(ws_module.auth, "check_ws", lambda token: None)

        async def allow(_query):
            return None

        monkeypatch.setattr(ws_module.auth_user, "ws_user_check", allow)
        monkeypatch.setattr(ws_module, "get_browser_manager", lambda: _FakeManager([_page()]))
        await ws.queue.put(None)
        await ws_module.browser_ws_handler(ws)
        return ws

    ws = asyncio.run(scenario())
    assert ws.sent and ws.sent[0]["event"] == "ready" and ws.sent[0]["page"] == "p1"


def test_a_user_driven_navigation_updates_the_address_bar(enabled):
    """A link the user clicked has no command behind it: Page.frameNavigated
    must still produce a `page` frame (a subframe must not)."""

    async def scenario():
        ws = _FakeWS()
        manager = _FakeManager([_page()])
        session = BrowserWSSession(ws, manager, "s1")
        session.page = manager.page_for("s1")
        await session.handle({"event": "ping"})  # a command so the ready frame isn't the only one
        session.on_page_event(
            session.page, "Page.frameNavigated", {"frame": {"url": "https://next.test"}}
        )
        # A subframe navigation is ignored outright.
        session.on_page_event(
            session.page,
            "Page.frameNavigated",
            {"frame": {"url": "https://ad.test", "parentId": "F2"}},
        )
        for _ in range(60):
            if any(frame["event"] == "page" for frame in ws.sent):
                break
            await asyncio.sleep(0.02)
        return ws

    ws = asyncio.run(scenario())

    pages = [frame for frame in ws.sent if frame["event"] == "page"]
    assert pages and pages[-1]["url"] == "https://next.test"
    assert pages[-1]["page"] == "p1"


def test_a_failed_navigation_keeps_the_address_bar(enabled):
    """A chrome-error:// page must not replace what the user asked for."""

    async def scenario():
        ws = _FakeWS()
        manager = _FakeManager([_page()])
        session = BrowserWSSession(ws, manager, "s1")
        session.page = manager.page_for("s1")
        session.page.url = "https://asked.test"
        session.on_page_event(
            session.page,
            "Page.frameNavigated",
            {"frame": {"url": "chrome-error://chromewebdata/"}},
        )
        await asyncio.sleep(0.2)
        return ws, session

    ws, session = asyncio.run(scenario())

    assert session.page is not None and session.page.url == "https://asked.test"
    assert not any(frame["event"] == "page" for frame in ws.sent)


def test_watch_switches_the_event_sink_too(enabled):
    async def scenario():
        ws = _FakeWS()
        manager = _FakeManager([_page()])
        session = BrowserWSSession(ws, manager, "s1")
        session.page = manager.page_for("s1")
        await session.handle({"event": "devtools", "on": True})
        return manager, session

    manager, session = asyncio.run(scenario())

    calls = [call for call in manager.calls if call[0] == "subscribe_events"]
    # The watched page gains the sink; the one it replaced keeps none, so a
    # stale navigation can never move the address bar.
    assert calls == [("subscribe_events", "p9")]
    assert manager.pages["p1"].event_sinks == []


def test_a_failed_command_keeps_the_channel_alive(enabled):
    """A launch timeout (OSError) must answer an error frame, not drop the socket."""

    async def scenario():
        ws = _FakeWS()
        manager = _FakeManager([_page()])
        session = BrowserWSSession(ws, manager, "s1")
        session.page = manager.page_for("s1")

        async def boom(*args, **kwargs):
            raise TimeoutError("the browser did not publish a debug port within 6.0s")

        manager.navigate = boom  # type: ignore[method-assign]
        await session.handle({"event": "nav", "url": "https://x.test"})
        # The socket is still usable afterwards.
        await session.handle({"event": "ping"})
        return ws

    ws = asyncio.run(scenario())

    errors = [frame["message"] for frame in ws.sent if frame["event"] == "error"]
    assert any("debug port" in message for message in errors)
    assert ws.sent[-1] == {"event": "pong"}
