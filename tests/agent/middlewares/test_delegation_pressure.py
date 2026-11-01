"""Delegation-pressure gate: 8-read warn, 16-read read/write block per turn."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from langchain_core.messages import ToolMessage

from agent.middlewares.delegation_pressure import core as gate_core
from agent.middlewares.delegation_pressure.core import DelegationPressureGate
from config.features import DELEGATION_PRESSURE

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
    monkeypatch.setenv("SHERRY_EVAL_DELEGATE_ONLY", "0")
    monkeypatch.setitem(DELEGATION_PRESSURE, "warn_after", 2)
    monkeypatch.setitem(DELEGATION_PRESSURE, "block_after", 4)
    monkeypatch.setattr(gate_core, "state_register_mem", _FakeRegister())
    return DelegationPressureGate()


def _request(name: str, **args: Any) -> SimpleNamespace:
    return SimpleNamespace(
        tool_call={"name": name, "args": args, "id": f"call-{name}"},
        state={"session_id": _SESSION},
    )


async def _run(gate: DelegationPressureGate, name: str) -> tuple[Any, list[str]]:
    calls: list[str] = []

    async def handler(request):
        calls.append(str(request.tool_call["name"]))
        return ToolMessage(content="ok", tool_call_id=str(request.tool_call["id"]), name=name)

    out = await gate.awrap_tool_call(_request(name), handler)
    return out, calls


def _state(gate: DelegationPressureGate):
    return gate._get(_SESSION)


def test_defaults_are_eight_and_sixteen():
    assert DELEGATION_PRESSURE["warn_after"] == 8
    assert DELEGATION_PRESSURE["block_after"] == 16
    assert "read_file" in DELEGATION_PRESSURE["pressure_tools"]
    assert "patch_file" in DELEGATION_PRESSURE["blocked_tools"]
    assert "browser" in DELEGATION_PRESSURE["excluded_tools"]
    assert "browser" not in DELEGATION_PRESSURE["pressure_tools"]
    assert "browser" not in DELEGATION_PRESSURE["blocked_tools"]


@pytest.mark.asyncio
async def test_warn_eight_then_block_reads_and_writes_until_after_agent(
    gate: DelegationPressureGate,
):
    state_obj = {"session_id": _SESSION}

    # Warning at 2 (test override); the call executes.
    await _run(gate, "read_file")
    await _run(gate, "read_file")
    warn = await gate.abefore_model(state_obj)
    assert warn is not None
    assert "DELEGATION PRESSURE" in warn["messages"][0].content
    assert _state(gate).warned is True

    # The 4th read executes; blocking starts AFTER it.
    await _run(gate, "read_file")
    fourth, calls = await _run(gate, "read_file")
    assert fourth.status != "error"
    assert calls == ["read_file"]
    assert _state(gate).blocked is True

    # Every read/write call now errors and does not execute.
    for name in ("read_file", "search_files", "terminal", "patch_file", "write_file"):
        blocked, calls = await _run(gate, name)
        assert isinstance(blocked, ToolMessage)
        assert blocked.status == "error"
        assert "sessions_spawn" in blocked.content
        assert calls == [], f"{name} must not execute after the block"

    # Browser debugging tools are neither counted nor blocked.
    browser, calls = await _run(gate, "browser")
    assert browser.status != "error"
    assert calls == ["browser"]
    assert (await _run(gate, "browser_evaluate"))[0].status != "error"

    # A spawn is allowed but does not unlock the turn.
    spawn, calls = await _run(gate, "sessions_spawn")
    assert spawn.status != "error"
    assert calls == ["sessions_spawn"]
    still_blocked, calls = await _run(gate, "read_file")
    assert still_blocked.status == "error"
    assert calls == []

    # after_agent is the only reset.
    gate._after_agent_impl(state_obj)
    assert _state(gate).read_calls == 0
    assert _state(gate).blocked is False
    next_turn, calls = await _run(gate, "read_file")
    assert next_turn.status != "error"
    assert calls == ["read_file"]


@pytest.mark.asyncio
async def test_write_before_block_is_allowed_and_does_not_reset_reads(
    gate: DelegationPressureGate,
):
    await _run(gate, "read_file")
    write, calls = await _run(gate, "patch_file")
    assert write.status != "error"
    assert calls == ["patch_file"]
    assert _state(gate).read_calls == 1

    # Reads still accumulate independently of the write.
    for _ in range(3):
        await _run(gate, "read_file")
    assert _state(gate).blocked is True
    assert (await _run(gate, "patch_file"))[0].status == "error"


@pytest.mark.asyncio
async def test_inactive_session_is_a_noop(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SHERRY_EVAL_DELEGATION_PRESSURE", "1")
    monkeypatch.setenv("SHERRY_EVAL_DELEGATE_ONLY", "0")
    monkeypatch.setattr(gate_core, "state_register_mem", _FakeRegister())
    instance = DelegationPressureGate()
    other = SimpleNamespace(
        tool_call={"name": "read_file", "args": {}, "id": "c"},
        state={"session_id": "agent:main:subagent:child"},
    )

    async def handler(_request):
        return ToolMessage(content="ok", tool_call_id="c", name="read_file")

    assert await instance.awrap_tool_call(other, handler) is not None
    assert await instance.abefore_model(other) is None
