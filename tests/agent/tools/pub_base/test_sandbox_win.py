"""Windows sandbox backend: config, launcher shape, and real containment paths.

The launcher's end-to-end tests run only on Windows (they create a real
restricted token and Job Object). Everything else runs on every platform: the
payload contract, the config parsing and the fail-open dispatch are platform
independent, and CI's Linux leg still guards them.
"""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
import time
import zlib
from pathlib import Path

import pytest
from loguru import logger

from agent.tools.pub_base import sandbox_win
from agent.tools.pub_base.sandbox import get_backend, SandboxPolicy
from agent.tools.pub_base.sandbox_win import (
    WinSandboxBackend,
    build_launcher_source,
    read_win_config,
)

pytestmark = [pytest.mark.module, pytest.mark.timeout(180)]

_WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Win32 launcher")

_B64_RE = re.compile(r"b64decode\('([^']+)'\)")
_PAYLOAD_RE = re.compile(r'PAYLOAD = json\.loads\(r"""(.*?)"""\)', re.S)


def _decode_launcher(argv: list[str]) -> str:
    """Decode the ``python -c`` body back to the launcher source."""
    match = _B64_RE.search(argv[2])
    assert match, f"not a launcher command: {argv[2][:80]!r}"
    return zlib.decompress(base64.b64decode(match.group(1))).decode("utf-8")


def _payload_of(argv: list[str]) -> dict:
    """Extract the JSON payload the launcher will read."""
    match = _PAYLOAD_RE.search(_decode_launcher(argv))
    assert match, "launcher source carries no PAYLOAD literal"
    return json.loads(match.group(1))


@pytest.fixture
def log_capture():
    messages: list[str] = []
    sink_id = logger.add(lambda m: messages.append(str(m)), level="WARNING")
    yield messages
    logger.remove(sink_id)


# --------------------------------------------------------------------------
# Config (env-overridable bounds)
# --------------------------------------------------------------------------


def test_defaults_when_nothing_is_configured():
    config = read_win_config({})
    assert config.max_processes == 50
    assert config.max_memory_mb == 4096
    assert config.block_network is False
    assert config.appcontainer is True


def test_appcontainer_is_on_unless_explicitly_disabled():
    for raw in ("", "1", "true", "on", "yes"):
        assert read_win_config({"SHERRY_WIN_SANDBOX_APPCONTAINER": raw}).appcontainer is True
    for raw in ("0", "false", "off", "no", "OFF"):
        assert read_win_config({"SHERRY_WIN_SANDBOX_APPCONTAINER": raw}).appcontainer is False


def test_values_are_read_and_clamped():
    assert read_win_config({"SHERRY_WIN_SANDBOX_MAX_PROCESSES": "7"}).max_processes == 7
    assert read_win_config({"SHERRY_WIN_SANDBOX_MAX_PROCESSES": "0"}).max_processes == 1
    assert read_win_config({"SHERRY_WIN_SANDBOX_MAX_PROCESSES": "999999"}).max_processes == 10_000
    assert read_win_config({"SHERRY_WIN_SANDBOX_MAX_MEMORY_MB": "128"}).max_memory_mb == 128
    assert read_win_config({"SHERRY_WIN_SANDBOX_MAX_MEMORY_MB": "1"}).max_memory_mb == 64
    assert (
        read_win_config({"SHERRY_WIN_SANDBOX_MAX_MEMORY_MB": "999999999"}).max_memory_mb
        == 1_048_576
    )


def test_an_unparseable_value_falls_back_to_the_default(log_capture):
    assert read_win_config({"SHERRY_WIN_SANDBOX_MAX_PROCESSES": "many"}).max_processes == 50
    assert read_win_config({"SHERRY_WIN_SANDBOX_MAX_MEMORY_MB": "4 GB"}).max_memory_mb == 4096
    assert any("not an integer" in line for line in log_capture)


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", "yes", "on", " True "])
def test_block_network_truthy_values(raw: str):
    assert read_win_config({"SHERRY_WIN_SANDBOX_BLOCK_NETWORK": raw}).block_network is True


