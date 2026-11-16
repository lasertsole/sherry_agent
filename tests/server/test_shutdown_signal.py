"""SIGTERM/SIGINT must actually stop the backend.

Robyn 0.84 with ``processes=1`` runs the server in-process and registers its
own Python signal handlers only AFTER the blocking ``server.start()`` call, so
they never install: the Rust listener stops accepting but the process hangs
until SIGKILL. ``server.__main__`` installs its own handlers BEFORE
``app.start``; these tests pin that contract and the exit sequence.
"""

from __future__ import annotations

import signal
import threading

import pytest

from server import __main__ as server_main

pytestmark = [pytest.mark.unit, pytest.mark.timeout(30)]


def test_both_stop_signals_are_installed(monkeypatch: pytest.MonkeyPatch):
    calls: list[tuple[int, object]] = []
    monkeypatch.setattr(
        server_main.signal, "signal", lambda sig, handler: calls.append((sig, handler))
    )

    server_main._install_shutdown_handlers()

    assert (signal.SIGINT, server_main._graceful_exit_after_signal) in calls
    assert (signal.SIGTERM, server_main._graceful_exit_after_signal) in calls


def test_the_handler_tears_down_then_exits(monkeypatch: pytest.MonkeyPatch):
    order: list[str] = []
    exited = threading.Event()

    import runtime.process.crash_loop_breaker as breaker
    import server.service.browser_manager as browser_manager

    monkeypatch.setattr(server_main, "_shutdown_started", False)
    monkeypatch.setattr(server_main, "_SHUTDOWN_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(
        browser_manager, "shutdown_browser_blocking", lambda: order.append("browser")
    )
    monkeypatch.setattr(breaker, "mark_clean_exit", lambda: order.append("clean"))

    def _exit(code: int) -> None:
        order.append(f"exit:{code}")
        exited.set()

    monkeypatch.setattr(server_main.os, "_exit", _exit)

    server_main._graceful_exit_after_signal(signal.SIGTERM, None)
    # A second signal must not schedule a second exit.
    server_main._graceful_exit_after_signal(signal.SIGINT, None)

    assert exited.wait(5), "the shutdown thread never exited"
    assert order == ["browser", "clean", "exit:0"]


def test_a_teardown_failure_still_exits(monkeypatch: pytest.MonkeyPatch):
    exited = threading.Event()

    import runtime.process.crash_loop_breaker as breaker
    import server.service.browser_manager as browser_manager

    def _boom() -> None:
        raise RuntimeError("browser teardown failed")

    monkeypatch.setattr(server_main, "_shutdown_started", False)
    monkeypatch.setattr(server_main, "_SHUTDOWN_GRACE_SECONDS", 0.0)
    monkeypatch.setattr(browser_manager, "shutdown_browser_blocking", _boom)
    monkeypatch.setattr(breaker, "mark_clean_exit", lambda: None)
    monkeypatch.setattr(server_main.os, "_exit", lambda code: exited.set())

    server_main._graceful_exit_after_signal(signal.SIGTERM, None)

    assert exited.wait(5), "a teardown failure must not block the exit"
