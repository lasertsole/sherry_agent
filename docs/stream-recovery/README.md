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

The answer is not an event log. It is a **point-in-time snapshot plus a sequence watermark**.

## The Authoritative State

`StreamTurn` owns the authoritative text of the running turn. Visible model text is appended to `self.ai_text` as it passes the think scrubber; reasoning is tracked separately by `_has_reasoning`. Tool calls are not part of `ai_text`; they are recorded in the trajectory ledger.

The engine registers itself in `_ACTIVE_TURNS[session_id]` when `_run_frames` starts and removes itself in the generator's `finally`, guarded by an identity check so an older turn cannot remove a newer one. At most one turn per session is active because the turn runner/queue serializes turns.

`active_turn_snapshot()` reads three fields from that engine:

- `ai_text`: the visible text produced so far;
- `_has_reasoning`: whether a reasoning channel exists;
- `_frame_seq`: the highest frame sequence stamped so far.

The read runs on the same event loop as the stream generator, so the text and the sequence number are read at one consistent cut; no lock is needed. If no engine is registered, the session has no running turn and the snapshot function returns `None`.

## Frame Sequencing and the Watermark

Every frame emitted by `StreamTurn.run()` is stamped with a per-turn monotonic `_frame_seq` before it is yielded:

```text
_run_frames() produces a frame
  → run() increments _frame_seq
  → frame["seq"] = _frame_seq
  → the frame is broadcast
```

The snapshot carries the `_frame_seq` value it was read at. That number is the watermark:

- Any frame with `seq <= snapshot.seq` was already reflected in `ai_text` at the snapshot cut.
- Such a frame may still be in flight to the reconnecting socket, or delivered after the snapshot reply.
- The resumed client therefore drops every frame at or below the watermark and replaces the turn's AI row with the snapshot text.

This is a linearization argument. The snapshot is a cut through the stream; `seq` identifies the cut; the replacement text is the state at that cut. The client never needs to know which individual deltas the snapshot already contains.

## Lifecycle Across Disconnect

`_release_socket` unregisters the socket and may clear pending tool arguments, but it does **not** cancel the turn. The turn task stays in `_active_tasks` until it finishes; `_ACTIVE_TURNS` is removed by the generator's own `finally`, not by the disconnect path. A turn therefore survives the last viewer leaving.

On reconnect, the client asks `_handle_resume_stream`. The handler reads the active engine:

- an engine exists → `ok` with the snapshot;
- no engine, but the session has persisted messages → `ended` (the client refetches history);
- no engine and no history → `gone`;
- a read failure → `unknown`, and the client refetches rather than silently keeping stale text.

The reply is sent to the requesting socket only. Other viewers keep receiving live frames; the snapshot is not a broadcast and never becomes a replay source.

## Why There Is No Replay Log

A replay log would have to store every delta for every turn, retain it for every viewer, and maintain a per-viewer cursor. It would also need a retention policy, backpressure, and a way to distinguish "already delivered" from "in flight" — the same watermark problem, but with a log attached.

The snapshot approach has bounded state: one text accumulator and one counter per running turn. It is also naturally idempotent: replacing the AI row with the snapshot yields the same result whether the client missed one frame or a thousand. Reconnect cost is O(current text), not O(history).

The tradeoff is explicit: the snapshot restores the **visible text tail**, not every intermediate event.

## What the Snapshot Omits

- Reasoning deltas: the snapshot reports `has_reasoning`, not the reasoning text.
- Incremental tool-input JSON and per-delta usage telemetry.
- Tool-call cards as stream events; a committed tool call remains queryable in the trajectory ledger (`GET /sessions/:session_id/trajectory`).

This division is deliberate. Text is the only state whose partial rendering cannot be reconstructed from a durable record. Tool calls already have a durable ledger, so replaying their stream frames would duplicate a record that already exists.

## Failure Semantics

- A stale frame arriving after the snapshot is discarded by the watermark; it can never duplicate visible text.
- `unknown` and `ended` both lead to a history refetch; `gone` leads to no action.
- A snapshot read is consistent because it runs on the stream's event loop; a frame is either included in `ai_text` and at or below the watermark, or it is newer and survives the filter.
- The per-socket targeting preserves multi-viewer independence: one viewer's reconnect cannot alter another viewer's cursor or stream.

## Rejected Alternatives

- **Rerun the turn on reconnect.** Duplicates tool side effects and token spend, and can change the answer.
- **Replay all deltas.** Requires per-viewer history, a cursor, and retention; the exact race the `seq` watermark solves is reintroduced.
- **ZCode's seven-event anchor protocol.** It gives finer-grained reasoning/tool-input recovery, but needs a frontend protocol and a durable anchor/tail-discard pipeline. Sherry's snapshot protocol meets the user-visible recovery goal with less machinery; the finer-grained event types remain unimplemented by design.

## Code Map

- `server/service/stream_dispatch.py`: `StreamTurn` (`ai_text`, `_frame_seq`, `_ACTIVE_TURNS`) and `active_turn_snapshot`.
- `server/trigger/ws/messages.py`: `_handle_resume_stream` and `_release_socket`.
- `runtime/session/ws_push.py`: exactly-once broadcast with no replay log.
- `client/app/composables/bridge/agent-socket.ts`: reconnect detection and `resume_stream`.
- `client/app/composables/bridge/agent-socket-frames.ts`: snapshot `seq` watermark and frame filtering.
- `client/app/composables/use-chat-stream.ts`: snapshot replacement and history refetch.
- `tests/server/test_stream_resume.py`, `client/app/composables/__tests__/agent-socket-resume.test.ts`: the invariants above.
