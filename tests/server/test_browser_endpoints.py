"""The browser routes: the feature switch gate, the manager pass-through, errors.

The routes are a thin face over the manager, so these tests pin the contract
the panel depends on: with the feature off every route answers 404 and the
manager is never consulted; with it on, a bad request is a 400, an unknown page
a 404, and the manager's own errors surface without leaking internals.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from server.service.browser_cdp import CdpError
from server.trigger.http import browser as http

pytestmark = [pytest.mark.unit]


class _FakeRequest:
    """Minimal Robyn-request double: ``query_params`` for GET, ``json()`` for POST."""

    def __init__(self, query_params: dict | None = None, body: dict | None = None):
        self.query_params = query_params or {}
        self._body = body or {}

    def json(self) -> dict:
        """Robyn's JSON body reader (``read_body`` calls exactly this)."""
        return self._body


class _FakeManager:
    def __init__(self) -> None:
        self.opened: list[tuple[str, str]] = []
        self.navigated: list[tuple[str, str, str | None]] = []
        self.closed: list[tuple[str, str | None]] = []
        self.error: Exception | None = None

    def status(self) -> dict:
        return {"enabled": True, "running": True, "pid": 7, "pages": 1, "sessions": 1}

    async def open_page(self, session_id: str, url: str):
        if self.error:
            raise self.error
        self.opened.append((session_id, url))

        class _Page:
            def info(self) -> dict:
                return {"page": "p1", "url": url, "title": "", "session_id": session_id}

        return _Page()

    async def navigate(self, session_id: str, url: str, page_id: str | None):
        if self.error:
            raise self.error
        self.navigated.append((session_id, url, page_id))
        return {
            "page": page_id or "p1",
            "url": url,
            "title": "t",
            "session_id": session_id,
            "loaded": True,
        }

    async def close_page(self, session_id: str, page_id: str | None):
        if self.error:
            raise self.error
        self.closed.append((session_id, page_id))
        return {"page": page_id or "p1", "closed": True}


@pytest.fixture
def on(monkeypatch):
    """Feature ON with a fake manager wired in."""
    manager = _FakeManager()
    monkeypatch.setitem(http.BROWSER_AGENT, "enabled", 1)
    monkeypatch.setattr(http, "get_browser_manager", lambda: manager)
    return manager


@pytest.fixture
def off(monkeypatch):
    """Feature OFF (the default install)."""
    monkeypatch.setitem(http.BROWSER_AGENT, "enabled", 0)
    monkeypatch.setattr(
        http, "get_browser_manager", lambda: pytest.fail("the manager must not be consulted")
    )


def _payload(response) -> dict:
    return json.loads(response.description)


def test_every_route_is_404_while_disabled(off):
    status = asyncio.run(http.browser_status_handler(_FakeRequest()))
    page = asyncio.run(http.browser_page_handler(_FakeRequest(body={"session_id": "s1"})))
    navigate = asyncio.run(
        http.browser_navigate_handler(_FakeRequest(body={"session_id": "s1", "url": "https://x"}))
    )
    close = asyncio.run(http.browser_close_handler(_FakeRequest(body={"session_id": "s1"})))

    assert [status.status_code, page.status_code, navigate.status_code, close.status_code] == [
        404,
        404,
        404,
        404,
    ]
    assert "disabled" in _payload(status)["message"]


def test_status_reports_the_manager(on):
    response = asyncio.run(http.browser_status_handler(_FakeRequest()))

    assert response.status_code == 200
    body = _payload(response)
    assert body["running"] is True and body["pages"] == 1
    assert "port" not in body


def test_the_page_route_opens_a_page(on):
    response = asyncio.run(
        http.browser_page_handler(_FakeRequest(body={"session_id": "s1", "url": "https://a.test"}))
    )

    assert response.status_code == 200
    assert _payload(response)["page"] == "p1"
    assert on.opened == [("s1", "https://a.test")]


def test_the_page_route_defaults_to_about_blank_and_needs_a_session(on):
    blank = asyncio.run(http.browser_page_handler(_FakeRequest(body={"session_id": "s1"})))
    assert _payload(blank)["url"] == "about:blank"
    missing = asyncio.run(http.browser_page_handler(_FakeRequest(body={})))
    assert missing.status_code == 400


def test_navigate_maps_errors(on):
    ok = asyncio.run(
        http.browser_navigate_handler(
            _FakeRequest(body={"session_id": "s1", "url": "https://a.test", "page": "p1"})
        )
    )
    assert ok.status_code == 200 and _payload(ok)["loaded"] is True
    assert on.navigated == [("s1", "https://a.test", "p1")]

    missing_session = asyncio.run(
        http.browser_navigate_handler(_FakeRequest(body={"url": "https://a.test"}))
    )
    assert missing_session.status_code == 400

    on.error = ValueError("url is required")
    bad = asyncio.run(
        http.browser_navigate_handler(_FakeRequest(body={"session_id": "s1", "url": ""}))
    )
    assert bad.status_code == 400 and _payload(bad)["message"] == "url is required"

    on.error = KeyError("unknown page 'p9'")
    absent = asyncio.run(
        http.browser_navigate_handler(
            _FakeRequest(body={"session_id": "s1", "url": "https://a.test", "page": "p9"})
        )
    )
    assert absent.status_code == 404

    # A transport failure is a 5xx that names no internals.
    on.error = CdpError("Page.navigate: the CDP connection closed")
    broken = asyncio.run(
        http.browser_navigate_handler(
            _FakeRequest(body={"session_id": "s1", "url": "https://a.test"})
        )
    )
    assert broken.status_code == 500
    assert _payload(broken)["message"] == "CdpError"


def test_close_passes_the_page_through(on):
    response = asyncio.run(
        http.browser_close_handler(_FakeRequest(body={"session_id": "s1", "page": "p2"}))
    )

    assert response.status_code == 200 and _payload(response)["closed"] is True
    assert on.closed == [("s1", "p2")]