@pytest.mark.parametrize("raw", ["", "0", "false", "no", "off", "later"])
def test_block_network_falsey_values(raw: str):
    assert read_win_config({"SHERRY_WIN_SANDBOX_BLOCK_NETWORK": raw}).block_network is False


def test_reads_the_process_environment_by_default(monkeypatch):
    monkeypatch.setenv("SHERRY_WIN_SANDBOX_MAX_PROCESSES", "12")
    assert read_win_config().max_processes == 12


# --------------------------------------------------------------------------
# Launcher payload contract
# --------------------------------------------------------------------------


def test_wrap_returns_a_self_contained_python_launcher(tmp_path):
    backend = WinSandboxBackend()
    env = {"PATH": "x"}
    argv, returned = backend.wrap(["cmd.exe", "/c", "echo hi"], env, [tmp_path])

    assert argv[:2] == [sys.executable, "-c"]
    assert returned == {**env, "TEMP": returned["TEMP"], "TMP": returned["TMP"]}, (
        "wrap must keep the scrubbed env and only repoint temp at a granted root"
    )
    assert returned["TEMP"] == returned["TMP"]
    assert len(argv[2]) < 32_000, "the launcher must stay under the Windows argv limit"

    payload = _payload_of(argv)
    assert payload["cmd"] == ["cmd.exe", "/c", "echo hi"]
    assert payload["max_processes"] == 50
    assert payload["max_memory_mb"] == 4096
    assert payload["block_network"] is False
    assert payload["appcontainer"] is True
    assert payload["appcontainer_name"].startswith("sherry-agent-")
    assert str(tmp_path.resolve()) in payload["roots"]
    # TEMP_DIR is appended by the shared normalizer, exactly like the POSIX backends.
    from config.path import TEMP_DIR

    assert str(Path(TEMP_DIR).resolve()) in payload["roots"]


def test_payload_round_trips_awkward_commands(tmp_path):
    awkward = 'echo "a b" %PATH% $(date) \n second line """ end'
    argv, _ = WinSandboxBackend().wrap(["bash", "-c", awkward], {}, [tmp_path])
    assert _payload_of(argv)["cmd"][2] == awkward


def test_payload_carries_the_configured_limits(tmp_path, monkeypatch):
    monkeypatch.setenv("SHERRY_WIN_SANDBOX_MAX_PROCESSES", "9")
    monkeypatch.setenv("SHERRY_WIN_SANDBOX_MAX_MEMORY_MB", "256")
    argv, _ = WinSandboxBackend().wrap(["cmd.exe", "/c", "ver"], {}, [tmp_path])
    payload = _payload_of(argv)
    assert payload["max_processes"] == 9
    assert payload["max_memory_mb"] == 256


def test_wrap_refuses_an_empty_write_surface():
    with pytest.raises(ValueError, match="at least one writable root"):
        WinSandboxBackend().wrap(["cmd.exe", "/c", "ver"], {}, [])


def test_wrap_dedupes_the_write_surface(tmp_path):
    argv, _ = WinSandboxBackend().wrap(["cmd.exe", "/c", "ver"], {}, [tmp_path, tmp_path])
    roots = _payload_of(argv)["roots"]
    assert roots.count(str(tmp_path.resolve())) == 1


def test_block_network_is_carried_in_the_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("SHERRY_WIN_SANDBOX_BLOCK_NETWORK", "1")
    backend = WinSandboxBackend()

    first, _ = backend.wrap(["cmd.exe", "/c", "ver"], {}, [tmp_path])
    second, _ = backend.wrap(["cmd.exe", "/c", "ver"], {}, [tmp_path])

    assert _payload_of(first)["block_network"] is True
    assert _payload_of(second)["block_network"] is True


def test_appcontainer_switch_is_carried_in_the_payload(tmp_path, monkeypatch):
    monkeypatch.setenv("SHERRY_WIN_SANDBOX_APPCONTAINER", "0")
    argv, _ = WinSandboxBackend().wrap(["cmd.exe", "/c", "ver"], {}, [tmp_path])
    assert _payload_of(argv)["appcontainer"] is False


