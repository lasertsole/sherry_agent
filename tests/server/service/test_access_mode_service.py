"""Tests for the per-session access mode (toolbar shield control).

The control has three positions: ``confirm_all`` (strict — every command and
every file change asks), ``auto_edit`` (default — the normal approval gates) and
``full_access`` (the session's HITL bypass-all flag). The strict and bypass flags
are two ends of one setting and clear each other. All of them live in the session
state register the approval pipeline reads on every tool call.
"""

import asyncio
import json

import pytest

from server.service import access_mode_service as service
from server.trigger.http import access_mode as http

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
def register(monkeypatch):
    """One fake register behind both readers: the service's own import and the
    approval pipeline's lazy `TypedState` lookup."""
    from runtime.session import state_register as register_module
    from runtime.session.state_keys import StateKey

    mem = _FakeRegister()
    monkeypatch.setattr(service, "state_register_mem", mem)
    monkeypatch.setattr(register_module, "state_register_mem", mem)
    # The register stores the enum key (as the real one does).
    mem.yolo_key = StateKey.HITL_SESSION_YOLO
    mem.confirm_all_key = StateKey.HITL_SESSION_CONFIRM_ALL
    return mem


def test_default_mode_is_auto_edit(register):
    assert service.get_access_mode("s1") == service.AUTO_EDIT


def test_full_access_sets_the_bypass_flag(register):
    assert service.set_access_mode("s1", service.FULL_ACCESS) == service.FULL_ACCESS

    # The session flag the approval pipeline reads is what got set…
    assert register.data[("s1", register.yolo_key)] is True
    # …and the control reports it back.
    assert service.get_access_mode("s1") == service.FULL_ACCESS


def test_switching_back_clears_the_bypass_flag(register):
    service.set_access_mode("s1", service.FULL_ACCESS)
    service.set_access_mode("s1", service.AUTO_EDIT)

    assert register.data[("s1", register.yolo_key)] is False
    assert service.get_access_mode("s1") == service.AUTO_EDIT


def test_modes_are_isolated_per_session(register):
    service.set_access_mode("s1", service.FULL_ACCESS)

    assert service.get_access_mode("s1") == service.FULL_ACCESS
    assert service.get_access_mode("s2") == service.AUTO_EDIT


def test_confirm_all_sets_the_strict_flag(register):
    assert service.set_access_mode("s1", service.CONFIRM_ALL) == service.CONFIRM_ALL

    assert register.data[("s1", register.confirm_all_key)] is True
    assert register.data[("s1", register.yolo_key)] is False
    assert service.get_access_mode("s1") == service.CONFIRM_ALL


def test_confirm_all_and_full_access_clear_each_other(register):
    service.set_access_mode("s1", service.FULL_ACCESS)
    service.set_access_mode("s1", service.CONFIRM_ALL)

    # Switching ends of the setting leaves no stale bypass behind…
    assert register.data[("s1", register.yolo_key)] is False
    assert register.data[("s1", register.confirm_all_key)] is True
    assert service.get_access_mode("s1") == service.CONFIRM_ALL

    # …and the reverse direction is symmetric.
    service.set_access_mode("s1", service.FULL_ACCESS)
    assert register.data[("s1", register.confirm_all_key)] is False
    assert register.data[("s1", register.yolo_key)] is True
    assert service.get_access_mode("s1") == service.FULL_ACCESS


def test_a_card_answered_with_yolo_reports_full_access(register):
    # The approval card's bypass-all answer sets the same flag the control reads.
    from agent.middlewares.humanInTheLoop.approval import set_session_yolo

    set_session_yolo("s1")

    assert service.get_access_mode("s1") == service.FULL_ACCESS

    # Switching the control back to the default cancels it.
    service.set_access_mode("s1", service.AUTO_EDIT)
    assert register.data[("s1", register.yolo_key)] is False
    assert service.get_access_mode("s1") == service.AUTO_EDIT


def test_switching_to_auto_edit_clears_both_flags(register):
    service.set_access_mode("s1", service.CONFIRM_ALL)
    service.set_access_mode("s1", service.AUTO_EDIT)

    assert register.data[("s1", register.confirm_all_key)] is False
    assert register.data[("s1", register.yolo_key)] is False
    assert service.get_access_mode("s1") == service.AUTO_EDIT


def test_unknown_mode_and_bad_session_are_rejected(register):
    with pytest.raises(ValueError):
        service.set_access_mode("s1", "yolo")
    with pytest.raises(ValueError):
        service.set_access_mode("", service.AUTO_EDIT)
    with pytest.raises(ValueError):
        service.get_access_mode("../escape")


def test_http_roundtrip(register):
    put = asyncio.run(
        http.access_mode_put_handler(
            _FakeRequest(body={"session_id": "s1", "mode": service.FULL_ACCESS})
        )
    )
    assert put.status_code == 200
    assert json.loads(put.description)["mode"] == service.FULL_ACCESS

    get = asyncio.run(http.access_mode_get_handler(_FakeRequest({"session_id": "s1"})))
    assert get.status_code == 200
    assert json.loads(get.description)["mode"] == service.FULL_ACCESS

    strict = asyncio.run(
        http.access_mode_put_handler(
            _FakeRequest(body={"session_id": "s1", "mode": service.CONFIRM_ALL})
        )
    )
    assert strict.status_code == 200
    assert json.loads(strict.description)["mode"] == service.CONFIRM_ALL


def test_http_rejects_a_bad_mode(register):
    response = asyncio.run(
        http.access_mode_put_handler(_FakeRequest(body={"session_id": "s1", "mode": "nope"}))
    )

    assert response.status_code == 400
    assert json.loads(response.description)["success"] is False
