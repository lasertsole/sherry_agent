"""Shared daemon-lifecycle contract: pid file, spawn, readiness, kill, idle exit.

Every local model daemon (STT / embed / reranker / local chat) runs on this
module, so these tests pin the ONE lifecycle implementation instead of each
daemon re-testing it. ``subprocess.Popen``, ``requests`` and ``os.kill`` are
mocked; the kill test uses a real short-lived child to prove the SIGTERM path
without touching a model daemon.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

import models.daemon.base as base

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


def _spec(tmp_path: Path, **overrides) -> base.DaemonSpec:
    values = {
        "name": "test",
        "host": "127.0.0.1",
        "port": 19012,
        "pid_path": tmp_path / "test.pid",
        "import_fn": "models.embed_model.text.daemon",
        "ready_wait_s": 0.5,
        "liveness_timeout_s": 0.5,
    }
    values.update(overrides)
    return base.DaemonSpec(**values)


class _FakePopen:
    def __init__(self, pid: int = 424242) -> None:
        self.pid = pid
        self.calls: list[dict] = []

    def __call__(self, *args, **kwargs):
        self.calls.append({"args": args, "kwargs": kwargs})
        return self


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: object = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class TestPidFile:
    def test_self_pid_is_alive(self):
        assert base.pid_alive(os.getpid()) is True

    def test_reaped_child_pid_is_not_alive(self):
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        assert base.pid_alive(proc.pid) is False

    def test_records_round_trip(self, tmp_path):
        spec = _spec(tmp_path)
        assert base.recorded_pid(spec) is None

        spec.pid_path.write_text("12345", encoding="utf-8")
        assert base.recorded_pid(spec) == 12345

        spec.pid_path.write_text("garbage", encoding="utf-8")
        assert base.recorded_pid(spec) is None

        spec.pid_path.write_text("0", encoding="utf-8")
        assert base.recorded_pid(spec) is None


class TestReadiness:
    def test_requires_ready_true(self, monkeypatch):
        responses = iter(
            [
                _FakeResponse(200, {"ready": False}),
                _FakeResponse(200, {"ready": True}),
                _FakeResponse(500, {"ready": True}),
                _FakeResponse(200, {"status": "ok"}),
            ]
        )
        monkeypatch.setattr(base.requests, "get", lambda *args, **kwargs: next(responses))
        spec = _spec(Path("/tmp"))

        assert base.is_ready(spec) is False  # warming
        assert base.is_ready(spec) is True
        assert base.is_ready(spec) is False  # HTTP error
        assert base.is_ready(spec) is False  # old payload shape

    def test_transport_failure_is_not_ready(self, monkeypatch):
        def _boom(*args, **kwargs):
            raise base.requests.ConnectionError("refused")

        monkeypatch.setattr(base.requests, "get", _boom)
        assert base.is_ready(_spec(Path("/tmp"))) is False


class TestSpawn:
    def test_checkout_command_runs_main_by_import(self, tmp_path, monkeypatch):
        monkeypatch.setattr(base, "INTERPRETER_PATH", Path("/venv/bin/python"))
        monkeypatch.setattr(base, "APP_HOME", Path("/data/home"))
        monkeypatch.setattr(base, "SHERRY_DESKTOP", False)
        monkeypatch.delattr(sys, "frozen", raising=False)

        command = base._child_command("models.embed_model.text.daemon")

        assert command[0] == "/venv/bin/python"
        assert command[1] == "-c"
        assert "from models.embed_model.text.daemon import main; main()" in command[2]
        assert command[3:] == ["--sherry-home", "/data/home"]

    def test_frozen_command_dispatches_through_the_backend_entry(self, tmp_path, monkeypatch):
        monkeypatch.setattr(base, "INTERPRETER_PATH", Path("/opt/sherry-backend"))
        monkeypatch.setattr(base, "APP_HOME", Path("/data/home"))
        monkeypatch.setattr(base, "SHERRY_DESKTOP", True)
        monkeypatch.setattr(sys, "frozen", True, raising=False)

        command = base._child_command("models.embed_model.text.daemon")

        assert command[1:3] == ["--sherry-daemon", "models.embed_model.text.daemon"]
        assert "--sherry-home" in command and "/data/home" in command
        assert "--sherry-desktop" in command

    def test_spawn_writes_pid_and_detaches(self, tmp_path, monkeypatch):
        spec = _spec(tmp_path)
        popen = _FakePopen()
        monkeypatch.setattr(base.subprocess, "Popen", popen)

        assert base.spawn(spec) is True
        assert spec.pid_path.read_text(encoding="utf-8") == "424242"
        kwargs = popen.calls[0]["kwargs"]
        assert kwargs["stdout"] is subprocess.DEVNULL
        assert kwargs["stderr"] is subprocess.DEVNULL

    def test_spawn_is_suppressed_by_a_live_recorded_pid(self, tmp_path, monkeypatch):
        spec = _spec(tmp_path)
        spec.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        popen = _FakePopen()
        monkeypatch.setattr(base.subprocess, "Popen", popen)

        assert base.spawn(spec) is False
        assert popen.calls == []

    def test_spawn_failure_is_swallowed(self, tmp_path, monkeypatch):
        spec = _spec(tmp_path)
        spec.pid_path.write_text("99999999", encoding="utf-8")

        def _boom(*args, **kwargs):
            raise OSError("cannot execute")

        monkeypatch.setattr(base.subprocess, "Popen", _boom)
        assert base.spawn(spec) is False
        assert not spec.pid_path.exists(), "a stale pid file must not survive a failed spawn"


class TestEnsureReady:
    def test_a_ready_daemon_is_not_spawned(self, tmp_path, monkeypatch):
        spec = _spec(tmp_path)
        monkeypatch.setattr(base, "is_ready", lambda _spec: True)
        monkeypatch.setattr(base, "spawn", lambda *a, **k: pytest.fail("must not spawn"))

        assert base.ensure_ready(spec) is base.ReadyState.READY

    def test_spawn_then_wait(self, tmp_path, monkeypatch):
        spec = _spec(tmp_path)
        states = iter([False, False, True])
        monkeypatch.setattr(base, "is_ready", lambda _spec: next(states, True))
        monkeypatch.setattr(base, "spawn", lambda *a, **k: True)
        monkeypatch.setattr(base, "register_atexit_kill", lambda _spec: None)

        assert base.ensure_ready(spec) is base.ReadyState.READY

    def test_timeout_with_a_live_process_is_warming(self, tmp_path, monkeypatch):
        spec = _spec(tmp_path, ready_wait_s=0.01)
        monkeypatch.setattr(base, "is_ready", lambda _spec: False)
        monkeypatch.setattr(base, "spawn", lambda *a, **k: True)
        monkeypatch.setattr(base, "register_atexit_kill", lambda _spec: None)

        assert base.ensure_ready(spec) is base.ReadyState.WARMING

    def test_a_failed_spawn_with_no_pid_is_unavailable(self, tmp_path, monkeypatch):
        spec = _spec(tmp_path, ready_wait_s=0.01)
        monkeypatch.setattr(base, "is_ready", lambda _spec: False)
        monkeypatch.setattr(base, "spawn", lambda *a, **k: False)
        monkeypatch.setattr(base, "recorded_pid", lambda _spec: None)
        monkeypatch.setattr(base, "wait_until_ready", lambda *a, **k: pytest.fail("must not wait"))

        assert base.ensure_ready(spec) is base.ReadyState.UNAVAILABLE

    def test_a_daemon_that_died_while_loading_is_unavailable(self, tmp_path, monkeypatch):
        spec = _spec(tmp_path, ready_wait_s=0.01)
        spec.pid_path.write_text("4242", encoding="utf-8")
        monkeypatch.setattr(base, "is_ready", lambda _spec: False)
        monkeypatch.setattr(base, "spawn", lambda *a, **k: True)
        monkeypatch.setattr(base, "register_atexit_kill", lambda _spec: None)
        monkeypatch.setattr(base, "pid_alive", lambda _pid: False)

        assert base.ensure_ready(spec) is base.ReadyState.UNAVAILABLE

    def test_a_daemon_joined_from_another_process_is_not_registered_for_kill(
        self, tmp_path, monkeypatch
    ):
        spec = _spec(tmp_path)
        spec.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        monkeypatch.setattr(base, "is_ready", lambda _spec: False)
        monkeypatch.setattr(base, "spawn", lambda *a, **k: False)  # live pid, still warming
        monkeypatch.setattr(base, "wait_until_ready", lambda _spec, _timeout=None: True)
        monkeypatch.setattr(base, "register_atexit_kill", lambda _spec: pytest.fail("foreign"))

        assert base.ensure_ready(spec) is base.ReadyState.READY


class _FakePsutil:
    """Minimal ``psutil`` stand-in for the pid-identity check."""

    class Error(Exception):
        """The subset of psutil.Error the check catches."""

    def __init__(self, cmdline: list[str] | None = None, error: Exception | None = None) -> None:
        self._cmdline = cmdline or []
        self._error = error

    def Process(self, _pid: int) -> SimpleNamespace:  # noqa: N802 - mirrors psutil's API
        if self._error is not None:
            raise self._error
        return SimpleNamespace(cmdline=lambda: self._cmdline)


class TestPidIdentity:
    def test_psutil_missing_is_treated_as_foreign(self, tmp_path, monkeypatch):
        """A pid we cannot inspect must never be signalled (fail closed)."""
        monkeypatch.setitem(sys.modules, "psutil", None)

        assert base._pid_is_daemon(_spec(tmp_path), 4242) is False

    def test_matching_command_line_is_the_daemon(self, tmp_path, monkeypatch):
        spec = _spec(tmp_path)
        monkeypatch.setitem(
            sys.modules, "psutil", _FakePsutil([sys.executable, "-c", spec.import_fn])
        )

        assert base._pid_is_daemon(spec, 4242) is True

    def test_another_command_line_is_foreign(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "psutil", _FakePsutil(["/usr/bin/other", "--flag"]))

        assert base._pid_is_daemon(_spec(tmp_path), 4242) is False

    def test_an_uninspectable_process_is_foreign(self, tmp_path, monkeypatch):
        monkeypatch.setitem(sys.modules, "psutil", _FakePsutil(error=_FakePsutil.Error("gone")))

        assert base._pid_is_daemon(_spec(tmp_path), 4242) is False


class TestKill:
    def test_kill_terminates_a_real_child_and_drops_the_file(self, tmp_path, monkeypatch):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        spec = _spec(tmp_path)
        spec.pid_path.write_text(str(proc.pid), encoding="utf-8")
        # The fake child's command line does not name the daemon module; the
        # identity check itself is covered by the mismatch test below.
        monkeypatch.setattr(base, "_pid_is_daemon", lambda _spec, _pid: True)
        try:
            base.kill(spec)

            assert not spec.pid_path.exists()
            proc.wait(timeout=5)
            assert proc.returncode is not None
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait(timeout=5)

    def test_kill_leaves_a_reused_pid_alone(self, tmp_path, monkeypatch):
        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
        spec = _spec(tmp_path)
        spec.pid_path.write_text(str(proc.pid), encoding="utf-8")
        monkeypatch.setattr(base, "_pid_is_daemon", lambda _spec, _pid: False)
        try:
            base.kill(spec)

            assert not spec.pid_path.exists()
            assert proc.poll() is None, "an unrelated process must not be signalled"
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_kill_ignores_a_stale_pid(self, tmp_path, monkeypatch):
        spec = _spec(tmp_path)
        spec.pid_path.write_text("99999999", encoding="utf-8")
        monkeypatch.setattr(base, "pid_alive", lambda _pid: False)
        base.kill(spec)
        assert not spec.pid_path.exists()

    def test_kill_never_signals_the_calling_process(self, tmp_path, monkeypatch):
        spec = _spec(tmp_path)
        spec.pid_path.write_text(str(os.getpid()), encoding="utf-8")
        monkeypatch.setattr(base.os, "kill", lambda *a: pytest.fail("must not signal self"))

        base.kill(spec)

        assert not spec.pid_path.exists()

    def test_atexit_registration_is_idempotent(self, tmp_path, monkeypatch):
        registered: list = []
        monkeypatch.setattr(base.atexit, "register", lambda fn, *a: registered.append(fn))
        monkeypatch.setattr(base, "_ATEXIT_SPECS", set())
        spec = _spec(tmp_path)

        base.register_atexit_kill(spec)
        base.register_atexit_kill(spec)

        assert len(registered) == 1


class TestRequestJson:
    def test_posts_and_decodes(self, tmp_path, monkeypatch):
        seen: list = []

        def _post(url, json=None, timeout=None):  # noqa: A002 - mirror requests
            seen.append({"url": url, "json": json, "timeout": timeout})
            return _FakeResponse(200, {"data": [1, 2]})

        monkeypatch.setattr(base.requests, "post", _post)
        spec = _spec(tmp_path)

        assert base.request_json(spec, "/embeddings", {"input": ["a"]}, timeout_s=3) == {
            "data": [1, 2]
        }
        assert seen[0]["url"].endswith("/embeddings")
        assert seen[0]["timeout"] == 3

    def test_http_error_carries_bounded_detail(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            base.requests,
            "post",
            lambda *a, **k: _FakeResponse(500, None, text="x" * 1000),
        )
        with pytest.raises(base.DaemonError) as error:
            base.request_json(_spec(tmp_path), "/x", {}, timeout_s=1)
        assert len(str(error.value)) < 500

    def test_non_json_body_is_an_error(self, tmp_path, monkeypatch):
        monkeypatch.setattr(base.requests, "post", lambda *a, **k: _FakeResponse(200, None))
        with pytest.raises(base.DaemonError):
            base.request_json(_spec(tmp_path), "/x", {}, timeout_s=1)


class TestIdleWatchdog:
    def test_fires_after_the_idle_window(self):
        fired = threading.Event()
        watchdog = base.IdleWatchdog(0.6, fired.set)
        watchdog.start()
        try:
            assert fired.wait(timeout=3.0) is True
        finally:
            watchdog.stop()

    def test_touches_postpone_the_exit(self):
        fired = threading.Event()
        watchdog = base.IdleWatchdog(1.0, fired.set, interval_s=0.5)
        watchdog.start()
        try:
            for _ in range(3):
                time.sleep(0.3)
                watchdog.touch()

            assert fired.is_set() is False
        finally:
            watchdog.stop()


def test_send_json_writes_one_json_body():
    written: list[bytes] = []

    class _Handler:
        wfile = SimpleNamespace(write=written.append)

        def __init__(self):
            self.status: int | None = None
            self.headers: list[tuple[str, str]] = []

        def send_response(self, status):
            self.status = status

        def send_header(self, key, value):
            self.headers.append((key, value))

        def end_headers(self):
            pass

    handler = _Handler()
    base.send_json(handler, 200, {"ready": True, "text": "你好"})

    assert handler.status == 200
    assert json.loads(b"".join(written)) == {"ready": True, "text": "你好"}
    assert ("Content-Type", "application/json; charset=utf-8") in handler.headers
