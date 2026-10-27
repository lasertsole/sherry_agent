"""terminal stdout is fd-direct-written to disk — never a pipe into Python.

Two halves, and the second keeps the first honest:

* the helper contract (``agent/tools/pub_base/stdout_fd.py``, shared with the
  sibling exec tools) — ``open_stdout_tmp`` places the file in the session
  directory (OS temp only for an unsafe id) and sweeps abandoned leftovers,
  ``read_or_evict_output`` reads small outputs whole but MOVES an oversized
  one into ``evicted/`` and returns a byte-labelled head/tail preview without
  ever reading the payload into the parent, and ``discard_stdout_tmp`` makes
  every failure path delete its temp file;
* the wiring — both spawn sites (sync ``_run`` / async ``_arun``) hand the
  child a real FILE for stdout (stderr merged into it) and wait on the
  process, so no pipe has to be drained to EOF and an oversized output never
  sits on the Python heap.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import time
from pathlib import Path
from typing import Any

import pytest

import agent.tools.pub_base.sandbox_guard as sandbox_guard
import agent.tools.terminal as terminal
import pub.func.message.eviction as eviction_module
from agent.tools.pub_base.sandbox import SandboxPolicy
from agent.tools.pub_base.stdout_fd import (
    FD_WRITE_THRESHOLD,
    discard_stdout_tmp,
    open_stdout_tmp,
    read_or_evict_output,
)
from agent.tools.terminal import SafeShellTool
from config.features import TOOLS_TIMEOUTS

pytestmark = [
    pytest.mark.filterwarnings("ignore:The shell tool has no safeguards"),
]

_SID = "sess-fd-direct"
#: Comfortably past the threshold; written by a real child in the e2e cases.
_BIG_BYTES = FD_WRITE_THRESHOLD + 40_000
_BIG_CMD = f"head -c {_BIG_BYTES} /dev/zero | tr '\\0' 'x'"


@pytest.fixture
def sessions_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point SESSIONS_DIR at a temp root for BOTH consumers.

    ``terminal`` imports the constant inside the helper (a live lookup on the
    ``config`` module), ``eviction`` binds it at import — both must move.
    """
    import config

    root = tmp_path / "sessions"
    root.mkdir()
    monkeypatch.setattr(config, "SESSIONS_DIR", root)
    monkeypatch.setattr(eviction_module, "SESSIONS_DIR", root)
    return root


@pytest.fixture
def tool(monkeypatch: pytest.MonkeyPatch) -> SafeShellTool:
    """A terminal tool on the deterministic bare-spawn path.

    Policy AUTO + no backend degrades to the direct spawn (one warning), which
    is the same ``_execute_sync`` body the sandboxed wrap uses — and it cannot
    be refused by an environment pinned to ``SANDBOX_POLICY=required``.
    """
    monkeypatch.setattr(sandbox_guard, "read_policy", lambda: SandboxPolicy.AUTO)
    monkeypatch.setattr(terminal, "read_policy", lambda: SandboxPolicy.AUTO)
    monkeypatch.setattr(terminal, "get_backend", lambda policy: None)
    monkeypatch.setattr(terminal, "scrub_env", lambda base_env=None: dict(os.environ))
    return SafeShellTool()


def _temp_files(root: Path, sid: str = _SID) -> list[Path]:
    return sorted((root / sid).glob(".sherry-tmp-*")) if (root / sid).exists() else []


