# 🔁 Runaway-Loop Prevention: Guards, Breakers & Crash Gating

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> How an always-on agent stops itself from getting stuck: per-turn pathology guards on model and tool calls, exponential-backoff breakers on background services, reliable completion delivery between subagents, process-level crash gating at boot, and REST / manual escape hatches when everything else fails.

A runaway loop is any cycle the system cannot exit on its own: a model call that repeats the same text forever, a tool call retried with the same failing arguments, a heartbeat or cron job that ticks into the void, a subagent completion that never reaches its parent (or reaches it twice), or a process that crashes and gets restarted into the same crash. This harness runs unattended (cron schedules, heartbeat wake-ups, subagent sweeps, long chat turns), so every loop must have a bound that the *system itself* enforces, not just a prompt asking the model to behave.

Two design rules run through every guard below:

1. **Degrade, never crash.** Protection never takes the process down: background services *stop*, turns *end gracefully*, and the boot gate *narrows the footprint* to HTTP-only mode.
2. **Always leave a hatch.** Every breaker has a documented manual reset (REST endpoint, state-file delete, or process restart).

**Source of truth:** `agent/middlewares/tool_guardrails.py`, `agent/middlewares/iteration_budget.py`, `agent/middlewares/output_repetition_guard.py`, `agent/stream_repetition_guard_wrapper.py`, `agent/middlewares/heartbeat_staleness.py`, `agent/middlewares/subagent_completion_drain.py`, `agent/tools/subagent/announce/delivery.py`, `agent/tools/subagent/announce/idempotency.py`, `runtime/periodic_backoff.py`, `runtime/crash_loop_breaker.py`, `skills/builtin/core/cron/scripts/base.py`, `skills/builtin/core/heartbeat/scripts/base.py`, `agent/tools/subagent/registry/sweeper.py`, `server/__main__.py`, `server/trigger/http/cron.py`, `server/trigger/__init__.py`, `server/trigger/channels/core.py`.

## 🎯 Overview & Threat Model

| Loop altitude | What it looks like | Defense |
|---|---|---|
| **Text death loop** (one model call) | Same sentence / character run streamed forever | `OutputRepetitionGuard` (worker) + `RepetitionGuardWrapper` (main-agent stream) |
| **Tool pathology loop** (one turn) | Same failing tool call, ping-pong pairs, argument churn | `ToolGuardrails`: 5 pathologies → WARN → BLOCK → HALT, with recovery mode |
| **Unbounded turn** | Model/tool calls never stop | `IterationBudget` (90 main / 60 worker combined calls) |
| **Stuck turn** | No progress for minutes (hung tool, wedged loop) | `HeartbeatStaleness` watchdog → `HeartbeatTimeoutError` |
| **Background service loop** | Heartbeat / sweeper / cron tick failing forever | `PeriodicBackoff` (exhaustion = service stops) / cron degrade → auto-disable |
| **Lost or duplicated completion** | Subagent finished, but the parent never hears about it, or hears twice | Completion drain (inject-once) + announce retry ladder + idempotency keys |
| **Crash-reboot loop** | Process crashes at boot, supervisor restarts into the same crash | `CrashLoopBreaker` → HTTP-only mode |
| **Everything else** | A breaker disabled a job or tripped the boot gate | Cron REST `failure-state` / `reset-failures`, state-file reset |

The defense is layered by altitude, so a failure that slips one layer lands in the next:

1. **Turn level**: per-turn guards bound a single conversation turn (text, tool, iteration, staleness).
2. **Recovery interlude**: a `ToolGuardrails` BLOCK gets one *managed retry window* before escalating to HALT.
3. **Background level**: recurring services back off exponentially and *stop* after 5 consecutive failures.
4. **Process level**: a crash-reboot loop trips a boot breaker that boots into a minimal HTTP-only mode.
5. **Operations**: REST endpoints and documented manual resets.

## ⚙️ Implementation & Architecture

### Turn level: `ToolGuardrails`, tool-call pathology detection

Active on the main agent, worker agents, and nudge sub-agents. Every tool call record is appended to per-turn state (`tool_guardrail_state` in `state_register_mem`) *before* evaluation, so thresholds are self-inclusive: "after 2" means the current call is the 2nd, and the 5th identical no-progress call itself reaches np=5 and blocks. Escalation ladder: ALLOW → WARN (a nudge is appended to the transcript) → BLOCK (the call is rejected with an explanatory message) → HALT (the turn ends gracefully with a terminal message, same pattern as the iteration budget).

