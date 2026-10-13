"""The compiled-graph lease: a replaced graph is closed, a held one is spared.

Every ``built_agent(force_rebuild=True)`` — the WS server does one per turn —
opens a NEW checkpointer (its own aiosqlite connection + non-daemon worker
thread + file handle), so a replaced graph that nobody closes leaks one
connection per turn. The contract pinned here:

* replacing the cached graph closes the old one;
* a graph a live caller holds (``hold_agent``) survives the replacement and is
  closed by its last ``release_agent`` instead;
* a graph still sitting in the cache is never closed by a release;
* closing without a running loop is a no-op (fail-open, process teardown owns
  the fds by then).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from agent import core as agent_core

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


async def _drain_releases() -> None:
    """Let every scheduled checkpointer close actually run."""
    import asyncio

    while agent_core._release_tasks:
        await asyncio.gather(*list(agent_core._release_tasks), return_exceptions=True)


class _Checkpointer:
    """Records whether the graph's checkpointer was closed."""

    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


@pytest.fixture
def graphs(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """``built_agent`` builds recording fakes; returns them in build order."""
    created: list[Any] = []

    async def _fake_build(**_kwargs: Any) -> Any:
        graph = SimpleNamespace(checkpointer=_Checkpointer())
        created.append(graph)
        return graph

    monkeypatch.setattr(agent_core, "_build_graph", _fake_build)
    monkeypatch.setattr(agent_core, "_agent", None)
    monkeypatch.setattr(agent_core, "_agent_loop", None)
    monkeypatch.setattr(agent_core, "_agent_holders", {})
    monkeypatch.setenv("MAIN_LLM_MAX_TOKEN", "131072")
    monkeypatch.setenv("AUXILIARY_LLM_MAX_TOKEN", "131072")
    return created


@pytest.mark.asyncio
async def test_a_replaced_graph_is_closed(graphs: list[Any]) -> None:
    first = await agent_core.built_agent(force_rebuild=True)
    second = await agent_core.built_agent(force_rebuild=True)
    await _drain_releases()

    assert first is not second
    assert first.checkpointer.closed is True, "the replaced graph leaked its connection"
    assert second.checkpointer.closed is False


@pytest.mark.asyncio
async def test_a_held_graph_survives_replacement_and_closes_on_release(graphs: list[Any]) -> None:
    first = await agent_core.built_agent(force_rebuild=True)
    agent_core.hold_agent(first)

    await agent_core.built_agent(force_rebuild=True)
    await _drain_releases()
    assert first.checkpointer.closed is False, "a live holder must keep its checkpointer"

    agent_core.release_agent(first)
    await _drain_releases()
    assert first.checkpointer.closed is True


@pytest.mark.asyncio
async def test_two_holders_close_only_after_the_last_release(graphs: list[Any]) -> None:
    first = await agent_core.built_agent(force_rebuild=True)
    agent_core.hold_agent(first)
    agent_core.hold_agent(first)
    await agent_core.built_agent(force_rebuild=True)

    agent_core.release_agent(first)
    await _drain_releases()
    assert first.checkpointer.closed is False

    agent_core.release_agent(first)
    await _drain_releases()
    assert first.checkpointer.closed is True


@pytest.mark.asyncio
async def test_releasing_the_cached_graph_does_not_close_it(graphs: list[Any]) -> None:
    graph = await agent_core.built_agent(force_rebuild=True)
    agent_core.hold_agent(graph)

    agent_core.release_agent(graph)
    await _drain_releases()

    assert graph.checkpointer.closed is False, "the cache still owns it"


def test_closing_without_a_running_loop_is_a_no_op() -> None:
    graph = SimpleNamespace(checkpointer=_Checkpointer())

    agent_core.close_agent_graph(graph)  # must not raise

    assert graph.checkpointer.closed is False


def test_closing_a_graph_without_aclose_is_a_no_op() -> None:
    agent_core.close_agent_graph(SimpleNamespace(checkpointer=object()))
    agent_core.close_agent_graph(SimpleNamespace())  # no checkpointer attribute at all
