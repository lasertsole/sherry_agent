"""Batch-aware ToolNode: file-path ordering, safe concurrency and barriers."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from langgraph.graph import END, START, MessagesState, StateGraph

from agent.safe_tool_node import SafeToolNode
from config.features import TOOL_PARALLEL

pytestmark = [pytest.mark.integration, pytest.mark.timeout(60)]


class _Trace:
    def __init__(self) -> None:
        self.order: list[str] = []
        self.active = 0
        self.max_active = 0
        self.both_started = asyncio.Event()
        self.release = asyncio.Event()

    def enter(self, label: str) -> None:
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        self.order.append(label)
        if self.active >= 2:
            self.both_started.set()

    def leave(self) -> None:
        self.active -= 1


def _make_tools(trace: _Trace, *, read_blocks: bool = False):
    @tool("read_file")
    async def read_file(file_path: str) -> str:
        "read"
        trace.enter(f"read:{file_path}")
        try:
            if read_blocks:
                await trace.release.wait()
            else:
                await asyncio.sleep(0.02)
            return f"read:{file_path}"
        finally:
            trace.leave()

    @tool("write_file")
    async def write_file(file_path: str) -> str:
        "write"
        trace.enter(f"write:{file_path}")
        try:
            if read_blocks:
                await trace.release.wait()
            else:
                await asyncio.sleep(0.02)
            return f"write:{file_path}"
        finally:
            trace.leave()

    @tool("terminal")
    async def terminal(commands: str) -> str:
        "shell"
        trace.enter(f"terminal:{commands}")
        try:
            await asyncio.sleep(0.02)
            return f"terminal:{commands}"
        finally:
            trace.leave()

    read_file.metadata = {"idempotent": True}
    write_file.metadata = {"idempotent": False, "side_effect_scope": "workspace"}
    terminal.metadata = {"idempotent": False, "side_effect_scope": "system"}
    return [read_file, write_file, terminal]


def _graph(tools: list[Any]):
    node = SafeToolNode(tools=tools)
    builder = StateGraph(MessagesState)
    builder.add_node("tools", node)
    builder.add_edge(START, "tools")
    builder.add_edge("tools", END)
    return builder.compile()


def _input(*calls: tuple[str, str]) -> dict[str, Any]:
    return {
        "messages": [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": name,
                        "args": {"file_path": path} if name != "terminal" else {"commands": path},
                        "id": f"c{index}",
                        "type": "tool_call",
                    }
                    for index, (name, path) in enumerate(calls)
                ],
            )
        ]
    }


@pytest.mark.asyncio
async def test_two_reads_of_one_path_overlap(tmp_path: Path) -> None:
    path = str(tmp_path / "a.txt")
    trace = _Trace()
    graph = _graph(_make_tools(trace, read_blocks=True))
    task = asyncio.create_task(graph.ainvoke(_input(("read_file", path), ("read_file", path))))
    await asyncio.wait_for(trace.both_started.wait(), timeout=2)
    assert trace.max_active == 2
    trace.release.set()
    out = await task
    assert [m.content for m in out["messages"][1:]] == [f"read:{path}", f"read:{path}"]


@pytest.mark.asyncio
async def test_read_then_write_same_path_is_ordered(tmp_path: Path) -> None:
    path = str(tmp_path / "a.txt")
    trace = _Trace()
    graph = _graph(_make_tools(trace))
    await graph.ainvoke(_input(("read_file", path), ("write_file", path)))

    assert trace.max_active == 1
    assert trace.order == [f"read:{path}", f"write:{path}"]


@pytest.mark.asyncio
async def test_write_then_read_same_path_is_ordered(tmp_path: Path) -> None:
    path = str(tmp_path / "a.txt")
    trace = _Trace()
    graph = _graph(_make_tools(trace))
    await graph.ainvoke(_input(("write_file", path), ("read_file", path)))

    assert trace.max_active == 1
    assert trace.order == [f"write:{path}", f"read:{path}"]


@pytest.mark.asyncio
async def test_different_paths_overlap(tmp_path: Path) -> None:
    read_path = str(tmp_path / "a.txt")
    write_path = str(tmp_path / "b.txt")
    trace = _Trace()
    graph = _graph(_make_tools(trace, read_blocks=True))
    task = asyncio.create_task(
        graph.ainvoke(_input(("read_file", read_path), ("write_file", write_path)))
    )
    await asyncio.wait_for(trace.both_started.wait(), timeout=2)
    assert trace.max_active == 2
    trace.release.set()
    await task


@pytest.mark.asyncio
async def test_a_terminal_barrier_serializes_the_batch(tmp_path: Path) -> None:
    path = str(tmp_path / "a.txt")
    trace = _Trace()
    graph = _graph(_make_tools(trace))
    await graph.ainvoke(_input(("read_file", path), ("terminal", "pwd")))

    assert trace.max_active == 1
    assert trace.order == [f"read:{path}", "terminal:pwd"]


@pytest.mark.asyncio
async def test_enforce_false_keeps_native_concurrency(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setitem(TOOL_PARALLEL, "enforce", False)
    path = str(tmp_path / "a.txt")
    trace = _Trace()
    graph = _graph(_make_tools(trace, read_blocks=True))
    task = asyncio.create_task(graph.ainvoke(_input(("read_file", path), ("write_file", path))))
    await asyncio.wait_for(trace.both_started.wait(), timeout=2)
    assert trace.max_active == 2
    trace.release.set()
    await task


def test_sync_path_orders_same_file(tmp_path: Path) -> None:
    path = str(tmp_path / "a.txt")
    order: list[str] = []

    @tool("read_file")
    def read_file(file_path: str) -> str:
        "read"
        order.append(f"read:{file_path}")
        return f"read:{file_path}"

    @tool("write_file")
    def write_file(file_path: str) -> str:
        "write"
        order.append(f"write:{file_path}")
        return f"write:{file_path}"

    read_file.metadata = {"idempotent": True}
    write_file.metadata = {"idempotent": False, "side_effect_scope": "workspace"}
    graph = _graph([read_file, write_file])
    out = graph.invoke(_input(("read_file", path), ("write_file", path)))

    assert order == [f"read:{path}", f"write:{path}"]
    assert [m.content for m in out["messages"][1:]] == [f"read:{path}", f"write:{path}"]


def test_install_patches_the_factory() -> None:
    import langchain.agents.factory as factory

    import agent.core  # noqa: F401 - import installs SafeToolNode

    assert factory.ToolNode is SafeToolNode
