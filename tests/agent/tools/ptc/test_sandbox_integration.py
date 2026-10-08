"""Unit tests for the PTC sandbox-backend policy mapping.

Exercises ``runner._resolve_sandboxed_argv`` (the single integration seam) and a
runner-level refusal, with the backend and policy stubbed at the module seam —
no real bwrap / Seatbelt is ever probed.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from typing import Any

import pytest
from loguru import logger

from agent.tools.ptc import runner
from agent.tools.ptc.runner import PtcSandboxUnavailableError, _resolve_sandboxed_argv
from agent.tools.pub_base.sandbox import SandboxPolicy
from config.features import PTC

pytestmark = [pytest.mark.unit]


@contextmanager
def _loguru_warnings():
    records: list[str] = []
    sink_id = logger.add(lambda m: records.append(str(m)), level="WARNING")
    try:
        yield records
    finally:
        logger.remove(sink_id)


class _FakeBackend:
    """Records the wrap call and returns a recognisable wrapped argv."""

    def __init__(self) -> None:
        self.wrap_calls: list[tuple[list[str], dict]] = []

    def wrap(self, cmd: list[str], env: dict) -> tuple[list[str], dict]:
        self.wrap_calls.append((list(cmd), dict(env)))
        return ["/fake/bwrap", "--", *cmd], env


def _policy(monkeypatch: pytest.MonkeyPatch, policy: SandboxPolicy) -> None:
    monkeypatch.setattr(runner, "read_policy", lambda: policy)


def test_auto_with_backend_wraps_the_interpreter_argv(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = _FakeBackend()
    _policy(monkeypatch, SandboxPolicy.AUTO)
    monkeypatch.setattr(runner, "get_backend", lambda _policy: backend)

    argv, env = _resolve_sandboxed_argv(["/usr/bin/python", "/tmp/script.py"], {"PATH": "/bin"})
    assert argv == ["/fake/bwrap", "--", "/usr/bin/python", "/tmp/script.py"]
    assert backend.wrap_calls[0][0] == ["/usr/bin/python", "/tmp/script.py"]
    assert env == {"PATH": "/bin"}


def test_off_never_consults_the_backend(monkeypatch: pytest.MonkeyPatch) -> None:
    _policy(monkeypatch, SandboxPolicy.OFF)

    def _fail(_policy: SandboxPolicy) -> Any:
        raise AssertionError("get_backend must not be called when policy=off")

    monkeypatch.setattr(runner, "get_backend", _fail)
    argv = ["/usr/bin/python", "/tmp/script.py"]
    with _loguru_warnings() as records:
        resolved, env = _resolve_sandboxed_argv(argv, {"PATH": "/bin"})
    assert resolved == argv
    assert env == {"PATH": "/bin"}
    assert records == []


def test_required_without_backend_refuses_with_actionable_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _policy(monkeypatch, SandboxPolicy.REQUIRED)

    def _raise(_policy: SandboxPolicy) -> Any:
        raise RuntimeError("Required sandbox unavailable on Linux")

    monkeypatch.setattr(runner, "get_backend", _raise)
    with pytest.raises(PtcSandboxUnavailableError) as excinfo:
        _resolve_sandboxed_argv(["/usr/bin/python", "/tmp/script.py"], {"PATH": "/bin"})
    message = str(excinfo.value)
    assert "SANDBOX_POLICY=required" in message
    assert "SANDBOX_POLICY=auto" in message


def test_auto_without_backend_degrades_with_exactly_one_warning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _policy(monkeypatch, SandboxPolicy.AUTO)
    monkeypatch.setattr(runner, "get_backend", lambda _policy: None)
    argv = ["/usr/bin/python", "/tmp/script.py"]
    with _loguru_warnings() as records:
        resolved, env = _resolve_sandboxed_argv(argv, {"PATH": "/bin"})
    assert resolved == argv
    assert env == {"PATH": "/bin"}
    assert len(records) == 1
    assert "execute_code" in records[0]
    assert "policy=auto" in records[0]


def test_runner_refusal_returns_envelope_without_spawning(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _policy(monkeypatch, SandboxPolicy.REQUIRED)

    def _raise(_policy: SandboxPolicy) -> Any:
        raise RuntimeError("Required sandbox unavailable on Linux")

    def _no_spawn(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError("execute_code must not spawn a child when required is unavailable")

    monkeypatch.setattr(runner, "get_backend", _raise)
    monkeypatch.setattr(runner.subprocess, "Popen", _no_spawn)

    raw = asyncio.run(runner.run_ptc("print('x')", {}, "child:sess", dict(PTC)))
    result = json.loads(raw)
    assert result["status"] == "sandbox_unavailable"
    assert "SANDBOX_POLICY=required" in result["error"]
    assert result["tool_calls_made"] == 0
