"""``GET/PUT /sessions/search_backend``: the 模型联网 panel's per-session choice.

The HTTP boundary drives the service through ``asyncio.to_thread`` (sync SQLite
off the event loop); the choice is parked mid-turn under the twin pending key
and promoted at the turn boundary — the same pair the thinking and model
controls use. The GET is also the panel's readiness probe, so it answers with an
EMPTY session id too (availability + default view).
"""

import asyncio
import json

import pytest

from server.service import session_settings_service as service
from server.trigger.http import session_settings as session_settings_http

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
    # The tool resolves the preferred backend through `from runtime import
    # state_register_mem`, which reads the PACKAGE attribute — patch that too so
    # the read path under test sees the same register the service writes to.
    monkeypatch.setattr("runtime.state_register_mem", mem)
    return mem, db


@pytest.fixture(autouse=True)
def _env_file(tmp_path, monkeypatch):
    """A throwaway .env for the panel's FILE view (never the real file)."""
    path = tmp_path / ".env"
    path.write_text(
        'TAVILY_API_KEY = "tvly-secret"\n'
        "SEARXNG_URL = http://localhost:9999\n"
        "SEARXNG_PROXY = http://127.0.0.1:7890\n"
        "SEARXNG_ENGINES = google,bing\n"
        "SEARXNG_CATEGORIES = it\n"
        "SEARXNG_VERIFY_TLS = false\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("server.service.env.ENV_PATH", path)
    return path


@pytest.fixture(autouse=True)
def _providers(monkeypatch):
    """Both providers configured and the search tool's resolution stubbed out."""
    import agent.tools.web_search as web_search

    monkeypatch.setattr(web_search, "tavily_available", lambda: True)
    monkeypatch.setattr(
        web_search,
        "searxng_available",
        lambda: True,
    )
    # The service imports both lazily from the tool module; the availability of
    # the instance itself is reported by searxng_status.
    import agent.tools.web_search_searxng as searxng

    monkeypatch.setattr(
        searxng,
        "searxng_status",
        lambda: {
            "configured": True,
            "url": "http://localhost:8888",
            "engines": "",
            "categories": "",
            "proxy": "",
            "verify_tls": True,
        },
    )


def _wire_http(monkeypatch):
    monkeypatch.setattr(
        session_settings_http, "apply_search_backend_choice", service.apply_search_backend_choice
    )
    monkeypatch.setattr(
        session_settings_http, "get_search_backend_state", service.get_search_backend_state
    )


def _body(resp) -> dict:
    return json.loads(resp.description)


# ----------------------------------------------------------------------
# the service's read model
# ----------------------------------------------------------------------


def test_unset_choice_reports_the_availability_default(registers):
    state = service.get_search_backend_state("s1")

    assert state["backend"] is None
    assert state["effective"] == "tavily"
    assert state["pending"] is False
    assert state["tavily_available"] is True
    assert state["searxng"]["configured"] is True


def test_the_file_view_carries_what_the_panel_edits(registers):
    """The search keys are withheld from GET /env, so the state payload is the
    panel's read side — and it must never carry the Tavily VALUE."""
    state = service.get_search_backend_state("s1")

    assert state["env"] == {
        "searxng_url": "http://localhost:9999",
        "searxng_proxy": "http://127.0.0.1:7890",
        "searxng_engines": "google,bing",
        "searxng_categories": "it",
        "searxng_verify_tls": False,
        "tavily_key_present": True,
    }
    assert "tvly-secret" not in json.dumps(state)


def test_an_empty_session_id_answers_the_global_view(registers):
    state = service.get_search_backend_state("")

    assert state["backend"] is None
    assert state["pending"] is False
    assert state["searxng"]["url"] == "http://localhost:8888"


def test_a_rejected_session_id_is_refused(registers):
    with pytest.raises(ValueError):
        service.get_search_backend_state("../../etc")


def test_the_stored_choice_is_reported_back(registers, monkeypatch):
    from runtime.session.state_keys import StateKey

    mem, _db = registers
    mem.set_state("s2", StateKey.SEARCH_BACKEND, "searxng")

    state = service.get_search_backend_state("s2")

    assert state["backend"] == "searxng"
    assert state["effective"] == "searxng"


def test_an_idle_read_promotes_a_lingering_park(registers):
    from runtime.session.state_keys import StateKey

    mem, _db = registers
    service._park_value("s3", StateKey.SEARCH_BACKEND_PENDING.value, "searxng")

    state = service.get_search_backend_state("s3")

    assert state["pending"] is False
    assert state["backend"] == "searxng"
    assert mem.data[("s3", StateKey.SEARCH_BACKEND.value)] == "searxng"
    assert ("s3", StateKey.SEARCH_BACKEND_PENDING.value) not in mem.data


# ----------------------------------------------------------------------
# HTTP: read + write round trip
# ----------------------------------------------------------------------


def test_get_reports_availability_for_the_panel(registers, monkeypatch):
    _wire_http(monkeypatch)

    resp = asyncio.run(
        session_settings_http.get_search_backend_handler(
            _FakeRequest(query_params={"session_id": "s4"})
        )
    )

    assert resp.status_code == 200
    payload = _body(resp)
    assert payload["success"] is True
    assert payload["backend"] is None
    assert payload["effective"] == "tavily"
    assert payload["tavily_available"] is True
    assert payload["searxng"]["configured"] is True
    assert payload["env"]["tavily_key_present"] is True
    assert payload["env"]["searxng_url"] == "http://localhost:9999"


def test_get_without_a_session_id_is_the_settings_tab_view(registers, monkeypatch):
    _wire_http(monkeypatch)

    resp = asyncio.run(session_settings_http.get_search_backend_handler(_FakeRequest()))

    assert resp.status_code == 200
    assert _body(resp)["backend"] is None


def test_get_rejects_a_traversal_session(registers, monkeypatch):
    _wire_http(monkeypatch)

    resp = asyncio.run(
        session_settings_http.get_search_backend_handler(
            _FakeRequest(query_params={"session_id": "../../workspace"})
        )
    )

    assert resp.status_code == 400


def test_put_then_get_roundtrip(registers, monkeypatch):
    _wire_http(monkeypatch)
    monkeypatch.setattr(service, "_session_turn_active", lambda sid: False)
    monkeypatch.setattr(service, "_apply_session_choice", _idle_apply)

    put = asyncio.run(
        session_settings_http.put_search_backend_handler(
            _FakeRequest(body={"session_id": "s5", "backend": "searxng"})
        )
    )
    assert put.status_code == 200
    assert _body(put)["pending"] is False

    mem, _db = registers
    assert mem.data[("s5", "search_backend")] == "searxng"

    get = asyncio.run(
        session_settings_http.get_search_backend_handler(
            _FakeRequest(query_params={"session_id": "s5"})
        )
    )
    assert _body(get)["backend"] == "searxng"


def test_put_null_clears_the_choice(registers, monkeypatch):
    from runtime.session.state_keys import StateKey

    _wire_http(monkeypatch)
    monkeypatch.setattr(service, "_apply_session_choice", _idle_apply)
    mem, _db = registers
    mem.set_state("s6", StateKey.SEARCH_BACKEND, "searxng")

    resp = asyncio.run(
        session_settings_http.put_search_backend_handler(
            _FakeRequest(body={"session_id": "s6", "backend": None})
        )
    )

    assert resp.status_code == 200
    assert ("s6", StateKey.SEARCH_BACKEND.value) not in mem.data
    assert service.get_search_backend_state("s6")["backend"] is None


def test_put_rejects_an_unknown_backend(registers):
    resp = asyncio.run(
        session_settings_http.put_search_backend_handler(
            _FakeRequest(body={"session_id": "s7", "backend": "google"})
        )
    )

    assert resp.status_code == 400


def test_put_rejects_a_missing_backend_field(registers):
    resp = asyncio.run(
        session_settings_http.put_search_backend_handler(_FakeRequest(body={"session_id": "s8"}))
    )

    assert resp.status_code == 400


def test_put_rejects_a_missing_body(registers):
    resp = asyncio.run(session_settings_http.put_search_backend_handler(_FakeRequest()))

    assert resp.status_code == 400


def test_put_rejects_a_traversal_session(registers):
    resp = asyncio.run(
        session_settings_http.put_search_backend_handler(
            _FakeRequest(body={"session_id": "../../workspace", "backend": "tavily"})
        )
    )

    assert resp.status_code == 400


# ----------------------------------------------------------------------
# parking + promotion (mid-turn switching)
# ----------------------------------------------------------------------


async def _idle_apply(session_id, *, live_key, pending_key, value, write_live):
    """The real applier with the busy check removed (idle path)."""
    if not session_id or not service.is_safe_session_id(session_id):
        raise ValueError("invalid session_id")
    if value is None:
        service._delete_value(session_id, live_key)
    else:
        await asyncio.to_thread(write_live)
    service._delete_value(session_id, pending_key)
    return False


async def _parked_apply(session_id, *, live_key, pending_key, value, write_live):
    """The real applier with a turn permanently in flight (parked path)."""
    await asyncio.to_thread(service._park_value, session_id, pending_key, value)
    return True


def test_a_mid_turn_choice_is_parked_and_reported(registers, monkeypatch):
    _wire_http(monkeypatch)
    monkeypatch.setattr(service, "_apply_session_choice", _parked_apply)

    resp = asyncio.run(
        session_settings_http.put_search_backend_handler(
            _FakeRequest(body={"session_id": "s9", "backend": "searxng"})
        )
    )

    assert resp.status_code == 200
    assert _body(resp)["pending"] is True
    mem, _db = registers
    # The running turn keeps its backend; the choice waits under the twin key.
    assert ("s9", "search_backend") not in mem.data
    assert mem.data[("s9", "search_backend_pending")] == {"value": "searxng"}

    # While the turn runs the panel already shows the picked value as pending.
    monkeypatch.setattr(service, "_session_turn_active", lambda sid: True)
    state = service.get_search_backend_state("s9")
    assert state["backend"] == "searxng"
    assert state["pending"] is True
    assert state["effective"] == "tavily", "the running turn still serves from the live choice"
    assert state["pending_value"] == "searxng"


def test_promotion_moves_the_parked_choice_into_the_live_key(registers):
    from runtime.session.state_keys import StateKey

    mem, _db = registers
    service._park_value("s10", StateKey.SEARCH_BACKEND_PENDING.value, "searxng")

    promoted = service.promote_pending_settings_sync("s10")

    assert promoted == ["search_backend"]
    assert mem.data[("s10", StateKey.SEARCH_BACKEND.value)] == "searxng"
    assert ("s10", StateKey.SEARCH_BACKEND_PENDING.value) not in mem.data
    assert service.promote_pending_settings_sync("s10") == []


def test_promotion_honours_a_parked_clear(registers):
    from runtime.session.state_keys import StateKey

    mem, _db = registers
    mem.set_state("s11", StateKey.SEARCH_BACKEND, "searxng")
    service._park_value("s11", StateKey.SEARCH_BACKEND_PENDING.value, None)

    service.promote_pending_settings_sync("s11")

    # A parked null means "follow the default again": the live key is removed.
    assert ("s11", StateKey.SEARCH_BACKEND.value) not in mem.data
    assert ("s11", StateKey.SEARCH_BACKEND_PENDING.value) not in mem.data
