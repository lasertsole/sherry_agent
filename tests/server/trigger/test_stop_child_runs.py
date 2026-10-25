"""User stop propagates to the session's own subagents, not other sessions."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from server.trigger.ws import messages as ws_messages

pytestmark = [pytest.mark.unit]


@pytest.mark.asyncio
async def test_stop_child_runs_uses_the_requester_key(monkeypatch):
    calls: list[dict] = []

    def _list(requester_key: str):
        assert requester_key == "agent:main:session:sess-a"
        return [SimpleNamespace(run_id="run-1"), SimpleNamespace(run_id="run-2")]

    async def _kill(run_id: str, **kwargs):
        calls.append({"run_id": run_id, **kwargs})
        return SimpleNamespace(run_id=run_id)

    monkeypatch.setattr("agent.tools.subagent.control.kill.list_killable_children", _list)
    monkeypatch.setattr("agent.tools.subagent.control.kill.kill_subagent_run_with_cascade", _kill)

    killed = await ws_messages._stop_child_runs("sess-a")

    assert killed == 2
    assert {c["run_id"] for c in calls} == {"run-1", "run-2"}
    assert all(c["reason"] == "user_stop" for c in calls)
    assert all(c["cascade"] is True for c in calls)
    assert all(c["requester_session_key"] == "agent:main:session:sess-a" for c in calls)


@pytest.mark.asyncio
async def test_stop_child_runs_is_fail_open(monkeypatch):
    def _boom(_requester_key: str):
        raise RuntimeError("registry down")

    monkeypatch.setattr("agent.tools.subagent.control.kill.list_killable_children", _boom)

    assert await ws_messages._stop_child_runs("sess-b") == 0
