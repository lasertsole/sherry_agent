"""Tests for the per-session thinking control: HTTP boundary + service state.

The HTTP handlers drive the service through ``asyncio.to_thread`` (sync
SQLite never lands on the event loop); the service dual-writes the mem and
db registers, rehydrates mem on db reads, validates the value against the
model's control mode, and rejects writes while the session has a turn in
progress (409) so a running turn never switches model variants mid-flight.
"""

import asyncio

import pytest

from server.trigger.http import session_settings as session_settings_http
from server.service import session_settings_service as service

pytestmark = [pytest.mark.module, pytest.mark.timeout(60)]


class _FakeRequest:
    def __init__(self, query_params: dict | None = None, body: dict | None = None):
        self.query_params = query_params or {}
        self._body = body

    def json(self):
        if self._body is None:
            raise ValueError("no body")
        return self._body


class _FakeRegister:
    """State-register double with the real get_state/set_state surface."""

    def __init__(self):
        self.data: dict[tuple[str, str], object] = {}

    def get_state(self, sid, key, default=None):
        return self.data.get((sid, key), default)

    def set_state(self, sid, key, value):
        self.data[(sid, key)] = value
        return True


@pytest.fixture
def registers(monkeypatch):
    mem, db = _FakeRegister(), _FakeRegister()
    monkeypatch.setattr(service, "state_register_mem", mem)
    monkeypatch.setattr(service, "state_register_db", db)
    monkeypatch.setattr(service, "is_session_busy", lambda sid: False)
    return mem, db


# ----------------------------------------------------------------------
# service layer
# ----------------------------------------------------------------------


def test_service_bool_roundtrip_on_off_mode(registers, monkeypatch):
    mem, db = registers
    monkeypatch.setattr(service, "get_thinking_mode", lambda: "on_off")
    service.set_thinking_value("s1", True)
    state = service.get_thinking_state("s1")
    assert state["mode"] == "on_off" and state["enabled"] is True and state["level"] is None
    assert mem.data[("s1", "llm_thinking_enabled")] is True
    assert db.data[("s1", "llm_thinking_enabled")] is True


def test_service_level_roundtrip_levels_mode(registers, monkeypatch):
    mem, db = registers
    monkeypatch.setattr(service, "get_thinking_mode", lambda: "levels")
    service.set_thinking_value("s1", "low")
    state = service.get_thinking_state("s1")
    assert state["mode"] == "levels" and state["enabled"] is None and state["level"] == "low"
    assert mem.data[("s1", "llm_thinking_enabled")] == "low"
    assert db.data[("s1", "llm_thinking_enabled")] == "low"


def test_service_rejects_value_contradicting_mode(registers, monkeypatch):
    monkeypatch.setattr(service, "get_thinking_mode", lambda: "levels")
    with pytest.raises(ValueError):
        service.set_thinking_value("s1", True)
    monkeypatch.setattr(service, "get_thinking_mode", lambda: "on_off")
    with pytest.raises(ValueError):
        service.set_thinking_value("s1", "low")


def test_service_unset_returns_nulls(registers):
    state = service.get_thinking_state("never-set")
    assert state["enabled"] is None and state["level"] is None


def test_service_db_read_rehydrates_mem(registers, monkeypatch):
    mem, db = registers
    monkeypatch.setattr(service, "get_thinking_mode", lambda: "levels")
    db.data[("s2", "llm_thinking_enabled")] = "high"
    # Mem empty (fresh process): the db mirror answers AND rehydrates mem.
    assert service.get_thinking_state("s2")["level"] == "high"
    assert mem.data[("s2", "llm_thinking_enabled")] == "high"


def test_service_rejects_bad_inputs(registers):
    with pytest.raises(ValueError):
        service.set_thinking_value("../escape", True)
    with pytest.raises(ValueError):
        service.get_thinking_state("")
    with pytest.raises(ValueError):
        service.set_thinking_value("s1", "yes")


def test_service_rejects_write_while_session_busy(registers, monkeypatch):
    mem, db = registers
    monkeypatch.setattr(service, "get_thinking_mode", lambda: "on_off")
    monkeypatch.setattr(service, "is_session_busy", lambda sid: True)
    with pytest.raises(service.SessionBusyError):
        service.set_thinking_value("s1", False)
    # Nothing was written.
    assert ("s1", "llm_thinking_enabled") not in mem.data
    assert ("s1", "llm_thinking_enabled") not in db.data


# ----------------------------------------------------------------------
# HTTP boundary
# ----------------------------------------------------------------------


def _wire_http(monkeypatch):
    monkeypatch.setattr(
        session_settings_http, "set_thinking_value_guarded", service.set_thinking_value_guarded
    )
    monkeypatch.setattr(session_settings_http, "get_thinking_state", service.get_thinking_state)


