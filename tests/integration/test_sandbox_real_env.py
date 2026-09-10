"""Real-environment integration tests for the OS sandbox backends (audit 1-D).

Unlike the construction-only unit tests (which inspect the argv returned by
``wrap``), these tests EXECUTE a real ``bwrap`` / ``sandbox-exec`` and assert
the sandbox is actually effective at runtime:

- a write outside the writable allowlist is refused;
- a parent-process environment variable is not visible inside (``--clearenv``)
  unless explicitly injected via ``--setenv``;
- ``--unshare-all`` pid isolation is exercised.

Real-runtime cases skip when the tool is absent OR non-functional (bwrap is on
PATH on many hosts but fails to create unprivileged user namespaces). The
``probe()`` fallback cases are hermetic and run on every platform.
"""

from __future__ import annotations

import os
import platform
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import agent.tools.pub_base.sandbox_bwrap as sandbox_bwrap
from agent.tools.pub_base.sandbox import SandboxPolicy, get_backend
from agent.tools.pub_base.sandbox_bwrap import BwrapBackend
from agent.tools.pub_base.sandbox_seatbelt import SeatbeltBackend
from config.path import ROOT_DIR, TEMP_DIR

pytestmark = [pytest.mark.integration]

_PROBE_SMOKE = ["bwrap", "--ro-bind", "/", "/", "--proc", "/proc", "--dev", "/dev", "true"]


