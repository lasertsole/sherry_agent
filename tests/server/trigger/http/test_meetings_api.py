"""The meetings HTTP surface: create (registry-resolved seats), list, messages,
cancel and the inline run route — handlers called directly with fake requests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import agent.meeting.persistence as persistence
from server.trigger.http import meetings as meetings_http

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


@pytest.fixture
def api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    persistence._reset_store_for_tests(tmp_path / "registry.db")
    experts = tmp_path / "experts"
    experts.mkdir(parents=True)
    # The registry module binds EXPERTS_DIR at import; repoint it at the temp
    # tree so load_expert() sees the fixture definitions.
    monkeypatch.setattr("agent.tools.expert_registry.EXPERTS_DIR", experts)
    (experts / "fe.json").write_text(
        json.dumps({"persona_prompt": "You are a frontend expert.", "skills": ["code_wiki"]}),
        encoding="utf-8",
    )
    (experts / "be.json").write_text(
        json.dumps({"persona_prompt": "You are a backend expert."}),
        encoding="utf-8",
    )
    return meetings_http


def _payload(response) -> dict:
    """Robyn's Response carries the JSON payload in ``description``."""
    return json.loads(response.description)


def _request(body: dict | None = None, query: dict | None = None):
    class _Request:
        """Shape the handlers actually read: read_body() calls request.json()."""

        def __init__(self):
            self.query_params = query or {}
            self._body = body

        def json(self):
            if body is None:
                raise ValueError("no body")
            return self._body

    return _Request()


async def test_create_resolves_participants_from_the_registry(api, tmp_path):
    response = await api.create_meeting_handler(
        _request({"session_id": "s1", "topic": "architecture", "participants": ["fe", "be"]})
    )

    payload = _payload(response)
    assert payload["success"] is True
    participants = payload["meeting"]["participants"]
    assert participants[0]["agent_id"] == "fe"
    assert participants[0]["persona_prompt"] == "You are a frontend expert."


async def test_create_with_an_unknown_expert_is_refused(api):
    response = await api.create_meeting_handler(
        _request({"session_id": "s1", "topic": "t", "participants": ["ghost"]})
    )

    payload = _payload(response)
    assert "not registered" in payload["message"]


async def test_list_get_and_messages_round_trip(api):
    created = _payload(
        await api.create_meeting_handler(
            _request({"session_id": "s", "topic": "t", "participants": ["fe"]})
        )
    )["meeting"]
    meeting_id = created["id"]

    listed = _payload(await api.list_meetings_handler(_request()))
    assert [m["id"] for m in listed["meetings"]] == [meeting_id]

    one = _payload(await api.get_meeting_handler(_request(query={"id": meeting_id})))
    assert one["meeting"]["id"] == meeting_id

    missing = await api.get_meeting_handler(_request(query={"id": "nope"}))
    assert missing.status_code == 404


async def test_cancel_and_rerun_guards(api):
    created = _payload(
        await api.create_meeting_handler(
            _request({"session_id": "s", "topic": "t", "participants": ["fe"]})
        )
    )["meeting"]
    meeting_id = created["id"]

    cancelled = _payload(await api.cancel_meeting_handler(_request(query={"id": meeting_id})))
    assert cancelled["meeting"]["status"] == "cancelled"

    again = await api.cancel_meeting_handler(_request(query={"id": meeting_id}))
    assert again.status_code == 400


async def test_run_route_executes_the_loop_with_a_fake_engine(api, monkeypatch):
    created = _payload(
        await api.create_meeting_handler(
            _request({"session_id": "s", "topic": "converge", "participants": ["fe", "be"]})
        )
    )["meeting"]

    # The engine is injectable at the route's import seam: a scripted selector
    # converges after one statement, so the spike route needs no model.
    import agent.meeting.flow as flow_mod

    class _FakeSelector:
        def __call__(self, history, participants):
            if len(history) < 2:
                from agent.meeting.models import MeetingTurnDecision

                return MeetingTurnDecision(next_speaker_id="fe", reason="first")
            from agent.meeting.models import MeetingTurnDecision

            return MeetingTurnDecision(reason="converged")

    def _fake_speak(participant, history, topic):
        return "statement from " + participant.name

    monkeypatch.setattr(flow_mod, "make_selector", lambda: _FakeSelector())
    monkeypatch.setattr(flow_mod, "make_participants", lambda: _fake_speak)

    response = await api.run_meeting_handler(_request({"meeting_id": created["id"]}))
    payload = _payload(response)

    assert payload["success"] is True
    assert payload["rounds"] == 1
    assert payload["meeting"]["status"] == "ended"
    assert payload["conclusion"]["summary"]

    messages = _payload(await api.meeting_messages_handler(_request(query={"id": created["id"]})))[
        "messages"
    ]
    assert any("statement from fe" in m["content"] for m in messages)
