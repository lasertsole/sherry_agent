"""Meeting CRUD routes (spike, plan §7 minus the WS stream and attachments).

POST /meetings                      create a meeting (topic + seated experts)
GET  /meetings                      list the meetings
GET  /meetings?id=…                 one meeting's row
DELETE /meetings?id=…               cancel a meeting (in_progress/waiting only)
GET  /meetings/messages?id=…        the appended utterances
POST /meetings/run                  run the discussion loop to convergence

The run route executes the whole loop inline (spike): the real deployment moves
it to a background task + the meeting WS stream (阶段 5). The expert registry
seats participants by name; unknown expert names fail the create.
"""

from __future__ import annotations

import uuid

from server.trigger.core import app
from server.trigger.http.helpers import bad_request, not_found, ok, read_body

# The agent-layer imports are FUNCTION-LEVEL: importing them at module scope
# pulls the full agent package while server.trigger.http is itself imported
# from agent/__init__'s own import graph, which is a circular import.


@app.post("/meetings")
async def create_meeting_handler(request):
    """Create a meeting row (topic + participants resolved from the registry)."""
    from agent.meeting.models import MeetingParticipant
    from agent.meeting.persistence import get_meeting_store
    from agent.tools.expert_registry import ExpertRegistryError, load_expert

    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    session_id = str(body.get("session_id") or "").strip()
    topic = str(body.get("topic") or "").strip()
    expert_names = body.get("participants")
    if not session_id:
        return bad_request("session_id is required")
    if not topic:
        return bad_request("topic is required")
    if not isinstance(expert_names, list) or not expert_names:
        return bad_request("participants (a non-empty expert-name list) is required")
    participants = []
    for name in expert_names[:8]:
        try:
            expert = load_expert(str(name))
        except ExpertRegistryError as exc:
            return bad_request(str(exc))
        participants.append(
            MeetingParticipant(
                agent_id=expert.name,
                name=expert.name,
                persona_prompt=expert.persona_prompt,
                join_order=len(participants) + 1,
            )
        )
    store = await get_meeting_store()
    meeting = await store.create_meeting(
        uuid.uuid4().hex,
        session_id=session_id,
        topic=topic,
        participants=[p.model_dump() for p in participants],
    )
    return ok({"success": True, "meeting": meeting})


@app.get("/meetings")
async def list_meetings_handler(request):
    from agent.meeting.persistence import get_meeting_store

    store = await get_meeting_store()
    return ok({"success": True, "meetings": await store.list_meetings()})


@app.get("/meetings/one")
async def get_meeting_handler(request):
    meeting_id = str(request.query_params.get("id") or "")
    if not meeting_id:
        return bad_request("id is required")
    from agent.meeting.persistence import get_meeting_store

    store = await get_meeting_store()
    meeting = await store.get_meeting(meeting_id)
    if meeting is None:
        return not_found("meeting not found")
    return ok({"success": True, "meeting": meeting})


@app.get("/meetings/messages")
async def meeting_messages_handler(request):
    meeting_id = str(request.query_params.get("id") or "")
    if not meeting_id:
        return bad_request("id is required")
    from agent.meeting.persistence import get_meeting_store

    store = await get_meeting_store()
    if await store.get_meeting(meeting_id) is None:
        return not_found("meeting not found")
    return ok({"success": True, "messages": await store.list_messages(meeting_id)})


@app.delete("/meetings")
async def cancel_meeting_handler(request):
    meeting_id = str(request.query_params.get("id") or "")
    if not meeting_id:
        return bad_request("id is required")
    from agent.meeting.persistence import get_meeting_store

    store = await get_meeting_store()
    meeting = await store.get_meeting(meeting_id)
    if meeting is None:
        return not_found("meeting not found")
    if meeting["status"] in {"ended", "cancelled"}:
        return bad_request(f"meeting is already {meeting['status']}")
    await store.update_meeting(meeting_id, status="cancelled", ended_at=None)
    return ok({"success": True, "meeting": await store.get_meeting(meeting_id)})


@app.post("/meetings/run")
async def run_meeting_handler(request):
    """Run one meeting's discussion loop to convergence (spike: inline)."""
    from agent.meeting.flow import run_meeting
    from agent.meeting.models import MeetingParticipant
    from agent.meeting.persistence import get_meeting_store

    body = read_body(request)
    if body is None:
        return bad_request("JSON body required")
    meeting_id = str(body.get("meeting_id") or "").strip()
    if not meeting_id:
        return bad_request("meeting_id is required")

    store = await get_meeting_store()
    meeting = await store.get_meeting(meeting_id)
    if meeting is None:
        return not_found("meeting not found")
    if meeting["status"] == "ended":
        return ok({"success": True, "already_ended": True, "meeting": meeting})
    participants = [
        MeetingParticipant(
            agent_id=p["agent_id"],
            name=p.get("name") or p["agent_id"],
            persona_prompt=p.get("persona_prompt") or "",
            join_order=int(p.get("join_order") or 0),
        )
        for p in meeting["participants"]
    ]
    result = await run_meeting(
        store,
        session_id=meeting["session_id"],
        topic=meeting["topic"],
        participants=participants,
        meeting_id=meeting_id,
    )
    return ok({"success": True, **result})
