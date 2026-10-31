"""SWE-bench teardown: quiesce the writers before the harness touches the tree."""

from __future__ import annotations

from pathlib import Path

import pytest

from evals.swebench import teardown

pytestmark = [pytest.mark.unit]


@pytest.fixture()
def _fake_runtime(monkeypatch):
    import agent.tools.pub_base.shadow_git as shadow_git
    import agent.tools.subagent.control.kill as kill_mod
    from types import SimpleNamespace
    import agent.tools.subagent.registry.queries as queries
    import agent.tools.terminal_bg_registry as bg
    import server.service.turn_runner as turn_runner

    state = {"descendants": 1, "background": 1, "frozen": 0, "killed": []}

    async def _cancel(_sid: str) -> None:
        return None

    async def _kill(run_id: str, reason: str = "", **_kwargs) -> list:
        state["killed"].append((run_id, reason))
        state["descendants"] = 0
        return []

    monkeypatch.setattr(turn_runner, "cancel_session_turn", _cancel)
    monkeypatch.setattr(
        kill_mod,
        "list_killable_children",
        lambda _key: [SimpleNamespace(run_id="run-1", controller_session_key=None)],
    )
    monkeypatch.setattr(kill_mod, "kill_subagent_run_with_cascade", _kill)
    monkeypatch.setattr(queries, "count_active_descendant_runs", lambda _key: state["descendants"])
    monkeypatch.setattr(
        bg.background_registry,
        "kill_session_tasks",
        lambda _sid: state.__setitem__("background", 0) or 1,
    )
    monkeypatch.setattr(bg.background_registry, "running_count", lambda _sid: state["background"])
    monkeypatch.setattr(shadow_git, "wait_for_idle", lambda *_a, **_k: True)
    monkeypatch.setattr(
        shadow_git, "freeze", lambda _root: state.__setitem__("frozen", state["frozen"] + 1)
    )
    monkeypatch.setattr(shadow_git, "active_writers", lambda _root: 0)
    return state


@pytest.mark.asyncio
async def test_quiesce_stops_descendants_background_and_freezes_shadow(
    tmp_path: Path, _fake_runtime
):
    result = await teardown.quiesce_session("sess-1", tmp_path, timeout_s=2)

    assert result["ok"] is True
    assert result["timed_out"] is False
    assert result["active_descendants"] == 0
    assert result["background_running"] == 0
    assert result["shadow_writers"] == 0
    assert _fake_runtime["killed"], "the descendant kill cascade was not called"
    assert _fake_runtime["frozen"] == 1


@pytest.mark.asyncio
async def test_quiesce_timeout_is_reported_not_raised(tmp_path: Path, _fake_runtime, monkeypatch):
    import agent.tools.subagent.registry.queries as queries

    monkeypatch.setattr(queries, "count_active_descendant_runs", lambda _key: 1)

    result = await teardown.quiesce_session("sess-2", tmp_path, timeout_s=0.1)

    assert result["ok"] is False
    assert result["timed_out"] is True
    assert result["active_descendants"] == 1
