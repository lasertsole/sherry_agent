"""GET /sessions/{id}/trajectory — the ledger's read face (Robyn handler driven directly)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from server.service import trajectory_store as ts
from server.trigger.http import trajectory as api

pytestmark = [pytest.mark.module]


@pytest.fixture()
def store(tmp_path: Path, monkeypatch):
    # The route module resolves the store through ITS OWN bound name, so the
    # seam is there (patching the service module would not reach it).
    fresh = ts.TrajectoryStore(tmp_path / "trajectory.db")
    monkeypatch.setattr(api, "get_trajectory_store", lambda: fresh)
    return fresh


def _request(query: dict | None = None) -> SimpleNamespace:
    return SimpleNamespace(query_params=query or {})


async def _call(store, session_id: str, query: dict | None = None) -> dict:
    response = await api.get_session_trajectory_handler(_request(query), {"session_id": session_id})
    return json.loads(response.description)


class TestTrajectoryRoute:
    @pytest.mark.asyncio
    async def test_returns_the_turns_events_in_order(self, store):
        await store.append(
            "s1",
            "t1",
            "tool_start",
            {"type": "tool_start", "content": "read_file", "tool_id": "c1", "args": {"p": "a"}},
            seq=1,
        )
        await store.append(
            "s1",
            "t1",
            "tool_end",
            {"type": "tool_result", "tool_id": "c1", "tool_name": "read_file", "duration_ms": 4},
            seq=2,
        )

        payload = await _call(store, "s1", {"turn_id": "t1"})

        assert payload["session_id"] == "s1"
        assert [event["kind"] for event in payload["events"]] == ["tool_start"]
        assert payload["events"][0]["payload"]["duration_ms"] == 4

    @pytest.mark.asyncio
    async def test_unknown_session_is_an_empty_list_not_an_error(self, store):
        payload = await _call(store, "nope")

        assert payload["events"] == []

    @pytest.mark.asyncio
    async def test_bad_session_id_is_rejected(self, store):
        response = await api.get_session_trajectory_handler(
            _request(), {"session_id": "../etc/passwd"}
        )

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_bad_limit_is_rejected(self, store):
        response = await api.get_session_trajectory_handler(
            _request({"limit": "lots"}), {"session_id": "s1"}
        )

        assert response.status_code == 400

    @pytest.mark.asyncio
    async def test_limit_is_capped(self, store, monkeypatch):
        seen: dict = {}

        async def _read(session_id, *, turn_id=None, limit=50, offset=0):
            seen.update(limit=limit, offset=offset)
            return []

        monkeypatch.setattr(store, "read", _read)

        await _call(store, "s1", {"limit": "9999", "offset": "-5"})

        assert seen == {"limit": 500, "offset": 0}
