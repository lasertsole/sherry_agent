"""HTTP boundary tests for ``GET /subagents/runs``.

The handler accepts either a ``run_id`` (one run + its subtree) or a
``session_id`` (every run visible to that session). Omitting both used to raise
``ValueError`` inside the route, which the global exception handler turned into a
500 plus a full traceback in the error log for what is really a client contract
error — the handler now answers the same structured 400 its POST/DELETE siblings
use. Both branches return the same ``{"runs": [...]}`` payload shape.
"""

from __future__ import annotations

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

from server.trigger.http import subagent as subagent_http

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]


class _FakeRequest:
    def __init__(self, query_params: dict | None = None):
        self.query_params = query_params or {}


class _FakeBodyRequest:
    def __init__(self, body: dict):
        self._body = body

    def json(self) -> dict:
        return self._body


def _payload(response) -> dict:
    """Decode a Robyn Response body (its `description` carries the JSON)."""
    return json.loads(response.description)


def test_missing_session_and_run_id_is_a_structured_400():
    response = asyncio.run(subagent_http.get_subagent_runs_handler(_FakeRequest()))
    assert response.status_code == 400
    body = _payload(response)
    assert body["success"] is False
    assert body["message"] == "session_id is required"


def test_session_scoped_query_canonicalizes_a_bare_session_id(monkeypatch):
    """A bare id and its prefixed form name the same session.

    The browser sends the bare id; the registry matches on the prefixed
    ``agent:main:session:{id}`` key. Without canonicalization the query returns
    an empty list for every caller that only has the bare id.
    """
    seen: list[str] = []

    def fake_list_readonly(session_id: str):
        seen.append(session_id)
        return []

    monkeypatch.setattr(subagent_http, "list_descendant_runs_readonly", fake_list_readonly)
    response = asyncio.run(
        subagent_http.get_subagent_runs_handler(_FakeRequest({"session_id": "default"}))
    )

    assert seen == ["agent:main:session:default"]
    # Robyn's route wrapper serializes the returned dict into a 200 JSON body.
    assert int(response.status_code) == 200
    assert _payload(response) == {"runs": []}


def test_an_already_prefixed_session_id_is_passed_through(monkeypatch):
    seen: list[str] = []

    def fake_list_readonly(session_id: str):
        seen.append(session_id)
        return []

    monkeypatch.setattr(subagent_http, "list_descendant_runs_readonly", fake_list_readonly)
    asyncio.run(
        subagent_http.get_subagent_runs_handler(
            _FakeRequest({"session_id": "agent:main:session:default"})
        )
    )

    assert seen == ["agent:main:session:default"]


def test_controller_scope_reads_the_controller_view(monkeypatch):
    seen: list[str] = []
    monkeypatch.setattr(
        subagent_http,
        "list_runs_for_controller_readonly",
        lambda session_id: seen.append(session_id) or [],
    )
    response = asyncio.run(
        subagent_http.get_subagent_runs_handler(
            _FakeRequest({"session_id": "default", "scope": "controller"})
        )
    )

    assert seen == ["agent:main:session:default"]
    assert int(response.status_code) == 200
    assert _payload(response) == {"runs": []}


def test_post_handler_rejects_missing_task_as_structured_400():
    response = asyncio.run(
        subagent_http.post_subagent_run_handler(_FakeBodyRequest({"session_id": "s"}))
    )
    assert response.status_code == 400
    assert _payload(response)["success"] is False


def test_post_handler_dispatches_off_the_event_loop(monkeypatch):
    """POST /subagents/runs must run the sync delegate_task on a worker thread.

    delegate_task owns its own event loop (asyncio.run / run_until_complete on a
    fresh loop); called directly on the handler's loop thread it would always
    raise RuntimeError (→ 500) — regression coverage for the to_thread offload.
    """
    calls: dict = {}

    def fake_delegate_task(**kwargs):
        try:
            asyncio.get_running_loop()
            calls["off_loop"] = False
        except RuntimeError:
            calls["off_loop"] = True
        calls["thread"] = threading.current_thread()
        calls["kwargs"] = kwargs
        return SimpleNamespace(
            accepted=True, run_id="run-1", to_dict=lambda: {"status": "accepted"}
        )

    monkeypatch.setattr(subagent_http, "delegate_task", fake_delegate_task)
    monkeypatch.setattr(subagent_http, "get_run", lambda run_id: None)

    response = asyncio.run(
        subagent_http.post_subagent_run_handler(
            _FakeBodyRequest({"task": "demo task", "session_id": "agent:main:session:s"})
        )
    )

    assert int(response.status_code) == 200
    assert _payload(response) == {"handle": {"status": "accepted"}}
    assert calls["off_loop"] is True
    assert calls["thread"] is not threading.main_thread()
    assert calls["kwargs"]["requester_session_key"] == "agent:main:session:s"
