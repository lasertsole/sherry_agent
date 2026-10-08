"""Clearing a session also releases its browser pages.

The page registry is keyed by session and nothing else would ever close those
pages (the LRU only caps the process-wide count), so ``clear_session`` asks the
browser manager first — best-effort: a dead browser must never block the clear.
"""

from __future__ import annotations

import asyncio

import pytest

from server.service import browser_manager as bm
from server.service import messages as service

pytestmark = [pytest.mark.unit]


class _RecordingManager:
    def __init__(self, boom: bool = False) -> None:
        self.closed: list[str] = []
        self._boom = boom

    async def close_session(self, session_id: str):
        if self._boom:
            raise RuntimeError("the browser is gone")
        self.closed.append(session_id)
        return {"session_id": session_id, "closed": 1}


def _wire(monkeypatch, manager) -> list[str]:
    cleared: list[str] = []

    async def fake_dao(session_id: str) -> None:
        cleared.append(session_id)

    monkeypatch.setattr(bm, "get_browser_manager", lambda: manager)
    monkeypatch.setattr(service, "clear_session_dao", fake_dao)
    return cleared


def test_clear_session_releases_the_pages_then_wipes(monkeypatch):
    manager = _RecordingManager()
    cleared = _wire(monkeypatch, manager)

    asyncio.run(service.clear_session("s1"))

    assert manager.closed == ["s1"]
    assert cleared == ["s1"]


def test_a_broken_browser_does_not_block_the_clear(monkeypatch):
    manager = _RecordingManager(boom=True)
    cleared = _wire(monkeypatch, manager)

    asyncio.run(service.clear_session("s1"))

    assert cleared == ["s1"]
