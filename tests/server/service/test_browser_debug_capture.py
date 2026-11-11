"""Passive browser debug capture: console, exceptions, failed requests, buffers.

The manager is exercised directly (no Chromium): canned CDP events go through
the same sink the transport calls, so the wire shapes and filters are pinned
without a browser process.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from config.features import BROWSER_AGENT
from server.service.browser_manager import BrowserManager, BrowserPage

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _manager(**overrides: Any) -> BrowserManager:
    config = {
        **BROWSER_AGENT,
        "enabled": 1,
        "debug_capture": 1,
        "debug_buffer_max": 5,
        "op_timeout_s": 0.05,
        **overrides,
    }
    return BrowserManager(config=config)


def _page(manager: BrowserManager, **overrides: Any) -> BrowserPage:
    page = BrowserPage(
        page_id="p1",
        target_id="t1",
        cdp_session="s1",
        session_id="sess",
        **overrides,
    )
    manager._pages[page.page_id] = page
    return page


def test_console_arguments_become_readable_text():
    manager = _manager()
    page = _page(manager)

    manager._debug_sink(
        page,
        "Runtime.consoleAPICalled",
        {
            "type": "error",
            "args": [
                {"type": "string", "value": "boom"},
                {"type": "object", "description": "Object"},
                {"type": "number", "unserializableValue": "NaN"},
            ],
        },
    )

    event = page.console_events[-1]
    assert event["level"] == "error"
    assert event["kind"] == "console"
    assert event["text"] == "boom Object NaN"


def test_uncaught_exception_carries_the_stack():
    manager = _manager()
    page = _page(manager)

    manager._debug_sink(
        page,
        "Runtime.exceptionThrown",
        {
            "exceptionDetails": {
                "text": "Uncaught",
                "url": "app.js",
                "lineNumber": 9,
                "columnNumber": 4,
                "exception": {"type": "object", "description": "ReferenceError: foo"},
                "stackTrace": {
                    "callFrames": [
                        {
                            "functionName": "handler",
                            "url": "app.js",
                            "lineNumber": 8,
                            "columnNumber": 1,
                        },
                        {"functionName": "", "url": "app.js", "lineNumber": 3, "columnNumber": 2},
                    ]
                },
            }
        },
    )

    event = page.console_events[-1]
    assert event["kind"] == "exception"
    assert event["level"] == "error"
    assert "ReferenceError: foo" in event["text"]
    assert event["line"] == 10 and event["column"] == 5
    assert event["stack"][0] == {"function": "handler", "url": "app.js", "line": 9, "column": 2}


def test_browser_log_entries_are_level_mapped():
    manager = _manager()
    page = _page(manager)

    manager._debug_sink(
        page,
        "Log.entryAdded",
        {
            "entry": {
                "level": "warning",
                "text": "deprecated",
                "source": "deprecation",
                "lineNumber": 2,
            }
        },
    )

    event = page.console_events[-1]
    assert event["kind"] == "log"
    assert event["level"] == "warn"
    assert event["line"] == 3


def test_only_failed_responses_are_buffered():
    manager = _manager()
    page = _page(manager)
    manager._debug_sink(
        page,
        "Network.requestWillBeSent",
        {"requestId": "r1", "request": {"url": "https://x.test/api", "method": "POST"}},
    )

    manager._debug_sink(
        page,
        "Network.responseReceived",
        {
            "requestId": "r1",
            "type": "Fetch",
            "response": {
                "url": "https://x.test/api",
                "status": 200,
                "mimeType": "application/json",
            },
        },
    )
    assert page.network_events == []

    manager._debug_sink(
        page,
        "Network.responseReceived",
        {
            "requestId": "r1",
            "type": "Fetch",
            "response": {"url": "https://x.test/api", "status": 404, "mimeType": "text/html"},
        },
    )
    event = page.network_events[-1]
    assert event["status"] == 404
    assert event["method"] == "POST"
    assert event["url"] == "https://x.test/api"


def test_loading_failed_names_the_request_url():
    manager = _manager()
    page = _page(manager)
    manager._debug_sink(
        page,
        "Network.requestWillBeSent",
        {"requestId": "r2", "request": {"url": "https://x.test/missing", "method": "GET"}},
    )

    manager._debug_sink(
        page,
        "Network.loadingFailed",
        {"requestId": "r2", "type": "Fetch", "errorText": "net::ERR_CONNECTION_REFUSED"},
    )

    event = page.network_events[-1]
    assert event["kind"] == "failure"
    assert event["url"] == "https://x.test/missing"
    assert "ERR_CONNECTION_REFUSED" in event["error"]


def test_buffers_are_capped():
    manager = _manager(debug_buffer_max=3)
    page = _page(manager)

    for index in range(10):
        manager._debug_sink(
            page,
            "Runtime.consoleAPICalled",
            {"type": "log", "args": [{"type": "string", "value": str(index)}]},
        )

    assert [event["text"] for event in page.console_events] == ["7", "8", "9"]


def test_main_frame_navigation_clears_but_a_subframe_does_not():
    manager = _manager()
    page = _page(manager)
    manager._debug_sink(
        page,
        "Runtime.consoleAPICalled",
        {"type": "log", "args": [{"type": "string", "value": "x"}]},
    )

    manager._debug_sink(page, "Page.frameNavigated", {"frame": {"id": "child", "parentId": "root"}})
    assert len(page.console_events) == 1

    manager._debug_sink(page, "Page.frameNavigated", {"frame": {"id": "root"}})
    assert page.console_events == []
    assert page.network_events == []


def test_console_events_filter_and_cap():
    manager = _manager()
    page = _page(manager)
    for level, text in (("error", "e1"), ("info", "i1"), ("error", "e2")):
        manager._debug_sink(
            page,
            "Runtime.consoleAPICalled",
            {"type": level, "args": [{"type": "string", "value": text}]},
        )

    outcome = manager.console_events("sess", level="error")

    assert outcome["enabled"] is True
    assert [event["text"] for event in outcome["events"]] == ["e1", "e2"]
    assert outcome["buffered"] == 3


def test_network_events_filters():
    manager = _manager()
    page = _page(manager)
    manager._debug_sink(
        page,
        "Network.responseReceived",
        {"requestId": "a", "response": {"url": "https://x.test/a", "status": 404}},
    )
    manager._debug_sink(
        page,
        "Network.responseReceived",
        {"requestId": "b", "response": {"url": "https://x.test/b", "status": 500}},
    )
    manager._debug_sink(page, "Network.loadingFailed", {"requestId": "c", "errorText": "refused"})

    only_500 = manager.network_events("sess", min_status=500, include_failures=False)
    assert [request["status"] for request in only_500["requests"]] == [500]

    without_failures = manager.network_events("sess", min_status=0, include_failures=False)
    assert [request["url"] for request in without_failures["requests"]] == [
        "https://x.test/a",
        "https://x.test/b",
    ]


def test_capture_disabled_reports_not_enabled():
    manager = _manager(debug_capture=0)
    _page(manager)

    outcome = manager.console_events("sess")

    assert outcome["enabled"] is False
    assert outcome["events"] == []


def test_enable_debug_capture_calls_the_three_domains_once():
    class _FakeTransport:
        connected = True

        def __init__(self) -> None:
            self.calls: list[str] = []

        async def call(self, method: str, session: str | None = None, **params: Any):
            self.calls.append(method)
            return {}

    manager = _manager()
    page = _page(manager)
    transport = _FakeTransport()
    manager._transport = transport

    asyncio.run(manager._enable_debug_capture(page))
    asyncio.run(manager._enable_debug_capture(page))

    assert transport.calls == ["Runtime.enable", "Network.enable", "Log.enable"]
    assert page.debug_capture is True