| Pathology | Signal | WARN after | BLOCK after | hard-stop mode |
|---|---|---|---|---|
| Exact-failure repetition | Same tool + same (hashed) args keeps failing | 2 | 5 | HALT at 5 |
| Same-tool failure storm | Same tool keeps failing, args may vary | 3 | 8 | HALT at 8 |
| Idempotent no-progress | Identical (hashed) result on an idempotent tool | 2 | 5 | HALT at 5 |
| Ping-pong | Unbroken read-only A → B → A → B bouncing | 4 | 6 | HALT at 6 |
| Argument churn | Same idempotent tool cycling argument variants | 3 variants | 5 variants | HALT at 5 |

Details that matter:

- **Recovery mode** (`recovery_mode_enabled=True` by default): the first BLOCK does not brick the turn. The turn enters recovery, and the *precheck* path releases the blocked tool so the retry is evaluated fresh. Each further BLOCK increments a violation counter; once the counter exceeds `recovery_max_violations` (default 1), the action escalates to HALT. In effect: a managed retry window instead of an immediate wall. Set `recovery_mode_enabled=False` for the old strict behavior, or `hard_stop_enabled=True` to turn every BLOCK threshold into HALT (see table).
- **Ping-pong pairs** hash the two tool names of adjacent calls and accumulate only while *both* consecutive calls are successful idempotent calls (both records carry a result hash). Any error, or any successful non-idempotent (mutating) call, zeroes every accumulated pair streak. Result content is never compared: unbroken read-only bouncing is treated as a loop signal on its own. A non-idempotent tool success likewise resets argument-churn state.
- Guard state is strictly **turn-scoped**: `before_agent` resets it, so a fresh turn starts clean.

### Turn level: `IterationBudget`

