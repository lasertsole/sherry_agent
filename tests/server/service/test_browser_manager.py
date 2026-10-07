"""The browser manager: page registry, bounded verbs, refs, screencast routing.

Everything runs against a fake transport (the slice of ``CdpConnection`` the
manager depends on), so these tests pin the manager's own contract: pages are
allocated per session with an LRU cap, a snapshot stores refs SERVER-side and
serves them without xpaths, click/type resolve refs through that map, every
bound (timeouts, element caps, screenshot size) is enforced, and the evaluate
escape hatch answers only when the config says so.
"""

from __future__ import annotations

import asyncio
import base64
import json
import struct
import zlib
from pathlib import Path

import pytest

from config.features.infra_side.browser_agent import _build_browser_agent
from server.service import browser_manager as bm
from server.service.browser_cdp import CdpError, LaunchedBrowser

pytestmark = [pytest.mark.unit]


class _FakeTransport:
    """The ``BrowserTransport`` protocol over a dict of method handlers."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self.handlers: dict = {}
        self.connected = True
        self.started = False
        self._listeners: list = []

    async def start(self, *, timeout: float = 10.0) -> None:
        self.started = True

    async def call(self, method, session=None, *, timeout=None, **params):
        self.calls.append({"method": method, "session": session, "params": params})
        handler = self.handlers.get(method)
        return handler(session, params) if handler else {}

    def add_listener(self, listener) -> None:
        self._listeners.append(listener)

    def remove_listener(self, listener) -> None:
        self._listeners.remove(listener)

    async def close(self) -> None:
        self.connected = False

    def emit(self, message: dict) -> None:
        for listener in list(self._listeners):
            listener(message)


class _FakeBrowser:
    """Handler set emulating the CDP surface the manager uses."""

    def __init__(self, transport: _FakeTransport) -> None:
        self.t = transport
        self.targets: list[str] = []
        self.closed: list[str] = []
        self.evals: list[str] = []
        self.screenshot = _tiny_png()
        transport.handlers.update(
            {
                "Target.createTarget": self._create_target,
                "Target.attachToTarget": lambda s, p: {"sessionId": "S-" + p["targetId"]},
                "Target.closeTarget": self._close_target,
                "Page.enable": lambda s, p: {},
                "Page.navigate": lambda s, p: {},
                "Runtime.evaluate": self._evaluate,
                "Input.dispatchMouseEvent": lambda s, p: {},
                "Input.dispatchKeyEvent": lambda s, p: {},
                "Input.insertText": lambda s, p: {},
                "Page.captureScreenshot": lambda s, p: {
                    "data": base64.b64encode(self.screenshot).decode()
                },
                "Browser.close": lambda s, p: {},
            }
        )

    def _create_target(self, session, params):
        target = f"T{len(self.targets) + 1}"
        self.targets.append(target)
        return {"targetId": target}

    def _close_target(self, session, params):
        self.closed.append(params["targetId"])
        return {}

    def _evaluate(self, session, params):
        expression = params["expression"]
        self.evals.append(expression)
        if expression == "document.readyState":
            return {"result": {"value": "complete"}}
        if "location.href" in expression:
            return {
                "result": {
                    "value": json.dumps({"url": "https://fake.test/page", "title": "Fake page"})
                }
            }
        if "window.innerWidth" in expression:
            return {"result": {"value": json.dumps({"w": 800, "h": 600})}}
        return {"result": {"value": None}}


def _tiny_png(width: int = 4, height: int = 3) -> bytes:
    """A real (tiny) PNG so the manager's IHDR reader has something to parse."""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = struct.pack(">I", len(data)) + tag + data
        return body + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00" * (width * 3) for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def rig():
    """(manager, transport, browser, launched) with a fake launcher."""
    transport = _FakeTransport()
    browser = _FakeBrowser(transport)
    launched: list[dict] = []

    async def launcher(**kwargs):
        launched.append(kwargs)
        return LaunchedBrowser(
            pid=4242, port=9, ws_url="ws://fake/devtools/browser/x", headless=False
        )

    config = _build_browser_agent(env={})
    config.update(
        {
            "enabled": 1,
            "max_pages": 4,
            "op_timeout_s": 1.0,
            "nav_timeout_s": 1.0,
            "snapshot_max_elements": 50,
            "snapshot_max_text_chars": 200,
        }
    )
    manager = bm.BrowserManager(config, launcher=launcher, transport_factory=lambda url: transport)
    return manager, transport, browser, launched


def _run(coro):
    return asyncio.run(coro)


# ------------------------------------------------------------------ lifecycle


def test_ensure_started_launches_once_and_status_hides_the_port(rig):
    manager, transport, _browser, launched = rig

    async def scenario():
        await manager.ensure_started()
        await manager.ensure_started()
        return manager.status()

    status = _run(scenario())

    assert len(launched) == 1
    assert transport.started is True
    assert status == {"enabled": True, "running": True, "pid": 4242, "pages": 0, "sessions": 0}
    assert "port" not in status


