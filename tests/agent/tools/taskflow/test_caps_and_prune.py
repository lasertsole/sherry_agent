"""Per-session flow ceilings and prune debounce (orphan-prevention plan §1.9)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agent.tools.taskflow import build_taskflow_tools
from agent.tools.taskflow import progress_push
from agent.tools.taskflow.registry import store_sqlite

pytestmark = [pytest.mark.unit]

SESSION = "sess-caps"


@pytest.mark.asyncio
async def test_hard_cap_blocks_admission(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    from config.features import TASKFLOW_INFRA

    monkeypatch.setitem(TASKFLOW_INFRA, "max_total_flows_hard_cap", 2)
    monkeypatch.setitem(TASKFLOW_INFRA, "max_flows_per_session", 20)
    tools = {t.name: t for t in build_taskflow_tools()}

    assert "TaskFlow created" in await tools["taskflow_create"].coroutine(
        session_id=SESSION, flow_id="flow-1"
    )
    assert "TaskFlow created" in await tools["taskflow_create"].coroutine(
        session_id=SESSION, flow_id="flow-2"
    )

    out = await tools["taskflow_create"].coroutine(session_id=SESSION, flow_id="flow-3")

    assert "hard cap" in out
    assert await store_sqlite.get_flow("flow-3", SESSION) is None


@pytest.mark.asyncio
async def test_prune_is_debounced_per_session(isolated_db: Path, monkeypatch: pytest.MonkeyPatch):
    from config.features import TASKFLOW_INFRA

    monkeypatch.setitem(TASKFLOW_INFRA, "prune_debounce_seconds", 60.0)
    progress_push._PRUNE_DEBOUNCE.clear()
    calls: list[str] = []

    async def _fake_prune(session_id: str) -> int:
        calls.append(session_id)
        return 0

    monkeypatch.setattr(store_sqlite, "prune_session_flows", _fake_prune)

    await progress_push.push_taskflow_progress(SESSION)
    await progress_push.push_taskflow_progress(SESSION)
    await progress_push.push_taskflow_progress(SESSION)

    assert calls == [SESSION]
