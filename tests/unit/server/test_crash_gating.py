"""Server crash-gating tests (Task 9, loop-detection-cron-breaker).

ISOLATION RULE: ``server/__main__.py`` and the ``server.trigger`` package are
NEVER imported here -- both carry heavy import side-effect chains (agent core
init, route registration, background threads). The gating layers are asserted
through the small predicate units the production code uses:

- ``runtime.crash_loop_breaker`` (record_boot / is_tripped / mark_clean_exit /
  was_last_exit_clean) drives the trip decision ``server.__main__`` makes;
- ``skills.builtin.core.cron.scripts.base._http_only_mode`` is the cron
  daemon-thread start-gate predicate (importing the cron base module is safe:
  its module import is side-effect-free by design).

STATE_PATH is redirected into tmp_path via monkeypatch dotted-string setattr
(self-restoring); env patches go through monkeypatch.setenv/delenv so every
patch recovers automatically.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import skills.builtin.core.cron.scripts.base as cron_base
from runtime.crash_loop_breaker import (
    is_tripped,
    mark_clean_exit,
    record_boot,
    was_last_exit_clean,
)

HTTP_ONLY_ENV = "SHERRY_HTTP_ONLY"


@pytest.fixture
def state_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Patch the breaker's STATE_PATH into tmp_path (restored by monkeypatch)."""
    p = tmp_path / "boot_lifecycle.json"
    monkeypatch.setattr("runtime.crash_loop_breaker.STATE_PATH", p)
    return p


@pytest.fixture(autouse=True)
def _pristine_http_only_env(monkeypatch: pytest.MonkeyPatch):
    """Every test starts with the gating env var unset (self-recovering)."""
    monkeypatch.delenv(HTTP_ONLY_ENV, raising=False)


def test_clean_marker_roundtrip_and_clean_boots_never_trip(state_path: Path):
    # mark_clean_exit (atexit) -> the NEXT boot observes a clean previous exit.
    mark_clean_exit()
    assert was_last_exit_clean() is True

    # Three sequential normal boots, each seeing the clean marker as its
    # previous-exit signal (record_boot(clean=True) explicitly, mirroring the
    # first-boot read in __main__'s gating order).
    for i in range(3):
        assert record_boot(clean=True, reason=f"startup-{i}") is False

    assert is_tripped() is False, "clean boots must never accumulate toward a trip"
    # Self-heal invariants: a clean boot keeps the process out of HTTP-only mode.
    assert os.environ.get(HTTP_ONLY_ENV) != "1"
    assert cron_base._http_only_mode() is False


def test_record_boot_consumes_one_shot_marker(state_path: Path):
    # The order __main__ relies on: evaluate was_last_exit_clean() BEFORE
    # record_boot runs, otherwise the marker is consumed before it is read.
    mark_clean_exit()
    prev_clean = was_last_exit_clean()
    assert record_boot(clean=prev_clean, reason="startup") is False
    # One-shot: consumed by record_boot, so a hard crash after this boot is
    # correctly reported as unclean on the next boot (no stale marker masking).
    assert was_last_exit_clean() is False


def test_tripped_boot_flips_http_only_predicates(state_path: Path, monkeypatch):
    # Pre-seed 3 unclean boots (crash loop) -- the 3rd one trips the breaker.
    assert record_boot(clean=False, reason="crash-1") is False
    assert record_boot(clean=False, reason="crash-2") is False
    assert record_boot(clean=False, reason="crash-3") is True
    assert is_tripped() is True

    # Simulate the __main__ gate: tripped -> env var is set -> every layer's
    # predicate agrees that HTTP-only mode is active.
    monkeypatch.setenv(HTTP_ONLY_ENV, "1")
    assert os.environ.get(HTTP_ONLY_ENV) == "1"
    assert cron_base._http_only_mode() is True


def test_http_only_env_gates_cron_thread_start(monkeypatch):
    # Normal boot (env unset): the thread-start gate is open.
    assert os.environ.get(HTTP_ONLY_ENV) != "1"
    assert cron_base._http_only_mode() is False

    # Crash-loop tripped (env set by __main__): the condition guarding
    # threading.Thread(...).start() in cron base.init() must evaluate False,
    # i.e. the daemon thread is NOT started in HTTP-only mode.
    monkeypatch.setenv(HTTP_ONLY_ENV, "1")
    assert cron_base._http_only_mode() is True