def test_status_before_start_is_not_running(rig):
    manager, _t, _b, _l = rig
    assert manager.status()["running"] is False
    assert manager.status()["pid"] is None


def test_shutdown_closes_the_browser_and_the_transport(rig):
    manager, transport, _browser, _launched = rig

    async def scenario():
        await manager.ensure_started()
        await manager.shutdown()

    _run(scenario())

    assert any(call["method"] == "Browser.close" for call in transport.calls)
    assert transport.connected is False
    assert manager.running is False


# ---------------------------------------------------------------------- pages


def test_open_page_registers_and_the_newest_is_the_default(rig):
    manager, transport, _browser, _launched = rig

    async def scenario():
        first = await manager.open_page("s1", "https://a.test")
        second = await manager.open_page("s1", "https://b.test")
        return first, second, manager.page_for("s1"), manager.list_pages("s1")

    first, second, default, listing = _run(scenario())

    assert first.page_id == "p1" and second.page_id == "p2"
    assert default.page_id == second.page_id
    assert [entry["page"] for entry in listing] == ["p2", "p1"]
    assert any(
        call["method"] == "Page.enable" and call["session"] == "S-T1" for call in transport.calls
    )


def test_page_for_raises_without_a_page(rig):
    manager, _t, _b, _l = rig
    with pytest.raises(KeyError, match="browser_navigate"):
        manager.page_for("s1")


def test_max_pages_evicts_the_least_recently_used(rig):
    manager, _t, browser, _l = rig
    manager.config["max_pages"] = 2

    async def scenario():
        first = await manager.open_page("s1", "https://a.test")
        second = await manager.open_page("s2", "https://b.test")
        # Make the first the stalest, then open a third page.
        first.last_used = 0.0
        third = await manager.open_page("s3", "https://c.test")
        return first, second, third

    first, _second, third = _run(scenario())

    assert browser.closed == [first.target_id]  # only the LRU went
    with pytest.raises(KeyError):
        manager.page_for("s1")
    assert manager.page_for("s3").page_id == "p3"


def test_close_page_removes_it_and_closes_the_target(rig):
    manager, _t, browser, _l = rig

    async def scenario():
        page = await manager.open_page("s1", "about:blank")
        outcome = await manager.close_page("s1", page.page_id)
        return page, outcome

    page, outcome = _run(scenario())

    assert outcome == {"page": "p1", "closed": True}
    assert browser.closed == [page.target_id]
    with pytest.raises(KeyError):
        manager.page_for("s1")


def test_close_session_closes_only_that_sessions_pages(rig):
    manager, _t, browser, _l = rig

    async def scenario():
        first = await manager.open_page("s1", "about:blank")
        second = await manager.open_page("s1", "about:blank")
        other = await manager.open_page("s2", "about:blank")
        outcome = await manager.close_session("s1")
        return first, second, other, outcome

    first, second, other, outcome = _run(scenario())

    assert outcome == {"session_id": "s1", "closed": 2}
    assert browser.closed == [first.target_id, second.target_id]
    assert manager.page_for("s2").page_id == other.page_id
    with pytest.raises(KeyError):
        manager.page_for("s1")


# ---------------------------------------------------------------------- verbs


def test_navigate_sets_the_location_and_clears_refs(rig):
    manager, transport, _browser, _l = rig

    async def scenario():
        page = await manager.open_page("s1", "about:blank")
        page.refs = {"e1": "//div[1]"}
        outcome = await manager.navigate("s1", "https://fake.test/page")
        return page, outcome

    page, outcome = _run(scenario())

    assert outcome["url"] == "https://fake.test/page"
    assert outcome["title"] == "Fake page"
    assert outcome["loaded"] is True
    assert page.refs == {}
    assert any(
        call["method"] == "Page.navigate" and call["params"]["url"] == "https://fake.test/page"
        for call in transport.calls
    )


def test_navigate_requires_a_url(rig):
    manager, _t, _b, _l = rig

    async def scenario():
        await manager.open_page("s1", "about:blank")
        with pytest.raises(ValueError, match="url is required"):
            await manager.navigate("s1", "   ")

    _run(scenario())


