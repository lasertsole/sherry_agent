"""``auto_kill_on_detach``: a failed flow must not leave children running.

A failed flow can never collect a child's result, so a detached child only holds
a SUBAGENT lane slot and keeps writing to the tree. The kill is per-child
fail-open: an unknown key is skipped, and a raise never turns the failure answer
into an error.
"""

from __future__ import annotations

import importlib
from types import SimpleNamespace

import pytest

from config.features import TASKFLOW_INFRA

# The package re-exports a TOOL named ``taskflow_finish``; the module is what
# carries the helper.
taskflow_finish = importlib.import_module("agent.tools.taskflow.tools.taskflow_finish")

pytestmark = [pytest.mark.unit]


@pytest.fixture()
def kills(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    killed: list[str] = []

    async def _fake_kill(run_id: str, reason: str = "") -> list:
        killed.append(run_id)
        return []

    import agent.tools.subagent.control.kill as kill_mod
    import agent.tools.subagent.registry.queries as queries_mod

    monkeypatch.setattr(kill_mod, "kill_subagent_run_with_cascade", _fake_kill)
    monkeypatch.setattr(
        queries_mod,
        "get_run_by_child_session_key",
        lambda key: SimpleNamespace(run_id=f"run-{key}"),
    )
    return killed


@pytest.mark.asyncio
async def test_every_detached_child_is_killed(kills: list[str]):
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setitem(TASKFLOW_INFRA, "auto_kill_on_detach", True)
    try:
        await taskflow_finish._kill_detached_children(["a", "b"])
    finally:
        monkeypatch.undo()

    assert kills == ["run-a", "run-b"]


@pytest.mark.asyncio
async def test_the_switch_off_leaves_them_running(kills: list[str]):
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setitem(TASKFLOW_INFRA, "auto_kill_on_detach", False)
    try:
        await taskflow_finish._kill_detached_children(["a"])
    finally:
        monkeypatch.undo()

    assert kills == []
