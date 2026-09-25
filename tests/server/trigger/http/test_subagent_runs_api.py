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

import pytest

from server.trigger.http import subagent as subagent_http

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]


class _FakeRequest:
    def __init__(self, query_params: dict | None = None):
        self.query_params = query_params or {}


def _payload(response) -> dict:
    """Decode a Robyn Response body (its `description` carries the JSON)."""
    return json.loads(response.description)


def test_missing_session_and_run_id_is_a_structured_400():
    response = asyncio.run(subagent_http.get_subagent_runs_handler(_FakeRequest()))
    assert response.status_code == 400
    body = _payload(response)
    assert body["success"] is False
    assert body["message"] == "session_id is required"


def test_session_scoped_query_returns_the_runs_payload(monkeypatch):
    seen: list[str] = []

    def fake_list_readonly(session_id: str):
        seen.append(session_id)
        return []

    monkeypatch.setattr(subagent_http, "list_descendant_runs_readonly", fake_list_readonly)
    response = asyncio.run(
        subagent_http.get_subagent_runs_handler(_FakeRequest({"session_id": "default"}))
    )

    assert seen == ["default"]
    # Robyn's route wrapper serializes the returned dict into a 200 JSON body.
    assert int(response.status_code) == 200
    assert _payload(response) == {"runs": []}


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

    assert seen == ["default"]
    assert int(response.status_code) == 200
    assert _payload(response) == {"runs": []}