def test_snapshot_stores_refs_server_side_and_strips_xpaths(rig):
    manager, transport, _browser, _l = rig
    payload = {
        "url": "https://fake.test/page",
        "title": "Fake page",
        "text": "hello",
        "elements": [
            {"ref": "e1", "xpath": "//button[1]", "tag": "button", "name": "Go"},
            {"ref": "e2", "xpath": '//*[@id="x"]', "tag": "input", "name": "Name"},
        ],
        "truncated": True,
    }

    def evaluate(session, params):
        return {"result": {"value": json.dumps(payload)}}

    transport.handlers["Runtime.evaluate"] = evaluate

    async def scenario():
        page = await manager.open_page("s1", "about:blank")
        outcome = await manager.snapshot("s1")
        return page, outcome

    page, outcome = _run(scenario())

    assert page.refs == {"e1": "//button[1]", "e2": '//*[@id="x"]'}
    assert all("xpath" not in element for element in outcome["elements"])
    assert outcome["truncated"] is True
    assert outcome["text"] == "hello"
    assert outcome["url"] == "https://fake.test/page"


def test_click_resolves_a_ref_through_the_server_side_map(rig):
    manager, transport, _browser, _l = rig

    def evaluate(session, params):
        if "singleNodeValue" in params["expression"]:
            assert json.dumps("//button[1]") in params["expression"]
            return {"result": {"value": json.dumps({"ok": True, "tag": "button", "text": "Go"})}}
        return {"result": {"value": json.dumps({"url": "u", "title": "t"})}}

    transport.handlers["Runtime.evaluate"] = evaluate

    async def scenario():
        page = await manager.open_page("s1", "about:blank")
        page.refs = {"e1": "//button[1]"}
        return await manager.click("s1", ref="e1")

    outcome = _run(scenario())

    assert outcome["clicked"] == "e1"
    assert outcome["tag"] == "button"


def test_click_with_an_unknown_ref_is_refused(rig):
    manager, _t, _b, _l = rig

    async def scenario():
        await manager.open_page("s1", "about:blank")
        with pytest.raises(ValueError, match="unknown ref"):
            await manager.click("s1", ref="e9")

    _run(scenario())


def test_click_needs_a_ref_or_coordinates(rig):
    manager, _t, _b, _l = rig

    async def scenario():
        await manager.open_page("s1", "about:blank")
        with pytest.raises(ValueError, match="x/y"):
            await manager.click("s1")

    _run(scenario())


def test_click_by_coordinates_dispatches_real_input_events(rig):
    manager, transport, _browser, _l = rig

    async def scenario():
        await manager.open_page("s1", "about:blank")
        return await manager.click("s1", x=10, y=20, double=True)

    outcome = _run(scenario())

    assert outcome["clicked"] == "10,20"
    kinds = [
        call["params"]["type"]
        for call in transport.calls
        if call["method"] == "Input.dispatchMouseEvent"
    ]
    assert kinds == ["mousePressed", "mouseReleased"]
    assert all(
        call["params"]["clickCount"] == 2
        for call in transport.calls
        if call["method"] == "Input.dispatchMouseEvent"
    )


def test_type_text_focuses_the_ref_then_inserts_text(rig):
    manager, transport, _browser, _l = rig

    def evaluate(session, params):
        if "singleNodeValue" in params["expression"]:
            return {"result": {"value": "ok"}}
        return {"result": {"value": json.dumps({"url": "u", "title": "t"})}}

    transport.handlers["Runtime.evaluate"] = evaluate

    async def scenario():
        page = await manager.open_page("s1", "about:blank")
        page.refs = {"e2": '//*[@id="q"]'}
        return await manager.type_text("s1", "hello", ref="e2")

    outcome = _run(scenario())

    assert outcome == {"page": "p1", "typed": 5, "submitted": False}
    assert any(call["method"] == "Input.insertText" for call in transport.calls)
    assert any(
        call["method"] == "Runtime.evaluate" and "setSelectionRange" in call["params"]["expression"]
        for call in transport.calls
    )


def test_press_rejects_an_unknown_key_and_sends_enter(rig):
    manager, transport, _browser, _l = rig

    async def scenario():
        await manager.open_page("s1", "about:blank")
        with pytest.raises(ValueError, match="unsupported key"):
            await manager.press("s1", "F13")
        return await manager.press("s1", "Enter")

    outcome = _run(scenario())

    assert outcome == {"page": "p1", "pressed": "Enter"}
    down = [
        call
        for call in transport.calls
        if call["method"] == "Input.dispatchKeyEvent" and call["params"]["type"] == "keyDown"
    ]
    assert down and down[0]["params"]["windowsVirtualKeyCode"] == 13


def test_scroll_dispatches_a_wheel_at_the_viewport_centre(rig):
    manager, transport, _browser, _l = rig

    async def scenario():
        await manager.open_page("s1", "about:blank")
        return await manager.scroll("s1", delta_y=400)

    outcome = _run(scenario())

    wheel = [c for c in transport.calls if c["params"].get("type") == "mouseWheel"]
    assert outcome["scrolled"] == [0.0, 400.0]
    assert wheel and wheel[0]["params"]["x"] == 400 and wheel[0]["params"]["y"] == 300


