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

## Durable Execution: What Survives a Crash

Sherry's durability model is **"state persists + a sweeper backstops + a new turn re-drives"** — not Temporal-style activity replay. A crash cannot be replayed; what can be guaranteed is that no *state* lies about what happened, so recovery has something true to act on. A 2026-10 audit of all five subsystems (HITL, checkpointer, TaskFlow, subagent registry, evidence ledger) fixed the first batch:

| Defect | Fix |
| :--- | :--- |
| The subagent registry's terminal predicate compared STRING literals (`COMPLETED`, `FAILED`, …) against an enum whose values are `pending / running / interrupted / terminal` — nothing matched, so the retention cap never evicted and memory grew without bound | Compare against `ExecutionStatus.TERMINAL`; the cap now keeps the live runs and evicts old terminal ones |
| The session-directory sweep probed checkpoints with the raw session id while the checkpointer stores `session_thread_id(session_id)` (an INTEGER) — a folder whose only remaining record was its checkpoint looked empty and was deleted | ONE `session_thread_id()` mapping in `pub.func`, used by the sweep |
| The trajectory store documented "pruned on write" while `prune_session` had no caller; its ordering also used `MAX(seq)`, a column the hook events always wrote as 0, so it kept an arbitrary set of turns | Wired on write, debounced to once per minute per session, ordered by insertion (rowid) |
| The global keep-latest checkpoint prune ran inside `built_agent()` — i.e. EVERY turn — on a bare connection with no `busy_timeout`, and computed its keep-set before deleting (a concurrent commit in between lost the newest checkpoint) | Runs once at boot (`server/service/checkpoint_maintenance.py`) inside `BEGIN IMMEDIATE`, with its keep-set biased to the top-level row |
| Every checkpointer connection lacked `busy_timeout` (a per-connection PRAGMA, unlike WAL) | One helper (`agent/checkpointer/pragmas.py`, 30 s) for the main, prune and purge connections |
| `delete_thread_history` had no guard, so a locked DB aborted the purge AFTER the message store was already emptied — the worst half of a half-delete | Guards every step; the checkpoint delete now runs FIRST, so a retry can always finish the job |
| A terminal run's delivery state reached disk only through the 30 s periodic flush; a crash in that window left SQLite saying TERMINAL+PENDING, a state with no retry path — the completed result was lost | Delivery transitions (`delivered / failed / suspended / in_progress`) persist immediately (`set_run_durable`) |
| The auto-turn driver inherited a no-op `apply_hitl_pending`, so a headless turn that interrupted left the graph suspended while the session looked idle: the turn-end prune could delete that checkpoint and the drain could inject a new message into the suspended graph | The driver sets the flag; `submit_user_input` also treats a checkpoint-held interrupt as busy (fail-open probe), which is the case a restart leaves behind |

The SECOND batch closed the remaining audit items, and three of them share one insight: a restart invalidates more than the data, it invalidates the *clock* and the *memory-only flags*.

- **Boot reconciliation** (`server/service/boot_recovery.py`, one capped pass over session directories that hold a checkpoint): a session whose graph is still suspended gets its HITL-pending flag rebuilt (the register is memory-only, so the server otherwise reports IDLE and the next message is dispatched as an ordinary turn instead of `Command(resume=...)`); a transcript whose last assistant turn carries tool calls with no results is healed with the error placeholders plus the interrupt marker (one commit, deterministic id); a `CLAIMED` queue row — claimed by a turn that can never finish — goes back to `QUEUED` instead of holding its slot until the 24-hour expiry. Every step is fail-open and the scan is capped, so a broken session costs its own probe.
- **A restored RUNNING run is not alive.** No task survives a restart, so it is terminalized at load (`pause_reason=process_restart`, outcome TIMEOUT); leaving it RUNNING made the WAITING-flow sweep believe the child was alive and orphan recovery wait out a staleness threshold that nothing would ever cross. Its delivery stays PENDING and the sweeper's undelivered scan announces it, which is how the owning step learns instead of staying DISPATCHED.
- **Every persisted timestamp is rebased at load** (`_rebase_restored_clock`). `time.monotonic()` is process-relative: a value from the previous process can read as hours old or as a future time, so each `now - timestamp` expiry check silently stopped working. Deadlines (`archive_at`) get a fresh window rather than being rebased to "now", which would archive on the first sweep.
- **Interrupting tools refuse on a headless turn.** The approval gates already auto-denied for a turn with no operator, but the tools that call `interrupt()` themselves (`question`, `exit_plan_mode`, the self-file approval, the generic `interrupt_on` strategy, the sandbox bypass) suspended anyway — a turn interrupted with nobody able to answer. They now deny with a message that tells the model to proceed (or stop), never to retry.
- **The trajectory timeline is real.** Hook events and frame projections now share ONE per-turn sequence (hooks used to pass `seq=0`, so every hook event sorted before the turn's first frame), timestamps carry milliseconds, and a failed stream records an `error` event before re-raising — a timeline that simply stops reads as "still running".


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

## Resuming an Interrupted Answer

A turn can end mid-answer for two reasons: the user pressed Stop, or the
heartbeat idle timeout fired. Both already write an `[interrupted] {partial}`
marker into the checkpoint and one `[interrupted:<reason>]` row into MesMemory.
What an interrupted turn can additionally be is **resumable**.

The cancel path records `StateKey.RESUMABLE_BREAKPOINT` (mem + DB dual-write,
so a restart keeps it) with the marker id, the partial text and the turn count
the marker id was derived from. It does so only for a PURE-TEXT interruption:
a fresh marker write, no healed tool calls (a dangling tool call would need a
rerun/skip decision), and something to continue from (visible text or
reasoning). `stopped` and `done` frames carry an additive `resumable` boolean;
`done` carries it because a heartbeat timeout ends the turn normally.

The client's **Continue generating** button sends `continue_generation`. The server validates
the breakpoint against the live transcript (marker still trailing, turn count
unchanged), refuses with `stale`/`busy` otherwise, consumes it (one resume per
interruption), rewrites the marker to the clean partial text and replays the
ordinary max-tokens continuation: the model sees its own unfinished answer plus
a continuation prompt and finishes it. The continuation is a new turn; the
prompt is tagged `provenance: continuation` so message persistence never
renders it as a user row. A new turn (user message, HITL resume, auto turn)
clears the breakpoint, and a cancelled continuation records its own.

The same turn-boundary machinery powers **editing a sent user message**: the
edited turn and everything after it are removed from the live checkpoint
(`RemoveMessage`, since only the newest checkpoint is retained) and hard-deleted
from MesMemory, then the edited text is re-run as a new turn that reuses the
old turn number. The client drops the corresponding Dexie cache rows too, or
the next history merge would resurrect them.

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
