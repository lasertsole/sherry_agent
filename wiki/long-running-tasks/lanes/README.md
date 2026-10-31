# 🚦 Concurrency Lanes — MAIN · SUBAGENT · NUDGE · NESTED

**English** · [中文](README.zh.md) · [한국어](README.ko.md) · [日本語](README.ja.md)

> Part of [Long-Running Tasks](../README.md): the process-level concurrency lanes (MAIN, SUBAGENT, NUDGE, NESTED) that gate child agents, background nudges, and nested reply turns.

---

## 🚦 Concurrency Lanes

Long-running work does not fan out unbounded: every detached child agent, every background nudge and every nested `sessions.send` reply turn passes through a **process-level lane** first. A lane is an `asyncio.Semaphore` plus `active`/`queued` counters (`runtime/lane/core.py`); over-limit work **queues FIFO** instead of being rejected.

| Lane | Constrains | Default | Config key |
| :--- | :--- | :--- | :--- |
| `MAIN` | main-agent turn (`server/service/input_queue_service.py::_run_executor`) | `min(16, max(8, CPU))`, clamped up to `SUBAGENT + NUDGE` → 12–16 | `LANE_SYSTEM["main_max_concurrent"]` |
| `SUBAGENT` | child-agent execution (`spawn/core.py`, `control/steer.py`) | `8` | `LANE_SYSTEM["subagent_max_concurrent"]` |
| `NUDGE` | memory nudge / plan extraction / compaction todo update (`agent/middlewares/summarization/nudges.py`, 3 sites) | `4` | `LANE_SYSTEM["nudge_max_concurrent"]` |
| `NESTED` | `sessions_send` reply turns (serialized) | `1` | `LANE_SYSTEM["nested_max_concurrent"]` |

### Configuration & validation

Lane limits live in `config/features/infra_side/lane_system.py`:

```python
LANE_SYSTEM: LaneSystemConfig = {
    "main_max_concurrent": _resolve_main_concurrency(),   # 12–16
    "subagent_max_concurrent": 8,
    "nudge_max_concurrent": 4,
    "nested_max_concurrent": 1,
    "lane_wait_warn_ms": 5000,
    "lane_drain_timeout_seconds": 30.0,
}
```

`_resolve_main_concurrency()` mirrors OpenClaw's CPU scaling — `min(16, max(8, CPU))` — then clamps **up** to the hard invariant `SUBAGENT + NUDGE` (8 + 4 = 12), so the shipped default is valid on any CPU count. `validate_lane_config()` is called once at server startup (never at import) by `install_lane_lifecycle()` (`server/service/lane_lifecycle.py`); it raises when `main_max_concurrent < subagent + nudge` or when any lane limit is `< 1`. `lane_wait_warn_ms` controls the "waited too long for a slot" warning and `lane_drain_timeout_seconds` is the default for `LaneManager.drain_all()`. `LaneManager.set_concurrency(lane, n)` hot-updates a limit — in-flight slots keep their permits, only new acquires see the new limit.

### Environment gate in front of the SUBAGENT lane

A lane is **counting** backpressure — it answers "are all 8 slots busy?". It
cannot answer "is the machine still able to carry one more", so a dispatch at
99% CPU used to enter the lane anyway and make everything slower. The load gate
(`agent/tools/pub_base/load_gate.py`) adds that missing half. The two are
orthogonal and ordered: the gate runs BEFORE `lane_slot(SUBAGENT)`, so a
deferred dispatch holds **no** slot while it waits.

| Host load | Behavior |
| :--- | :--- |
| both < warn | proceed |
| either ≥ warn (90%) and < stop | proceed — the lane cap already throttles here; the gate only logs |
| either ≥ stop (95%) | **defer**: the run stays PENDING, no slot is taken, one `load_warning` frame tells the session, and the dispatch is re-checked after `load_gate_retry_s` |

```text
spawn ─► load gate (defer while stop) ─► lane_slot(SUBAGENT) ─► PENDING→RUNNING ─► execute
              │ stop & budget spent
              └─► terminalize (timeout / "load_timeout")   ← never a bare return
```

