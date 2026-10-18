"""Per-path read/write gate: shared readers, exclusive writers, no cross-path wait."""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from agent.tools.pub_base.path_lock import (
    active_path_locks,
    path_read_lock,
    path_write_lock,
)

pytestmark = [pytest.mark.integration, pytest.mark.timeout(30)]


def _wait(event: threading.Event, timeout: float = 2.0) -> None:
    assert event.wait(timeout), "thread did not reach the expected state"


def test_two_readers_of_one_path_overlap(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    count_lock = threading.Lock()
    both_inside = threading.Event()
    release = threading.Event()
    inside = 0

    def reader() -> None:
        nonlocal inside
        with path_read_lock(target):
            with count_lock:
                inside += 1
                if inside == 2:
                    both_inside.set()
            release.wait(timeout=2)

    threads = [threading.Thread(target=reader) for _ in range(2)]
    for thread in threads:
        thread.start()
    _wait(both_inside)
    assert all(thread.is_alive() for thread in threads)
    release.set()
    for thread in threads:
        thread.join(timeout=2)
        assert not thread.is_alive()
    assert active_path_locks() == 0


def test_a_writer_waits_for_a_reader(tmp_path: Path) -> None:
    target = tmp_path / "a.txt"
    reader_inside = threading.Event()
    release_reader = threading.Event()
    writer_inside = threading.Event()

    def reader() -> None:
        with path_read_lock(target):
            reader_inside.set()
            release_reader.wait(timeout=2)

    def writer() -> None:
        with path_write_lock(target):
            writer_inside.set()

    reader_thread = threading.Thread(target=reader)
    writer_thread = threading.Thread(target=writer)
    reader_thread.start()
    _wait(reader_inside)
    writer_thread.start()
    time.sleep(0.05)
    assert writer_inside.is_set() is False

    release_reader.set()
    writer_thread.join(timeout=2)
    reader_thread.join(timeout=2)
    assert writer_inside.is_set() is True
    assert active_path_locks() == 0


def test_a_read_of_another_path_does_not_wait_for_a_writer(tmp_path: Path) -> None:
    first = tmp_path / "a.txt"
    second = tmp_path / "b.txt"
    writer_inside = threading.Event()
    release_writer = threading.Event()

    def writer() -> None:
        with path_write_lock(first):
            writer_inside.set()
            release_writer.wait(timeout=2)

    writer_thread = threading.Thread(target=writer)
    writer_thread.start()
    _wait(writer_inside)

    acquired = threading.Event()

    def reader() -> None:
        with path_read_lock(second):
            acquired.set()

    reader_thread = threading.Thread(target=reader)
    reader_thread.start()
    _wait(acquired, 1.0)
    release_writer.set()
    writer_thread.join(timeout=2)
    reader_thread.join(timeout=2)
    assert active_path_locks() == 0
