"""TDD tests for audit issue #38 — message serialization on the event loop.

``add_messages`` used to build every row (up to 5 ``json.dumps`` per message)
and run its INSERT loop directly inside the coroutine, stalling the event loop
for the whole turn. The fix offloads the synchronous persistence core to a
worker thread via ``asyncio.to_thread``.

Contract pinned here:

1. The persistence core runs on a non-loop thread.
2. The event loop stays responsive while a turn is being persisted.
3. The observable result (rows, decoded content) is unchanged.
"""

import asyncio
import contextlib
import threading
import time

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from context_engine.store import add_messages
from context_engine.store import core as store_core
from context_engine.store import db as store_db

pytestmark = [pytest.mark.unit, pytest.mark.asyncio, pytest.mark.timeout(60)]


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    db_path = tmp_path / "mes_memory.db"
    monkeypatch.setattr(store_db, "_db_path", db_path)
    monkeypatch.setattr(store_db, "_db", None)
    monkeypatch.setattr(store_core, "_db", store_db.get_db())
    return db_path


async def test_persistence_runs_off_the_event_loop_thread(isolated_db, monkeypatch):
    """The SQLite/serialization core must execute on a worker thread."""
    loop_thread = threading.current_thread()
    observed: dict[str, threading.Thread] = {}
    real_max = store_core.get_max_turn_num

    def recording_max(session_id: str) -> int:
        observed["thread"] = threading.current_thread()
        return real_max(session_id)

    monkeypatch.setattr(store_core, "get_max_turn_num", recording_max)

    await add_messages("p38-offload", [HumanMessage(content="hello")])

    assert observed["thread"] is not loop_thread, (
        "add_messages ran its persistence core on the event-loop thread"
    )
    rows = store_core.get_history_by_turn_page("p38-offload")
    assert [row["content"] for row in rows] == ["hello"]


async def test_slow_persistence_keeps_event_loop_responsive(isolated_db, monkeypatch):
    """A slow persistence pass must not block the loop."""
    real_max = store_core.get_max_turn_num

    def slow_max(session_id: str) -> int:
        value = real_max(session_id)
        time.sleep(0.3)
        return value

    monkeypatch.setattr(store_core, "get_max_turn_num", slow_max)

    ticks = 0

    async def ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.02)
            ticks += 1

    ticker_task = asyncio.create_task(ticker())
    try:
        await add_messages("p38-responsive", [HumanMessage(content="q"), AIMessage(content="a")])
    finally:
        ticker_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ticker_task

    assert ticks >= 5, (
        f"event loop was blocked during add_messages: only {ticks} ticks in 0.3s "
        "(to_thread offload missing?)"
    )
