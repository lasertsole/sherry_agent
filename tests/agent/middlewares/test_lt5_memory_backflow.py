"""LT-5: subagent completion drain reconciles the shared memory files.

The drain middleware is the parent-turn ingestion point for completion
carriers. On a non-empty drain it also performs the LT-5 memory backflow:
reload the process-wide ``MemoryStore`` from ``MEMORY.md`` / ``USER.md`` (so a
child session's persisted writes become visible to the parent), then persist
the reconciled state.

Isolation: ``MEMORY_DIR`` is redirected to a per-test tmp directory and the
shared singleton's in-memory state is restored afterwards, so these tests
never touch the real workspace memory.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

import agent.tools.memory as memory_module
from agent.middlewares import subagent_completion_drain as drain_mod
from agent.middlewares.subagent_completion_drain import SubagentCompletionDrainMiddleware

pytestmark = [pytest.mark.unit]

SID = "sess-lt5-backflow"


def _carrier(content: str = "subtask finished") -> HumanMessage:
    """Task-4-shaped completion carrier (internal + provenance metadata)."""
    return HumanMessage(
        content=content,
        metadata={
            "internal": True,
            "provenance": "subagent_completion",
            "run_id": "run-lt5-1",
            "status": "completed",
        },
    )


@pytest.fixture()
def patched_drain(monkeypatch):
    """Replace the middleware's drain/rehydrate seams with an in-memory queue."""
    state: dict = {"items": [], "rehydrated": []}

    async def _rehydrate(key: str) -> None:
        state["rehydrated"].append(key)

    async def _drain(key: str) -> list:
        return list(state["items"])

    monkeypatch.setattr(drain_mod, "rehydrate", _rehydrate)
    monkeypatch.setattr(drain_mod, "drain", _drain)
    return state


@pytest.fixture()
def tmp_memory_dir(tmp_path, monkeypatch):
    """Point MemoryStore at a tmp dir; restore the singleton's state after."""
    store = memory_module.memory_store
    saved_memory = list(store.memory_entries)
    saved_user = list(store.user_entries)
    saved_snapshot = dict(store._system_prompt_snapshot)

    mem_dir = tmp_path / "memory"
    mem_dir.mkdir()
    monkeypatch.setattr(memory_module, "MEMORY_DIR", mem_dir)
    yield mem_dir

    # Runs before monkeypatch reverts, so this is a pure in-memory restore.
    store.memory_entries = saved_memory
    store.user_entries = saved_user
    store._system_prompt_snapshot = saved_snapshot


def test_drain_backflows_child_memory_written_to_disk(patched_drain, tmp_memory_dir):
    # Given: the parent store loaded an empty MEMORY.md at session start ...
    store = memory_module.memory_store
    store.load_from_disk()
    assert store.memory_entries == []
    # ... and the child session persisted a learned fact to the shared file.
    child_fact = "child learned: this project uses uv"
    (tmp_memory_dir / "MEMORY.md").write_text(child_fact, encoding="utf-8")
    patched_drain["items"] = [SimpleNamespace(message=_carrier())]

    # When: the drain middleware ingests the completion carrier.
    result = asyncio.run(
        SubagentCompletionDrainMiddleware().abefore_model({"session_id": SID}, None)
    )

    # Then: the carrier is injected AND the parent sees the child's fact.
    assert result is not None
    assert store.memory_entries == [child_fact]
    assert child_fact in store.format_for_system_prompt("memory")
    assert (tmp_memory_dir / "MEMORY.md").read_text(encoding="utf-8") == child_fact


def test_drain_reconcile_does_not_clobber_newer_disk_state(patched_drain, tmp_memory_dir):
    # Given: the in-memory view is stale relative to a newer disk write.
    store = memory_module.memory_store
    store.load_from_disk()
    store.memory_entries = ["stale parent view"]
    child_fact = "child learned: conftest.py overrides env"
    (tmp_memory_dir / "MEMORY.md").write_text(child_fact, encoding="utf-8")
    patched_drain["items"] = [SimpleNamespace(message=_carrier())]

    # When: the drain middleware reconciles.
    asyncio.run(SubagentCompletionDrainMiddleware().abefore_model({"session_id": SID}, None))

    # Then: disk (source of truth) wins; the stale view is not persisted over it.
    written = (tmp_memory_dir / "MEMORY.md").read_text(encoding="utf-8")
    assert child_fact in written
    assert "stale parent view" not in written
    assert store.memory_entries == [child_fact]


def test_drain_backflows_user_profile_target(patched_drain, tmp_memory_dir):
    # Given: the child persisted a user-profile entry on disk.
    store = memory_module.memory_store
    store.load_from_disk()
    child_pref = "child learned: user prefers Chinese replies"
    (tmp_memory_dir / "USER.md").write_text(child_pref, encoding="utf-8")
    patched_drain["items"] = [SimpleNamespace(message=_carrier())]

    # When: the drain middleware ingests the completion carrier.
    asyncio.run(SubagentCompletionDrainMiddleware().abefore_model({"session_id": SID}, None))

    # Then: the parent's user store and the shared file both carry it.
    assert store.user_entries == [child_pref]
    assert (tmp_memory_dir / "USER.md").read_text(encoding="utf-8") == child_pref


def test_backflow_failure_does_not_block_carrier_injection(
    patched_drain, tmp_memory_dir, monkeypatch
):
    # Given: memory I/O is broken.
    def _boom():
        raise OSError("memory I/O unavailable")

    monkeypatch.setattr(memory_module.memory_store, "load_from_disk", _boom)
    patched_drain["items"] = [SimpleNamespace(message=_carrier("must arrive"))]

    # When: the drain middleware ingests the completion carrier.
    result = asyncio.run(
        SubagentCompletionDrainMiddleware().abefore_model({"session_id": SID}, None)
    )

    # Then: the carrier still reaches the parent turn (fail-open).
    assert result is not None
    assert "must arrive" in result["messages"][0].text


def test_empty_queue_does_not_touch_memory(patched_drain, tmp_memory_dir, monkeypatch):
    # Given: a spy on every shared-memory I/O entry point.
    calls: list[str] = []
    monkeypatch.setattr(memory_module.memory_store, "load_from_disk", lambda: calls.append("load"))
    monkeypatch.setattr(
        memory_module.memory_store, "save_to_disk", lambda target: calls.append(target)
    )

    # When: the queue is empty.
    result = asyncio.run(
        SubagentCompletionDrainMiddleware().abefore_model({"session_id": SID}, None)
    )

    # Then: no-op drain, no memory I/O.
    assert result is None
    assert calls == []
