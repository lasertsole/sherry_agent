"""TDD regression tests: relation_register websocket unbind must not clobber.

Bug context (2026-09-06 streaming fix): the agent WS handler now registers its
per-message socket under the chat session_id so the Task 7 WsTurnExecutor can
resolve the reply socket via relation_register (see
server/trigger/ws/messages.py::agent_ws_handler). The client opens one agent
WS per message, so a newer socket can register while an older socket of the
same session is still connected. When the OLD socket disconnects late, its
unregister must not remove the NEW socket's session mapping — otherwise the
live connection silently loses its frames (lookup returns None →
turn_runner._send_ws(None, ...) is a no-op).

Contract pinned here:
- unregister by websocket / websocket_id only clears the session→ws mapping
  when it still points at THAT websocket (last-writer-wins safe);
- a mapping-owner unregister still cleans everything;
- overwrite registration keeps the replaced socket's own reverse entries
  (it is still connected — only its streaming duty was reassigned).
"""

import pytest
from runtime.relation_register import relation_register

pytestmark = pytest.mark.unit


class _FakeWs:
    """Minimal websocket double — register/unregister only touch ``.id``."""

    def __init__(self, ws_id: str):
        self.id = ws_id


@pytest.fixture
def reg():
    """Hermetic view of the relation_register singleton (snapshot/restore)."""
    saved = (
        dict(relation_register.websocket_id_to_session_id),
        dict(relation_register.session_id_to_websocket_id),
        dict(relation_register.websocket_id_to_ws),
    )
    relation_register.websocket_id_to_session_id.clear()
    relation_register.session_id_to_websocket_id.clear()
    relation_register.websocket_id_to_ws.clear()
    try:
        yield relation_register
    finally:
        relation_register.websocket_id_to_session_id.clear()
        relation_register.session_id_to_websocket_id.clear()
        relation_register.websocket_id_to_ws.clear()
        relation_register.websocket_id_to_session_id.update(saved[0])
        relation_register.session_id_to_websocket_id.update(saved[1])
        relation_register.websocket_id_to_ws.update(saved[2])


def test_stale_websocket_unregister_keeps_newer_binding(reg):
    old, new = _FakeWs("ws-old"), _FakeWs("ws-new")
    reg.register_websocket("s1", old)
    reg.register_websocket("s1", new)  # newer socket overwrites the binding

    reg.unregister_websocket_by_websocket(old)  # old socket disconnects late

    assert reg.get_websocket_by_websocket_id("ws-old") is None, (
        "the old socket itself must be fully unbound"
    )
    assert reg.get_websocket_by_session_id("s1") is new, (
        "a stale disconnect must not clobber the newer socket's binding"
    )


def test_stale_websocket_id_unregister_keeps_newer_binding(reg):
    old, new = _FakeWs("ws-old"), _FakeWs("ws-new")
    reg.register_websocket("s1", old)
    reg.register_websocket("s1", new)

    reg.unregister_websocket_by_websocket_id("ws-old")

    assert reg.get_websocket_by_websocket_id("ws-old") is None
    assert reg.get_websocket_by_session_id("s1") is new


def test_owner_unregister_still_clears_everything(reg):
    ws = _FakeWs("ws-1")
    reg.register_websocket("s1", ws)

    reg.unregister_websocket_by_websocket(ws)

    assert reg.get_websocket_by_session_id("s1") is None
    assert reg.get_websocket_by_websocket_id("ws-1") is None


def test_newest_owner_unregister_clears_session_binding(reg):
    old, new = _FakeWs("ws-old"), _FakeWs("ws-new")
    reg.register_websocket("s1", old)
    reg.register_websocket("s1", new)

    reg.unregister_websocket_by_websocket(new)  # the CURRENT owner leaves

    assert reg.get_websocket_by_session_id("s1") is None
    assert reg.get_websocket_by_websocket_id("ws-new") is None
    # The old socket is still physically connected — only its streaming duty
    # was reassigned — so its own reverse entries must survive.
    assert reg.get_websocket_by_websocket_id("ws-old") is old


def test_overwrite_registration_rebinds_session(reg):
    old, new = _FakeWs("ws-old"), _FakeWs("ws-new")
    reg.register_websocket("s1", old)
    reg.register_websocket("s1", new)

    assert reg.get_websocket_by_session_id("s1") is new
    assert reg.get_websocket_id_by_session_id("s1") == "ws-new"
    assert reg.get_session_id_by_websocket_id("ws-new") == "s1"
