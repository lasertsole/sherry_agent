"""High-risk gate on the PTC path: script-inner calls and the system prefill.

``run_ptc`` dispatches each script call straight into ``tool.ainvoke`` with no
middleware chain, so the RPC dispatch itself must apply the shared gate. A
refusal is recorded in the ``calls_log`` as a failed step, which is exactly
what makes the prefill fall back to ReAct.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from pydantic import BaseModel

from agent.tools.ptc.rpc_server import PtcRpcServer
from agent.tools.ptc.runner import run_ptc
from agent.tools.pub_base.high_risk_guard_store import HighRiskGuardStore
from config.features import HIGH_RISK_GUARD, PTC

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

ROOT = "root-session"


class _Args(BaseModel):
    """A one-field schema so the generated stub forwards a keyword-bearing arg."""

    commands: list[str]


class _FakeTool:
    def __init__(self, name: str, metadata: dict[str, Any] | None = None) -> None:
        self.name = name
        self.metadata = metadata or {}
        self.tool_call_schema = _Args
        self.calls = 0
        self.seen: list[dict[str, Any]] = []

    async def ainvoke(self, args, config=None):
        self.calls += 1
        self.seen.append(args)
        return "ran"


def _terminal() -> _FakeTool:
    return _FakeTool(
        "terminal",
        {"high_risk": False, "idempotent": False, "scan_keywords": True},
    )


def _mcp() -> _FakeTool:
    return _FakeTool("mcp__stripe__charge", {"high_risk": True, "idempotent": False})


@pytest.fixture(autouse=True)
def _guard_db(tmp_path, monkeypatch):
    path = tmp_path / "guard.db"
    monkeypatch.setenv("SHERRY_HIGH_RISK_GUARD_PATH", str(path))
    return HighRiskGuardStore(path=str(path))


def _server(tool: _FakeTool) -> PtcRpcServer:
    return PtcRpcServer({tool.name: tool}, None, 10, "agent:main:subagent:ptc", scope=ROOT)


def test_a_keyword_hit_refuses_the_inner_call(_guard_db) -> None:
    tool = _terminal()
    server = _server(tool)

    response = server._dispatch("terminal", {"commands": ["rm -rf /tmp/x"]})

    assert response["ok"] is False
    assert "NEEDS_APPROVAL" in response["error"]
    assert "no approval channel" in response["error"]
    assert tool.calls == 0
    assert server.calls_log[0]["ok"] is False


def test_a_ledger_hit_refuses_the_inner_call(_guard_db) -> None:
    tool = _mcp()
    _guard_db.commit_op(ROOT, tool.name, {"amount": 100}, "charged")
    server = _server(tool)

    response = server._dispatch("mcp__stripe__charge", {"amount": 100})

    assert response["ok"] is False
    assert "already ran" in response["error"] and "charged" in response["error"]
    assert tool.calls == 0


def test_an_ordinary_tool_never_reads_the_store(monkeypatch, _guard_db) -> None:
    tool = _FakeTool("read_file", {"idempotent": True})
    # The metadata short-circuit lives inside the shared gate, so a store that
    # explodes on construction proves the ledger was never touched.
    monkeypatch.setattr(
        "agent.tools.pub_base.high_risk_gate.default_store",
        lambda: (_ for _ in ()).throw(AssertionError("store touched")),
    )
    server = _server(tool)

    response = server._dispatch("read_file", {"file_path": "a.py"})

    assert response["ok"] is True and tool.calls == 1


def test_the_escape_switch_restores_the_old_behaviour(monkeypatch, _guard_db) -> None:
    tool = _terminal()
    server = _server(tool)
    monkeypatch.setitem(HIGH_RISK_GUARD, "replay_path_gate_enabled", False)

    response = server._dispatch("terminal", {"commands": ["rm -rf /tmp/x"]})

    assert response["ok"] is True and tool.calls == 1


def test_a_prompt_refusal_does_not_poison_the_next_call(_guard_db) -> None:
    """A refused call is a failed step, not a budget/state failure."""
    tool = _terminal()
    server = _server(tool)

    refused = server._dispatch("terminal", {"commands": ["rm -rf /tmp/x"]})
    allowed = server._dispatch("terminal", {"commands": ["ls"]})

    assert refused["ok"] is False
    assert allowed["ok"] is True and tool.calls == 1


def test_the_system_prefill_path_is_gated_end_to_end(_guard_db) -> None:
    """The real child process sees the refusal in its calls_log (no tool ran)."""
    tool = _terminal()
    cfg = dict(PTC)
    code = "from sherry_tools import terminal\nprint(terminal(commands=['rm -rf /tmp/x']))\n"

    raw = asyncio.run(run_ptc(code, {tool.name: tool}, "agent:main:subagent:ptc", cfg, scope=ROOT))
    result = json.loads(raw)

    assert result["status"] != "ok"
    assert result["calls_log"][0]["ok"] is False
    assert "NEEDS_APPROVAL" in result["calls_log"][0]["error"]
    assert tool.calls == 0