def test_put_then_get_roundtrip(registers, monkeypatch):
    _wire_http(monkeypatch)
    monkeypatch.setattr(service, "get_thinking_mode", lambda: "levels")

    put_resp = asyncio.run(
        session_settings_http.put_thinking_handler(
            _FakeRequest(body={"session_id": "s3", "value": "max"})
        )
    )
    assert put_resp.status_code == 200

    get_resp = asyncio.run(
        session_settings_http.get_thinking_handler(_FakeRequest(query_params={"session_id": "s3"}))
    )
    assert get_resp.status_code == 200
    assert '"mode": "levels"' in get_resp.description
    assert '"level": "max"' in get_resp.description


def test_get_unset_returns_nulls(registers):
    resp = asyncio.run(
        session_settings_http.get_thinking_handler(_FakeRequest(query_params={"session_id": "s4"}))
    )
    assert resp.status_code == 200
    assert '"enabled": null' in resp.description


def test_put_rejects_value_contradicting_mode(registers, monkeypatch):
    _wire_http(monkeypatch)
    _wire_queue(monkeypatch, [])
    monkeypatch.setattr(service, "get_thinking_mode", lambda: "on_off")
    resp = asyncio.run(
        session_settings_http.put_thinking_handler(
            _FakeRequest(body={"session_id": "s5", "value": "low"})
        )
    )
    assert resp.status_code == 400


def test_put_returns_409_while_session_busy(registers, monkeypatch):
    _wire_http(monkeypatch)
    _wire_queue(monkeypatch, [])
    monkeypatch.setattr(service, "get_thinking_mode", lambda: "on_off")
    monkeypatch.setattr(service, "is_session_busy", lambda sid: True)
    resp = asyncio.run(
        session_settings_http.put_thinking_handler(
            _FakeRequest(body={"session_id": "s6", "value": True})
        )
    )
    assert resp.status_code == 409
    assert "turn in progress" in resp.description


def test_put_rejects_path_traversal_session(registers):
    resp = asyncio.run(
        session_settings_http.put_thinking_handler(
            _FakeRequest(body={"session_id": "../../workspace", "value": True})
        )
    )
    assert resp.status_code == 400


def test_put_rejects_missing_body():
    resp = asyncio.run(session_settings_http.put_thinking_handler(_FakeRequest()))
    assert resp.status_code == 400


# ----------------------------------------------------------------------
# guarded async entry (queue-aware busy detection)
# ----------------------------------------------------------------------


class _FakeQueue:
    def __init__(self, statuses):
        self._statuses = statuses

    async def list_active(self, session_id):
        from types import SimpleNamespace

        return [SimpleNamespace(status=s) for s in self._statuses]


def _wire_queue(monkeypatch, statuses):
    """Patch the queue getter the guarded entry resolves at call time."""
    from server.service import input_queue_service

    monkeypatch.setattr(input_queue_service, "get_default_queue", lambda: _FakeQueue(statuses))


def test_guarded_write_happy_path(registers, monkeypatch):
    _wire_queue(monkeypatch, [])
    monkeypatch.setattr(service, "get_thinking_mode", lambda: "levels")
    asyncio.run(service.set_thinking_value_guarded("s7", "low"))
    mem, _db = registers
    assert mem.data[("s7", "llm_thinking_enabled")] == "low"


async def _guarded_expect_busy(registers, monkeypatch, value=True):
    with pytest.raises(service.SessionBusyError):
        await service.set_thinking_value_guarded("s7", value)
    mem, _db = registers
    assert ("s7", "llm_thinking_enabled") not in mem.data


def test_guarded_write_rejected_on_detect_state_busy(registers, monkeypatch):
    _wire_queue(monkeypatch, [])
    monkeypatch.setattr(service, "get_thinking_mode", lambda: "on_off")
    monkeypatch.setattr(service, "is_session_busy", lambda sid: True)
    asyncio.run(_guarded_expect_busy(registers, monkeypatch))


def test_guarded_write_rejected_on_queued_row(registers, monkeypatch):
    from server.queue.user_input_queue import UserInputQueueStatus

    _wire_queue(monkeypatch, [UserInputQueueStatus.QUEUED])
    monkeypatch.setattr(service, "get_thinking_mode", lambda: "on_off")
    asyncio.run(_guarded_expect_busy(registers, monkeypatch))


def test_guarded_write_rejected_on_claimed_row(registers, monkeypatch):
    from server.queue.user_input_queue import UserInputQueueStatus

    _wire_queue(monkeypatch, [UserInputQueueStatus.CLAIMED])
    monkeypatch.setattr(service, "get_thinking_mode", lambda: "on_off")
    asyncio.run(_guarded_expect_busy(registers, monkeypatch))


def test_guarded_write_ignores_terminal_rows(registers, monkeypatch):
    from server.queue.user_input_queue import UserInputQueueStatus

    _wire_queue(monkeypatch, [UserInputQueueStatus.DELIVERED, UserInputQueueStatus.VOIDED])
    monkeypatch.setattr(service, "get_thinking_mode", lambda: "levels")
    asyncio.run(service.set_thinking_value_guarded("s7", "high"))
    mem, _db = registers
    assert mem.data[("s7", "llm_thinking_enabled")] == "high"
