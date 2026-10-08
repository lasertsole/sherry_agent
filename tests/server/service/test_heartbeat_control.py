"""Tests for the global heartbeat switch (service + HTTP boundary).

The switch is the 心跳 panel's toggle: flipping it must (a) persist the choice
to ``sherry.jsonc`` so the next boot honours it and (b) start/stop the live
scheduler immediately, on the loop the service owns. A switch that only did one
of the two would either look applied while the loop kept firing, or silently
turn itself back on after a restart.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from server.service import heartbeat_control
from server.trigger.http import heartbeat as heartbeat_http

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]


def _payload(response) -> dict:
    """Decode a Robyn-wrapped handler response (its `description` holds the JSON)."""
    return json.loads(response.description)


class _FakeRequest:
    def __init__(self, body: dict | None = None):
        self._body = body

    def json(self) -> dict:
        if self._body is None:
            raise ValueError("no body")
        return self._body


class _FakeService:
    """Heartbeat service double: records enable/start/stop traffic."""

    def __init__(self, *, enabled: bool = True, running: bool = False):
        self.enabled = enabled
        self._running = running
        self.interval_s = 1800
        self.started = 0
        self.stopped = 0

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        self.started += 1
        self._running = True

    def stop(self) -> None:
        self.stopped += 1
        self._running = False


@pytest.fixture
def service(monkeypatch):
    """A service double behind both readers (module + HTTP handler import)."""
    fake = _FakeService()
    monkeypatch.setattr(heartbeat_control, "heartbeat_service", fake)
    return fake


@pytest.fixture
def scheduled(monkeypatch):
    """Capture the coroutines handed to the channel loop.

    The real seam schedules them on the channel event loop; here they run
    immediately on this test's loop so the start/stop side effects are visible.
    """
    ran: list[str] = []

    def fake_schedule(coro):
        # Drive the coroutine synchronously: these are the toggle's own
        # start/stop coroutines (no awaits), and the HTTP tests already run
        # inside an event loop, where a nested asyncio.run() would explode.
        ran.append(coro.cr_code.co_name)
        try:
            coro.send(None)
        except StopIteration:
            pass

    monkeypatch.setattr(heartbeat_control, "_schedule", fake_schedule)
    return ran


@pytest.fixture
def persisted(monkeypatch):
    """Capture (and never touch) the sherry.jsonc writes."""
    writes: list[dict[str, str]] = []
    monkeypatch.setattr(
        heartbeat_control, "write_sherry_config", lambda changes: writes.append(changes)
    )
    return writes


def test_status_reports_choice_and_live_state(service, monkeypatch):
    monkeypatch.setattr(
        heartbeat_control, "heartbeat_service", _FakeService(enabled=False, running=False)
    )

    status = heartbeat_control.get_heartbeat_status()

    assert status == {"enabled": False, "running": False, "interval_s": 1800}


def test_disabling_persists_and_stops_the_loop(service, scheduled, persisted):
    service._running = True

    status = heartbeat_control.set_heartbeat_enabled(False)

    assert persisted == [{"heartbeat.enabled": "false"}]
    assert scheduled == ["_stop_on_loop"]
    assert service.stopped == 1
    assert service.enabled is False
    assert status["running"] is False


def test_enabling_persists_and_starts_the_loop(service, scheduled, persisted):
    status = heartbeat_control.set_heartbeat_enabled(True)

    assert persisted == [{"heartbeat.enabled": "true"}]
    assert scheduled == ["start"]
    assert service.started == 1
    assert service.enabled is True
    assert status == {"enabled": True, "running": True, "interval_s": 1800}


def test_a_failing_persist_leaves_the_loop_alone(service, scheduled, monkeypatch):
    def boom(_changes):
        raise FileNotFoundError("sherry.jsonc missing")

    monkeypatch.setattr(heartbeat_control, "write_sherry_config", boom)

    with pytest.raises(FileNotFoundError):
        heartbeat_control.set_heartbeat_enabled(True)

    # Nothing ran half-applied.
    assert scheduled == []
    assert service.started == 0


def test_an_unreachable_loop_does_not_break_the_toggle(service, persisted, monkeypatch):
    def unreachable():
        raise RuntimeError("event loop is closed")

    monkeypatch.setattr(heartbeat_control.channel_manager, "get_event_loop", unreachable)

    status = heartbeat_control.set_heartbeat_enabled(True)

    # The choice is still persisted (and will apply on the next boot) …
    assert persisted == [{"heartbeat.enabled": "true"}]
    # … and reported as enabled-but-not-running, which is the honest state.
    assert status["enabled"] is True
    assert status["running"] is False


def test_boot_adopts_the_persisted_choice(monkeypatch):
    monkeypatch.setattr(heartbeat_control, "get_sherry_setting", lambda _key: False)
    monkeypatch.setattr(heartbeat_control, "heartbeat_service", _FakeService())

    applied = heartbeat_control.apply_persisted_heartbeat_setting()

    assert applied is False
    assert heartbeat_control.heartbeat_service.enabled is False


def test_http_reads_the_status(service, monkeypatch):
    monkeypatch.setattr(
        heartbeat_http,
        "get_heartbeat_status",
        lambda: {"enabled": True, "running": True, "interval_s": 60},
    )

    response = asyncio.run(heartbeat_http.read_heartbeat_status_handler(_FakeRequest()))

    assert _payload(response) == {"enabled": True, "running": True, "interval_s": 60}


def test_http_toggles_and_returns_the_applied_state(service, scheduled, persisted):
    response = asyncio.run(
        heartbeat_http.set_heartbeat_status_handler(_FakeRequest({"enabled": False}))
    )

    body = _payload(response)
    assert body["success"] is True
    assert body["enabled"] is False
    assert persisted == [{"heartbeat.enabled": "false"}]


def test_http_rejects_a_non_boolean(service):
    response = asyncio.run(
        heartbeat_http.set_heartbeat_status_handler(_FakeRequest({"enabled": "yes"}))
    )

    body = _payload(response)
    assert body["success"] is False
    assert "boolean" in body["message"]


def test_http_reports_a_config_write_failure(service, monkeypatch):
    def boom(_changes):
        raise ValueError("Unknown sherry config keys: heartbeat.enabled")

    monkeypatch.setattr(heartbeat_http, "set_heartbeat_enabled", lambda _enabled: boom({}))

    response = asyncio.run(
        heartbeat_http.set_heartbeat_status_handler(_FakeRequest({"enabled": True}))
    )

    body = _payload(response)
    assert body["success"] is False
    assert "Unknown sherry config" in body["message"]
