"""MCP tool exposure and role scoping tests."""

from __future__ import annotations

import pytest

from superagent.app.tools import read_tool, tools_for_role
from superagent.mcp.server import SuperAgentMCPServer


class Engine:
    def __init__(self) -> None:
        self.calls: list[tuple[str, object]] = []

    def report(self, ticket_id: str, report) -> None:
        self.calls.append(("report", ticket_id))

    def retry_ticket(self, ticket_id: str) -> None:
        self.calls.append(("retry", ticket_id))


def test_tool_lists_are_role_scoped() -> None:
    main = {tool.name for tool in tools_for_role("main")}
    worker = {tool.name for tool in tools_for_role("worker")}
    assert "orchestrator_route" in main and "orchestrator_route" not in worker
    assert "orchestrator_report" in worker and "orchestrator_report" not in main
    assert read_tool("orchestrator_eval_decide") is not None
    assert read_tool("nope") is None


def test_worker_cannot_call_main_tool() -> None:
    server = SuperAgentMCPServer(Engine())
    with pytest.raises(PermissionError):
        server.call("worker", "orchestrator_route", {})


def test_worker_is_scoped_to_own_ticket() -> None:
    engine = Engine()
    server = SuperAgentMCPServer(engine, worker_tickets={"worker-1": "t1"})
    with pytest.raises(PermissionError):
        server.call(
            "worker",
            "orchestrator_report",
            {"ticket_id": "t2", "kind": "progress", "summary": "x"},
            session_key="worker-1",
        )
    server.call(
        "worker",
        "orchestrator_report",
        {"ticket_id": "t1", "kind": "progress", "summary": "x"},
        session_key="worker-1",
    )
    assert engine.calls == [("report", "t1")]


def test_tool_list_descriptors() -> None:
    server = SuperAgentMCPServer(Engine())
    descriptors = server.tool_list("worker")
    assert len(descriptors) == 6
    assert all(item["inputSchema"]["type"] == "object" for item in descriptors)
