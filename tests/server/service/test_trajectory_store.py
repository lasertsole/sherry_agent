"""The per-turn trajectory ledger: discrete events, tool_start/tool_end merged."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from server.service import trajectory_store as ts

pytestmark = [pytest.mark.unit]


@pytest.fixture()
def store(tmp_path: Path, monkeypatch):
    """A store on a per-test DB, with the singleton cache reset."""
    monkeypatch.setattr(ts, "get_trajectory_store", lambda: ts.TrajectoryStore(tmp_path / "t.db"))
    ts.reset_trajectory_store()
    yield ts.TrajectoryStore(tmp_path / "t.db")
    ts.reset_trajectory_store()


@pytest.mark.asyncio
async def test_tool_start_and_end_merge_into_one_row(store):
    await store.append(
        "s1",
        "t1",
        "tool_start",
        {"type": "tool_start", "content": "read_file", "tool_id": "c1", "args": {"path": "a.py"}},
        seq=2,
    )
    await store.append(
        "s1",
        "t1",
        "tool_end",
        {
            "type": "tool_result",
            "tool_id": "c1",
            "tool_name": "read_file",
            "content": "file body",
            "duration_ms": 12,
            "error": False,
        },
        seq=3,
    )

    events = await store.read("s1", turn_id="t1")

    assert len(events) == 1, events
    payload = events[0]["payload"]
    assert payload["args"] == {"path": "a.py"}
    assert payload["result"] == "file body"
    assert payload["duration_ms"] == 12


@pytest.mark.asyncio
async def test_events_read_back_in_seq_order(store):
    for seq, kind in enumerate(("tool_start", "tool_end"), start=1):
        await store.append(
            "s1",
            "t1",
            kind,
            {"type": "tool_start", "content": "x", "tool_id": f"c{seq}"},
            seq=seq,
        )

    events = await store.read("s1", turn_id="t1")

    assert [event["seq"] for event in events] == [1, 2]


@pytest.mark.asyncio
async def test_error_frames_are_flagged(store):
    await store.append(
        "s1",
        "t1",
        "tool_end",
        {"type": "tool_result", "tool_id": "c9", "tool_name": "terminal", "error": True},
        seq=1,
    )

    events = await store.read("s1", turn_id="t1")

    assert events[0]["is_error"] is True


@pytest.mark.asyncio
async def test_prune_keeps_only_the_newest_turns(store, monkeypatch):
    monkeypatch.setitem(ts.TRAJECTORY, "max_turns_per_session", 2)
    for turn in range(4):
        await store.append(
            "s1",
            f"t{turn}",
            "tool_start",
            {"type": "tool_start", "content": "x", "tool_id": f"c{turn}"},
            seq=turn,
        )

    removed = await store.prune_session("s1")

    assert removed == 2
    remaining = {event["turn_id"] for event in await store.read("s1", limit=50)}
    assert remaining == {"t2", "t3"}


@pytest.mark.asyncio
async def test_project_frame_ignores_deltas_and_records_tools(store, monkeypatch):
    monkeypatch.setattr(ts, "get_trajectory_store", lambda: store)

    assert ts.project_frame({"type": "text", "content": "hi"}, "s1", "t1", 1) is None
    assert ts.project_frame({"type": "reasoning", "content": "hm"}, "s1", "t1", 2) is None

    task = ts.project_frame(
        {"type": "tool_start", "content": "read_file", "tool_id": "c1"}, "s1", "t1", 3
    )
    assert task is not None
    await task

    events = await store.read("s1", turn_id="t1")
    assert [event["kind"] for event in events] == ["tool_start"]


@pytest.mark.asyncio
async def test_write_failure_is_fail_open(store, monkeypatch):
    """A broken DB must not raise into the caller (the stream keeps flowing)."""

    def _boom(*_args, **_kwargs):
        raise RuntimeError("disk gone")

    monkeypatch.setattr(store, "_ensure", _boom)

    await store.append("s1", "t1", "tool_start", {"type": "tool_start"}, seq=1)

    assert await store.read("s1", turn_id="t1") == []


@pytest.mark.asyncio
async def test_large_payload_is_clipped_to_the_config_budget(store, monkeypatch):
    monkeypatch.setitem(ts.TRAJECTORY, "payload_max_chars", 60)

    await store.append(
        "s1",
        "t1",
        "tool_start",
        {
            "type": "tool_start",
            "content": "read_file",
            "tool_id": "c1",
            "args": {"body": "x" * 500},
        },
        seq=1,
    )

    events = await store.read("s1", turn_id="t1")
    assert len(json.dumps(events[0]["payload"], ensure_ascii=False)) <= 60 + 2


@pytest.mark.asyncio
async def test_disabled_ledger_writes_nothing(store, monkeypatch):
    monkeypatch.setitem(ts.TRAJECTORY, "enabled", False)

    await store.append("s1", "t1", "tool_start", {"type": "tool_start"}, seq=1)

    assert await store.read("s1", turn_id="t1") == []


def test_event_ids_are_deterministic_per_tool_call():
    a = ts._event_id("s1", "t1", "tool_start", {"tool_id": "c1"})
    b = ts._event_id("s1", "t1", "tool_end", {"tool_id": "c1"})
    assert a == b == "s1:t1:tool:c1"


def test_the_shared_loop_write_lock_is_reused():
    """Two appends in one loop must not deadlock (the lock is per-store)."""
    store = ts.TrajectoryStore()
    assert isinstance(store._lock, asyncio.Lock)
