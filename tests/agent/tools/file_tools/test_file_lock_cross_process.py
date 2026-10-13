"""Cross-process advisory lock: real contention, timeout, and crash release.

The lock's whole point is the property a hand-rolled lock FILE cannot give for
free: the kernel drops the lock when the holder dies. That is asserted against a
REAL second process (`kill -9`, then acquire immediately with no cleanup code),
not a mock.

The holder is a forked child (not a fresh interpreter): a cold import of the
package costs ~40 s because it drags langgraph in, and the fork inherits the
parent's already-imported modules. The lock protocol itself is plain
``flock`` on a sidecar file, so forking costs nothing in fidelity.
"""

import os
import signal
import time
from pathlib import Path

import pytest

from agent.tools.pub_base.file_lock import FileBusyError, cross_process_lock, lock_path_for

pytestmark = [
    pytest.mark.integration,
    pytest.mark.timeout(120),
    pytest.mark.skipif(not hasattr(os, "fork"), reason="fork is required to spawn the holder"),
]


def _hold(target: str, seconds: float, ready) -> None:  # pragma: no cover - child process
    """Child body: take the lock, announce it, hold it."""
    with cross_process_lock(Path(target)):
        ready.set()
        time.sleep(seconds)


@pytest.fixture
def lock_env(tmp_path, monkeypatch):
    """A scratch lock directory (the child inherits the patched environment)."""
    monkeypatch.setenv("SHERRY_FILE_LOCKS_DIR", str(tmp_path / "locks"))
    return tmp_path


def _start_holder(target: Path, seconds: float = 60.0):
    import multiprocessing as mp

    ctx = mp.get_context("fork")
    ready = ctx.Event()
    proc = ctx.Process(target=_hold, args=(str(target), seconds, ready), daemon=True)
    proc.start()
    assert ready.wait(timeout=10), "holder never took the lock"
    return proc


def test_a_second_process_waits_then_times_out(tmp_path, lock_env):
    target = tmp_path / "contended.txt"
    target.write_text("content", encoding="utf-8")
    holder = _start_holder(target)
    try:
        started = time.monotonic()
        with pytest.raises(FileBusyError) as excinfo:
            with cross_process_lock(target, timeout_s=0.5):
                pytest.fail("the lock was free while another process held it")
        elapsed = time.monotonic() - started

        assert "another process" in str(excinfo.value)
        assert elapsed >= 0.5, elapsed
        # The lock NAMESPACE is shared: both sides resolve the same sidecar.
        assert lock_path_for(target).exists()
    finally:
        holder.kill()
        holder.join(timeout=10)


def test_a_killed_holder_releases_the_lock_without_cleanup(tmp_path, lock_env):
    target = tmp_path / "crashed.txt"
    target.write_text("content", encoding="utf-8")
    holder = _start_holder(target)
    lock_path = lock_path_for(target)
    assert lock_path.exists()

    # SIGKILL: no finally block runs in the holder — the kernel must release.
    os.kill(holder.pid, signal.SIGKILL)
    holder.join(timeout=10)

    with cross_process_lock(target, timeout_s=5.0):
        pass  # acquired with no stale-lock protocol in sight

    # The sidecar file itself may remain; it carries no ownership state.
    assert lock_path.exists()


def test_the_lock_is_per_target(tmp_path, lock_env):
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    first.write_text("a", encoding="utf-8")
    second.write_text("b", encoding="utf-8")
    holder = _start_holder(first)
    try:
        # Another target is not blocked by the held one.
        with cross_process_lock(second, timeout_s=5.0):
            pass
    finally:
        holder.kill()
        holder.join(timeout=10)
