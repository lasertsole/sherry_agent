# 🔁 Stream Recovery

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> This page documents the mechanism, not an API contract: which state is authoritative, how a reconnect finds it, and why the design uses a snapshot watermark instead of replaying deltas.

## The Design Problem

A turn is a long-lived async generator. Its output is pushed to every live socket as it is produced; there is no per-client queue and no replay log. A viewer can disappear at any chunk, and the turn must keep running because it may have already performed tool calls and spent tokens.

The requirements pull in opposite directions:

- A reconnect must not rerun the turn: that would duplicate side effects and cost.
- A reconnect must not replay frames: another viewer may have received them, and a replay would duplicate visible text.
- A reconnect must not require the server to remember every delta for every viewer: that is unbounded state.
- Multiple viewers of one session must converge on the same text without sharing a replay cursor.

The answer is a **point-in-time snapshot plus a per-turn sequence watermark**, delivered together with the persisted history in one request.

## The Authoritative State

`StreamTurn` owns the authoritative text of the running turn. Visible model text is appended to `self.ai_text` as it passes the think scrubber; reasoning is tracked separately by `_has_reasoning`. Tool calls are not part of `ai_text`; they are recorded in the trajectory ledger.

The engine registers itself in `_ACTIVE_TURNS[session_id]` when `_run_frames` starts and removes itself in the generator's `finally`, guarded by an identity check so an older turn cannot remove a newer one. At most one turn per session is active because the turn runner/queue serializes turns.

`StreamTurn` captures its `turn_id` from the surrounding turn scope. `active_turn_snapshot()` reads four fields from that engine:

- `ai_text`: the visible text produced so far;
- `_has_reasoning`: whether a reasoning channel exists;
- `_frame_seq`: the highest frame sequence stamped so far;
- `turn_id`: the identity of the turn the text belongs to.

The read runs on the same event loop as the stream generator, so the text and the sequence number are read at one consistent cut; no lock is needed. If no engine is registered, the session has no running turn and the snapshot function returns `None`. The HTTP history path adds `turn_num` (the next persisted turn number) as display metadata.

## Frame Sequencing and the Watermark

Every frame emitted by `StreamTurn.run()` is stamped with a per-turn monotonic `_frame_seq` before it is yielded:

```text
_run_frames() produces a frame
  → run() increments _frame_seq
  → frame["seq"] = _frame_seq
  → frame["turn_id"] = self.turn_id
  → the frame is broadcast
```

`StreamDriver` also stamps `turn_id` on `turn_started`, `done`, `hitl_request`, `error` and `stopped` (the frames it builds outside the chunk loop). A reconnecting client can therefore route every frame to the turn it belongs to, even after a turn switch.

The watermark is **per turn**. A snapshot carries the `seq` it was read at and its `turn_id`; the client drops a frame only when the frame's `turn_id` matches and its `seq <= snapshot.seq`. A NEW turn restarts at `seq = 1`, so it is never mistaken for old data merely because its sequence is numerically lower than another turn's watermark.

## One Round Trip on Reconnect

The client no longer sends `resume_stream` on reconnect. It sends one history request:

```text
GET /get_history_by_turn_page?session_id=…&include_active_turn=1
  → {messages: [...persisted rows...],
     active_turn: {text, has_reasoning, seq, turn_id, turn_num} | null}
```

Without `include_active_turn=1` the endpoint returns the plain row array, so every existing caller keeps its contract. With the flag, the persisted history and the running turn's snapshot arrive together: a turn that completed while the socket was down is in `messages`, and the still-running turn is in `active_turn`.

The WS handler `_handle_resume_stream` and its `stream_resume` reply remain as a legacy fallback for older clients; the current client never sends that frame.

## The Client Snapshot Queue

While the history request is in flight, the socket holds every inbound WS frame instead of applying it (`holdFrames`, bounded at 1000 frames — the oldest is dropped). When the response arrives:

1. the persisted messages are merged into the chat list (the completed turns missed while disconnected become visible immediately);
2. `active_turn` is applied to the row for its turn: by `turn_id` when a row already carries it, else by `turn_num`, else a new AI row is created;
3. the socket records the snapshot watermark (its `seq` and `turn_id`);
4. held frames are released in arrival order. A frame at or below the watermark for the same turn is dropped; every other frame is applied normally, routed by its own `turn_id`.

A late/lost request cannot wedge the UI: the wait is bounded at 10 s. On timeout the response is gated out of the chat merge, the queue is released without a watermark, and the socket's terminal frames or the busy watchdog settle the turn. A pre-chunk send is never settled by an idle snapshot because it is resumable; only a send that already received a chunk can be concluded as ended-while-disconnected.

## Lifecycle Across Disconnect