def _bwrap_usable() -> bool:
    """Return True only when bwrap exists AND can actually create its namespaces."""
    if shutil.which("bwrap") is None:
        return False
    try:
        result = subprocess.run(_PROBE_SMOKE, capture_output=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0


_BWRAP_USABLE = _bwrap_usable()
_SANDBOX_EXEC = shutil.which("sandbox-exec")

requires_bwrap = pytest.mark.skipif(
    not _BWRAP_USABLE,
    reason="bwrap absent or non-functional (user namespaces unavailable)",
)
requires_seatbelt = pytest.mark.skipif(
    sys.platform != "darwin" or _SANDBOX_EXEC is None,
    reason="macOS sandbox-exec unavailable",
)


@pytest.fixture(autouse=True)
def _reset_probe_caches():
    """Class-level probe caches must not leak between tests."""
    BwrapBackend._probe_cache = None
    SeatbeltBackend._probe_result = None
    yield
    BwrapBackend._probe_cache = None
    SeatbeltBackend._probe_result = None


def _bwrap_run(cmd: list[str], wrap_env: dict[str, str] | None = None, timeout: int = 15):
    """Wrap cmd with the real backend and execute it through a real bwrap.

    The parent environment is inherited by the bwrap process on purpose: this
    is what proves ``--clearenv`` drops it (a child that sees the inherited env
    would mean the flag is ineffective).
    """
    if wrap_env is None:
        wrap_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    argv, _ = BwrapBackend().wrap(cmd, wrap_env)
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


# ─────────────────────────────────────────────────────────────────────────────
# Linux / bwrap — real isolation
# ─────────────────────────────────────────────────────────────────────────────
@requires_bwrap
class TestBwrapRealIsolation:
    def test_write_outside_allowlist_is_blocked(self):
        target = Path.home() / f"sherry_sandbox_probe_{os.getpid()}"
        try:
            proc = _bwrap_run(["/bin/sh", "-c", f"echo sandbox-escape > {target}"])
            assert proc.returncode != 0, (proc.stdout, proc.stderr)
            assert not target.exists(), "read-only root was writable inside bwrap"
        finally:
            target.unlink(missing_ok=True)

    def test_write_inside_writable_allowlist_succeeds(self):
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        target = TEMP_DIR / f"sherry_sandbox_probe_{os.getpid()}"
        try:
            proc = _bwrap_run(["/bin/sh", "-c", f"echo ok > {target}"])
            assert proc.returncode == 0, (proc.stdout, proc.stderr)
            assert target.read_text() == "ok\n"
        finally:
            target.unlink(missing_ok=True)

    def test_parent_env_not_visible_without_setenv(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("SHERRY_SANDBOX_CANARY", "leaked-canary-value")
        proc = _bwrap_run(["/bin/sh", "-c", 'printf "%s" "${SHERRY_SANDBOX_CANARY:-MISSING}"'])
        assert proc.stdout == "MISSING", "parent env leaked through --clearenv"

    def test_explicitly_set_env_is_visible(self):
        wrap_env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "SHERRY_SANDBOX_EXPLICIT": "present",
        }
        proc = _bwrap_run(
            ["/bin/sh", "-c", 'printf "%s" "$SHERRY_SANDBOX_EXPLICIT"'], wrap_env=wrap_env
        )
        assert proc.stdout == "present", "--setenv did not inject the allowlisted variable"

    def test_pid_namespace_hides_parent_process(self):
        script = f"test -d /proc/{os.getpid()} && echo VISIBLE || echo HIDDEN"
        proc = _bwrap_run(["/bin/sh", "-c", script])
        assert proc.stdout == "HIDDEN", "--unshare-all did not isolate the pid namespace"

    def test_network_namespace_blocks_loopback_connect(self):
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(1)
            port = server.getsockname()[1]
            script = (
                "import socket\n"
                "s = socket.socket()\n"
                "s.settimeout(2)\n"
                "try:\n"
                f"    s.connect(('127.0.0.1', {port}))\n"
                "    print('REACHABLE')\n"
                "except OSError:\n"
                "    print('BLOCKED')\n"
            )
            proc = _bwrap_run([sys.executable, "-c", script])
        assert proc.stdout.strip() == "BLOCKED", "--unshare-all did not isolate the network"


# ─────────────────────────────────────────────────────────────────────────────
# macOS / seatbelt — real isolation
# ─────────────────────────────────────────────────────────────────────────────
@requires_seatbelt
class TestSeatbeltRealIsolation:
    def test_write_outside_allowlist_is_blocked(self):
        target = Path.home() / f"sherry_seatbelt_probe_{os.getpid()}"
        try:
            argv, _ = SeatbeltBackend().wrap(
                ["/bin/sh", "-c", f"echo sandbox-escape > {target}"], {}
            )
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
            assert proc.returncode != 0, (proc.stdout, proc.stderr)
            assert not target.exists(), "seatbelt deny file-write* did not block the write"
        finally:
            target.unlink(missing_ok=True)

    def test_write_inside_writable_allowlist_succeeds(self):
        target = ROOT_DIR / f".sherry_seatbelt_probe_{os.getpid()}"
        try:
            argv, _ = SeatbeltBackend().wrap(["/bin/sh", "-c", f"echo ok > {target}"], {})
            proc = subprocess.run(argv, capture_output=True, text=True, timeout=15)
            assert proc.returncode == 0, (proc.stdout, proc.stderr)
            assert target.read_text() == "ok\n"
        finally:
            target.unlink(missing_ok=True)


# ─────────────────────────────────────────────────────────────────────────────
# probe() fallback — hermetic, runs on every platform
# ─────────────────────────────────────────────────────────────────────────────
class TestProbeFallback:
    def test_bwrap_probe_false_when_tool_absent(self, monkeypatch: pytest.MonkeyPatch):
        BwrapBackend._probe_cache = None
        monkeypatch.setenv("PATH", "")
        assert BwrapBackend().probe() is False

    def test_bwrap_probe_true_when_smoke_test_succeeds(self, monkeypatch: pytest.MonkeyPatch):
        BwrapBackend._probe_cache = None
        completed = subprocess.CompletedProcess(args=_PROBE_SMOKE, returncode=0)
        monkeypatch.setattr(sandbox_bwrap.subprocess, "run", lambda *a, **kw: completed)
        assert BwrapBackend().probe() is True

    def test_seatbelt_probe_false_when_which_missing(self, monkeypatch: pytest.MonkeyPatch):
        SeatbeltBackend._probe_result = None
        monkeypatch.setattr(shutil, "which", lambda name: None)
        assert SeatbeltBackend().probe() is False

    def test_seatbelt_probe_true_when_which_present(self, monkeypatch: pytest.MonkeyPatch):
        SeatbeltBackend._probe_result = None
        monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/sandbox-exec")
        assert SeatbeltBackend().probe() is True

    def test_get_backend_auto_degrades_when_bwrap_absent(self, monkeypatch: pytest.MonkeyPatch):
        BwrapBackend._probe_cache = None
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setenv("PATH", "")
        assert get_backend(SandboxPolicy.AUTO) is None

    def test_get_backend_required_raises_when_bwrap_absent(self, monkeypatch: pytest.MonkeyPatch):
        BwrapBackend._probe_cache = None
        monkeypatch.setattr(platform, "system", lambda: "Linux")
        monkeypatch.setenv("PATH", "")
        with pytest.raises(RuntimeError, match="Required sandbox unavailable on Linux"):
            get_backend(SandboxPolicy.REQUIRED)


# ─────────────────────────────────────────────────────────────────────────────
# Real probe() availability — confirms the probe matches the real tool state
# ─────────────────────────────────────────────────────────────────────────────
@requires_bwrap
def test_real_bwrap_probe_returns_true():
    BwrapBackend._probe_cache = None
    assert BwrapBackend().probe() is True


@requires_seatbelt
def test_real_seatbelt_probe_returns_true():
    SeatbeltBackend._probe_result = None
    assert SeatbeltBackend().probe() is True
