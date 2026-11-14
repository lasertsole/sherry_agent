"""Unit tests for STT daemon spawn de-duplication via PID file (audit round 2 #27).

``_spawn_daemon`` delegates to the shared :mod:`models.daemon.base` lifecycle
(extracted from this module), so the PID-file contract is exercised through the
STT spec: a live recorded PID suppresses the spawn, a stale PID file (dead
process) is refreshed by a respawn. ``subprocess.Popen`` is fully mocked — no
real daemon is started.
"""

import dataclasses
import os
import subprocess

import pytest

import models.daemon.base as daemon_base
import skills.builtin.core.speech_to_text.scripts.core as stt_core

pytestmark = pytest.mark.unit


@pytest.fixture
def pid_path(tmp_path, monkeypatch):
    path = tmp_path / "stt_daemon.pid"
    monkeypatch.setattr(
        stt_core, "_STT_SPEC", dataclasses.replace(stt_core._STT_SPEC, pid_path=path)
    )
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
    monkeypatch.setattr(daemon_base.subprocess, "Popen", _FakePopen(calls))

    stt_core._spawn_daemon()

    assert len(calls) == 1
    assert pid_path.read_text() == "424242"


def test_spawn_skipped_when_pid_file_holds_live_process(pid_path, monkeypatch):
    pid_path.write_text(str(os.getpid()))
    calls: list = []
    monkeypatch.setattr(daemon_base.subprocess, "Popen", _FakePopen(calls))

    stt_core._spawn_daemon()

    assert calls == [], "a live recorded daemon must not be spawned again"


def test_spawn_respawns_when_pid_file_is_stale(pid_path, monkeypatch):
    pid_path.write_text(str(_dead_pid()))
    calls: list = []
    monkeypatch.setattr(daemon_base.subprocess, "Popen", _FakePopen(calls))

    stt_core._spawn_daemon()

    assert len(calls) == 1, "a dead recorded daemon must be respawned"
    assert pid_path.read_text() == "424242"


def test_spawn_respawns_when_pid_file_is_unreadable_garbage(pid_path, monkeypatch):
    pid_path.write_text("not-a-pid")
    calls: list = []
    monkeypatch.setattr(daemon_base.subprocess, "Popen", _FakePopen(calls))

    stt_core._spawn_daemon()

    assert len(calls) == 1, "an unreadable PID file must not block spawning"
    assert pid_path.read_text() == "424242"


def test_spawn_reuses_the_shared_lifecycle_with_the_stt_identity():
    spec = stt_core._STT_SPEC

    assert spec.import_fn == "skills.builtin.core.speech_to_text.scripts.server"
    assert spec.port == stt_core.DAEMON_PORT
    assert spec.host == stt_core.DAEMON_HOST
    assert spec.name == "STT"
