"""Subagent completion drain reconciles the shared memory store.

The drain middleware is the parent-turn ingestion point for completion
carriers. On a non-empty drain it also performs the memory backflow:
reload the process-wide ``MemoryStore`` from the shared SQLite store (so a
child session's persisted writes become visible to the parent).

Isolation: the module-wide repository is pointed at a per-test SQLite database
and the singleton's in-memory state is restored afterwards, so these tests
never touch the real workspace memory.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage

import agent.tools.memory as memory_module
from agent.tools.subagent.announce import steering_queue as sq
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

    monkeypatch.setattr(sq, "rehydrate", _rehydrate)
    monkeypatch.setattr(sq, "drain", _drain)
    return state


@pytest.fixture()
def memory_repo(tmp_path, monkeypatch):
    """Point the shared store at a temp database; restore its view afterwards."""
    from context_engine.store.memory_entry_repository import MemoryEntryRepository

    store = memory_module.memory_store
    saved_memory = list(store.memory_entries)
    saved_user = list(store.user_entries)
    saved_snapshot = dict(store._system_prompt_snapshot)

    repo = MemoryEntryRepository(db_path=tmp_path / "memory.db")
    monkeypatch.setattr(memory_module, "memory_entry_repo", repo)
    yield repo
    repo.close()

    # Runs before monkeypatch reverts, so this is a pure in-memory restore.
    store.memory_entries = saved_memory
    store.user_entries = saved_user
    store._system_prompt_snapshot = saved_snapshot


def test_drain_backflows_child_memory_written_to_the_store(patched_drain, memory_repo):
    # Given: the parent store loaded an empty store at session start ...
    store = memory_module.memory_store
    store.load_from_store()
    assert store.memory_entries == []
    # ... and the child session persisted a learned fact.
    child_fact = "child learned: this project uses uv"
    memory_repo.add_entry("memory", child_fact)
    patched_drain["items"] = [SimpleNamespace(message=_carrier())]

    # When: the drain middleware ingests the completion carrier.
    result = asyncio.run(
        SubagentCompletionDrainMiddleware().abefore_model({"session_id": SID}, None)
    )

    # Then: the carrier is injected AND the parent sees the child's fact.
    assert result is not None
    assert store.memory_entries == [child_fact]
    assert child_fact in store.format_for_system_prompt("memory")


def test_drain_reconcile_does_not_clobber_newer_store_state(patched_drain, memory_repo):
    # Given: the in-memory view is stale relative to the store.
    store = memory_module.memory_store
    store.load_from_store()
    store.memory_entries = ["stale parent view"]
    child_fact = "child learned: conftest.py overrides env"
    memory_repo.add_entry("memory", child_fact)
    patched_drain["items"] = [SimpleNamespace(message=_carrier())]

    # When: the drain middleware reconciles.
    asyncio.run(SubagentCompletionDrainMiddleware().abefore_model({"session_id": SID}, None))

    # Then: the store (source of truth) wins; the stale view is not persisted over it.
    stored = memory_repo.active_entries_ordered("memory")
    assert [entry["content"] for entry in stored] == [child_fact]
    assert store.memory_entries == [child_fact]


def test_drain_backflows_user_profile_target(patched_drain, memory_repo):
    # Given: the child persisted a user-profile entry.
    store = memory_module.memory_store
    store.load_from_store()
    child_pref = "child learned: user prefers Chinese replies"
    memory_repo.add_entry("user", child_pref)
    patched_drain["items"] = [SimpleNamespace(message=_carrier())]

    # When: the drain middleware ingests the completion carrier.
    asyncio.run(SubagentCompletionDrainMiddleware().abefore_model({"session_id": SID}, None))

    # Then: the parent's user store carries it.
    assert store.user_entries == [child_pref]


def test_backflow_failure_does_not_block_carrier_injection(patched_drain, memory_repo, monkeypatch):
    # Given: memory I/O is broken.
    def _boom():
        raise OSError("memory I/O unavailable")

    monkeypatch.setattr(memory_module.memory_store, "load_from_store", _boom)
    patched_drain["items"] = [SimpleNamespace(message=_carrier("must arrive"))]

    # When: the drain middleware ingests the completion carrier.
    result = asyncio.run(
        SubagentCompletionDrainMiddleware().abefore_model({"session_id": SID}, None)
    )

    # Then: the carrier still reaches the parent turn (fail-open).
    assert result is not None
    assert "must arrive" in result["messages"][0].text


def test_empty_queue_does_not_touch_memory(patched_drain, memory_repo, monkeypatch):
    # Given: a spy on the shared-memory reload.
    calls: list[str] = []
    monkeypatch.setattr(memory_module.memory_store, "load_from_store", lambda: calls.append("load"))

    # When: the queue is empty.
    result = asyncio.run(
        SubagentCompletionDrainMiddleware().abefore_model({"session_id": SID}, None)
    )

    # Then: no-op drain, no memory I/O.
    assert result is None
    assert calls == []