def test_allow_network_suppresses_the_block_request(tmp_path, monkeypatch):
    monkeypatch.setenv("SHERRY_WIN_SANDBOX_BLOCK_NETWORK", "1")
    argv, _ = WinSandboxBackend().wrap(["cmd.exe", "/c", "ver"], {}, [tmp_path], allow_network=True)
    assert _payload_of(argv)["block_network"] is False


@pytest.mark.parametrize(
    "needle",
    [
        "CreateRestrictedToken",
        "CreateJobObjectW",
        "SetInformationJobObject",
        "AssignProcessToJobObject",
        "JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE",
        "STARTF_USESTDHANDLES",
        "CreateProcessAsUserW",
        "CreateProcessW",
        "ResumeThread",
        "WaitForSingleObject",
        "CreateAppContainerProfile",
        "DeriveAppContainerSidFromAppContainerName",
        "DeriveCapabilitySidsFromName",
        "PROC_THREAD_ATTRIBUTE_SECURITY_CAPABILITIES",
        "EXTENDED_STARTUPINFO_PRESENT",
        "SetEntriesInAclW",
        "SetNamedSecurityInfoW",
        "KILL_ON_JOB_CLOSE",
        "venv_home",
        "pyvenv.cfg",
        "system_path",
        "build_command_line",
    ],
)
def test_launcher_source_keeps_every_layer(needle: str):
    """A refactor that drops a fallback must fail here, not silently at runtime."""
    source = _decode_launcher(
        WinSandboxBackend().wrap(["cmd.exe", "/c", "ver"], {}, [Path.cwd()])[0]
    )
    assert needle in source


def test_build_launcher_source_is_pure_and_payload_specific():
    first = build_launcher_source({"cmd": ["a"], "roots": [], "max_processes": 1})
    second = build_launcher_source({"cmd": ["b"], "roots": [], "max_processes": 2})
    assert first.startswith("import base64,zlib;exec(zlib.decompress(base64.b64decode('")
    assert first != second
    assert _payload_of([sys.executable, "-c", first])["cmd"] == ["a"]


# --------------------------------------------------------------------------
# Probe (cached, never raises)
# --------------------------------------------------------------------------


def test_probe_returns_false_without_win32(monkeypatch):
    calls: list[int] = []

    def unavailable():
        calls.append(1)
        return None

    monkeypatch.setattr(sandbox_win, "_load_win32", unavailable)
    backend = WinSandboxBackend()
    assert backend.probe() is False
    assert backend.probe() is False
    assert len(calls) == 1, "the probe result must be cached for the process lifetime"


@pytest.mark.skipif(sys.platform == "win32", reason="a real Windows host has Win32")
def test_probe_returns_false_on_a_non_windows_host():
    assert WinSandboxBackend().probe() is False


# --------------------------------------------------------------------------
# Real Win32 paths (Windows only)
# --------------------------------------------------------------------------


@_WINDOWS_ONLY
def test_probe_reports_true_on_windows():
    assert WinSandboxBackend().probe() is True


@_WINDOWS_ONLY
def test_registered_dispatch_reaches_the_windows_backend():
    backend = get_backend(SandboxPolicy.AUTO)
    assert isinstance(backend, WinSandboxBackend)


@_WINDOWS_ONLY
def test_launcher_propagates_stdout_stderr_and_exit_code(tmp_path):
    argv, env = WinSandboxBackend().wrap(
        ["cmd.exe", "/c", "echo out-line & echo err-line 1>&2 & exit /b 7"],
        {},
        [tmp_path],
    )
    result = subprocess.run(argv, capture_output=True, env={**os.environ, **env}, timeout=120)

    assert result.returncode == 7
    assert b"out-line" in result.stdout
    assert b"err-line" in result.stderr


@_WINDOWS_ONLY
def test_launcher_inherits_cwd_and_environment(tmp_path):
    code = (
        "import os, sys;print(os.getcwd());print(os.environ.get('SHERRY_TEST_MARKER'));sys.exit(3)"
    )
    argv, _ = WinSandboxBackend().wrap([sys.executable, "-c", code], {}, [tmp_path])
    result = subprocess.run(
        argv,
        capture_output=True,
        cwd=tmp_path,
        env={**os.environ, "SHERRY_TEST_MARKER": "carried"},
        timeout=120,
    )
    out = result.stdout.decode("utf-8", "replace")

    assert result.returncode == 3
    assert str(tmp_path) in out
    assert "carried" in out


