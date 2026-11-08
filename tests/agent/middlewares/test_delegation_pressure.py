"""Delegation-pressure gate: 7-read warning, 12-read escalated warning, no block."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from agent.middlewares.delegation_pressure import core as gate_core
from agent.middlewares.delegation_pressure.core import DelegationPressureGate
from config.features import DELEGATION_PRESSURE
from pub.func.message.system_reminder import is_system_reminder

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]

_SESSION = "evals-swe-test-delegation-pressure"


class _FakeRegister:
    def __init__(self) -> None:
        self.data: dict[tuple[str, str], Any] = {}

    def get_state(self, session_id: str, key: str, default: Any = None) -> Any:
        return self.data.get((session_id, key), default)

    def set_state(self, session_id: str, key: str, value: Any) -> bool:
        self.data[(session_id, key)] = value
        return True

    def delete_state(self, session_id: str, key: str) -> bool:
        self.data.pop((session_id, key), None)
        return True


@pytest.fixture()
def gate(monkeypatch: pytest.MonkeyPatch) -> DelegationPressureGate:
    monkeypatch.setenv("SHERRY_EVAL_DELEGATION_PRESSURE", "1")
    monkeypatch.setitem(DELEGATION_PRESSURE, "warn_after", 2)
    monkeypatch.setitem(DELEGATION_PRESSURE, "block_after", 4)
    monkeypatch.setattr(gate_core, "state_register_mem", _FakeRegister())
    return DelegationPressureGate()


def _request(name: str, **args: Any) -> SimpleNamespace:
    return SimpleNamespace(
        tool_call={"name": name, "args": args, "id": f"call-{name}"},
        state={"session_id": _SESSION},
    )


async def _run(
    gate: DelegationPressureGate,
    name: str,
    *,
    content: str = "ok",
    status: str = "success",
) -> tuple[Any, list[str]]:
    calls: list[str] = []

    async def handler(request):
        calls.append(str(request.tool_call["name"]))
        return ToolMessage(
            content=content,
            tool_call_id=str(request.tool_call["id"]),
            name=name,
            status=status,
        )

    out = await gate.awrap_tool_call(_request(name), handler)
    return out, calls


def _state(gate: DelegationPressureGate):
    return gate._get(_SESSION)


def test_defaults_are_seven_and_twelve_without_a_blocked_set():
    assert DELEGATION_PRESSURE["warn_after"] == 7
    assert DELEGATION_PRESSURE["block_after"] == 12
    assert "read_file" in DELEGATION_PRESSURE["pressure_tools"]
    assert "blocked_tools" not in DELEGATION_PRESSURE
    assert "browser" in DELEGATION_PRESSURE["excluded_tools"]
    assert "browser" not in DELEGATION_PRESSURE["pressure_tools"]


@pytest.mark.asyncio
async def test_warn_then_escalate_never_blocks_tools(gate: DelegationPressureGate):
    state_obj = {"session_id": _SESSION}

    await _run(gate, "read_file")
    await _run(gate, "read_file")
    warn = await gate.abefore_model(state_obj)
    assert warn is not None
    message = warn["messages"][0]
    assert is_system_reminder(message) is True
    assert message.metadata["origin"] == "delegation_pressure"
    assert message.metadata["provenance"] == "reminder_catalog"
    assert '<system-reminder source="delegation_pressure">' in message.content
    assert "[SYSTEM DIRECTIVE" not in message.content
    assert _state(gate).warned is True

    # The third/fourth reads still execute.
    await _run(gate, "read_file")
    fourth, calls = await _run(gate, "read_file")
    assert fourth.status != "error"
    assert calls == ["read_file"]

    escalated = await gate.abefore_model(state_obj)
    assert escalated is not None
    escalated_msg = escalated["messages"][0]
    assert is_system_reminder(escalated_msg) is True
    assert "ESCALATED" in escalated_msg.content
    assert "[SYSTEM DIRECTIVE" not in escalated_msg.content
    assert _state(gate).escalated is True

    # Every read/write tool remains available after the escalated warning.
    for name in ("read_file", "search_files", "terminal", "patch_file", "write_file"):
        result, calls = await _run(gate, name)
        assert result.status != "error"
        assert calls == [name], f"{name} must execute"

    # Browser tools are not counted.
    browser, calls = await _run(gate, "browser")
    assert browser.status != "error"
    assert calls == ["browser"]

    # An accepted spawn resets the counters.
    spawn, calls = await _run(
        gate,
        "sessions_spawn",
        content="Subagent spawned: status=accepted, run_id=run-1",
    )
    assert spawn.status != "error"
    assert calls == ["sessions_spawn"]
    assert _state(gate).read_calls == 0
    assert _state(gate).warned is False
    assert _state(gate).escalated is False

    # A later cycle can escalate again.
    for _ in range(4):
        await _run(gate, "read_file")
    assert _state(gate).escalated is False
    await gate.abefore_model(state_obj)
    assert _state(gate).escalated is True

    # after_agent resets at the turn boundary.
    gate._after_agent_impl(state_obj)
    assert _state(gate).read_calls == 0
    assert _state(gate).warned is False
    assert _state(gate).escalated is False
    next_turn, calls = await _run(gate, "read_file")
    assert next_turn.status != "error"
    assert calls == ["read_file"]


@pytest.mark.asyncio
async def test_failed_spawn_does_not_reset_but_tools_remain_available(
    gate: DelegationPressureGate,
):
    for _ in range(4):
        await _run(gate, "read_file")
    assert _state(gate).read_calls == 4

    failed, calls = await _run(
        gate,
        "sessions_spawn",
        content="Error: spawn rejected",
        status="error",
    )
    assert calls == ["sessions_spawn"]
    assert failed.status == "error"
    assert _state(gate).read_calls == 4

    still_works, calls = await _run(gate, "read_file")
    assert still_works.status != "error"
    assert calls == ["read_file"]


@pytest.mark.asyncio
async def test_write_tools_are_always_available_and_do_not_reset_reads(
    gate: DelegationPressureGate,
):
    await _run(gate, "read_file")
    write, calls = await _run(gate, "patch_file")
    assert write.status != "error"
    assert calls == ["patch_file"]
    assert _state(gate).read_calls == 1

    for _ in range(3):
        await _run(gate, "read_file")
    write_after, calls = await _run(gate, "patch_file")
    assert write_after.status != "error", "write tools must not be blocked"
    assert calls == ["patch_file"]
    assert _state(gate).read_calls == 4


@pytest.mark.asyncio
async def test_inactive_child_is_a_noop(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHERRY_EVAL_DELEGATION_PRESSURE", "1")
    monkeypatch.setattr(gate_core, "state_register_mem", _FakeRegister())
    instance = DelegationPressureGate()
    other = SimpleNamespace(
        tool_call={"name": "read_file", "args": {}, "id": "c"},
        state={"session_id": "agent:main:subagent:child"},
    )

    async def handler(_request):
        return ToolMessage(content="ok", tool_call_id="c", name="read_file")

    result = await instance.awrap_tool_call(other, handler)
    assert result.status != "error"
    assert await instance.abefore_model(other) is None
