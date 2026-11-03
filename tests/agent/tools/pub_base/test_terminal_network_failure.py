"""Network-command status bridge for terminal.

A failing curl/wget/git-remote must surface as a tool error so ToolGuardrails
counts it; a failing local test/compiler must keep status="success" so the
agent can read the output and iterate.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.tools import ToolException

import agent.tools.terminal.core as terminal


def test_network_failure_detection_handles_shell_segments_and_git_options() -> None:
    assert terminal._is_network_failure("curl https://example.com", 22)
    assert terminal._is_network_failure("cd /tmp && curl -s https://x", 22)
    assert terminal._is_network_failure("VAR=1 wget https://x", 4)
    assert terminal._is_network_failure("timeout 5 git -C /tmp fetch origin", 128)
    assert terminal._is_network_failure(["curl", "https://x"], 22)


def test_local_and_successful_commands_are_not_network_failures() -> None:
    assert not terminal._is_network_failure("pytest -q", 1)
    assert not terminal._is_network_failure("python -m compileall pkg", 1)
    assert not terminal._is_network_failure("echo curl", 1)
    assert not terminal._is_network_failure("git status", 0)
    assert not terminal._is_network_failure("curl https://x", 0)


class _FakeProc:
    def __init__(self, *args: Any, out: bytes, returncode: int, **kwargs: Any) -> None:
        self.returncode = returncode
        handle = kwargs.get("stdout")
        if hasattr(handle, "write"):
            handle.write(out)
            handle.flush()

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode

    def kill(self) -> None:
        return None


def _patch_spawn(monkeypatch: pytest.MonkeyPatch, *, out: bytes, returncode: int) -> None:
    def _spawn(*args: Any, **kwargs: Any) -> _FakeProc:
        return _FakeProc(*args, out=out, returncode=returncode, **kwargs)

    monkeypatch.setattr(terminal.subprocess, "Popen", _spawn)


def test_a_failed_network_command_raises_a_tool_exception(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_spawn(monkeypatch, out=b"curl: (22) 403 Forbidden", returncode=22)
    tool = terminal.SafeShellTool()

    with pytest.raises(ToolException, match="Exit code 22"):
        tool._execute_sync(
            "curl https://example.com",
            shell=True,
            env={},
            encoding="utf-8",
        )


def test_a_failed_local_command_returns_its_output(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_spawn(monkeypatch, out=b"1 failed, 9 passed", returncode=1)
    tool = terminal.SafeShellTool()

    result = tool._execute_sync(
        "pytest -q",
        shell=True,
        env={},
        encoding="utf-8",
    )

    assert result == "Exit code 1\n1 failed, 9 passed"