# ─────────────────────────────────────────────────────────────────────────────
# Helper contract
# ─────────────────────────────────────────────────────────────────────────────
class TestStdoutTempFile:
    @pytest.mark.unit
    def test_threshold_is_bound_to_the_feature_registry(self):
        assert FD_WRITE_THRESHOLD == TOOLS_TIMEOUTS["fd_write_threshold_bytes"]

    @pytest.mark.unit
    def test_temp_file_lands_in_the_session_dir(self, sessions_root: Path):
        path, handle = open_stdout_tmp(_SID)
        try:
            assert path.parent == sessions_root / _SID
            assert path.name.startswith(".sherry-tmp-stdout-")
            assert path.name.endswith(".log")
            handle.write(b"x")
            handle.close()
            assert path.read_bytes() == b"x"
        finally:
            path.unlink(missing_ok=True)

    @pytest.mark.unit
    def test_unsafe_session_id_falls_back_to_os_temp(self, sessions_root: Path):
        path, handle = open_stdout_tmp("../evil")
        try:
            assert path.parent != sessions_root
            assert sessions_root.parent not in path.parents or path.parent == sessions_root
            # Nothing was created outside the temp file itself.
            assert list(sessions_root.iterdir()) == []
        finally:
            handle.close()
            path.unlink(missing_ok=True)

    @pytest.mark.unit
    def test_opening_sweeps_an_abandoned_leftover(self, sessions_root: Path):
        sid_dir = sessions_root / _SID
        sid_dir.mkdir()
        leftover = sid_dir / ".sherry-tmp-stdout-crashed.log"
        leftover.write_bytes(b"stale")
        long_ago = time.time() - 2 * 60 * 60
        os.utime(leftover, (long_ago, long_ago))

        path, handle = open_stdout_tmp(_SID)
        try:
            assert not leftover.exists(), "an old .sherry-tmp-*.log must be swept"
            assert path.exists()
        finally:
            handle.close()
            path.unlink(missing_ok=True)

    @pytest.mark.unit
    def test_under_threshold_is_read_whole_and_unlinked(self, sessions_root: Path):
        path, handle = open_stdout_tmp(_SID)
        handle.write(b"small output")
        handle.close()

        out = read_or_evict_output(path, _SID, "utf-8")

        assert out == "small output"
        assert not path.exists(), "a read-back temp file must be removed"
        assert not (sessions_root / _SID / "evicted").exists()

    @pytest.mark.unit
    def test_a_non_positive_threshold_disables_eviction(
        self, sessions_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        import agent.tools.pub_base.stdout_fd as stdout_fd_mod

        monkeypatch.setattr(stdout_fd_mod, "FD_WRITE_THRESHOLD", 0)
        path, handle = open_stdout_tmp(_SID)
        handle.write(b"c" * _BIG_BYTES)
        handle.close()

        out = read_or_evict_output(path, _SID, "utf-8")

        assert out == "c" * _BIG_BYTES, "0 = always read back in memory (the old behaviour)"
        assert not path.exists()
        assert not (sessions_root / _SID / "evicted").exists()

    @pytest.mark.unit
    def test_over_threshold_moves_the_file_and_previews_it(
        self, sessions_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        path, handle = open_stdout_tmp(_SID)
        handle.write(b"a" * _BIG_BYTES)
        handle.close()
        # The oversized path must never read the payload into this process:
        # any whole-file read fails the test loudly.
        monkeypatch.setattr(Path, "read_bytes", lambda self: pytest.fail(f"read whole: {self}"))

        out = read_or_evict_output(path, _SID, "utf-8")

        assert not path.exists(), "the temp file is moved, not copied + deleted"
        evicted = list((sessions_root / _SID / "evicted").glob("terminal-*.log"))
        assert len(evicted) == 1
        assert evicted[0].stat().st_size == _BIG_BYTES
        assert f"[evicted to: {evicted[0]}]" in out
        assert "Use read_file(file_path=" in out and str(evicted[0]) in out
        assert f"[full content: {_BIG_BYTES} bytes," in out, "the total is BYTES (stat), not lines"

    @pytest.mark.unit
    def test_eviction_is_skipped_for_an_unsafe_id_and_read_back(self, sessions_root: Path):
        path, handle = open_stdout_tmp("")
        handle.write(b"b" * _BIG_BYTES)
        handle.close()

        out = read_or_evict_output(path, "", "utf-8")

        assert out == "b" * _BIG_BYTES, "no session dir -> the historical whole-read"
        assert not path.exists()
        assert list(sessions_root.iterdir()) == [], "an unsafe id must not create a dir"

    @pytest.mark.unit
    def test_discard_helper_survives_a_closed_handle_and_missing_file(self, sessions_root: Path):
        path, handle = open_stdout_tmp(_SID)
        handle.close()
        discard_stdout_tmp(handle, path)
        discard_stdout_tmp(None, None)
        assert not path.exists()


# ─────────────────────────────────────────────────────────────────────────────
# Spawn wiring — the child writes into a FILE, nothing rides a pipe
# ─────────────────────────────────────────────────────────────────────────────
class _FileWritingSyncProc:
    """Popen stand-in: writes into ``stdout`` and serves ``wait()``."""

    returncode = 0

    def __init__(self, record: list[dict[str, Any]], *args: Any, **kwargs: Any):
        record.append({"args": args, "kwargs": kwargs})
        self.waits = 0
        handle = kwargs.get("stdout")
        if hasattr(handle, "write"):
            handle.write(b"ok")

    def wait(self, timeout=None):
        self.waits += 1
        return self.returncode

    def communicate(self, timeout=None):  # pragma: no cover - must never be reached
        raise AssertionError("the fd-direct-write path must not drain a pipe")

    def kill(self):
        pass


class _FileWritingAsyncProc:
    """asyncio subprocess stand-in: same contract, async ``wait()``."""

    returncode = 0

    def __init__(self, record: list[dict[str, Any]], *args: Any, **kwargs: Any):
        record.append({"args": args, "kwargs": kwargs})
        handle = kwargs.get("stdout")
        if hasattr(handle, "write"):
            handle.write(b"ok")

    async def wait(self):
        return self.returncode

    def communicate(self):  # pragma: no cover - must never be reached
        raise AssertionError("the fd-direct-write path must not drain a pipe")

    def kill(self):
        pass


class TestSpawnWiring:
    @pytest.mark.unit
    def test_sync_spawn_hands_the_child_a_file_and_waits(
        self, tool: SafeShellTool, sessions_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        record: list[dict[str, Any]] = []
        monkeypatch.setattr(
            subprocess, "Popen", lambda *a, **kw: _FileWritingSyncProc(record, *a, **kw)
        )

        out = tool._run(["echo ok"], session_id=_SID)

        assert out == "ok"
        assert len(record) == 1
        kwargs = record[0]["kwargs"]
        handle = kwargs.get("stdout")
        assert hasattr(handle, "fileno"), "stdout must be a FILE handle, not a pipe"
        assert kwargs.get("stderr") is subprocess.STDOUT, "stderr stays merged into stdout"
        assert not _temp_files(sessions_root), "the temp file is consumed, not left behind"

    @pytest.mark.unit
    def test_async_spawn_hands_the_child_a_file_and_waits(
        self, tool: SafeShellTool, sessions_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        record: list[dict[str, Any]] = []

        async def fake_shell(*args: Any, **kwargs: Any) -> _FileWritingAsyncProc:
            return _FileWritingAsyncProc(record, *args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)

        out = asyncio.run(tool._arun(["echo ok"], session_id=_SID))

        assert out == "ok"
        assert len(record) == 1
        kwargs = record[0]["kwargs"]
        assert hasattr(kwargs.get("stdout"), "fileno"), "stdout must be a FILE handle"
        assert kwargs.get("stderr") is asyncio.subprocess.STDOUT
        assert not _temp_files(sessions_root)

    @pytest.mark.unit
    def test_sync_timeout_discards_the_temp_file(
        self, tool: SafeShellTool, sessions_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        class _HangingProc(_FileWritingSyncProc):
            returncode = None

            def wait(self, timeout=None):
                self.waits += 1
                if self.returncode is None and self.waits == 1:
                    raise subprocess.TimeoutExpired("cmd", timeout or 0)
                return self.returncode if self.returncode is not None else -9

            def poll(self):
                return self.returncode

            def terminate(self):
                self.returncode = -15

            def kill(self):
                self.returncode = -9

        monkeypatch.setattr(subprocess, "Popen", lambda *a, **kw: _HangingProc([], *a, **kw))

        out = tool._run(["sleep 999"], session_id=_SID)

        assert "timed out" in out
        assert not _temp_files(sessions_root), "a timed-out run must delete its stdout file"
        evicted = sessions_root / _SID / "evicted"
        assert not evicted.exists(), "a timed-out run evicts nothing"

    @pytest.mark.unit
    def test_async_timeout_discards_the_temp_file(
        self, tool: SafeShellTool, sessions_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        class _HangingAsyncProc(_FileWritingAsyncProc):
            returncode = None

            async def wait(self):
                raise TimeoutError("simulated wait_for timeout")

            def terminate(self):
                self.returncode = -15

            def kill(self):
                self.returncode = -9

        async def fake_shell(*args: Any, **kwargs: Any) -> _HangingAsyncProc:
            return _HangingAsyncProc([], *args, **kwargs)

        monkeypatch.setattr(asyncio, "create_subprocess_shell", fake_shell)

        out = asyncio.run(tool._arun(["sleep 999"], session_id=_SID))

        assert "timed out" in out
        assert not _temp_files(sessions_root)


# ─────────────────────────────────────────────────────────────────────────────
# End to end through a REAL child
# ─────────────────────────────────────────────────────────────────────────────
class TestRealChild:
    @pytest.mark.integration
    def test_small_output_round_trips(self, tool: SafeShellTool, sessions_root: Path):
        out = tool._run(["echo hello-fd"], session_id=_SID)

        assert out.strip() == "hello-fd"
        assert not _temp_files(sessions_root)
        assert not (sessions_root / _SID / "evicted").exists()

    @pytest.mark.integration
    def test_sync_large_output_is_evicted_with_a_preview(
        self, tool: SafeShellTool, sessions_root: Path
    ):
        out = tool._run([_BIG_CMD], session_id=_SID)

        evicted = list((sessions_root / _SID / "evicted").glob("terminal-*.log"))
        assert len(evicted) == 1, "an oversized output must land in evicted/"
        assert evicted[0].stat().st_size == _BIG_BYTES
        assert f"[evicted to: {evicted[0]}]" in out
        assert f"[full content: {_BIG_BYTES} bytes," in out
        assert "Use read_file(file_path=" in out
        assert len(out) < 20_000, "the preview must be small, not the payload"
        assert not _temp_files(sessions_root)

    @pytest.mark.integration
    def test_async_large_output_is_evicted_too(self, tool: SafeShellTool, sessions_root: Path):
        out = asyncio.run(tool._arun([_BIG_CMD], session_id=_SID))

        assert "[evicted to:" in out
        assert f"[full content: {_BIG_BYTES} bytes," in out
        assert not _temp_files(sessions_root)

    @pytest.mark.integration
    def test_distinct_outputs_get_distinct_evicted_names(
        self, tool: SafeShellTool, sessions_root: Path
    ):
        a = tool._run([f"head -c {_BIG_BYTES} /dev/zero | tr '\\0' 'a'"], session_id=_SID)
        b = tool._run([f"head -c {_BIG_BYTES} /dev/zero | tr '\\0' 'b'"], session_id=_SID)

        # Parse the HEADER line only: the preview body may carry a cut note
        # whose own punctuation would confuse a whole-payload split.
        names = set()
        for payload in (a, b):
            header = payload.splitlines()[0]
            names.add(Path(header.split("[evicted to: ", 1)[1].rstrip("]")).name)
        assert len(names) == 2, "the name hashes the content, so two outputs cannot collide"


# ---------------------------------------------------------------------------
# Per-call timeout (the model decides how long a command may run)
# ---------------------------------------------------------------------------


class TestPerCallTimeout:
    def test_the_resolver_defaults_clamps_and_rejects_nonsense(self):
        from agent.tools.terminal import (
            TERMINAL_MAX_TIMEOUT,
            TERMINAL_TIMEOUT,
            _resolve_timeout,
        )

        assert _resolve_timeout(None) == TERMINAL_TIMEOUT
        assert _resolve_timeout(0) == TERMINAL_TIMEOUT, "0 means 'unset', not 'instant'"
        assert _resolve_timeout(-5) == TERMINAL_TIMEOUT
        assert _resolve_timeout(60) == 60
        assert _resolve_timeout(TERMINAL_MAX_TIMEOUT + 5) == TERMINAL_MAX_TIMEOUT

    def test_the_schema_exposes_the_field_to_the_model(self):
        from agent.tools.terminal import SafeShellInput

        field = SafeShellInput.model_fields["timeout"]

        assert field.default is None
        assert "max" in field.description.lower() and "600" in field.description

    @pytest.mark.integration
    def test_a_short_timeout_cuts_a_long_command(self, tool: SafeShellTool, sessions_root: Path):
        import time

        started = time.monotonic()
        out = tool._run(["sleep 30"], session_id=_SID, timeout=2)
        elapsed = time.monotonic() - started

        assert "timed out after 2 seconds" in out
        assert elapsed < 10, f"the per-call timeout must bound the wait (took {elapsed:.1f}s)"

    @pytest.mark.integration
    def test_a_longer_timeout_lets_a_longer_command_finish(
        self, tool: SafeShellTool, sessions_root: Path
    ):
        out = tool._run(["sleep 1; echo finished-under-5s"], session_id=_SID, timeout=5)

        assert "finished-under-5s" in out


class _SilentSyncProc:
    """Popen stand-in: exits successfully without writing a byte."""

    returncode = 0

    def __init__(self, *args: Any, **kwargs: Any):
        self.kwargs = kwargs

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


class TestEmptyOutput:
    @pytest.mark.unit
    def test_an_empty_command_returns_a_default_hint(
        self, tool: SafeShellTool, sessions_root: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            subprocess, "Popen", lambda *a, **kw: _SilentSyncProc(*a, **kw)
        )

        out = tool._run(["true"], session_id=_SID)

        assert out == "(no output)"
        assert not _temp_files(sessions_root)