@_WINDOWS_ONLY
def test_job_object_caps_the_process_tree(tmp_path, monkeypatch):
    monkeypatch.setenv("SHERRY_WIN_SANDBOX_MAX_PROCESSES", "4")
    code = (
        "import subprocess\n"
        "ok = fail = 0\n"
        "for _ in range(6):\n"
        "    try:\n"
        "        subprocess.Popen(['cmd.exe', '/c', 'ping -n 3 127.0.0.1 >nul'])\n"
        "        ok += 1\n"
        "    except OSError:\n"
        "        fail += 1\n"
        "print(ok, fail, flush=True)\n"
    )
    argv, _ = WinSandboxBackend().wrap([sys.executable, "-c", code], {}, [tmp_path])
    result = subprocess.run(argv, capture_output=True, env=os.environ.copy(), timeout=180)

    parts = result.stdout.decode("utf-8", "replace").split()
    assert parts, result.stderr.decode("utf-8", "replace")
    ok, fail = (int(part) for part in parts)
    assert ok + fail == 6
    # launcher + python + two children fit under the limit of 4; the rest fail.
    assert fail >= 1, "the ActiveProcessLimit was not applied"
    assert ok >= 1, "the limit must not block the command itself"


@_WINDOWS_ONLY
def test_job_memory_limit_is_enforced(tmp_path, monkeypatch):
    monkeypatch.setenv("SHERRY_WIN_SANDBOX_MAX_MEMORY_MB", "64")
    code = (
        "try:\n"
        "    bytearray(300 * 1024 * 1024)\n"
        "    print('allocated', flush=True)\n"
        "except MemoryError:\n"
        "    print('memory-error', flush=True)\n"
    )
    argv, _ = WinSandboxBackend().wrap([sys.executable, "-c", code], {}, [tmp_path])
    result = subprocess.run(argv, capture_output=True, env=os.environ.copy(), timeout=180)

    assert b"memory-error" in result.stdout, result.stdout + result.stderr


@_WINDOWS_ONLY
def test_kill_on_job_close_reaps_grandchildren(tmp_path):
    """Killing the launcher (the tool's timeout path) kills the whole tree."""
    marker = tmp_path / "marker.txt"
    grandchild = f"import pathlib, time;time.sleep(4);pathlib.Path(r'{marker}').write_text('alive')"
    code = (
        "import subprocess, sys, time\n"
        f"subprocess.Popen([sys.executable, '-c', {grandchild!r}])\n"
        "print('started', flush=True)\n"
        "time.sleep(60)\n"
    )
    argv, _ = WinSandboxBackend().wrap([sys.executable, "-c", code], {}, [tmp_path])
    process = subprocess.Popen(
        argv,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        env=os.environ.copy(),
    )
    try:
        assert process.stdout is not None
        assert process.stdout.readline().strip() == b"started"
        time.sleep(1.0)
    finally:
        process.kill()
        process.wait(timeout=60)

    time.sleep(4.5)
    assert not marker.exists(), "the grandchild outlived the launcher's job object"


# --------------------------------------------------------------------------
# AppContainer (layer 2) — Windows only
# --------------------------------------------------------------------------


def _run_wrapped(cmd: list[str], roots: list[Path]) -> subprocess.CompletedProcess:
    argv, env = WinSandboxBackend().wrap(cmd, dict(os.environ), roots)
    return subprocess.run(argv, capture_output=True, env=env, timeout=180)


