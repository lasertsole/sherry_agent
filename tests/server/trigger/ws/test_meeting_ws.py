"""阶段 5: meeting WS frames and fail-open emission seam."""

from __future__ import annotations

import json

import pytest

from agent.meeting.emit import emit_meeting_event
from runtime import hooks
from server.trigger.ws.meeting_ws import MEETING_EVENTS, push_meeting_event

pytestmark = [pytest.mark.unit]


def test_push_meeting_event_enqueues_keyed_frame(monkeypatch):
    captured: list[tuple[str, str]] = []

    class FakeChannel:
        def enqueue_frame(self, frame: str, key: str | None = None) -> None:
            captured.append((frame, key or ""))

    import server.trigger.ws.meeting_ws as mod

    monkeypatch.setattr(mod, "_channel", FakeChannel())
    push_meeting_event("m1", {"event": "meeting_speaker_changed", "speaker_id": "fe"})
    frame, key = captured[0]
    assert key == "m1"
    payload = json.loads(frame)
    assert payload["meeting_id"] == "m1"
    assert payload["event"] == "meeting_speaker_changed"


def test_emit_resolves_hook_fail_open(monkeypatch):
    seen: list[tuple[str, dict]] = []
    monkeypatch.setattr(hooks, "MEETING_EVENT_PUSH", "meeting_event_push")
    monkeypatch.setattr(hooks, "resolve", lambda name: lambda mid, ev: seen.append((mid, ev)))
    emit_meeting_event("m1", {"event": "x"})
    assert seen == [("m1", {"event": "x"})]

    # A broken sink must not raise.
    def broken(_mid, _ev):
        raise RuntimeError("boom")

    monkeypatch.setattr(hooks, "resolve", lambda name: broken)
    emit_meeting_event("m1", {"event": "x"})  # no exception

    # Missing hook is a no-op.
    monkeypatch.setattr(hooks, "resolve", lambda name: None)
    emit_meeting_event("m1", {"event": "x"})


def test_event_names_match_frontend_contract():
    assert {
        "meeting_speaker_changed",
        "meeting_message_delta",
        "meeting_message_completed",
        "meeting_thinking_delta",
        "meeting_ask_user",
        "meeting_summary_draft",
    } <= set(MEETING_EVENTS)
