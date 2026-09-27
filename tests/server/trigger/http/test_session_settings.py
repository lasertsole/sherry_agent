"""Tests for the per-session thinking control: HTTP boundary + service state.

The HTTP handlers drive the service through ``asyncio.to_thread`` (sync
SQLite never lands on the event loop); the service dual-writes the mem and
db registers, rehydrates mem on db reads, validates the value against the
model's control mode, and rejects writes while the session has a turn in
progress is PARKED in a sibling pending key so a running turn never switches
model variants mid-flight; the parked choice is promoted when the turn ends.
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

    def delete_state(self, sid, key):
        return self.data.pop((sid, key), None) is not None


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
    monkeypatch.setattr(service, "get_thinking_mode", lambda *_args: "on_off")
    service.set_thinking_value("s1", True)
    state = service.get_thinking_state("s1")
    assert state["mode"] == "on_off" and state["enabled"] is True and state["level"] is None
    assert mem.data[("s1", "llm_thinking_enabled")] is True
    assert db.data[("s1", "llm_thinking_enabled")] is True


def test_service_level_roundtrip_levels_mode(registers, monkeypatch):
    mem, db = registers
    monkeypatch.setattr(service, "get_thinking_mode", lambda *_args: "levels")
    service.set_thinking_value("s1", "low")
    state = service.get_thinking_state("s1")
    assert state["mode"] == "levels" and state["enabled"] is None and state["level"] == "low"
    assert mem.data[("s1", "llm_thinking_enabled")] == "low"
    assert db.data[("s1", "llm_thinking_enabled")] == "low"


def test_service_rejects_value_contradicting_mode(registers, monkeypatch):
    monkeypatch.setattr(service, "get_thinking_mode", lambda *_args: "levels")
    with pytest.raises(ValueError):
        service.set_thinking_value("s1", True)
    monkeypatch.setattr(service, "get_thinking_mode", lambda *_args: "on_off")
    with pytest.raises(ValueError):
        service.set_thinking_value("s1", "low")


def test_service_unset_returns_nulls(registers):
    state = service.get_thinking_state("never-set")
    assert state["enabled"] is None and state["level"] is None


def test_service_db_read_rehydrates_mem(registers, monkeypatch):
    mem, db = registers
    monkeypatch.setattr(service, "get_thinking_mode", lambda *_args: "levels")
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


def test_direct_write_ignores_turn_state_and_clears_parking(registers, monkeypatch):
    """The sync entry writes the LIVE key (idle callers) and drops a parked twin."""
    mem, db = registers
    monkeypatch.setattr(service, "get_thinking_mode", lambda *_args: "on_off")
    service._park_value("s1", "llm_thinking_enabled_pending", False)

    service.set_thinking_value("s1", True)

    assert mem.data[("s1", "llm_thinking_enabled")] is True
    assert ("s1", "llm_thinking_enabled_pending") not in mem.data
    assert db.data[("s1", "llm_thinking_enabled")] is True


# ----------------------------------------------------------------------
# HTTP boundary
# ----------------------------------------------------------------------


def _wire_http(monkeypatch):
    monkeypatch.setattr(
        session_settings_http, "apply_thinking_choice", service.apply_thinking_choice
    )
    monkeypatch.setattr(session_settings_http, "get_thinking_state", service.get_thinking_state)


def test_put_then_get_roundtrip(registers, monkeypatch):
    _wire_http(monkeypatch)
    monkeypatch.setattr(service, "get_thinking_mode", lambda *_args: "levels")

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
    monkeypatch.setattr(service, "get_thinking_mode", lambda *_args: "on_off")
    resp = asyncio.run(
        session_settings_http.put_thinking_handler(
            _FakeRequest(body={"session_id": "s5", "value": "low"})
        )
    )
    assert resp.status_code == 400


def test_put_parks_while_a_turn_runs(registers, monkeypatch):
    """Switching mid-turn is allowed: the write parks and reports ``pending``."""
    _wire_http(monkeypatch)
    _wire_queue(monkeypatch, [])
    monkeypatch.setattr(service, "get_thinking_mode", lambda *_args: "on_off")
    monkeypatch.setattr(service, "is_session_busy", lambda sid: True)
    resp = asyncio.run(
        session_settings_http.put_thinking_handler(
            _FakeRequest(body={"session_id": "s6", "value": True})
        )
    )
    assert resp.status_code == 200
    assert '"pending": true' in resp.description
    mem, _db = registers
    # The running turn keeps the live value; the choice waits under the twin key.
    assert ("s6", "llm_thinking_enabled") not in mem.data
    assert mem.data[("s6", "llm_thinking_enabled_pending")] == {"value": True}


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
# parking + promotion (mid-turn switching, applied next turn)
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


def test_apply_write_lands_live_when_idle(registers, monkeypatch):
    _wire_queue(monkeypatch, [])
    monkeypatch.setattr(service, "get_thinking_mode", lambda *_args: "levels")
    pending = asyncio.run(service.apply_thinking_choice("s7", "low"))
    mem, _db = registers
    assert pending is False
    assert mem.data[("s7", "llm_thinking_enabled")] == "low"


async def _expect_parked(registers, value=True) -> None:
    pending = await service.apply_thinking_choice("s7", value)
    mem, _db = registers
    assert pending is True
    # Live key untouched: the running turn keeps its variant.
    assert ("s7", "llm_thinking_enabled") not in mem.data
    assert mem.data[("s7", "llm_thinking_enabled_pending")] == {"value": value}


def test_apply_write_parks_on_detect_state_busy(registers, monkeypatch):
    _wire_queue(monkeypatch, [])
    monkeypatch.setattr(service, "get_thinking_mode", lambda *_args: "on_off")
    monkeypatch.setattr(service, "is_session_busy", lambda sid: True)
    asyncio.run(_expect_parked(registers))


def test_apply_write_parks_on_queued_row(registers, monkeypatch):
    from server.queue.user_input_queue import UserInputQueueStatus

    _wire_queue(monkeypatch, [UserInputQueueStatus.QUEUED])
    monkeypatch.setattr(service, "get_thinking_mode", lambda *_args: "on_off")
    asyncio.run(_expect_parked(registers))


def test_apply_write_parks_on_claimed_row(registers, monkeypatch):
    from server.queue.user_input_queue import UserInputQueueStatus

    _wire_queue(monkeypatch, [UserInputQueueStatus.CLAIMED])
    monkeypatch.setattr(service, "get_thinking_mode", lambda *_args: "on_off")
    asyncio.run(_expect_parked(registers))


def test_apply_write_ignores_terminal_rows(registers, monkeypatch):
    from server.queue.user_input_queue import UserInputQueueStatus

    _wire_queue(monkeypatch, [UserInputQueueStatus.DELIVERED, UserInputQueueStatus.VOIDED])
    monkeypatch.setattr(service, "get_thinking_mode", lambda *_args: "levels")
    asyncio.run(service.apply_thinking_choice("s7", "high"))
    mem, _db = registers
    assert mem.data[("s7", "llm_thinking_enabled")] == "high"


def test_promote_moves_the_parked_choice_into_the_live_key(registers):
    mem, _db = registers
    service._park_value("s8", "llm_thinking_enabled_pending", "max")

    promoted = service.promote_pending_settings_sync("s8")

    assert promoted == ["llm_thinking_enabled"]
    assert mem.data[("s8", "llm_thinking_enabled")] == "max"
    assert ("s8", "llm_thinking_enabled_pending") not in mem.data
    # Idempotent: nothing left to promote.
    assert service.promote_pending_settings_sync("s8") == []


def test_promote_honours_a_parked_clear(registers):
    mem, _db = registers
    mem.data[("s9", "llm_thinking_enabled")] = True
    service._park_value("s9", "llm_thinking_enabled_pending", None)

    service.promote_pending_settings_sync("s9")

    # A parked null means "back to the env default": the live key is removed.
    assert ("s9", "llm_thinking_enabled") not in mem.data
    assert ("s9", "llm_thinking_enabled_pending") not in mem.data


def test_idle_read_promotes_a_lingering_park(registers, monkeypatch):
    """A parked choice cannot outlive its turn (restart, missed hook)."""
    mem, _db = registers
    monkeypatch.setattr(service, "get_thinking_mode", lambda *_args: "on_off")
    service._park_value("s10", "llm_thinking_enabled_pending", False)

    state = service.get_thinking_state("s10")

    assert state["enabled"] is False and state["pending"] is False
    assert mem.data[("s10", "llm_thinking_enabled")] is False
    assert ("s10", "llm_thinking_enabled_pending") not in mem.data


def test_busy_read_reports_the_parked_choice_as_pending(registers, monkeypatch):
    """While the turn is running the parked choice is reported, not promoted."""
    mem, _db = registers
    monkeypatch.setattr(service, "get_thinking_mode", lambda *_args: "on_off")
    monkeypatch.setattr(service, "is_session_busy", lambda sid: True)
    service._park_value("s11", "llm_thinking_enabled_pending", True)

    state = service.get_thinking_state("s11")

    # Effective value = what the user picked; the flag says it lands next turn.
    assert state["enabled"] is True and state["pending"] is True
    assert ("s11", "llm_thinking_enabled") not in mem.data


# ----------------------------------------------------------------------
# main-model override (service layer + HTTP boundary)
# ----------------------------------------------------------------------

_DESCRIPTOR = {
    "id": "profile-1",
    "label": "Kimi K2 (moonshot)",
    "provider": "openai",
    "model": "kimi-k2",
    "base_url": "https://api.moonshot.cn/v1",
    "api_key": "sk-secret",
}


def test_service_model_override_roundtrip_and_masking(registers):
    mem, db = registers
    service.set_main_model_override("m1", _DESCRIPTOR)

    assert mem.data[("m1", "llm_main_model")] == _DESCRIPTOR
    assert db.data[("m1", "llm_main_model")] == _DESCRIPTOR

    masked = service.get_main_model_override("m1", mask_secrets=True)
    assert "api_key" not in masked and masked["has_api_key"] is True
    assert masked["model"] == "kimi-k2"
    # The unmasked read (what the middleware uses) keeps the credential.
    assert service.get_main_model_override("m1")["api_key"] == "sk-secret"


def test_service_model_override_clears_on_none(registers):
    mem, db = registers
    service.set_main_model_override("m2", _DESCRIPTOR)
    service.set_main_model_override("m2", None)

    assert service.get_main_model_override("m2") is None
    assert ("m2", "llm_main_model") not in mem.data
    assert ("m2", "llm_main_model") not in db.data


def test_service_model_override_sanitizer_matrix(registers):
    for bad in (
        {"model": ""},
        {"model": 42},
        "kimi-k2",
        {"nope": "x", "model": "kimi-k2"},
        {"model": "kimi-k2", "label": "x" * 600},
        {"model": "kimi-k2", "api_key": 7},
    ):
        with pytest.raises(ValueError):
            service.set_main_model_override("m3", bad)

    # Blank optional fields are dropped, not stored as "".
    service.set_main_model_override("m3", {"model": " kimi-k2 ", "provider": "  ", "label": " L "})
    assert service.get_main_model_override("m3") == {"model": "kimi-k2", "label": "L"}


def test_service_model_override_rejects_bad_session(registers):
    with pytest.raises(ValueError):
        service.set_main_model_override("../escape", _DESCRIPTOR)
    with pytest.raises(ValueError):
        service.get_main_model_state("")


def test_model_apply_parks_while_a_turn_runs(registers, monkeypatch):
    mem, _db = registers
    _wire_queue(monkeypatch, [])
    monkeypatch.setattr(service, "is_session_busy", lambda sid: True)

    pending = asyncio.run(service.apply_main_model_choice("m4", _DESCRIPTOR))

    assert pending is True
    assert ("m4", "llm_main_model") not in mem.data
    assert mem.data[("m4", "llm_main_model_pending")] == {"value": _DESCRIPTOR}
    # The parked model already decides the mode the next turn will use.
    assert service.get_main_model_override("m4")["model"] == "kimi-k2"


def test_model_promotion_moves_the_parked_profile(registers, monkeypatch):
    mem, _db = registers
    _wire_queue(monkeypatch, [])
    monkeypatch.setattr(service, "is_session_busy", lambda sid: True)
    asyncio.run(service.apply_main_model_choice("m5", _DESCRIPTOR))

    promoted = service.promote_pending_settings_sync("m5")

    assert promoted == ["llm_main_model"]
    assert mem.data[("m5", "llm_main_model")] == _DESCRIPTOR
    assert ("m5", "llm_main_model_pending") not in mem.data


def test_service_model_state_reports_env_identity(registers, monkeypatch):
    monkeypatch.setattr(service, "_main_model_identity", lambda: ("zhipu", "glm-4.5"))

    state = service.get_main_model_state("m5")

    assert state["override"] is None
    assert state["env_model"] == {"provider": "zhipu", "model": "glm-4.5"}


def test_thinking_mode_follows_the_session_override(registers, monkeypatch):
    monkeypatch.setattr(service, "_main_model_identity", lambda: ("zhipu", "glm-4.6"))
    calls: list[tuple] = []

    def fake_mode(provider, model_name):
        calls.append((provider, model_name))
        return "levels" if model_name == "glm-5" else "on_off"

    monkeypatch.setattr("models.LLMs.reasoning_payload.thinking_control_mode", fake_mode)

    assert service.get_thinking_mode("m6") == "on_off"  # env model
    service.set_main_model_override("m6", {"model": "glm-5"})
    assert service.get_thinking_mode("m6") == "levels"  # override model wins
    # The provider falls back to the env identity when the profile omits it.
    assert calls[-1] == ("zhipu", "glm-5")


def _wire_model_http(monkeypatch):
    monkeypatch.setattr(
        session_settings_http,
        "apply_main_model_choice",
        service.apply_main_model_choice,
    )
    monkeypatch.setattr(session_settings_http, "get_main_model_state", service.get_main_model_state)


def test_model_put_then_get_roundtrip_without_echoing_secrets(registers, monkeypatch):
    _wire_model_http(monkeypatch)
    _wire_queue(monkeypatch, [])

    put_resp = asyncio.run(
        session_settings_http.put_session_model_handler(
            _FakeRequest(body={"session_id": "s8", "profile": _DESCRIPTOR})
        )
    )
    assert put_resp.status_code == 200
    assert "kimi-k2" in put_resp.description
    assert "sk-secret" not in put_resp.description

    get_resp = asyncio.run(
        session_settings_http.get_session_model_handler(
            _FakeRequest(query_params={"session_id": "s8"})
        )
    )
    assert get_resp.status_code == 200
    assert '"has_api_key": true' in get_resp.description


def test_model_put_null_clears_the_override(registers, monkeypatch):
    _wire_model_http(monkeypatch)
    _wire_queue(monkeypatch, [])

    asyncio.run(
        session_settings_http.put_session_model_handler(
            _FakeRequest(body={"session_id": "s9", "profile": _DESCRIPTOR})
        )
    )
    cleared = asyncio.run(
        session_settings_http.put_session_model_handler(
            _FakeRequest(body={"session_id": "s9", "profile": None})
        )
    )

    assert cleared.status_code == 200
    assert '"override": null' in cleared.description


def test_model_put_rejects_missing_fields_malformed_profile_and_busy(registers, monkeypatch):
    _wire_model_http(monkeypatch)
    _wire_queue(monkeypatch, [])

    missing_profile = asyncio.run(
        session_settings_http.put_session_model_handler(_FakeRequest(body={"session_id": "s10"}))
    )
    assert missing_profile.status_code == 400
    assert "profile" in missing_profile.description

    missing_session = asyncio.run(
        session_settings_http.put_session_model_handler(_FakeRequest(body={"profile": _DESCRIPTOR}))
    )
    assert missing_session.status_code == 400

    malformed = asyncio.run(
        session_settings_http.put_session_model_handler(
            _FakeRequest(body={"session_id": "s10", "profile": {"model": ""}})
        )
    )
    assert malformed.status_code == 400
    assert "model" in malformed.description

    monkeypatch.setattr(service, "is_session_busy", lambda sid: True)
    parked = asyncio.run(
        session_settings_http.put_session_model_handler(
            _FakeRequest(body={"session_id": "s10", "profile": _DESCRIPTOR})
        )
    )
    # Mid-turn switching is allowed now: the write parks and says so.
    assert parked.status_code == 200
    assert '"pending": true' in parked.description


def test_model_get_requires_session_id(registers):
    assert (
        asyncio.run(session_settings_http.get_session_model_handler(_FakeRequest())).status_code
        == 400
    )


def test_thinking_clear_restores_the_env_default(registers, monkeypatch):
    """`value: null` drops the explicit choice (symmetry with the model clear)."""
    mem, db = registers
    _wire_queue(monkeypatch, [])
    monkeypatch.setattr(service, "get_thinking_mode", lambda *_args: "on_off")
    service.set_thinking_value("s12", True)

    cleared = asyncio.run(
        session_settings_http.put_thinking_handler(
            _FakeRequest(body={"session_id": "s12", "value": None})
        )
    )

    assert cleared.status_code == 200
    assert '"pending": false' in cleared.description
    assert ("s12", "llm_thinking_enabled") not in mem.data
    assert ("s12", "llm_thinking_enabled") not in db.data
    assert service.get_thinking_state("s12")["enabled"] is None


def test_thinking_put_without_value_key_is_still_400(registers):
    resp = asyncio.run(
        session_settings_http.put_thinking_handler(_FakeRequest(body={"session_id": "s13"}))
    )
    assert resp.status_code == 400


def test_thinking_clear_parks_while_a_turn_runs(registers, monkeypatch):
    mem, _db = registers
    _wire_queue(monkeypatch, [])
    monkeypatch.setattr(service, "get_thinking_mode", lambda *_args: "on_off")
    service.set_thinking_value("s14", True)
    monkeypatch.setattr(service, "is_session_busy", lambda sid: True)

    parked = asyncio.run(
        session_settings_http.put_thinking_handler(
            _FakeRequest(body={"session_id": "s14", "value": None})
        )
    )

    assert parked.status_code == 200 and '"pending": true' in parked.description
    # The running turn keeps its value; the clear lands on the next turn.
    assert mem.data[("s14", "llm_thinking_enabled")] is True
    assert mem.data[("s14", "llm_thinking_enabled_pending")] == {"value": None}