Counts model calls and tool calls **combined** per turn. Main agent 90, worker agents 60 (base default 50). An exhausted model call returns a terminal AIMessage; an exhausted tool call returns an error ToolMessage so the model can wrap up instead of dying mid-loop. Internal completion-notification turns are exempt (they don't pay iterations). Counters (`iteration_budget` / `iteration_budget_used`) reset every turn.

### Turn level: text death loops, `OutputRepetitionGuard` + `RepetitionGuardWrapper`

**Cross-call detection** (`OutputRepetitionGuard`, middleware in the worker pipeline):

- Content is normalized (NFKC → strip whitespace → strip punctuation) and hashed as a dual `head|tail` MD5 over the first/last 500 chars, catching repetition at either end of long outputs. A rolling history of 30 hashes is kept per session.
- WARN at 2 consecutive identical outputs (a nudge is appended); HALT at 3 (terminal message; the halt flag is sticky for the turn). Cross-call matching needs only 1 char of content, so even a single short sentence repeated across consecutive calls is a valid death-loop signal.
- **Internal detection** per output: duplicate-segment ratio > 0.6 (segments split on punctuation/newlines, minimum 6 segments), character runs ≥ 8, and short phrases (2-10 chars) repeated ≥ 5× consecutively. Content under 20 chars is skipped to avoid false positives. Internal warnings fire at most once per label per session.
- **Reasoning is tracked independently**: `reasoning_content` / `reasoning` / `reasoning_text` kwargs and inline `<think>` / `<thinking>` / `<reasoning>` wrappers feed separate history and warned flags.
- The middleware owns exactly six session state keys (`SESSION_STATE_KEYS`), which are released when a subagent-derived agent is torn down; no cross-middleware state leaks.

**Stream layer** (`RepetitionGuardWrapper`, wraps the main agent):

- Cuts internal repetition *mid-stream*, before chunks reach the client: one warning is injected, then the rest of that call's stream is suppressed.
- HALT short-circuit: once a halt is recorded for the turn, subsequent model calls return the halt message directly.
- **Phantom-stream guard** (opt-in, enabled in production): drops pre-update model text when a fresh dict-input invocation supersedes an in-flight run.

### Turn level: `HeartbeatStaleness`, stuck-turn watchdog

Registers a 1-minute timer per turn (`timer_call_register`) that compares `heartbeat_iter` / `heartbeat_tool` counters against the last observation. Any progress resets the stale counter; no progress for **7** cycles (~7 min) on an idle agent or **20** cycles (~20 min) while inside a tool sets a killed flag, and the next agent-loop entry raises `HeartbeatTimeoutError`, ending the turn gracefully. Registered on both main and worker agents; per-turn state resets in `before_agent`, the timer is unregistered in `after_agent`.

### Pipeline level: subagent completion drain + announce retry

**Injection drain** (`agent/middlewares/subagent_completion_drain.py`): a `before_model` middleware that rehydrates and drains the session's `SteeringQueue`, injecting queued completion carriers right before the next model call. SQLite rows are marked `CONSUMED` on drain, so a checkpoint replay (HITL resume) can never re-inject the same completion. The middleware is fully fail-open: every failure is logged and swallowed, and the parent turn continues without injection. This closes the "parent waits forever for a child that already finished" loop.

**Delivery retry + idempotency** (`agent/tools/subagent/announce/delivery.py`, `idempotency.py`): busy-session completion announcements retry transient failures on a fixed ladder (5s / 10s / 20s, up to `announce_retry_max=3`; compaction errors use 1s / 2s / 4s / 8s). Permanent failures are never retried. Every delivery is keyed `subagent_announce:{run_id}:gen:{generation}` in a bounded in-memory idempotency set, so a retried announce cannot double-inject. Retries exhausted → run FAILED; soft retry cap → SUSPENDED; a run that hits `max_announce_retry_count` (10) retries, or a 24h age limit, is discarded. Together with the sweeper's orphan recovery, this bounds the delivery side of the subagent lifecycle.

### Background level: `PeriodicBackoff`, one breaker, three services

`runtime/periodic_backoff.py` is a pure state machine (no threads, no I/O):

- `record_failure()`: `consecutive_failures += 1`; `current_interval = min(base × factor^n, max_interval)`; exhausted when `consecutive_failures >= max_consecutive_failures`.
- `record_success()`: full reset. Defaults: `factor=2.0`, `max_interval=7200s`, `max_consecutive_failures=5`.

| Service | Base interval | Failure intervals | On exhaustion |
|---|---|---|---|
| Heartbeat (`skills/builtin/core/heartbeat/scripts/base.py`) | 1800s (matches `HeartbeatConfig.interval_s`) | 3600s → 7200s → 7200s → 7200s | CRITICAL log ("paused ... manual recovery required"); the loop returns, so the service stops while the process lives on |
| Subagent sweeper (`agent/tools/subagent/registry/sweeper.py`) | 60s (`sweeper_interval_seconds`) | 120s → 240s → 480s → 960s | CRITICAL log; `_running=False` ends the sweep task |
| Cron job breaker (below) | per-job, 5s base | degrade → disable | job auto-disabled |

Semantics worth knowing:

- The heartbeat records success *inside* its tick (which swallows its own errors), so only genuine tick failures count. `trigger_now()` still works after a pause: a manual poke bypasses the sleeping loop.
- `stop_sweeper()` discards the backoff object (`_backoff=None`), so a manually restarted sweep begins fresh. The backoff object is created lazily (`_get_backoff`), never at import time.
- In production the sweeper is started by `_schedule_sweeper` in `server/trigger/channels/core.py`, which hops the coroutine onto the main event loop (`run_coroutine_threadsafe`); the wiring is covered by `tests/unit/server/test_sweeper_wiring.py`.
- Backoff state lives in Python objects: restarting the process resets heartbeat and sweeper breakers.

### Background level: cron job failure breaker

Per-job state machine in `skills/builtin/core/cron/scripts/base.py` (`CronJobFailureState`, memory-only; never written to `cron_jobs.json` except the `enabled` flag):

| Consecutive failures | Effect |
|---|---|
| 1-4 | Job fails normally: status marked error, WS bell notification |
| ≥ 5 (degraded) | Triggers are skipped while inside the backoff window: `min(5000ms × 2^(n-5), 300000ms)` since the last failure |
| ≥ 10 | `enabled=False` is persisted; best-effort notification goes to the job's payload channel |

- **Record-then-re-raise:** the failure is recorded first, then the exception re-raised, so status/error reporting stays intact.
- **One-shot `at` jobs are exempt** (they can never fire twice, so a single failure is not a loop).
- Success resets the state completely. A manual `enable_job` clears it; the REST `reset-failures` endpoint re-enables only when *the breaker itself* did the disabling, so operator disables are preserved.

### Process level: `CrashLoopBreaker` + boot gating

`runtime/crash_loop_breaker.py` persists a boot journal to `src/data/boot_lifecycle.json` (keys: `boots` with `{ts, clean, reason}` entries, reason capped at 200 chars; `last_exit_clean` one-shot marker):

| Parameter | Value | Meaning |
|---|---|---|
| `TRIP_THRESHOLD` | 3 | Unclean boots needed to trip |
| `WINDOW_S` | 300 | within a 5-minute window |
| `RETENTION_S` | 3600 | Boot records pruned after 1 hour |

Boot sequence (`server/__main__.py`), in order:

1. `was_last_exit_clean()` reads the one-shot marker **before** `record_boot(clean=..., reason="startup")` consumes it.
2. `atexit.register(mark_clean_exit)`: a *graceful* shutdown marks the next boot clean. This is the self-heal; exit cleanly once, and old unclean records age out of the 5-minute window.
3. If tripped (3+ unclean boots within 5 min): set `SHERRY_HTTP_ONLY=1`, log CRITICAL, and boot into **HTTP-only mode**:
   - `init_agent_core()` still runs, so chat keeps working.
   - Curator and cron background init are skipped; `server/trigger/__init__` skips channel-manager and subagent imports, so the heartbeat service and sweeper never start either.
   - HTTP/WS routes and the cron REST API stay up.

Manual reset: delete `src/data/boot_lifecycle.json`, or simply exit cleanly once and let the window decay.

### Layering matrix: which layer catches what

| Layer | Mechanisms | What it catches |
|---|---|---|
| Middleware (in-graph, per turn) | `ToolGuardrails`, `IterationBudget`, `OutputRepetitionGuard` / `RepetitionGuardWrapper`, `HeartbeatStaleness`, `SubagentCompletionDrain` | tool pathologies, unbounded turns, text death loops, stuck turns, missing completion injections |
| Process (background services) | `PeriodicBackoff` (heartbeat, sweeper), cron failure breaker, announce retry ladder + idempotency | service retry storms, failing scheduled jobs, duplicated completion delivery |
| Boot (process lifecycle) | `CrashLoopBreaker`, `server/__main__` gating, `trigger.__init__` early exit | crash-reboot loops |
| Infra / ops | cron REST hatches, HTTP-only env, state-file delete | stuck breaker state that needs an operator exit |

## 📊 Precedence Matrix

| Guard | Altitude | State home | Reset when |
|---|---|---|---|
| `ToolGuardrails` | Turn (tool calls) | `state_register_mem` (`tool_guardrail_state`) | Every turn (`before_agent`) |
| `IterationBudget` | Turn (call count) | `state_register_mem` | Every turn |
| `OutputRepetitionGuard` | Turn + session (text) | 6 session keys | Halt flag per turn; hash history per session (released on subagent teardown) |
| `RepetitionGuardWrapper` | Stream call (text) | In-flight + halt key | Per model call |
| `HeartbeatStaleness` | Turn (wall clock) | `heartbeat_*` keys + 1-min timer | Every turn |
| `SubagentCompletionDrain` | Turn (injection) | SteeringQueue rows (SQLite) | Rows marked CONSUMED on drain |
| Announce retry + idempotency | Run (delivery) | In-memory idempotency set + run records | Success / retry cap / 24h expiry |
| `PeriodicBackoff` (heartbeat / sweeper) | Service (ticks) | Python object | Success / process restart / `stop_sweeper` |
| Cron failure breaker | Job (triggers) | In-memory `CronJobFailureState` | Success / `reset-failures` / manual `enable_job` |
| `CrashLoopBreaker` | Process (boots) | `src/data/boot_lifecycle.json` | Clean-exit decay / file delete |

Within a turn, the turn guards are orthogonal and fire in parallel: `OutputRepetitionGuard` / `RepetitionGuardWrapper` guard the *text*, `ToolGuardrails` the *tool calls*, `IterationBudget` the *count*, and `HeartbeatStaleness` the *wall clock*. Whichever trips first ends the turn; none blocks the others. If all of them miss, the background breakers bound the *next* trigger, and the boot breaker bounds the *next* process.

## 🛠️ Configuration & Usage

- **All thresholds are code defaults** (dataclass / constructor parameters); there are intentionally no env vars for them. Notably, `config/schema.py`'s `max_tool_iterations = 40` is *not* consumed by the middleware (budgets are passed explicitly: 90 / 60), and `HeartbeatConfig.interval_s = 1800` matches the heartbeat service default but the service is constructed with defaults.
- `TOOL_CALL_TIMEOUT_MINUTES` (default 5 in `.env.example`) is currently **documentation-only**: no code consumes it. The active per-tool bounds are constants (web search 15s, terminal 30s, python REPL 30s). Do not rely on it as a loop bound.
- Workers get `OutputRepetitionGuard` as middleware; the main agent is wrapped by `RepetitionGuardWrapper` (middleware hooks do not see raw stream chunks).
- `ToolGuardrails` knobs: `warnings_enabled` (default True), `hard_stop_enabled` (default False, BLOCK stays a block), `recovery_mode_enabled` (default True), `recovery_max_violations` (default 1).
- Announce delivery knobs (subagent announce config): `announce_retry_max=3` with 5s / 10s / 20s transient delays, plus `max_announce_retry_count=10` and a 24h run expiry.

Manual recovery cheatsheet:

| Situation | Action |
|---|---|
| Cron job auto-disabled by the breaker | `POST /cron/reset-failures {"id": ...}` (re-enables only breaker-disabled jobs) |
| Inspect a cron job's breaker state | `POST /cron/failure-state {"id": ...}` (unknown job → 404, never-failed job → zeroed state) |
| Heartbeat paused (5 failed ticks) | Restart the process; `trigger_now` still fires one-shot ticks |
| Sweeper stopped (backoff exhausted) | Restart the process; a fresh sweep starts with fresh backoff |
| Boot gate tripped (HTTP-only) | Exit cleanly once, or delete `src/data/boot_lifecycle.json` |

## 🧪 Testing

| Suite | Covers |
|---|---|
| `tests/unit/middlewares/test_tool_guardrails.py` | Pathology detection, escalation ladder, recovery mode |
| `tests/unit/runtime/test_periodic_backoff.py` | Interval math, exhaustion, success reset |
| `tests/unit/runtime/test_crash_loop_breaker.py` | Trip window / retention, clean marker, corrupt state |
| `tests/unit/cron/test_cron_failure_breaker.py` | Degrade → disable, reset semantics |
| `tests/unit/heartbeat/test_heartbeat_backoff.py` | Service backoff wiring, exhaustion pause |
| `tests/unit/subagent/test_sweeper_backoff.py` | Sweeper backoff wiring, loop stop |
| `tests/unit/server/test_sweeper_wiring.py` | Sweeper startup wiring |
| `tests/unit/server/test_crash_gating.py` | Boot gating, HTTP-only mode |
| `tests/unit/server/test_cron_api.py` | Cron REST incl. failure-state / reset-failures |

## ⚠️ Honesty & Limitations

- **Turn guards are turn-scoped by design**: a fresh turn starts with fresh guard state. Cross-turn repetition is `OutputRepetitionGuard`'s domain (session-scoped history), not the tool guardrails'.
- **In-memory breaker state does not survive restarts**: guardrail/repetition/iteration state is turn- or session-scoped anyway; the cron failure counters are lost on process restart (though the persisted `enabled` flag is not); heartbeat/sweeper backoff lives in Python objects. A restart is therefore always a reset, sometimes a too-generous one.
- **Exhaustion stops services until restart**: a paused heartbeat or stopped sweeper has no runtime re-arm API ("manual recovery required" is literal). The process itself keeps serving.
- **The crash gate is window-based**: a crash loop spaced more than 5 minutes apart never trips it, and any supervisor that deletes the state file resets it. The file is both the breaker's memory and the manual escape hatch.
- **`hard_stop_enabled` defaults to False**: in strict mode, only same-tool failures and hard-stop-converted BLOCKs reach HALT; other pathologies stop at BLOCK (subject to recovery mode).
- **Content normalization cuts both ways**: stripping whitespace/punctuation makes hashing resilient to formatting noise, but a model that *paraphrases* its loop each time evades hash-based detection. Internal segment/run detectors partially cover this; fully paraphrased loops are out of scope.
- **Recovery mode gives the model room to fail**: a stubborn pathology costs one managed retry before HALT. Operators who want the immediate wall should set `recovery_mode_enabled=False`.
- **`TOOL_CALL_TIMEOUT_MINUTES` is declared but unread**: it exists in `.env.example` (and is described in the root README), but no code consumes it today; the per-tool constants listed above are the real bounds.
- **HTTP-only mode is a reduced footprint, not a lock-down**: chat, HTTP/WS routes, and cron REST stay up by design; the goal is breaking the *crash loop*, not air-gapping the process.