@_WINDOWS_ONLY
def test_appcontainer_confines_writes_and_reads(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "existing.txt").write_text("existing", encoding="utf-8")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("secret", encoding="utf-8")
    code = (
        "import os, pathlib\n"
        f"root = pathlib.Path({str(root)!r})\n"
        f"outside = pathlib.Path({str(outside)!r})\n"
        "(root / 'new.txt').write_text('new')\n"
        "print('inside-write-ok')\n"
        "print('existing', (root / 'existing.txt').read_text())\n"
        "try:\n"
        "    (pathlib.Path(os.environ['TEMP']) / 'sandbox-temp.txt').write_text('t')\n"
        "    print('temp-write-ok')\n"
        "except OSError as exc:\n"
        "    print('temp-write-denied', type(exc).__name__)\n"
        "try:\n"
        "    (outside / 'out.txt').write_text('out')\n"
        "    print('outside-write-ok')\n"
        "except OSError as exc:\n"
        "    print('outside-write-denied', type(exc).__name__)\n"
        "try:\n"
        "    print('outside-read', (outside / 'secret.txt').read_text())\n"
        "except OSError as exc:\n"
        "    print('outside-read-denied', type(exc).__name__)\n"
    )

    result = _run_wrapped([sys.executable, "-c", code], [root])
    out = result.stdout.decode("utf-8", "replace")

    assert result.returncode == 0, out + result.stderr.decode("utf-8", "replace")
    assert "inside-write-ok" in out
    assert "existing existing" in out
    assert "temp-write-ok" in out, "the AppContainer profile temp must stay writable"
    assert "outside-write-ok" not in out
    assert "outside-read-denied" in out
    assert not (outside / "out.txt").exists()


@_WINDOWS_ONLY
def test_appcontainer_allows_network_by_default_and_blocks_on_request(tmp_path, monkeypatch):
    code = (
        "import socket\n"
        "try:\n"
        "    socket.create_connection(('192.0.2.1', 80), timeout=1).close()\n"
        "    print('allowed')\n"
        "except PermissionError:\n"
        "    print('blocked')\n"
        "except OSError:\n"
        "    print('allowed')\n"
    )

    monkeypatch.delenv("SHERRY_WIN_SANDBOX_BLOCK_NETWORK", raising=False)
    allowed = _run_wrapped([sys.executable, "-c", code], [tmp_path]).stdout.decode(
        "utf-8", "replace"
    )
    assert "allowed" in allowed, allowed

    monkeypatch.setenv("SHERRY_WIN_SANDBOX_BLOCK_NETWORK", "1")
    blocked = _run_wrapped([sys.executable, "-c", code], [tmp_path]).stdout.decode(
        "utf-8", "replace"
    )
    assert "blocked" in blocked, blocked


@_WINDOWS_ONLY
def test_appcontainer_off_disables_file_confinement(tmp_path, monkeypatch):
    monkeypatch.setenv("SHERRY_WIN_SANDBOX_APPCONTAINER", "0")
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    code = (
        "import pathlib\n"
        f"pathlib.Path({str(outside / 'out.txt')!r}).write_text('out')\n"
        "print('outside-write-ok')\n"
    )

    result = _run_wrapped([sys.executable, "-c", code], [root])
    out = result.stdout.decode("utf-8", "replace")

    assert "outside-write-ok" in out, out + result.stderr.decode("utf-8", "replace")


@_WINDOWS_ONLY
def test_a_broken_profile_still_runs_the_command(tmp_path):
    """Layer 2 failure is fail-open: the restricted-token + Job path still runs."""
    payload = {
        "cmd": [r"C:\Windows\System32\cmd.exe", "/c", "echo fallback-ok"],
        "roots": [str(tmp_path)],
        "max_processes": 50,
        "max_memory_mb": 4096,
        "block_network": False,
        "appcontainer": True,
        "appcontainer_name": "sherry\\invalid/profile name",
    }
    launcher = build_launcher_source(payload)
    result = subprocess.run(
        [sys.executable, "-c", launcher],
        capture_output=True,
        env=os.environ.copy(),
        timeout=120,
    )

    assert result.returncode == 0
    assert b"fallback-ok" in result.stdout
    stderr = result.stderr.decode("utf-8", "replace")
    # Either the profile is refused or the SID it derives names no profile
    # (CreateProcess then fails); both must log and fall back to layers 1+3.
    assert (
        "AppContainer profile unavailable" in stderr
        or "AppContainer process creation failed" in stderr
    ), stderr
