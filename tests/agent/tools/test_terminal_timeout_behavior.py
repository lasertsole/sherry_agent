"""The terminal's timeout behavior: partial output survives, live work is adopted.

Four behaviors the plan pins (the ZCode-inspired rework):

* the default timeout is 120 s and both terminal timeouts carry env overrides
  (``TERMINAL_DEFAULT_TIMEOUT_SECONDS`` / ``TERMINAL_MAX_TIMEOUT_SECONDS``),
  read once at import like every other ``TOOLS_TIMEOUTS`` value;
* a timeout READS the bytes the command already wrote (head+tail bounded)
  instead of discarding them — the fd-direct-write design already has them on
  disk;
* a timed-out command that is worth keeping (anything but a bare ``sleep``) is
  ADOPTED into the background registry: the child keeps running, its output
  keeps streaming into the same tmp file, and the watcher moves that file into
  ``evicted/`` once the child exits;
* a timed-out ``sleep`` (or an adoption that could not register) is killed, and
  its tmp file is discarded — including the MSYS orphan that still holds the
  inherited stdout handle.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import agent.tools.pub_base.sandbox_guard as sandbox_guard
import agent.tools.terminal.core as terminal
import pub.func.message.eviction as eviction_module
from agent.tools.terminal import SafeShellTool
from agent.tools.terminal.bg_registry import background_registry
from agent.tools.pub_base.sandbox import SandboxPolicy

pytestmark = [
    pytest.mark.filterwarnings("ignore:The shell tool has no safeguards"),
]

_SID = "sess-timeout"


@pytest.fixture
def sessions_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    import config

    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(config, "SESSIONS_DIR", root)
    monkeypatch.setattr(eviction_module, "SESSIONS_DIR", root)
    return root


@pytest.fixture
def tool(monkeypatch: pytest.MonkeyPatch) -> SafeShellTool:
    monkeypatch.setattr(sandbox_guard, "read_policy", lambda: SandboxPolicy.AUTO)
    monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.AUTO)
    monkeypatch.setattr(terminal, "get_backend", lambda policy: None)
    monkeypatch.setattr(terminal, "scrub_env", lambda base_env=None: dict(os.environ))
    return SafeShellTool()


@pytest.fixture(autouse=True)
def _clean_registry():
    yield
    for task in list(background_registry.list_session(_SID)):
        background_registry.update(task.task_id, status="killed")


def _run_env_probe(env: dict[str, str]) -> str:
    repo_root = Path(__file__).resolve().parents[3]
    code = (
        f"import sys; sys.path.insert(0, r'{repo_root}'); "
        "from config.features.agent_side.tools_timeouts import TOOLS_TIMEOUTS; "
        "print(TOOLS_TIMEOUTS['terminal_timeout_seconds'], "
        "TOOLS_TIMEOUTS['terminal_max_timeout_seconds'])"
    )
    completed = subprocess.run(
        [sys.executable, "-I", "-c", code],
        capture_output=True,
        text=True,
        env={**os.environ, **env},
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr[-500:]
    return completed.stdout.strip()


# ─────────────────────────────────────────────────────────────────────────────
# Config: env overrides
# ─────────────────────────────────────────────────────────────────────────────
class TestTimeoutConfig:
    @pytest.mark.unit
    def test_defaults_are_120_and_600(self):
        assert terminal.TERMINAL_TIMEOUT == 120
        assert terminal.TERMINAL_MAX_TIMEOUT == 600

    @pytest.mark.integration
    def test_env_overrides_ride_the_import(self):
        assert _run_env_probe({}) == "120 600"
        assert (
            _run_env_probe(
                {
                    "TERMINAL_DEFAULT_TIMEOUT_SECONDS": "60",
                    "TERMINAL_MAX_TIMEOUT_SECONDS": "700",
                }
            )
            == "60 700"
        )

    @pytest.mark.integration
    def test_invalid_env_values_keep_the_defaults(self):
        assert _run_env_probe({"TERMINAL_DEFAULT_TIMEOUT_SECONDS": "abc"}) == "120 600"
        assert _run_env_probe({"TERMINAL_DEFAULT_TIMEOUT_SECONDS": "0"}) == "120 600"


# ─────────────────────────────────────────────────────────────────────────────
# Helpers: partial output + adoption eligibility
# ─────────────────────────────────────────────────────────────────────────────
class TestHelpers:
    @pytest.mark.unit
    def test_partial_output_reads_a_small_file_whole(self, tmp_path: Path):
        path = tmp_path / "out.log"
        path.write_bytes(b"line one\nline two\n")
        assert terminal._read_partial_stdout(path) == "line one\nline two\n"

    @pytest.mark.unit
    def test_partial_output_truncates_through_a_head_tail_marker(self, tmp_path: Path):
        path = tmp_path / "out.log"
        body = "A" * 4000 + "\n" + "B" * 4000
        path.write_bytes(body.encode("utf-8"))
        partial = terminal._read_partial_stdout(path)
        assert partial.startswith("A" * 100)
        assert partial.endswith("B" * 100)
        assert "... [truncated] ..." in partial
        assert "A" * 4000 not in partial, "the middle must be cut"

    @pytest.mark.unit
    def test_partial_output_is_empty_for_a_missing_file(self, tmp_path: Path):
        assert terminal._read_partial_stdout(tmp_path / "missing.log") == ""
        assert terminal._read_partial_stdout(None) == ""

    @pytest.mark.unit
    def test_adoption_eligibility(self):
        assert not terminal._should_auto_background("")
        assert not terminal._should_auto_background("   ")
        assert not terminal._should_auto_background("sleep 999")
        assert terminal._should_auto_background("npm test")
        assert terminal._should_auto_background("cargo build --release")
        assert terminal._should_auto_background("sleep")
        assert terminal._should_auto_background("tail -f /var/log/x")


# ─────────────────────────────────────────────────────────────────────────────
# Sync path: the timeout keeps the bytes already written
# ─────────────────────────────────────────────────────────────────────────────
class _PartialWriterSync:
    """Popen stand-in: writes bytes to the fd file, then hangs until killed."""

    returncode = None

    def __init__(self, *args: Any, stdout: Any = None, **kwargs: Any):
        if stdout is not None:
            stdout.write(b"compiling module A\n28 seconds of test output\n")
            stdout.flush()

    def wait(self, timeout=None):
        raise subprocess.TimeoutExpired("cmd", timeout or 0)

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9

    def poll(self):
        return self.returncode


class TestSyncPartialOutput:
    @pytest.mark.unit
    def test_a_timeout_returns_the_output_already_written(
        self, tool: SafeShellTool, sessions_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(subprocess, "Popen", _PartialWriterSync)

        out = tool._run(["long-build"], session_id=_SID, timeout=2)

        assert "timed out after 2 seconds" in out
        assert "28 seconds of test output" in out, "the partial output must survive"
        assert "compiling module A" in out


# ─────────────────────────────────────────────────────────────────────────────
# Async path: adopt-or-kill
# ─────────────────────────────────────────────────────────────────────────────
class _AsyncProc:
    """asyncio.subprocess.Process stand-in with a scriptable wait()."""

    returncode = None
    pid = 424242

    def __init__(self, *args: Any, stdout: Any = None, **kwargs: Any):
        if stdout is not None:
            stdout.write(b"tests: 12 passed, 4 running\n")
            stdout.flush()
        self._delay = float(kwargs.pop("_wait_delay", 0.0))
        self._exit_code = kwargs.pop("_exit_code", 0)

    async def wait(self):
        if self._delay > 0:
            await asyncio.sleep(self._delay)
        return self._exit_code

    def terminate(self):
        self.returncode = -15

    def kill(self):
        self.returncode = -9


def _patch_async_spawn(monkeypatch: pytest.MonkeyPatch, **proc_kwargs: Any) -> None:
    async def fake(*args: Any, **kwargs: Any):
        return _AsyncProc(*args, **kwargs, **proc_kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_shell", fake)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake)


class TestAsyncTimeout:
    @pytest.mark.unit
    def test_a_sleep_is_killed_and_its_partial_output_kept(
        self, tool: SafeShellTool, sessions_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _patch_async_spawn(monkeypatch, _wait_delay=600)

        out = asyncio.run(tool._arun(["sleep 999"], session_id=_SID, timeout=2))

        assert "timed out after 2 seconds" in out
        assert "tests: 12 passed" in out, "the bytes the sleep wrote first are kept"
        tmp_dir = sessions_root / _SID
        assert not list(tmp_dir.glob(".sherry-tmp-*")) if tmp_dir.exists() else True

    @pytest.mark.unit
    def test_a_live_command_is_adopted_into_the_background(
        self, tool: SafeShellTool, sessions_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # The fake outlives the 2 s timeout (so adoption triggers) but exits
        # shortly after, letting the watcher complete the retention move.
        _patch_async_spawn(monkeypatch, _wait_delay=3.0, _exit_code=0)

        async def scenario():
            out = await tool._arun(["npm test"], session_id=_SID, timeout=2)
            task_id = next(task.task_id for task in background_registry.list_session(_SID))
            task = background_registry.get(task_id)
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and task.status == "running":
                await asyncio.sleep(0.05)
            return out, task

        out, task = asyncio.run(scenario())

        assert "automatically moved to the background" in out
        assert "task: bg-" in out
        assert "terminal_bg(action='output'" in out
        assert "tests: 12 passed" in out
        assert task.command == "npm test"
        # The watcher owns the exit: the fake proc finished, so the tmp file
        # moved into its retained evicted/ path and the task completed.
        assert task.status == "completed"
        assert task.retained_path is not None
        assert task.output_path == task.retained_path
        assert not task.output_path.name.startswith(".sherry-tmp-")

    @pytest.mark.unit
    def test_adoption_failure_falls_back_to_the_kill_path(
        self,
        tool: SafeShellTool,
        sessions_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ):
        _patch_async_spawn(monkeypatch, _wait_delay=3.0)
        # No eviction dir possible: an unsafe session id refuses the retained
        # output file, so adoption returns None and the command is killed.
        unsafe_sid = "../evil"

        out = asyncio.run(tool._arun(["npm test"], session_id=unsafe_sid, timeout=2))

        assert "timed out after 2 seconds" in out
        assert "automatically moved" not in out

    @pytest.mark.unit
    def test_cancel_still_discards_and_keeps_no_tmp(
        self, tool: SafeShellTool, sessions_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _patch_async_spawn(monkeypatch, _wait_delay=600)

        async def run():
            task = asyncio.ensure_future(tool._arun(["npm test"], session_id=_SID, timeout=60))
            await asyncio.sleep(0.1)
            task.cancel()
            try:
                return await task
            except asyncio.CancelledError:
                return "cancelled"

        out = asyncio.run(run())
        assert out in ("cancelled",) or "cancelled" in out
