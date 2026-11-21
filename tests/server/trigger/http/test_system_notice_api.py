"""POST /sessions/notice: the external (no-websocket) push entry."""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.trigger.http import system_notice as notice_http

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


class _FakeRequest:
    def __init__(self, body: dict | None):
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


@pytest.fixture
def pushed(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    captured: list[dict[str, Any]] = []

    async def fake_push(session_id: str, **kwargs: Any) -> dict[str, Any]:
        captured.append({"session_id": session_id, **kwargs})
        return {
            "title": kwargs.get("title", ""),
            "level": kwargs.get("level", "info"),
            "content": kwargs.get("content", ""),
            "source": kwargs.get("source", ""),
        }

    monkeypatch.setattr(notice_http, "push_system_notice", fake_push)
    return captured


async def test_valid_push_fans_out(pushed: list[dict[str, Any]]):
    response = await notice_http.push_system_notice_handler(
        _FakeRequest(
            {
                "session_id": "s1",
                "title": "Deploy done",
                "content": "v2 live",
                "level": "success",
                "source": "ci",
            }
        )
    )

    import json

    payload = json.loads(response.description)
    assert payload["success"] is True
    assert payload["notice"]["title"] == "Deploy done"
    assert pushed == [
        {
            "session_id": "s1",
            "title": "Deploy done",
            "content": "v2 live",
            "level": "success",
            "source": "ci",
        }
    ]


async def test_missing_session_or_title_refused(pushed: list[dict[str, Any]]):
    no_session = await notice_http.push_system_notice_handler(_FakeRequest({"title": "t"}))
    assert no_session.status_code == 400
    no_title = await notice_http.push_system_notice_handler(_FakeRequest({"session_id": "s"}))
    assert no_title.status_code == 400
    assert pushed == []


async def test_unknown_level_refused(pushed: list[dict[str, Any]]):
    response = await notice_http.push_system_notice_handler(
        _FakeRequest({"session_id": "s", "title": "t", "level": "nope"})
    )
    assert response.status_code == 400
    assert pushed == []


async def test_defaults_fill_in(pushed: list[dict[str, Any]]):
    response = await notice_http.push_system_notice_handler(
        _FakeRequest({"session_id": "s", "title": "t"})
    )
    payload = json.loads(response.description)
    assert payload["notice"]["level"] == "info"
    assert payload["notice"]["source"] == "external"