**The deferral is bounded, and the bound terminalizes.** Leaving a deferred run
PENDING would create an orphan — and orphan recovery re-dispatches it, which
defers again: a loop. Past `load_gate_max_defer_s` (600 s) the run is finalized
with outcome `TIMEOUT` and an error naming `load_timeout` (`ended_reason` stays
the enum's `timeout`; the load-specific detail rides the error text). A kill
during the wait is observed through `kill_reconciliation` (a killed PENDING run
is NOT marked TERMINAL) and ends the wait immediately.

**The snapshot is shared and the sampling never blocks the loop.** One sample
costs three CPU intervals (~4.5 s at the default 1.5 s), so it runs in
`asyncio.to_thread` and at most once per `load_gate_snapshot_ttl_s` (5 s) for
the whole process: concurrent waiters read the same verdict, and a lock with a
double-check makes N callers trigger exactly one sample.

**Fail-open, but only for the monitor.** A psutil failure (missing library, a
backend without `/proc`) returns an ok verdict so dispatch never stalls; a
working sample's `stop` is always honoured.

**Shipped OFF.** `load_gate_enabled` defaults to `False`, so the first release
behaves exactly as before. Enable it after confirming psutil made it into the
frozen backend — the desktop workflow's "Verify psutil made it into the frozen
tree" step exists for that, because the import is lazy and the freezer cannot
see it (`HIDDEN_IMPORTS` in `packaging/build_backend.py`).

### Queueing semantics: `PENDING`

Global concurrency is not a rejection counter. `spawn_subagent_direct` still enforces per-parent admission (`validate_spawn_depth`, `validate_concurrent_children`), but a spawn that exceeds the global limit is **accepted** and registered as `ExecutionStatus.PENDING`: `started_at` stays `None` and the run holds no lane slot. The SUBAGENT lane wrapper (`_execute_subagent_with_lane`, `agent/tools/subagent/spawn/core.py`) waits for a slot, then promotes `PENDING → RUNNING` via `mark_run_running()`, stamping `started_at` only at that moment — queue wait is never counted as run time. `validate_global_concurrent()` and `SubagentConfig.max_concurrent` are retained for backward compatibility and are not called by the spawn pipeline.

Because a queued child still occupies an admission slot, the registry counting functions count `RUNNING + PENDING` as active (`count_active_runs_for_session`, `count_active_descendant_runs`, `count_all_active_runs`), and `is_live_unended_run()` includes `PENDING`.

### PENDING lifecycle

| Path | Behavior |
| :--- | :--- |
| **Kill** | PENDING runs are listable/killable (`list_killable_children`); `cancel_task()` cancels the lane waiter and `CancelledError` propagates out of `Lane.acquire()` without consuming or leaking a permit |
| **Steer** | Rejected — `steer_subagent_run()` only accepts RUNNING/INTERRUPTED; a steered restart re-enters the lane as PENDING and is promoted inside its slot |
| **Sweeper / orphan recovery** | `is_live_unended_run()` includes PENDING, so a PENDING run whose task is gone is an orphan; `evaluate_recovery_gate()` classifies it `"wedged"` and `_recovery_loop()` finalizes it directly as `TERMINAL` with `ended_reason="pending_orphaned"` (outcome `TIMEOUT`, error `"pending orphaned"`) and runs the announce flow |
| **Restart / restore** | `restore_runs_from_disk()` (`registry/state.py`) finalizes every PENDING run restored from SQLite as `TERMINAL` / `pending_orphaned` at startup — silently, without the announce flow (parent sessions belong to the previous process lifetime); only a same-lifetime lost task reaches the sweeper's orphan recovery |
| **Yield** | `sessions_yield` counts PENDING children as active and `wake_yield_if_all_children_settled` only wakes the parent once none remain; the yield timeout covers the lane wait and returns normally on expiry |
| **Accounting / listing** | `control/list.py` and `runtime_tools.py` render PENDING children alongside RUNNING/INTERRUPTED |

A sweeper scan skips a PENDING run while its lane task still exists (the process is simply queueing); only a lost task makes it an orphan — and PENDING leftovers from a previous process never reach it, because restore finalizes them at startup.

### Drain mode & shutdown

`Lane.acquire()` consults a drain-check callback before waiting: while the subagent gateway reports draining, acquirers are refused with `RuntimeError` **without consuming a permit**. The callback is injected with `set_drain_check(is_gateway_draining)` at startup (`install_lane_lifecycle`), keeping `runtime/lane` free of upward imports.

At exit, the same seam flips drain mode (`set_draining(True)`) and runs a **bounded** drain via `atexit` — `asyncio.run(drain_all_lanes(timeout=0))` reports the final per-lane counters and never delays process exit. The repo has no async shutdown path (Robyn's `shutdown_handler` is never invoked on SIGINT/SIGTERM), so in-flight turns are abandoned to the OS rather than awaited.

### Observability

`GET /lane-status` (`server/trigger/http/lane.py`) returns the live snapshot for all four lanes:

```json
{"main": {"name": "main", "max_concurrent": 12, "active": 1, "queued": 0},
 "subagent": {"name": "subagent", "max_concurrent": 8, "active": 3, "queued": 2},
 "nudge": {"name": "nudge", "max_concurrent": 4, "active": 0, "queued": 0},
 "nested": {"name": "nested", "max_concurrent": 1, "active": 0, "queued": 0}}
```

### Deadlock protection

| Scenario | Mechanism |
| :--- | :--- |
| Main turn waits on a child result while holding a MAIN slot | The child only needs a SUBAGENT slot — the lanes are independent semaphores, so no cross-lane wait cycle exists (`test_filling_main_does_not_block_subagent`; spawn path: `test_subagent_runs_while_main_lane_slot_is_held`); an optional `run_timeout_seconds > 0` additionally bounds a hung child |
| Nudge waits for a SUBAGENT slot | Nudges do not spawn subagents (restricted toolset), and the NUDGE lane is independent anyway |
| Lane hot-update during in-flight work | `set_concurrency` only affects new acquires |
| `sessions_yield` on a PENDING child | The yield timeout includes lane wait time and returns normally on expiry |
| Drain mode + lane-waiting tasks | `acquire()` refuses via the drain check without consuming a permit |
| Kill a PENDING run | `CancelledError` exits `Lane.acquire()` without releasing a permit |
| Steer a PENDING run | Rejected by the status check; only RUNNING/INTERRUPTED are steerable |

### Lane file map

| File | Role |
| :--- | :--- |
| `config/features/infra_side/lane_system.py` | `LaneSystemConfig` / `LANE_SYSTEM` + `validate_lane_config()` |
| `runtime/lane/core.py` | `LaneType`, `Lane`, `LaneManager`, `get_lane_manager()`, `lane_slot()`, `set_drain_check()` |
| `server/service/lane_lifecycle.py` | Startup validation, drain-gate registration, bounded exit drain |
| `server/trigger/http/lane.py` | `GET /lane-status` |
| `agent/tools/subagent/spawn/core.py` · `control/steer.py` | SUBAGENT lane wrappers + PENDING → RUNNING promotion |
| `agent/middlewares/summarization/nudges.py` | 3 NUDGE lane call sites |
| `agent/tools/subagent/tools/sessions_send.py` | NESTED lane around the reply turn |
| `server/service/input_queue_service.py` | MAIN lane around `_run_executor` |
| `agent/tools/subagent/orphan/recovery.py` | `pending_orphaned` finalize for PENDING orphans |
| `agent/tools/subagent/registry/state.py` | restore-time `pending_orphaned` finalize for restart leftovers |
| `tests/runtime/lane/` · `tests/server/service/test_main_lane.py` · `tests/agent/tools/subagent/test_{spawn_lane_integration,kill_pending,steer_lane,sweeper_pending,sessions_yield_pending,registry_restore}.py` · `tests/server/trigger/http/test_lane_api.py` | Lane test suite |

### Implementation notes

Three deliberate trade-offs recorded by the implementation:

- **MAIN clamps up to the invariant.** CPU scaling alone (`min(16, max(8, CPU))`) would resolve to 8 on any machine with ≤ 8 CPUs and fail `validate_lane_config()` at startup, so the default is clamped up to `SUBAGENT + NUDGE` (12) and stays valid on 4/8/12/16/64-core machines alike.
- **`GET /lane-status` has no `/api` prefix.** The originally sketched `/api/lane-status` route was dropped for the repo's existing routing convention: the handler lives in `server/trigger/http/lane.py`, alongside `/channels`, `/cron`, etc.
- **The exit drain runs from `atexit`.** The repo has no async shutdown seam (Robyn's `shutdown_handler` is never invoked on SIGINT/SIGTERM), so the bounded `drain_all(timeout=0)` reports the final per-lane counters and never delays exit; in-flight turns are abandoned to the OS.

