"""``edit_message``: latest-only validation, rewind ordering and ack contract."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server.trigger.ws import messages as ws_messages

pytestmark = [pytest.mark.unit, pytest.mark.timeout(60)]


class _FakeSocket:
    def __init__(self) -> None:
        self.id = "ws-edit"


@pytest.fixture
def harness(monkeypatch):
    """Wire every step of the handler to a recorder (no checkpoint, no git)."""
    frames: list[dict] = []
    calls: dict[str, list] = {
        "truncate": [],
        "rollback": [],
        "delete": [],
        "submit": [],
    }

    async def _send_ws(_websocket, payload):
        frames.append(payload)

    monkeypatch.setattr(ws_messages, "_send_ws", _send_ws)
    monkeypatch.setattr(
        "agent.tools.subagent.registry.session_state.detect_state",
        lambda _sid: SimpleNamespace(busy=False),
    )

    from context_engine.store import core as store_core

    monkeypatch.setattr(
        store_core,
        "find_user_turn_cut",
        lambda _sid, turn, _row_id=None: (1, turn),
    )
    monkeypatch.setattr(store_core, "get_max_user_turn_num", lambda _sid: 3)
    monkeypatch.setattr(
        store_core,
        "delete_messages_from_turn",
        lambda _sid, turn: calls["delete"].append(turn) or 1,
    )

    import agent.state_port as state_port

    async def _truncate(_sid, ordinal):
        calls["truncate"].append(ordinal)
        return 4

    monkeypatch.setattr(state_port, "truncate_messages_after_turn", _truncate)

    from agent.tools.pub_base import shadow_git

    def _restore(_sid, turn):
        calls["rollback"].append(turn)
        return {"restored": True}

    monkeypatch.setattr(shadow_git, "restore_to_turn", _restore)

    from server.service import resumable_breakpoint

    monkeypatch.setattr(resumable_breakpoint, "clear_breakpoint", lambda _sid: None)

    async def _submit(_sid, text, _source, client_msg_id=None):
        calls["submit"].append((text, client_msg_id))
        return SimpleNamespace(status=ws_messages.iqs.SubmitStatus.STARTED)

    monkeypatch.setattr(ws_messages.iqs, "submit_user_input", _submit)

    return SimpleNamespace(frames=frames, calls=calls)


def _frame(turn_num: int = 3, text: str = "edited", msg_id: str = "m-new") -> dict:
    return {"turn_num": turn_num, "new_text": text, "msg_id": msg_id}


def _run(frame: dict) -> None:
    asyncio.run(ws_messages._handle_edit_message(_FakeSocket(), "sess", frame))


def test_only_the_latest_user_turn_is_editable(harness, monkeypatch):
    from context_engine.store import core as store_core

    # The clicked row is turn 2 while the newest user row is turn 3.
    monkeypatch.setattr(
        store_core, "find_user_turn_cut", lambda _sid, turn, _row_id=None: (1, turn)
    )

    _run(_frame(turn_num=2))

    assert harness.frames == [
        {
            "event": "edit_message_ack",
            "session_id": "sess",
            "content": {"ok": False, "reason": "not_latest"},
        }
    ]
    assert harness.calls == {"truncate": [], "rollback": [], "delete": [], "submit": []}


def test_compacted_history_is_refused_before_any_file_change(harness, monkeypatch):
    import agent.state_port as state_port

    async def _compacted(_sid, _ordinal):
        return -1

    monkeypatch.setattr(state_port, "truncate_messages_after_turn", _compacted)

    _run(_frame())

    assert harness.frames[0]["content"] == {"ok": False, "reason": "compacted"}
    # Ordering contract: nothing touched the workspace for a refused edit.
    assert harness.calls["rollback"] == []
    assert harness.calls["delete"] == []
    assert harness.calls["submit"] == []


def test_a_skipped_rollback_warns_but_still_re_runs(harness, monkeypatch):
    from agent.tools.pub_base import shadow_git

    monkeypatch.setattr(
        shadow_git,
        "restore_to_turn",
        lambda _sid, _turn: {"restored": False, "reason": "no_checkpoint"},
    )

    _run(_frame())

    assert harness.frames == [
        {
            "event": "edit_message_ack",
            "session_id": "sess",
            "content": {
                "ok": True,
                "rollback_skipped": True,
                "rollback_reason": "no_checkpoint",
            },
        }
    ]
    assert harness.calls["delete"] == [3]
    assert harness.calls["submit"] == [("edited", "m-new")]


def test_a_restored_rollback_is_silent_and_re_runs(harness):
    _run(_frame())

    assert harness.frames == []
    assert harness.calls["truncate"] == [1]
    assert harness.calls["rollback"] == [3]
    assert harness.calls["delete"] == [3]
    assert harness.calls["submit"] == [("edited", "m-new")]


def test_a_busy_session_refuses_the_edit(harness, monkeypatch):
    monkeypatch.setattr(
        "agent.tools.subagent.registry.session_state.detect_state",
        lambda _sid: SimpleNamespace(busy=True),
    )

    _run(_frame())

    assert harness.frames[0]["content"] == {"ok": False, "reason": "busy"}
    assert harness.calls["truncate"] == []
    assert harness.calls["submit"] == []
