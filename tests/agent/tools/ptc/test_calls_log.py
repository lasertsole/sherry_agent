"""PTC calls_log: success, tool-error results, and budget failures."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from agent.tools.ptc.rpc_server import PTCCallBudgetExceeded, PtcRpcServer
from agent.tools.ptc.runner import run_ptc
from config.features import PTC

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _Tool:
    name = "echo"

    def __init__(self, result=None) -> None:
        self._result = result if result is not None else {"ok": True}

    async def ainvoke(self, args, config=None):
        return self._result


def _server(tools: dict, max_calls: int = 10) -> PtcRpcServer:
    return PtcRpcServer(tools, None, max_calls, "child:sess")


def test_success_call_is_recorded_with_a_bounded_preview() -> None:
    server = _server({"echo": _Tool("x" * 20_000)})
    response = server._dispatch("echo", {"value": 1})

    assert response["ok"] is True
    assert len(server.calls_log) == 1
    entry = server.calls_log[0]
    assert entry["tool"] == "echo"
    assert entry["args"] == {"value": 1}
    assert entry["ok"] is True
    assert len(entry["result"]) == 8_192


def test_unknown_tool_is_recorded_as_a_failed_step() -> None:
    server = _server({})

    response = server._dispatch("missing", {"x": 1})

    assert response["ok"] is False
    assert server.calls_made == 0
    assert server.calls_log == [
        {"tool": "missing", "args": {"x": 1}, "error": "unknown tool: missing", "ok": False}
    ]


def test_tool_exception_is_recorded_as_a_failed_step() -> None:
    class _Boom:
        name = "echo"

        async def ainvoke(self, args, config=None):
            raise ValueError("boom")

    server = _server({"echo": _Boom()})

    response = server._dispatch("echo", {})

    assert response["ok"] is False
    assert "ValueError: boom" in response["error"]
    assert server.calls_log[0]["ok"] is False
    assert "ValueError: boom" in server.calls_log[0]["error"]


def test_a_tool_returning_an_error_result_is_recorded_as_failed() -> None:
    server = _server({"echo": _Tool(SimpleNamespace(status="error", content="denied"))})

    response = server._dispatch("echo", {})

    # The child still receives the tool result (it may choose to recover), but
    # the replay progress must treat this step as failed.
    assert response["ok"] is True
    assert server.calls_log[0]["ok"] is False
    assert "denied" in server.calls_log[0]["error"]


def test_dict_error_envelope_is_also_a_failed_step() -> None:
    server = _server({"echo": _Tool({"status": "error", "error": "nope"})})

    server._dispatch("echo", {})

    assert server.calls_log[0]["ok"] is False
    assert "nope" in server.calls_log[0]["error"]


def test_budget_failure_is_recorded() -> None:
    server = _server({"echo": _Tool()}, max_calls=1)
    server._dispatch("echo", {"n": 1})

    with pytest.raises(PTCCallBudgetExceeded):
        server._dispatch("echo", {"n": 2})

    assert [entry["ok"] for entry in server.calls_log] == [True, False]
    assert "at most 1" in server.calls_log[1]["error"]


def _run(code: str, tools: dict, **config) -> dict:
    cfg = dict(PTC)
    cfg.update(config)
    raw = asyncio.run(run_ptc(code, tools, "child:sess", cfg))
    return json.loads(raw)


def test_run_ptc_exposes_calls_log_in_the_envelope() -> None:
    result = _run(
        "from sherry_tools import echo\nprint(echo())\n",
        {"echo": _Tool("ok")},
    )

    assert result["status"] == "ok"
    assert result["calls_log"] == [
        {"tool": "echo", "args": {}, "result": "ok", "ok": True}
    ]