def test_screenshot_writes_a_png_and_reports_its_size(rig, monkeypatch, tmp_path):
    manager, _t, _b, _l = rig
    monkeypatch.setattr(bm, "TEMP_DIR", tmp_path)

    async def scenario():
        await manager.open_page("s1", "about:blank")
        return await manager.screenshot("s1")

    outcome = _run(scenario())

    assert outcome["width"] == 4 and outcome["height"] == 3
    path = Path(outcome["path"])
    assert path.exists() and path.suffix == ".png"
    assert path.read_bytes().startswith(b"\x89PNG")


def test_screenshot_over_the_limit_is_refused(rig, monkeypatch, tmp_path):
    manager, _t, _b, _l = rig
    monkeypatch.setattr(bm, "TEMP_DIR", tmp_path)
    manager.config["screenshot_max_bytes"] = 4

    async def scenario():
        await manager.open_page("s1", "about:blank")
        with pytest.raises(ValueError, match="limit"):
            await manager.screenshot("s1")

    _run(scenario())


def test_evaluate_is_gated_by_the_config(rig):
    manager, transport, _b, _l = rig

    async def scenario():
        await manager.open_page("s1", "about:blank")
        with pytest.raises(PermissionError, match="browser_evaluate is disabled"):
            await manager.evaluate("s1", "1 + 1")
        manager.config["allow_evaluate"] = 1
        transport.handlers["Runtime.evaluate"] = lambda s, p: {"result": {"value": [1, 2]}}
        return await manager.evaluate("s1", "1 + 1")

    outcome = _run(scenario())

    assert outcome["result"] == "[1, 2]"
    assert outcome["truncated"] is False


def test_a_page_script_exception_becomes_a_cdp_error(rig):
    manager, transport, _b, _l = rig
    transport.handlers["Runtime.evaluate"] = lambda s, p: {
        "result": {},
        "exceptionDetails": {"text": "Uncaught", "exception": {"description": "boom"}},
    }

    async def scenario():
        await manager.open_page("s1", "about:blank")
        with pytest.raises(CdpError, match="page script failed"):
            await manager.snapshot("s1")

    _run(scenario())


# --------------------------------------------------------------------- events


def test_start_and_stop_screencast_plus_the_first_frame_nudge(rig):
    manager, transport, _b, _l = rig

    async def scenario():
        await manager.open_page("s1", "about:blank")
        started = await manager.start_screencast("s1")
        stopped = await manager.stop_screencast("s1")
        return started, stopped

    started, stopped = _run(scenario())

    assert started["screencasting"] is True and stopped["screencasting"] is False
    methods = [call["method"] for call in transport.calls]
    assert "Page.startScreencast" in methods and "Page.stopScreencast" in methods
    # The nudge is what forces the first frame on a static page.
    assert any("sherryPaint" in call["params"].get("expression", "") for call in transport.calls)


def test_set_viewport_drives_the_emulation_override(rig):
    manager, transport, _b, _l = rig

    async def scenario():
        await manager.open_page("s1", "about:blank")
        return await manager.set_viewport("s1", width=393, height=852, device_scale_factor=2)

    outcome = _run(scenario())

    emulation = [c for c in transport.calls if c["method"] == "Emulation.setDeviceMetricsOverride"]
    assert outcome["viewport"] == [393, 852]
    assert emulation[0]["params"]["deviceScaleFactor"] == 2


def test_screencast_frames_reach_only_their_page_subscribers(rig):
    manager, transport, _b, _l = rig
    seen: list = []

    async def scenario():
        page = await manager.open_page("s1", "about:blank")
        other = await manager.open_page("s2", "about:blank")
        manager.subscribe_frames(page, lambda p, params: seen.append((p.page_id, params)))
        transport.emit(
            {"method": "Page.screencastFrame", "sessionId": page.cdp_session, "params": {"n": 1}}
        )
        transport.emit(
            {"method": "Page.screencastFrame", "sessionId": other.cdp_session, "params": {"n": 2}}
        )
        manager.unsubscribe_frames(page, page.framers[0])
        transport.emit(
            {"method": "Page.screencastFrame", "sessionId": page.cdp_session, "params": {"n": 3}}
        )

    _run(scenario())

    assert seen == [("p1", {"n": 1})]


def test_unknown_events_are_ignored(rig):
    manager, transport, _b, _l = rig

    async def scenario():
        await manager.open_page("s1", "about:blank")
        transport.emit({"method": "Page.frameNavigated", "sessionId": "S-unknown", "params": {}})

    _run(scenario())  # must not raise


def test_a_detached_page_is_marked_dead(rig):
    manager, transport, _b, _l = rig

    async def scenario():
        page = await manager.open_page("s1", "about:blank")
        transport.emit({"method": "Target.detachedFromTarget", "sessionId": page.cdp_session})
        return page

    page = _run(scenario())

    assert page.alive is False
