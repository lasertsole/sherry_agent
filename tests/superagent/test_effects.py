"""Effects queue tests."""

from __future__ import annotations

import pytest

from superagent.app.effects import Effects, Later
from superagent.app.store import Store
from superagent.infra.health import HealthBoard

FUTURE = "2099-01-01T00:00:00+00:00"


@pytest.mark.asyncio
async def test_enqueue_coalesces_without_changing_order(tmp_path) -> None:
    store = Store(tmp_path / "orchestrator.sqlite", tmp_path)
    effects = Effects(store, HealthBoard())
    effects.enqueue("reply", "msg-1", {"seq": 1})
    first = store.effects.get("reply", "msg-1")
    effects.enqueue("reply", "msg-1", {"seq": 2})
    second = store.effects.get("reply", "msg-1")
    assert first is not None and second is not None
    assert second.seq == first.seq
    assert second.payload == {"seq": 2}
    assert second.due_at == ""
    store.close()


@pytest.mark.asyncio
async def test_run_once_success_and_failure_backoff(tmp_path) -> None:
    store = Store(tmp_path / "orchestrator.sqlite", tmp_path)
    board = HealthBoard()
    effects = Effects(store, board)
    calls: list[dict] = []

    async def ok(payload: dict) -> None:
        calls.append(payload)

    effects.handle("notice", ok)
    effects.enqueue("notice", "n1", {"text": "hello"})
    assert await effects.run_once("notice") == 1
    assert calls == [{"text": "hello"}]
    assert store.effects.get("notice", "n1").status == "done"

    async def fail(payload: dict) -> None:
        raise RuntimeError("boom")

    effects.handle("launch", fail, retry=(2, 60))
    for index in range(3):
        effects.enqueue("launch", f"l{index}", {})
    await effects.run_once("launch", FUTURE)
    entry = board.all().get("launch")
    assert entry is not None and entry["state"] == "error"
    assert all((store.effects.get("launch", f"l{index}").attempts == 1) for index in range(3))
    store.close()


@pytest.mark.asyncio
async def test_later_does_not_count_as_failure(tmp_path) -> None:
    store = Store(tmp_path / "orchestrator.sqlite", tmp_path)
    board = HealthBoard()
    effects = Effects(store, board)

    async def later(payload: dict) -> None:
        raise Later()

    effects.handle("delivery", later)
    effects.enqueue("delivery", "d1", {})
    await effects.run_once("delivery", FUTURE)
    row = store.effects.get("delivery", "d1")
    assert row is not None and row.attempts == 0
    assert row.due_at
    assert board.all().get("delivery") is None
    store.close()


@pytest.mark.asyncio
async def test_pending_and_idle(tmp_path) -> None:
    store = Store(tmp_path / "orchestrator.sqlite", tmp_path)
    effects = Effects(store, HealthBoard())
    assert effects.idle() is True
    effects.enqueue("notice", "n1", {})
    assert effects.pending() == 1
    assert effects.idle() is False
    store.close()
