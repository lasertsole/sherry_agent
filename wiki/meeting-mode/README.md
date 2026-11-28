# Meeting Mode

Meeting mode is a third product mode for Sherry: several registered experts
discuss one topic, reach a shared conclusion, and hand a structured plan to the
expert-team executor. The discussion phase and the execution phase are
independent — a meeting can produce a plan that is saved and run later.

## Why a meeting

A single agent answers from one perspective. A meeting puts several domain
experts in the same conversation so decisions record their dependencies: the
frontend position informs the backend, which informs the database. The user is
the boss and may interject at any time; the experts never call each other
directly, so the discussion cannot deadlock.

## Architecture

```
User  ->  Host  ->  Selector  ->  Participants  ->  Conclusion
            |           |              |
         tools       one JSON       one statement
         allowlist   verdict        per turn
```

The three roles live in `agent/meeting/`. The `host` only coordinates and its
tool surface is stripped to a coordination allowlist; the `selector` returns one
`MeetingTurnDecision` per turn; each of the `participants` contributes one
statement built from the shared history. `run_meeting` is the original loop;
`run_meeting_with_interrupts` adds stage transitions, interjections and the
phase allow-list gate.

## Lifecycle

A meeting opens with the host confirming the topic, then enters the dispatching
loop: the selector names the next speaker, that participant speaks, and the
utterance is appended. When the selector returns no next speaker — or the
40-round cap is reached — the host drafts a summary and the meeting moves to
end confirmation. Each utterance is persisted with a speaker kind and a message
role.

## Interrupts and rollback

`apply_interruption` classifies a user interjection into one of six types. A
supplement is appended without changing the plan; an end request moves the
meeting to end confirmation. Corrections, scope changes and priority changes
call `apply_local_rollback`: the affected execution-plan phases — and every
phase that depends on them — are marked `needs_rediscussion` so the selector
re-plans them on the next turn. A phase allow-list gate keeps a participant from
speaking outside the phase that selected it.

## From conclusion to execution

`build_conclusion` produces a `MeetingConclusion` with a TaskFlow-shaped plan:
each step carries a task, a registered expert name and its dependencies.
`execute_conclusion` creates a flow with the deterministic id
`meeting-{meeting_id}` and dispatches its steps through the expert-team
machinery. A second execute request finds the existing flow instead of
duplicating it. The `reusable` hint stays on the meeting conclusion because it
is not a native TaskFlow step field.

## Streaming

The meeting stream is a per-meeting WebSocket channel at `/meetings/ws`. Agent
events are emitted through the `MEETING_EVENT_PUSH` hook, which is fail-open: a
process without the WS server still runs meetings normally. Frames cover
speaker changes, message deltas and completions, thinking deltas, ask-user
prompts and summary drafts. The Nuxt page feeds every frame into the store's
frame reducer; mock mode keeps the page navigable without a backend.

## Persistence and recovery

Meetings, messages and `meeting_compaction_segments` live on the shared
registry database. Long discussions are compacted with the summarization
middleware's reusable primitives through a meeting-owned trigger, so the main
agent's task-intent is never re-armed. At boot, `auto_adjourn_at_boot` cancels
live meetings with the `process_restart` reason because their in-memory loop
cannot survive a restart.

## API

The HTTP surface is `POST /meetings` to create, `GET /meetings` to list,
`GET /meetings/one` and `GET /meetings/messages` to read, `DELETE /meetings` to
cancel, plus `POST /meetings/run`, `/meetings/interrupt`, `/meetings/reply`,
`/meetings/confirm-end`, `/meetings/execute` and `GET /meetings/conclusion`.
All routes use query parameters or JSON bodies; none use path interpolation.

## Tests and gates

`tests/agent/meeting/` covers the loop, classification, rollback, compaction,
conclusion and boot recovery; `tests/server/trigger/http/test_meetings_api.py`
covers the routes and `tests/server/trigger/ws/test_meeting_ws.py` covers frame
emission. Frontend tests pin the store reducers and the page. Static gates are
`ruff`, `basedpyright` and `lint-imports`; doc gates are parity, links and
language.
