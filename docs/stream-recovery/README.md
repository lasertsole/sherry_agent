# 🔁 Stream Recovery

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> A mid-turn WebSocket drop must not force the client to rerun the turn, and it must not replay frames that other viewers already received. Sherry restores the running turn with a text snapshot plus a per-turn sequence watermark.

## Scope

- A viewer that drops **after the turn produced at least one chunk** reconnects and asks for the running turn's snapshot with `resume_stream`.
- A viewer that drops **before the first chunk** re-sends its pending payload instead; the backend deduplicates by `client_msg_id`, so the turn is never started twice.
- The turn itself keeps running when its last viewer disconnects. The socket release no longer cancels it.
- The protocol is snapshot-based, not ZCode's seven-event anchor protocol. The core goals — no rerun, no replay, no duplication, an authoritative text tail — are covered.

## Wire Protocol

1. The client sends `{"type": "resume_stream", "session_id": ..., "content": {}}` on the agent socket.
2. The server calls `active_turn_snapshot(session_id)`. When a turn is running, it returns the engine's `ai_text`, `has_reasoning`, and the current per-turn `seq`.
3. The server replies **on that socket only** with an `stream_resume` frame whose `status` is `ok`, `ended`, `gone`, or `unknown`.
4. `ok` carries the snapshot. `ended` means no turn is running but history exists (the client refetches). `gone` means the session was deleted or never persisted. `unknown` is the fail-open result of a read error.
5. The server never broadcasts the snapshot and never replays past frames. Live frames continue to be broadcast once, when produced.

## Client Contract

- The client flushes buffered deltas first; the snapshot already contains them.
- The turn's AI row is **replaced** with the snapshot text, never appended to.
- Every live frame carries a per-turn `seq`; the snapshot carries the `seq` it was read at. The resumed client drops frames at or below that watermark, which closes the race between the snapshot read and an in-flight frame arriving afterwards.
- `stream:resumed` is the bus event the business layer consumes. The watermark is stored in the socket frame context, not in the chat store.

## What Is Not Covered

- Reasoning deltas are not resumed; the snapshot only reports `has_reasoning`.
- Incremental tool-input JSON and per-delta usage telemetry are not part of the snapshot.
- A tool call committed before the drop is still queryable through the per-turn trajectory ledger (`GET /sessions/:session_id/trajectory`), but it is not replayed as stream events.
- The full ZCode seven-event anchor protocol is not implemented and is not planned; the snapshot protocol covers the user-visible recovery goal with less machinery.

## Code Map

- `server/service/stream_dispatch.py`: `active_turn_snapshot` and the active-turn registry.
- `server/trigger/ws/messages.py`: `_handle_resume_stream`, the per-socket reply.
- `runtime/session/ws_push.py`: `broadcast_to_session` for live frames; there is no replay log.
- `client/app/composables/bridge/agent-socket.ts`: reconnect detection and the `resume_stream` request.
- `client/app/composables/bridge/agent-socket-frames.ts`: `stream_resume` routing and the snapshot `seq` watermark.
- `client/app/composables/use-chat-stream.ts`: text replacement and the `ended` / `unknown` refetch decision.

## Tests

- `tests/server/test_stream_resume.py`: exactly-once broadcast, dead-socket pruning, snapshot only to the requester, no session crossover, and the watermark contract.
- `client/app/composables/__tests__/agent-socket-resume.test.ts`: reconnect request and `ok` / `ended` / `gone` handling.
- `client/app/composables/__tests__/agent-socket-frames.test.ts`: `stream_resume` frame routing and snapshot application.

## Failure Modes

- If the snapshot read fails, the server answers `unknown`; the client refetches history instead of silently keeping stale text.
- If the session has no active turn and no persisted history, the answer is `gone`; the client does nothing.
- If a frame stamped at or below the snapshot `seq` arrives after the snapshot, the client discards it. Duplicated delivery therefore cannot duplicate visible text.
- The snapshot is targeted at the requesting socket; other viewers of the same session continue with live frames and never see the snapshot as a replay.
