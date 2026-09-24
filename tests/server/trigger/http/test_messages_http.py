"""Tests for server/trigger/http/messages.py — history pagination validation.

Drives the ``GET /get_history_by_turn_page`` handler directly (no Robyn
server started). The module is registered on the real app, so the handler
object is Robyn's wrapped surface: a raised ``ValueError`` comes back as a
500 ``Response`` through the global exception funnel. ``turn_page_size`` is
capped at the HTTP boundary so an unbounded page request can never reach
the store.
"""

import asyncio

import pytest

from server.trigger.http import messages as messages_http

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]


class _FakeRequest:
    def __init__(self, query_params: dict):
        self.query_params = query_params


def _call_handler(query: dict):
    return asyncio.run(messages_http.get_history_by_turn_page(_FakeRequest(query)))


def _recording_stub(calls: list):
    def _stub(*args):
        calls.append(args)
        return []

    return _stub


def test_turn_page_size_over_cap_rejected(monkeypatch):
    calls: list = []
    monkeypatch.setattr(messages_http, "_get_history_by_turn_page", _recording_stub(calls))

    response = _call_handler(
        {
            "session_id": "s1",
            "min_turn_num": "1",
            "turn_page_size": "201",
            "turn_page_num": "1",
        }
    )

    assert calls == []
    assert int(response.status_code) == 500
    assert "turn_page_size must be <= 200" in response.description


def test_turn_page_size_at_cap_allowed(monkeypatch):
    calls: list = []
    monkeypatch.setattr(messages_http, "_get_history_by_turn_page", _recording_stub(calls))

    _call_handler(
        {
            "session_id": "s1",
            "min_turn_num": "1",
            "turn_page_size": "200",
            "turn_page_num": "1",
        }
    )

    assert calls == [("s1", "1", "200", "1")]


class _FakeJsonRequest:
    def __init__(self, payload: dict):
        self._payload = payload

    def json(self) -> dict:
        return self._payload


def _stub_clear_session(calls: list):
    async def _stub(**kwargs):
        calls.append(kwargs)

    return _stub


def test_clear_session_rejects_traversal_session_id(monkeypatch):
    """The traversal id dies at the boundary; the DAO is never called."""
    calls: list = []
    monkeypatch.setattr(messages_http, "clear_session", _stub_clear_session(calls))

    response = asyncio.run(
        messages_http.clear_session_handler(_FakeJsonRequest({"session_id": "../../workspace"}))
    )

    assert calls == []
    assert int(response.status_code) == 500
    assert "invalid session_id" in response.description


def test_clear_session_rejects_none_session_id(monkeypatch):
    calls: list = []
    monkeypatch.setattr(messages_http, "clear_session", _stub_clear_session(calls))

    response = asyncio.run(messages_http.clear_session_handler(_FakeJsonRequest({})))

    assert calls == []
    assert int(response.status_code) == 500


def test_clear_session_passes_valid_session_id(monkeypatch):
    calls: list = []
    monkeypatch.setattr(messages_http, "clear_session", _stub_clear_session(calls))

    asyncio.run(messages_http.clear_session_handler(_FakeJsonRequest({"session_id": "s1"})))

    assert calls == [{"session_id": "s1"}]