`_release_socket` unregisters the socket and may clear pending tool arguments, but it does **not** cancel the turn. The turn task stays in `_active_tasks` until it finishes; `_ACTIVE_TURNS` is removed by the generator's own `finally`, not by the disconnect path. A turn therefore survives the last viewer leaving.

Because frames are broadcast exactly once and never replayed, the reconnecting client gets the authoritative text from the snapshot and only frames produced after registration; the combination is gap-free without a replay log.

## What the Snapshot Omits

- Reasoning deltas: the snapshot reports `has_reasoning`, not the reasoning text.
- Incremental tool-input JSON and per-delta usage telemetry.
- Tool-call cards as stream events; a committed tool call remains queryable in the trajectory ledger (`GET /sessions/:session_id/trajectory`).

This division is deliberate. Text is the only state whose partial rendering cannot be reconstructed from a durable record. Tool calls already have a durable ledger, so replaying their stream frames would duplicate a record that already exists.

## Failure Semantics

- A stale frame arriving after the snapshot is discarded by the per-turn watermark; it can never duplicate visible text.
- A missing or timed-out history response releases held frames without a watermark; earlier frames are applied normally and the watchdog remains the last resort.
- A turn that ended while the socket was down is present in the history rows; a mid-stream send is settled when the snapshot reports no active turn. A pre-chunk send is re-sent (the backend dedupes on `msg_id`) and stays pending until its turn starts.
- The per-socket targeting preserves multi-viewer independence: one viewer's reconnect cannot alter another viewer's cursor or stream.

## Rejected Alternatives

- **Rerun the turn on reconnect.** Duplicates tool side effects and token spend, and can change the answer.
- **Replay all deltas.** Requires per-viewer history, a cursor, and retention; the exact race the `seq` watermark solves is reintroduced.
- **ZCode's seven-event anchor protocol.** It gives finer-grained reasoning/tool-input recovery, but needs a frontend protocol and a durable anchor/tail-discard pipeline. Sherry's snapshot protocol meets the user-visible recovery goal with less machinery; the finer-grained event types remain unimplemented by design.

## Trajectory Panel

The ledger also has a UI consumer: a SESSION-scoped right-sidebar tab
(`client/app/pages/home/components/TrajectoryPanel.vue`) reads
`GET /sessions/:session_id/trajectory` and renders one timeline per turn. It is
a debug view, not part of the reconnect protocol: opening it fetches the newest
two 500-row pages once (1000 events, then a truncation note), and refresh is a
button rather than a live subscription.

Rows are grouped by `turn_id` and re-sorted by `(ts, seq, event_id)`, because
`read()` orders by `seq` and every lifecycle event (turn_start / turn_end /
compact / model_retry / invalid_tool_call) uses `seq=0`; without the re-sort
they would all sit before the tool rows and `turn_end` would appear first. Tool
rows show name/status/duration plus expandable args/result JSON; lifecycle rows
show a kind badge and expandable payload. The header offers filter-style search
with an n/m counter, two folding switches (tool rows default collapsed),
expand/collapse-all, a per-turn filter and refresh. A tool card's history button
opens the same single tab via `ChatBox.vue`.

The pure helpers (`groupTurns`, `summarizeTurns`, `formatDuration`,
`previewOf`, `eventMatches`) live in `client/app/composables/use-trajectory.ts`;
the bridge call is `fetchTrajectory` in `bridge/session.ts`.

## Code Map

- `server/service/stream_dispatch.py`: `StreamTurn` (`ai_text`, `_frame_seq`, `turn_id`, `_ACTIVE_TURNS`) and `active_turn_snapshot`.
- `server/service/stream_driver.py`: `turn_id` on every driver-built frame.
- `server/trigger/http/messages.py`: `include_active_turn` on `GET /get_history_by_turn_page`.
- `server/trigger/ws/messages.py`: the legacy `_handle_resume_stream` path and `_release_socket`.
- `runtime/session/ws_push.py`: exactly-once broadcast with no replay log.
- `client/app/composables/messages.ts`: `get_history_page_with_active_turn`.
- `client/app/composables/use-session-lifecycle.ts`: history merge returning the snapshot.
- `client/app/composables/use-chat-stream.ts`: `syncHistory`, snapshot application and turn routing.
- `client/app/composables/bridge/agent-socket.ts`: frame hold/release and the snapshot watermark.
- `client/app/composables/bridge/agent-socket-frames.ts`: per-turn watermark filtering.
- `tests/server/test_stream_resume.py`, `tests/server/service/test_stream_turn_id.py`, `tests/server/trigger/test_history_active_turn.py`, `client/app/composables/__tests__/agent-socket-resume.test.ts`: the invariants above.
