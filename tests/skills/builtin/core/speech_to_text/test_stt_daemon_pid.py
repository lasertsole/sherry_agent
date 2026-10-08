"""Unit tests for STT daemon spawn de-duplication via PID file (audit round 2 #27).

``_spawn_daemon`` must consult the PID file before spawning: a live recorded
PID suppresses the spawn, a stale PID file (dead process) is refreshed by a
respawn. ``subprocess.Popen`` is fully mocked — no real daemon is started.
"""

import os
import subprocess

import pytest

import skills.builtin.core.speech_to_text.scripts.core as stt_core

pytestmark = pytest.mark.unit


@pytest.fixture
def pid_path(tmp_path, monkeypatch):
    path = tmp_path / "stt_daemon.pid"
    monkeypatch.setattr(stt_core, "_PID_PATH", path)
    return path


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


class _FakePopen:
    def __init__(self, calls: list, pid: int = 424242):
        self._calls = calls
        self.pid = pid

    def __call__(self, *args, **kwargs):
        self._calls.append((args, kwargs))
        return self


def test_spawn_writes_pid_file_when_none_exists(pid_path, monkeypatch):
    calls: list = []
    monkeypatch.setattr(stt_core.subprocess, "Popen", _FakePopen(calls))

    stt_core._spawn_daemon()

    assert len(calls) == 1
    assert pid_path.read_text() == "424242"


def test_spawn_skipped_when_pid_file_holds_live_process(pid_path, monkeypatch):
    pid_path.write_text(str(os.getpid()))
    calls: list = []
    monkeypatch.setattr(stt_core.subprocess, "Popen", _FakePopen(calls))

    stt_core._spawn_daemon()

    assert calls == [], "a live recorded daemon must not be spawned again"


def test_spawn_respawns_when_pid_file_is_stale(pid_path, monkeypatch):
    pid_path.write_text(str(_dead_pid()))
    calls: list = []
    monkeypatch.setattr(stt_core.subprocess, "Popen", _FakePopen(calls))

    stt_core._spawn_daemon()

    assert len(calls) == 1, "a dead recorded daemon must be respawned"
    assert pid_path.read_text() == "424242"


def test_spawn_respawns_when_pid_file_is_unreadable_garbage(pid_path, monkeypatch):
    pid_path.write_text("not-a-pid")
    calls: list = []
    monkeypatch.setattr(stt_core.subprocess, "Popen", _FakePopen(calls))

    stt_core._spawn_daemon()

    assert len(calls) == 1, "an unreadable PID file must not block spawning"
    assert pid_path.read_text() == "424242"
